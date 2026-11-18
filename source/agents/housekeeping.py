"""The housekeeping role: the work that follows a turn, one job at a time.

When a run ends, the assistant enqueues one item for this role. The worker
expands it into that run's jobs (`housekeeping_job` rows), then runs every job
that is waiting, in order. It starts as soon as nothing else is working, and a
new message kills the model call in progress so the turn has the machine to
itself. The killed job runs again when the system is idle.

Design: notes/housekeeping-design.md.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from types import FrameType
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

import db
from agents.base import Agent
from agents.config import ASSISTANT_RUN_SUMMARIZER_UUID

logger = logging.getLogger(__name__)

PREEMPT_SIGNAL = signal.SIGUSR1

#: Failures a job is retried after. A killed attempt is not a failure.
MAX_RETRIES = 1

#: Chunk jobs queued for a room when one of its runs ends. A turn adds about
#: two messages, so two chunks per turn keeps up with a room and works
#: through an older room's backlog a little at a time.
CHUNK_JOBS_PER_RUN = 2
#: Chunk jobs queued when the operator asks for the rest of a room.
CHUNK_JOBS_PER_REQUEST = 200
#: Topics jobs queued for a room when one of its runs ends, and when the
#: operator asks for the rest. A topics job is a few seconds of scoring by
#: the decision model, so a run takes more of a backlog than it does of
#: chunks to summarize.
TOPIC_JOBS_PER_RUN = 20
TOPIC_JOBS_PER_REQUEST = 200

#: Model calls in a row before the worker rests, and for how long. The
#: operator's machine takes 10 to 15 calls in a row before it needs a minute
#: to cool, so the burst uses the low end.
BURST_CALLS = 10
COOLDOWN_SECONDS = 60.0


def _is_local_model(model_uuid: UUID) -> bool:
    """Whether a model answers on this machine: a loopback endpoint, and not
    a `:cloud` name, which a local server relays to a remote one."""
    from agents.model_groups import model_base_url
    from diary.embeddings import is_loopback_url

    url = model_base_url(model_uuid)
    if url is None or not is_loopback_url(url):
        return False
    try:
        _provider, name, _args = db.resolved_model_kwargs(model_uuid)
    except Exception:
        return False
    return not (name.endswith(":cloud") or "-cloud" in name)


class Preempted(BaseException):
    """A turn started while a housekeeping call was running.

    A BaseException so that it passes through the model-fallback loop in
    `ModelGroupAgent._structured_completion`, which treats any Exception as
    one model failing and tries the next."""


class TurnWatch:
    """Watches for turn activity while a housekeeping job runs.

    A daemon thread asks `probe()` every `poll_seconds`. When it answers true
    the watch remembers it and, if the main thread is inside an
    `interruptible()` window, signals it. The handler raises `Preempted` there,
    which breaks even a blocked network read (PEP 475), so a call stops while
    a model is still prefilling and has produced nothing.

    The job declares where it may be interrupted. `interruptible()` wraps the
    model call. `shielded()` wraps a database write made from inside that
    call, so the signal never lands in the middle of a commit; a turn seen
    meanwhile raises as the shield ends.

    Must be entered from the main thread, where Python runs signal handlers.
    A `None` probe makes the watch inert.
    """

    def __init__(self, probe: Callable[[], bool] | None, *,
                 poll_seconds: float = 2.0) -> None:
        self._probe = probe
        self._poll = poll_seconds
        self._lock = threading.Lock()
        self._seen = threading.Event()
        self._halt = threading.Event()
        self._interruptible = False
        self._thread: threading.Thread | None = None
        self._previous: Any = None
        self._main = threading.main_thread()

    def __enter__(self) -> "TurnWatch":
        if self._probe is None:
            return self
        if threading.current_thread() is not self._main:
            raise RuntimeError("TurnWatch must be entered from the main thread")
        self._previous = signal.signal(PREEMPT_SIGNAL, self._on_signal)
        self._thread = threading.Thread(
            target=self._watch, name="turn-watch", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self._probe is None:
            return False
        # No more raising, then no more signalling, and only then the old
        # handler: a late signal under the default disposition would end the
        # process.
        with self._lock:
            self._interruptible = False
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        signal.signal(PREEMPT_SIGNAL, self._previous)
        return False

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        if self._interruptible:
            raise Preempted()

    def _watch(self) -> None:
        assert self._probe is not None
        while not self._halt.wait(self._poll):
            try:
                hit = bool(self._probe())
            except Exception:
                logger.warning("turn watch: probe failed; still watching",
                               exc_info=True)
                continue
            if not hit:
                continue
            with self._lock:
                self._seen.set()
                if self._interruptible and self._main.ident is not None:
                    signal.pthread_kill(self._main.ident, PREEMPT_SIGNAL)
            return

    @contextmanager
    def interruptible(self) -> Generator[None, None, None]:
        """A window in which a turn may interrupt the main thread. Entering
        with a turn already seen raises at once."""
        with self._lock:
            if self._seen.is_set():
                raise Preempted()
            self._interruptible = True
        try:
            yield
        finally:
            with self._lock:
                self._interruptible = False

    @contextmanager
    def shielded(self) -> Generator[None, None, None]:
        """Inside an interruptible window, a stretch that must not be
        interrupted. A turn seen during it raises when it ends."""
        with self._lock:
            was = self._interruptible
            self._interruptible = False
        yield
        with self._lock:
            hit = was and self._seen.is_set()
            self._interruptible = was and not hit
        if hit:
            raise Preempted()


class HousekeepingAgent(Agent):
    """Expand each inbox item into its run's jobs, then run every waiting job."""

    uses_model_group = False

    #: How often the worker asks whether a turn is in progress, both while it
    #: waits to start a job and while a job's call runs.
    POLL_SECONDS: float = 2.0
    #: How often a running call's reply is written to its attempt row.
    PROGRESS_SECONDS: float = 2.0
    BURST_CALLS: int = BURST_CALLS
    COOLDOWN_SECONDS: float = COOLDOWN_SECONDS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Model calls since the worker last rested.
        self._burst = 0

    def handle(self, journal_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        # One worker, so anything marked running was left by a dead one.
        db.recover_running_housekeeping_jobs(
            "The housekeeping worker exited while the job was running.")
        self._expand(payload)
        db.sweep_due_housekeeping_jobs()
        return {"ok": True, "jobs": self._drain()}

    def _expand(self, payload: dict[str, Any]) -> None:
        """An item's jobs: the run's summary, and the next chunks of its room.
        An item with `room_chunks` and no run asks for the rest of a room,
        one with `room_topics` for topics for the chunks it has, and one
        with `room_topic_merges` for the room's topics that look like one."""
        room_uuid: UUID | None = None
        try:
            if payload.get("room_uuid"):
                room_uuid = UUID(str(payload["room_uuid"]))
            run_uuid = UUID(str(payload["run_uuid"])) if payload.get("run_uuid") else None
        except ValueError:
            logger.warning("housekeeping: malformed uuid in %r", payload)
            return
        if run_uuid is not None:
            job, created = db.ensure_housekeeping_job(
                db.RUN_SUMMARY_JOB, run_uuid=run_uuid, room_uuid=room_uuid)
            if payload.get("force") and not created and job.state == "final":
                db.requeue_housekeeping_job(job)
        if room_uuid is None or payload.get("force"):
            return
        from agents.room_chunks import PROMPT_VERSION

        series = (int(db.get_setting("housekeeping.room_chunk_size")),
                  int(db.get_setting("housekeeping.room_chunk_overlap")),
                  PROMPT_VERSION)
        if payload.get("room_topic_merges"):
            # One job per room, run again each time it is asked for.
            job, created = db.ensure_housekeeping_job(
                db.ROOM_TOPIC_MERGES_JOB, room_uuid=room_uuid, subject=str(room_uuid))
            if not created and job.state == "final":
                db.requeue_housekeeping_job(job)
            return
        if payload.get("room_topics"):
            db.ensure_room_topic_jobs(room_uuid, *series, TOPIC_JOBS_PER_REQUEST)
            return
        if payload.get("room_chunks"):
            limit = CHUNK_JOBS_PER_REQUEST
        elif run_uuid is not None and db.get_setting("housekeeping.room_summaries"):
            limit = CHUNK_JOBS_PER_RUN
        else:
            return
        db.ensure_room_chunk_jobs(room_uuid, *series, limit)
        # Chunks summarized before, whose parts have no topic yet. A chunk
        # summarized from here on queues its own topics job when it is stored.
        db.ensure_room_topic_jobs(
            room_uuid, *series,
            TOPIC_JOBS_PER_REQUEST if payload.get("room_chunks") else TOPIC_JOBS_PER_RUN)

    def _drain(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        while db.next_housekeeping_job() is not None:
            self._cool_down_if_due()
            self._wait_until_idle()
            # Chosen after the wait: a more urgent job may have arrived.
            job = db.next_housekeeping_job()
            if job is None:
                break
            counts[self._run_job(job)] += 1
        return dict(counts)

    def _cool_down_if_due(self) -> None:
        """After BURST_CALLS model calls in a row, rest before the next. A
        turn may run meanwhile: the rest is for the machine, and a turn's own
        calls are the operator's to spend."""
        if self._burst < self.BURST_CALLS:
            return
        logger.info("housekeeping: cooling down %.0f s after %d calls",
                    self.COOLDOWN_SECONDS, self._burst)
        job = db.next_housekeeping_job()
        if job is not None:
            db.settle_housekeeping_job(job, "deferred", deferred_reason="cooldown")
        deadline = time.monotonic() + self.COOLDOWN_SECONDS
        while time.monotonic() < deadline:
            time.sleep(min(self.POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        self._burst = 0

    def _wait_until_idle(self) -> None:
        while db.turn_activity():
            # End the read's transaction, so the wait holds none open.
            db.session.commit()
            time.sleep(self.POLL_SECONDS)
        db.session.commit()

    def _probe(self) -> Callable[[], bool]:
        """What the watch asks, on a session of its own: the watcher thread
        has no app context, and the scoped session is not shared across
        threads."""
        engine = db.db.engine

        def probe() -> bool:
            with Session(bind=engine) as session:
                return db.turn_activity(session=session)
        return probe

    def _run_job(self, job: Any) -> str:
        """One attempt at one job. Returns `done`, `killed` or `failed`."""
        runner = self._RUNNERS.get(job.job_type)
        attempt = db.start_housekeeping_attempt(job)
        outcome: str | None = None
        # Counted before it runs: a killed or failed call heated the machine
        # as much as a finished one.
        self._burst += 1
        try:
            if runner is None:
                raise LookupError(f"no runner for job type {job.job_type!r}")
            with TurnWatch(self._probe(), poll_seconds=self.POLL_SECONDS) as watch:
                outcome = runner(self, job, attempt, watch)
        except Preempted:
            db.session.rollback()
            logger.info("housekeeping: %s for run %s killed by a new turn",
                        job.job_type, job.run_uuid)
            db.finish_housekeeping_attempt(job, attempt, "killed: turn")
            db.settle_housekeeping_job(job, "deferred", deferred_reason="turn")
            return "killed"
        except Exception as e:
            db.session.rollback()
            msg = f"{type(e).__name__}: {e}"
            logger.exception("housekeeping: %s for run %s failed",
                             job.job_type, job.run_uuid)
            db.finish_housekeeping_attempt(job, attempt, "failed", error=msg)
            if job.retries < MAX_RETRIES:
                db.settle_housekeeping_job(job, "waiting", retried=True)
            else:
                db.settle_housekeeping_job(job, "final", outcome=f"failed: {msg}")
            return "failed"
        skipped = (outcome or "").startswith("skipped")
        db.finish_housekeeping_attempt(
            job, attempt, "skipped" if skipped else "done")
        db.settle_housekeeping_job(job, "final", outcome=outcome or "done")
        return "done"

    def _summarize_run(self, job: Any, attempt: Any, watch: TurnWatch) -> str:
        """The assistant summary: one structured call by the summarizer, on its own
        model slot, interruptible for exactly the length of that call."""
        from agents.assistant_run_summarizer import AssistantRunSummarizerAgent

        run = db.get_assistant_run(job.run_uuid) if job.run_uuid else None
        if run is None:
            return "skipped: run not found"
        summarizer = AssistantRunSummarizerAgent(
            ASSISTANT_RUN_SUMMARIZER_UUID, "assistant.run_summarizer",
            lambda _msg: None)
        summarizer.setup()
        attempt_uuid = attempt.uuid
        last_write = 0.0

        def on_model(model_name: str) -> None:
            with watch.shielded():
                db.checkpoint_housekeeping_attempt(attempt_uuid, model=model_name)

        def on_progress(text: str) -> None:
            nonlocal last_write
            now = time.monotonic()
            if now - last_write < self.PROGRESS_SECONDS:
                return
            last_write = now
            with watch.shielded():
                db.checkpoint_housekeeping_attempt(
                    attempt_uuid, response_preview=text)

        summarizer.call_guard = watch.interruptible
        summarizer.on_model = on_model
        summarizer.on_progress = on_progress
        result = summarizer.handle(self._active_journal_id or job.uuid,
                                   {"run_uuid": str(run.uuid)})
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "summarizer returned no result")
        return "done"

    def _summarize_chunk(self, job: Any, attempt: Any, watch: TurnWatch) -> str:
        """One chunk of a room's messages, summarized by a structured call on
        the `assistant.room_summarizer` slot, then checked part by part by a
        second call on the same models, and written once more when the check
        finds a part's request or result wrong: two calls as a rule, four at
        most. Before them the decision model is asked of each follow-up
        whether it asks again (`_ask_follow_ups`). The chunk is the one the
        job's subject names; its row is written once, when the last call
        returns."""
        from agents import room_chunks
        from agents.config import ASSISTANT_ROOM_SUMMARIZER_UUID
        from agents.model_groups import resolve_assistant_model_uuids, structured_llm_call

        spec = db.parse_chunk_subject(job.subject)
        if spec is None or job.room_uuid is None:
            raise ValueError(f"malformed chunk subject {job.subject!r}")
        size, overlap, seq = spec["size"], spec["overlap"], spec["seq"]
        if spec["version"] != room_chunks.PROMPT_VERSION:
            return "skipped: queued for an earlier prompt"
        earlier = db.list_room_chunks(
            job.room_uuid, size, overlap, room_chunks.PROMPT_VERSION)
        if any(c.seq == seq for c in earlier):
            return "skipped: already summarized"
        if seq != len(earlier):
            # Its predecessor failed, so the series cannot continue past it.
            return "skipped: an earlier chunk of the series is missing"

        messages = db.counted_room_messages(job.room_uuid)
        inside = [i for i, m in enumerate(messages)
                  if spec["first_id"] <= m["id"] <= spec["last_id"]]
        if not inside:
            return "skipped: the chunk's messages are gone"
        first, last = inside[0], inside[-1]
        # Signals for the chunk and its context only: the rest is not shown.
        shown = messages[max(0, first - overlap):last + 1 + overlap]
        db.attach_reply_signals(job.room_uuid, shown)
        chunk = messages[first:last + 1]
        after = messages[last + 1:last + 1 + overlap]
        prompt = room_chunks.build_prompt(
            messages[max(0, first - overlap):first], chunk, after)

        models, _label = resolve_assistant_model_uuids(ASSISTANT_ROOM_SUMMARIZER_UUID)
        models = [m for m in models or [] if _is_local_model(m)]
        if not models:
            raise RuntimeError(
                "no local model is bound to assistant.room_summarizer or "
                "assistant.default; a room's messages go to local models only")
        attempt_uuid = attempt.uuid
        worker = self

        class Observer:
            last_write = 0.0

            def started(self, model_uuid: UUID, model_name: str,
                        timeout_seconds: float) -> None:
                with watch.shielded():
                    db.checkpoint_housekeeping_attempt(attempt_uuid, model=model_name)

            def progress(self, model_uuid: UUID, model_name: str,
                         reasoning: str | None, response_text: str | None) -> None:
                now = time.monotonic()
                if now - self.last_write < worker.PROGRESS_SECONDS:
                    return
                self.last_write = now
                with watch.shielded():
                    db.checkpoint_housekeeping_attempt(
                        attempt_uuid, response_preview=response_text or reasoning or "")

            def failed(self, *args: Any, **kwargs: Any) -> None:
                pass

        def call(p: dict[str, str], response_model: type, caller: str) -> tuple[Any, UUID]:
            with watch.interruptible():
                return structured_llm_call(
                    caller, models, p["system"], p["user"], response_model,
                    observer=Observer())

        retries, repeat_check = self._ask_follow_ups(job, chunk, after, first, watch)
        summarizer = db.JOB_CALLERS[db.ROOM_CHUNK_JOB]

        def room_numbers(reply: Any) -> Any:
            return room_chunks.room_numbers(reply, first)

        reply, model_uuid = call(prompt, room_chunks.ChunkSummary, summarizer)
        # The model answers in the prompt's numbers, m1 on; what is settled
        # and stored is in the room's.
        summary, topics, parts, problems = room_chunks.settle(
            room_numbers(reply), first, last, chunk, after, retries)

        # The check, and one rewrite when it finds a part's request or result
        # wrong. The rewrite is checked too, so what is stored always carries
        # the check of itself. A check or a rewrite that no model could
        # deliver leaves the answer there is, and says so.
        audit: dict[str, Any] = {"attempts": 1, "repeat_check": repeat_check}
        for attempt_no in (1, 2):
            self._burst += 1
            try:
                check, _ = call(room_chunks.build_audit_prompt(prompt, summary, parts, first),
                                room_chunks.ChunkCheck, room_chunks.AUDIT_CALLER)
            except RuntimeError as e:
                audit["error"] = f"check: {e}"[:300]
                break
            failed, check_problems = room_chunks.settle_audit(
                check, parts, first)  # type: ignore[arg-type]
            problems += check_problems
            if not failed or attempt_no == 2:
                break
            self._burst += 1
            try:
                reply, model_uuid = call(
                    room_chunks.build_retry_prompt(prompt, summary, parts, failed, first),
                    room_chunks.ChunkSummary, summarizer)
            except RuntimeError as e:
                audit["error"] = f"rewrite: {e}"[:300]
                break
            audit["rewritten"] = [
                {"first_message": p["first_message"], "last_message": p["last_message"],
                 "summary": p["summary"], "result": p["result"],
                 "reasoning": p["audit"]["reasoning"]} for p in failed]
            summary, topics, parts, problems = room_chunks.settle(
                room_numbers(reply), first, last, chunk, after, retries)
            audit["attempts"] = 2
        _provider, model_name, _args = db.resolved_model_kwargs(model_uuid)
        stored = db.store_room_chunk(
            room_uuid=job.room_uuid, chunk_size=size, overlap=overlap, seq=seq,
            first_message_id=chunk[0]["id"], last_message_id=chunk[-1]["id"],
            message_count=len(chunk),
            content_hash=room_chunks.content_hash(chunk),
            summary=summary, topics=topics, parts=parts, problems=problems,
            audit=audit,
            model=model_name, prompt_version=room_chunks.PROMPT_VERSION,
            job_uuid=job.uuid)
        self._queue_chunk_topics(stored)
        return "done"

    @staticmethod
    def _queue_chunk_topics(chunk: Any) -> None:
        """A stored chunk's parts are assigned to the room's topics by a job
        of their own, queued here so it follows the chunk's summary."""
        db.ensure_housekeeping_job(
            db.ROOM_TOPICS_JOB, room_uuid=chunk.room_uuid, subject=str(chunk.uuid))

    #: A decision call answers in well under a second. One that has not by
    #: then is given up, and the follow-up is read by rule.
    FOLLOW_UP_DEADLINE_SECONDS = 20.0
    #: A follow-up whose three messages are longer than this is not asked.
    FOLLOW_UP_MAX_STATE_CHARS = 8000

    def _ask_follow_ups(self, job: Any, chunk: list[dict[str, Any]],
                        after: list[dict[str, Any]], first: int, watch: TurnWatch,
                        ) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
        """Ask the decision model, of every follow-up in the chunk, whether
        it repeats the request before it. Returns what it said by message
        number, as `{yes, p, kind}`, and a line for the chunk's record: the model,
        how many follow-ups were asked and how many it answered; or why none
        was asked. A follow-up that is the request word for word is not put
        to the model: it asks again, and the record counts it as `identical`.
        An unanswered follow-up is simply absent, and is read by rule. These calls are not counted toward the rest: each is a
        fraction of a second of scoring, with no text generated."""
        from agents import room_chunks
        from agents.config import ASSISTANT_REPEAT_CHECK_UUID
        from llm import systemone
        from llm.systemone_questions import (
            FOLLOW_UP_REPEAT, FOLLOW_UP_SET_VERSION, follow_up_again, follow_up_state)

        bound = systemone.slot_model(ASSISTANT_REPEAT_CHECK_UUID)
        if bound is None:
            return {}, {"reason": "unbound"}
        asked = room_chunks.follow_ups(chunk, after, first)
        said: dict[int, dict[str, Any]] = {}
        identical = 0
        for item in asked:
            if item["request"].strip() == item["next_message"].strip():
                said[item["message"]] = {"yes": True, "p": 1.0, "kind": "duplicate"}
                identical += 1
                continue
            state = follow_up_state(item["request"], item["next_message"])
            with watch.interruptible():
                (answer,) = systemone.ask(
                    ASSISTANT_REPEAT_CHECK_UUID, state, [FOLLOW_UP_REPEAT],
                    deadline_s=self.FOLLOW_UP_DEADLINE_SECONDS,
                    max_state_chars=self.FOLLOW_UP_MAX_STATE_CHARS,
                    question_set_version=FOLLOW_UP_SET_VERSION,
                    job_uuid=job.uuid, bound=bound)
            if answer.status == "ok" and answer.probabilities:
                yes, p, kind = follow_up_again(answer.probabilities)
                said[item["message"]] = {"yes": yes, "p": p, "kind": kind}
        record = {"model": bound[1], "asked": len(asked) - identical,
                  "answered": len(said) - identical}
        if identical:
            record["identical"] = identical
        return said, record

    #: A decision call answers in well under a second. One that has not by
    #: then fails the attempt, and the job is tried once more.
    TOPIC_MATCH_DEADLINE_SECONDS = 20.0
    TOPIC_MATCH_MAX_STATE_CHARS = 2000

    def _assign_topics(self, job: Any, attempt: Any, watch: TurnWatch) -> str:
        """The parts of one chunk, each assigned to a topic of its room on
        each axis (`agents/room_topics.py`): the decision model is asked of
        the part and the exemplars of the nearest topics, and the part joins
        the best or starts a topic of its own. These calls are not counted
        toward the rest: each is a fraction of a second of scoring. A topic
        that now recurs is then named and explained by one generative call,
        which is counted.

        A part's memberships are written as soon as it is placed, so an
        attempt that a new message stops has kept the parts it finished,
        and the next attempt goes on with the rest."""
        from agents import room_topics
        from agents.config import ASSISTANT_REPEAT_CHECK_UUID, ASSISTANT_TOPIC_MATCH_UUID
        from llm import systemone

        # Every attempt is counted as a model call before it runs. This one
        # generates no text unless a topic is explained, which counts itself.
        self._burst -= 1
        try:
            chunk = db.get_room_chunk(UUID(job.subject))
        except ValueError:
            raise ValueError(f"malformed topics subject {job.subject!r}") from None
        if chunk is None:
            return "skipped: the chunk is gone"
        parts = [(n, part) for n, part in enumerate(chunk.parts or [])
                 if str(part.get("summary") or "").strip()]
        if not parts:
            return "skipped: the chunk has no parts"
        placed = db.assigned_chunk_parts(chunk.uuid)
        todo = [(n, part) for n, part in parts if n not in placed]
        if todo:
            bound = (systemone.slot_model(ASSISTANT_TOPIC_MATCH_UUID)
                     or systemone.slot_model(ASSISTANT_REPEAT_CHECK_UUID))
            if bound is None:
                return "skipped: no decision model is bound"
        for n, part in todo:
            phrase = " ".join(str(part["summary"]).split())
            vector = self._embed_phrase(phrase)
            candidates = {
                axis: room_topics.nearest(vector, db.room_topic_exemplars(
                    chunk.room_uuid, axis, room_topics.EXEMPLARS))
                for axis in room_topics.AXES}
            # One request answers both axes, so a part that is an exemplar
            # on both, as most are, is asked about once.
            earlier = dict.fromkeys(m.phrase for axis in room_topics.AXES
                                    for c in candidates[axis] for m in c["exemplars"])
            scores: dict[str, dict[str, float]] = {}
            for exemplar in earlier:
                with watch.interruptible():
                    answers = systemone.ask(
                        ASSISTANT_TOPIC_MATCH_UUID,
                        room_topics.pair_state(exemplar, phrase),
                        [room_topics.QUESTIONS[axis] for axis in room_topics.AXES],
                        deadline_s=self.TOPIC_MATCH_DEADLINE_SECONDS,
                        max_state_chars=self.TOPIC_MATCH_MAX_STATE_CHARS,
                        question_set_version=room_topics.QUESTION_SET_VERSION,
                        job_uuid=job.uuid, bound=bound)
                unanswered = [a for a in answers if a.status != "ok" or a.p is None]
                if unanswered:
                    raise RuntimeError(
                        f"the decision model did not answer: {unanswered[0].reason}")
                scores[exemplar] = {a.name: float(a.p) for a in answers}  # type: ignore[arg-type]
            rows = []
            for axis in room_topics.AXES:
                topic, p = room_topics.pick(candidates[axis], scores, axis)
                if topic is None:
                    topic = db.add_room_topic(
                        chunk.room_uuid, axis, room_topics.seed_name(part))
                rows.append({
                    "topic_uuid": topic.uuid, "room_uuid": chunk.room_uuid,
                    "axis": axis, "chunk_uuid": chunk.uuid, "part_index": n,
                    "phrase": phrase, "messages": room_topics.part_messages(part),
                    "p": p, "embedding": vector})
            db.add_room_topic_members(rows)
        explained = self._explain_topics(chunk, watch)
        if not todo and not explained:
            return "skipped: already assigned"
        return "done"

    @staticmethod
    def _embed_phrase(phrase: str) -> list[float] | None:
        """A phrase's vector, or None when the embedder does not answer: the
        part is then scored against the newest topics instead of the
        nearest."""
        try:
            from memory.seed_memory import embed_query

            return [float(v) for v in embed_query(phrase)]
        except Exception as e:   # noqa: BLE001 - ranking is an aid, not the answer
            logger.warning("housekeeping: no embedding for a part's phrase: %s", e)
            return None

    def _explain_topics(self, chunk: Any, watch: TurnWatch) -> int:
        """Name and explain each topic of the chunk's parts that is due for
        it (`room_topics.due_for_explaining`), one structured call each on
        the local models of `assistant.topic_namer`. Returns how many were written.
        A call that no model could deliver leaves the topic as it was: it
        is due again the next time a part joins it."""
        from agents import room_topics
        from agents.config import ASSISTANT_TOPIC_NAMER_UUID
        from agents.model_groups import resolve_assistant_model_uuids, structured_llm_call

        models: list[UUID] | None = None
        written = 0
        for topic in db.list_chunk_topics(chunk.uuid):
            members, requests = db.room_topic_requests(
                topic.uuid, room_topics.EXPLAIN_REQUESTS)
            if not room_topics.due_for_explaining(topic, members):
                continue
            if models is None:
                found, _label = resolve_assistant_model_uuids(ASSISTANT_TOPIC_NAMER_UUID)
                models = [m for m in found or [] if _is_local_model(m)]
            if not models:
                return written
            prompt = room_topics.build_explain_prompt(topic.axis, requests)
            self._burst += 1
            try:
                with watch.interruptible():
                    reply, _model = structured_llm_call(
                        room_topics.NAMER_CALLER, models, prompt["system"],
                        prompt["user"], room_topics.TopicName)
            except RuntimeError as e:
                logger.warning("housekeeping: topic %s was not explained: %s", topic.uuid, e)
                continue
            name, explanation = room_topics.settle_name(reply)  # type: ignore[arg-type]
            if name and db.explain_room_topic(topic.uuid, name, explanation, members):
                written += 1
        if written:
            # A topic that was just given the name another has is that topic.
            self._merge_same_named(chunk.room_uuid)
        return written

    def _suggest_merges(self, job: Any, attempt: Any, watch: TurnWatch) -> str:
        """Look through a room's topics for pairs that are one
        (`agents/room_topics.py`). Topics of one axis that the model gave
        the same name are merged here: two names it wrote alone, from two
        sets of requests, and that came out alike. Every other pair is
        only recorded, for the operator. On each axis a topic of
        `MERGE_MIN_PARTS` parts or more, counted in the room's current
        series, is compared with its nearest by embedding: the decision
        model scores their sample requests, and a pair at `SUGGEST_AT` or
        above is put to the generative model with both topics' requests. A
        pair is compared once; what was found of it is kept, whichever way
        it went."""
        from agents import room_topics
        from agents.config import (
            ASSISTANT_REPEAT_CHECK_UUID,
            ASSISTANT_TOPIC_MATCH_UUID,
            ASSISTANT_TOPIC_NAMER_UUID,
        )
        from agents.model_groups import resolve_assistant_model_uuids, structured_llm_call
        from llm import systemone

        # Counted as a model call before it ran; each judging call counts itself.
        self._burst -= 1
        if job.room_uuid is None:
            raise ValueError("a topic merges job needs a room")
        bound = (systemone.slot_model(ASSISTANT_TOPIC_MATCH_UUID)
                 or systemone.slot_model(ASSISTANT_REPEAT_CHECK_UUID))
        if bound is None:
            return "skipped: no decision model is bound"
        found, _label = resolve_assistant_model_uuids(ASSISTANT_TOPIC_NAMER_UUID)
        models = [m for m in found or [] if _is_local_model(m)]
        if not models:
            return "skipped: no local model is bound to assistant.topic_namer"
        self._merge_same_named(job.room_uuid)
        from agents.room_chunks import PROMPT_VERSION

        # A topic's size is its parts in the series the room is read in now:
        # a room summarized twice holds every request twice.
        series = {c.uuid for c in db.list_room_chunks(
            job.room_uuid, int(db.get_setting("housekeeping.room_chunk_size")),
            int(db.get_setting("housekeeping.room_chunk_overlap")), PROMPT_VERSION)}
        members: dict[UUID, list[Any]] = {}
        for m in db.list_room_topic_members(job.room_uuid):
            if m.chunk_uuid in series:
                members.setdefault(m.topic_uuid, []).append(m)
        compared = {frozenset((s.topic_uuid, s.other_uuid))
                    for s in db.list_room_topic_suggestions(job.room_uuid, state=None)}
        for axis in room_topics.AXES:
            topics = [{"topic": t, "members": members[t.uuid]}
                      for t in db.list_room_topics(job.room_uuid)
                      if t.axis == axis and members.get(t.uuid)]
            for i, j in room_topics.merge_pairs(topics):
                first, second = topics[i], topics[j]
                pair = (first["topic"].uuid, second["topic"].uuid)
                if frozenset(pair) in compared:
                    continue
                ps = []
                for a in room_topics.merge_sample(first["members"]):
                    for b in room_topics.merge_sample(second["members"]):
                        with watch.interruptible():
                            (answer,) = systemone.ask(
                                ASSISTANT_TOPIC_MATCH_UUID,
                                room_topics.pair_state(a.phrase, b.phrase),
                                [room_topics.QUESTIONS[axis]],
                                deadline_s=self.TOPIC_MATCH_DEADLINE_SECONDS,
                                max_state_chars=self.TOPIC_MATCH_MAX_STATE_CHARS,
                                question_set_version=room_topics.QUESTION_SET_VERSION,
                                job_uuid=job.uuid, bound=bound)
                        if answer.status != "ok" or answer.p is None:
                            raise RuntimeError(
                                f"the decision model did not answer: {answer.reason}")
                        ps.append(float(answer.p))
                score = sum(ps) / len(ps)
                if score < room_topics.SUGGEST_AT:
                    continue
                prompt = room_topics.build_judge_prompt(
                    axis, first["members"], second["members"])
                self._burst += 1
                try:
                    with watch.interruptible():
                        reply, _model = structured_llm_call(
                            room_topics.NAMER_CALLER, models, prompt["system"],
                            prompt["user"], room_topics.SameTopic)
                except RuntimeError as e:
                    # Not compared: the pair is looked at again next time.
                    logger.warning("housekeeping: two topics were not compared: %s", e)
                    continue
                reason = " ".join(str(reply.reason).split())[:room_topics.REASON_MAX_CHARS]  # type: ignore[attr-defined]
                same = bool(reply.same)  # type: ignore[attr-defined]
                db.suggest_room_topic_merge(job.room_uuid, *pair, score, reason,
                                            state="open" if same else "apart")
        return "done"

    @staticmethod
    def _merge_same_named(room_uuid: UUID) -> int:
        """Merge the room's topics that share a name on one axis, into the
        one with the most parts. Returns how many went. A name the
        operator wrote counts like any other: a topic the model names as
        they named theirs is theirs."""
        gone = 0
        for group in db.same_named_room_topics(room_uuid):
            for other in group[1:]:
                gone += int(db.merge_room_topics(group[0].uuid, other.uuid) is not None)
        return gone

    _RUNNERS: dict[str, Callable[..., str]] = {
        db.RUN_SUMMARY_JOB: _summarize_run,
        db.ROOM_CHUNK_JOB: _summarize_chunk,
        db.ROOM_TOPICS_JOB: _assign_topics,
        db.ROOM_TOPIC_MERGES_JOB: _suggest_merges,
    }
