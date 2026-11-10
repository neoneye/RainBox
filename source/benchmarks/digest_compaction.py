"""Opt-in test: compact one chat room into room-digest chunks.

Tries the chunking in notes/proposals/2026-11-09-room-digest.md (decision 5)
on a real room that predates topic recording, which is the backfill case:

- **Level 1:** every 10 counted messages, counted from the room's first one.
  Each chunk renders two parts:
  - `records`, from rows that already exist, with no model call: exchanges,
    runs by the run summarizer's outcome, and exact repeats;
  - `asked` and `topics`, written by one local generative call per chunk,
    which reads the chunk plus 2 counted messages of context on each side.
- **Level 2:** every 10 level 1 chunks, derived by summing their records and
  listing their topic names. Never generated.
- **What the loop would see:** the `<earlier>` block for the room as it is
  now, with the window starting at the newest chunk edge that leaves at least
  30 counted messages after it.

It cannot be a unit test: the text depends on the model. It is run
deliberately, against a local Ollama server, and reads the database only:

    python -m benchmarks.digest_compaction <room-uuid> \\
        --database-url postgresql://localhost/<database> --out <dir> --dry-run
    python -m benchmarks.digest_compaction <room-uuid> \\
        --database-url postgresql://localhost/<database> --out <dir>

`--dry-run` builds every prompt and the records part and calls no model.
Without it, generative calls run in bursts (10 by default) with a cool-down
(60 s by default) between bursts, because the operator's machine overheats
on longer runs.

The output holds the room's own messages, so `--out` must be outside the
repository. It writes `run.json` (prompts, raw replies, timings, records) and
`report.md`. The database connection is read-only. Refuses `:cloud` models
and non-loopback servers, since a local server relays `:cloud` names to a
remote one.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e4b-mlx"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
REPO_ROOT = Path(__file__).resolve().parents[2]

CHUNK_SIZE = 10
MARGIN = 2
WINDOW = 30
# The repeat proposal's exact matcher skips messages shorter than this, in
# non-whitespace code points: "again" twice is not a repeated request.
EXACT_MIN_CODEPOINTS = 24
# The repeat proposal's `extended` candidate: an earlier request's tokens are
# a prefix of the current one's, with 1 to 8 tokens added. Recording only.
EXTENDED_MAX_ADDED = 8
ASKED_MAX_CHARS = 120
TOPIC_MAX_CHARS = 24
MAX_TOPICS = 4
# One message's share of a prompt. Longer ones are middle-cut.
PROMPT_MESSAGE_MAX_CHARS = 1500
OUTCOME_ORDER = ("resolved", "partial", "failed")


@dataclass
class Message:
    id: int
    uuid: str
    sender: str  # "human" or "agent", from chat_user.user_type
    text: str
    created_at: datetime
    kind: str = "message"
    streaming: bool = False
    run_uuid: str | None = None


@dataclass
class Run:
    uuid: str
    status: str
    outcome: str | None


@dataclass
class Chunk:
    level: int
    first: int  # index into the counted messages, inclusive
    last: int
    first_id: int
    last_id: int
    started: str
    ended: str
    exchanges: int
    runs: dict[str, int]
    exact_repeats: list[list[int]]
    extended_repeats: list[list[int]]
    asked: str | None = None
    topics: list[str] = field(default_factory=list)
    prompt: dict[str, str] | None = None
    reply: str | None = None
    parse_problems: list[str] = field(default_factory=list)
    timing: dict[str, Any] | None = None
    error: str | None = None


# --- counting and edges (pure) ---------------------------------------------

def counted(messages: list[Message]) -> list[Message]:
    """The messages chunks and the history window count: real messages, by
    id. Progress rows are reaped when their sender's reply posts, and a row
    still streaming is not settled, so neither counts."""
    return sorted((m for m in messages if m.kind == "message" and not m.streaming),
                  key=lambda m: m.id)


def chunk_ranges(n: int, size: int = CHUNK_SIZE,
                 after_margin: int = MARGIN) -> list[tuple[int, int]]:
    """Complete level 1 chunks over n counted messages, as inclusive index
    ranges. A chunk is complete once its after-margin exists."""
    out = []
    start = 0
    while start + size - 1 + after_margin <= n - 1:
        out.append((start, start + size - 1))
        start += size
    return out


def window_start(n: int, size: int = CHUNK_SIZE, window: int = WINDOW) -> int:
    """The newest chunk edge that leaves at least `window` counted messages
    after it, so the verbatim window holds `window` to `window + size - 1`."""
    if n <= window:
        return 0
    return ((n - window) // size) * size


# --- records (pure) ---------------------------------------------------------

def _exact_key(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _codepoints(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def tokens(text: str) -> list[str]:
    """NFKC, casefolded, letter/mark/number runs: the repeat proposal's
    lexical tokens."""
    norm = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"[^\W_]+", norm)


def repeats(msgs: list[Message]) -> tuple[list[list[int]], list[list[int]]]:
    """Exact and extended repeats among human messages, as [later, earlier]
    index pairs into `msgs`. Exact: identical text after line-ending
    normalization, case and spacing kept, at least EXACT_MIN_CODEPOINTS long.
    Extended: an earlier request's tokens are a prefix of the later one's,
    with 1 to EXTENDED_MAX_ADDED tokens added. Each later message is paired
    with its most recent match."""
    exact: list[list[int]] = []
    extended: list[list[int]] = []
    humans = [(i, m) for i, m in enumerate(msgs) if m.sender == "human"]
    for pos, (i, m) in enumerate(humans):
        earlier = humans[:pos]
        if _codepoints(m.text) >= EXACT_MIN_CODEPOINTS:
            key = _exact_key(m.text)
            hit = next((j for j, e in reversed(earlier) if _exact_key(e.text) == key), None)
            if hit is not None:
                exact.append([i, hit])
                continue
        toks = tokens(m.text)
        for j, e in reversed(earlier):
            etoks = tokens(e.text)
            added = len(toks) - len(etoks)
            if etoks and 1 <= added <= EXTENDED_MAX_ADDED and toks[:len(etoks)] == etoks:
                extended.append([i, j])
                break
    return exact, extended


def run_counts(chunk_msgs: list[Message], runs: dict[str, Run]) -> dict[str, int]:
    """Runs whose reply is in the chunk, by the run summarizer's outcome. A
    run with no outcome is counted by its status."""
    seen: set[str] = set()
    c: Counter[str] = Counter()
    for m in chunk_msgs:
        if not m.run_uuid or m.run_uuid in seen:
            continue
        seen.add(m.run_uuid)
        run = runs.get(m.run_uuid)
        if run is None:
            c["unknown run"] += 1
        else:
            c[run.outcome or f"status {run.status}"] += 1
    return dict(c)


def outcome_text(runs: dict[str, int], exact: int) -> str:
    keys = [k for k in OUTCOME_ORDER if k in runs] + sorted(k for k in runs if k not in OUTCOME_ORDER)
    parts = [f"{runs[k]} {k}" for k in keys]
    if exact:
        parts.append(f"{exact} exact repeat{'s' if exact != 1 else ''}")
    return ", ".join(parts) or "no runs"


def _when(dt: datetime) -> str:
    return dt.strftime("%b %d %H:%M")


def build_level1(msgs: list[Message], runs: dict[str, Run]) -> list[Chunk]:
    exact, extended = repeats(msgs)
    out = []
    for first, last in chunk_ranges(len(msgs)):
        part = msgs[first:last + 1]
        out.append(Chunk(
            level=1, first=first, last=last,
            first_id=part[0].id, last_id=part[-1].id,
            started=_when(part[0].created_at), ended=_when(part[-1].created_at),
            exchanges=sum(1 for m in part if m.sender == "human"),
            runs=run_counts(part, runs),
            exact_repeats=[p for p in exact if first <= p[0] <= last],
            extended_repeats=[p for p in extended if first <= p[0] <= last],
        ))
    return out


def build_level2(level1: list[Chunk], size: int = CHUNK_SIZE) -> list[Chunk]:
    """Every complete group of `size` level 1 chunks, summed. Topic names are
    listed in first-seen order; nothing is generated."""
    out = []
    for g in range(len(level1) // size):
        group = level1[g * size:(g + 1) * size]
        runs: Counter[str] = Counter()
        topics: list[str] = []
        for c in group:
            runs.update(c.runs)
            topics += [t for t in c.topics if t not in topics]
        out.append(Chunk(
            level=2, first=group[0].first, last=group[-1].last,
            first_id=group[0].first_id, last_id=group[-1].last_id,
            started=group[0].started, ended=group[-1].ended,
            exchanges=sum(c.exchanges for c in group), runs=dict(runs),
            exact_repeats=[p for c in group for p in c.exact_repeats],
            extended_repeats=[p for c in group for p in c.extended_repeats],
            topics=topics,
        ))
    return out


# --- the generative call (prompt building and parsing are pure) -------------

SYSTEM_PROMPT = (
    "You index a chat log so that an assistant can later orient itself in a "
    "long conversation. You receive one chunk of a conversation between a "
    "human and an assistant, with a few messages of context before and after "
    "it. Describe only the chunk; the context is there so you can tell where "
    "a subject began or continues.\n\n"
    "Answer with exactly two lines and nothing else.\n"
    "The first line starts with \"asked:\" and says, in at most 120 "
    "characters, what kinds of things the human asked for in the chunk. "
    "Describe the kind of request, never its answer. Never state a value, a "
    "number, a date, a name or a fact that appears in an assistant reply, "
    "and never quote a reply.\n"
    "The second line starts with \"topics:\" and lists one to four topic "
    "names for the subjects of the chunk, separated by commas. A topic name "
    "is lowercase words joined by hyphens, at most 24 characters. When a "
    "subject is one of the known topic names, reuse that name exactly."
)


def _cut(text: str, limit: int = PROMPT_MESSAGE_MAX_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    half = (limit - 5) // 2
    return f"{text[:half]} [..] {text[-half:]}"


def _lines(msgs: list[Message], offset: int) -> str:
    who = {"human": "human", "agent": "assistant"}
    return "\n".join(f"[m{offset + k} {who.get(m.sender, m.sender)}] {_cut(m.text)}"
                     for k, m in enumerate(msgs)) or "(none)"


def build_prompt(msgs: list[Message], chunk: Chunk,
                 known_topics: list[str]) -> dict[str, str]:
    before_start = max(0, chunk.first - MARGIN)
    before = msgs[before_start:chunk.first]
    body = msgs[chunk.first:chunk.last + 1]
    after = msgs[chunk.last + 1:chunk.last + 1 + MARGIN]
    user = "\n\n".join([
        "Known topic names: " + (", ".join(known_topics) if known_topics else "none yet"),
        "Context before the chunk, not to be described:\n" + _lines(before, before_start),
        f"The chunk, m{chunk.first} to m{chunk.last}:\n" + _lines(body, chunk.first),
        "Context after the chunk, not to be described:\n" + _lines(after, chunk.last + 1),
    ])
    return {"system": SYSTEM_PROMPT, "user": user}


def slug(name: str) -> str:
    s = "-".join(tokens(name))
    return s[:TOPIC_MAX_CHARS].rstrip("-")


def parse_reply(text: str) -> tuple[str | None, list[str], list[str]]:
    """(asked, topics, problems). Lines are matched by their label anywhere in
    the reply; anything off-format is reported, not repaired silently."""
    problems = []
    asked = None
    topics: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("*-# ").strip()
        low = line.lower()
        if low.startswith("asked:") and asked is None:
            asked = line[len("asked:"):].strip()
        elif low.startswith("topics:") and not topics:
            raw = [t for t in line[len("topics:"):].split(",") if t.strip()]
            for t in raw:
                s = slug(t)
                if s != t.strip():
                    problems.append(f"topic {t.strip()!r} normalized to {s!r}")
                if s and s not in topics:
                    topics.append(s)
    if asked is None:
        problems.append("no asked: line")
    elif len(asked) > ASKED_MAX_CHARS:
        problems.append(f"asked is {len(asked)} characters")
    if not topics:
        problems.append("no topics: line")
    elif len(topics) > MAX_TOPICS:
        problems.append(f"{len(topics)} topics")
    return asked, topics, problems


def check_local(base_url: str, model: str) -> None:
    host = urllib.parse.urlparse(base_url).hostname
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(f"refusing non-loopback server {base_url!r}")
    if model.endswith(":cloud") or "-cloud" in model:
        raise SystemExit(f"refusing cloud-backed model {model!r}")


def check_out(out: Path) -> None:
    """The output holds the room's own messages; it never goes in the repo."""
    resolved = out.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise SystemExit(f"refusing --out inside the repository: {resolved}")


def _http(base_url: str, path: str, body: dict | None = None,
          timeout: float = 180) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url + path, data,
                                 {"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ollama_chat(base_url: str, model: str, prompt: dict[str, str]) -> tuple[str, dict[str, Any]]:
    t = time.monotonic()
    out = _http(base_url, "/api/chat", {
        "model": model, "stream": False, "think": False,
        "messages": [{"role": "system", "content": prompt["system"]},
                     {"role": "user", "content": prompt["user"]}],
        "options": {"temperature": 0, "num_predict": 200},
    })
    timing = {"wall_ms": round((time.monotonic() - t) * 1000)}
    for k in ("load_duration", "prompt_eval_duration", "eval_duration", "total_duration"):
        if k in out:
            timing[k.replace("_duration", "_ms")] = round(out[k] / 1e6)
    for k in ("prompt_eval_count", "eval_count"):
        if k in out:
            timing[k] = out[k]
    return out.get("message", {}).get("content", ""), timing


def environment(base_url: str, model: str) -> dict[str, Any]:
    env: dict[str, Any] = {"url": base_url, "model": model}
    try:
        env["server_version"] = _http(base_url, "/api/version").get("version")
        tags = _http(base_url, "/api/tags").get("models", [])
        env["digest"] = next((t.get("digest") for t in tags if t.get("name") == model), None)
    except OSError as e:
        env["error"] = str(e)
    return env


def generate(msgs: list[Message], level1: list[Chunk], base_url: str, model: str,
             dry_run: bool, burst: int, cooldown: float,
             sleep=time.sleep, chat=ollama_chat, log=print) -> None:
    """Fill `asked` and `topics` chunk by chunk, oldest first, so each call
    sees the topic names the earlier chunks used."""
    known: list[str] = []
    calls = 0
    for c in level1:
        c.prompt = build_prompt(msgs, c, known)
        if dry_run:
            continue
        if calls and calls % burst == 0:
            log(f"  cooling down {cooldown:.0f} s after {calls} calls")
            sleep(cooldown)
        calls += 1
        try:
            reply, timing = chat(base_url, model, c.prompt)
        except (OSError, ValueError) as e:
            c.error = f"{type(e).__name__}: {e}"
            log(f"  m{c.first}-{c.last}: {c.error}")
            continue
        c.reply, c.timing = reply, timing
        c.asked, c.topics, c.parse_problems = parse_reply(reply)
        known += [t for t in c.topics if t not in known]
        log(f"  m{c.first}-{c.last}: {timing.get('wall_ms')} ms  "
            f"topics={','.join(c.topics) or '-'}"
            + (f"  problems={c.parse_problems}" if c.parse_problems else ""))


# --- rendering (pure) -------------------------------------------------------

def _xml_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;"))


def render_chunk_xml(c: Chunk) -> str:
    attrs = (f'level="{c.level}" messages="{c.first}-{c.last}" '
             f'exchanges="{c.exchanges}" before_topics="true"')
    if c.level == 1 and c.asked:
        attrs += f' asked="{_xml_attr(c.asked)}"'
    if c.topics:
        attrs += f' topics="{_xml_attr(",".join(c.topics))}"'
    outcome = outcome_text(c.runs, len(c.exact_repeats))
    return (f"  <chunk {attrs}>\n"
            f'    <topic slug="?" exchanges="{c.exchanges}" outcome="{_xml_attr(outcome)}"/>\n'
            f"  </chunk>")


def earlier_block(n: int, level1: list[Chunk], level2: list[Chunk]) -> tuple[int, str]:
    """What the loop would see before the verbatim window: level 2 for every
    complete group of 100 before it, level 1 for the rest."""
    w = window_start(n)
    shown: list[Chunk] = [c for c in level2 if c.last < w]
    covered = shown[-1].last if shown else -1
    shown += [c for c in level1 if covered < c.first and c.last < w]
    body = "\n".join(render_chunk_xml(c) for c in shown)
    return w, f"<earlier>\n{body}\n</earlier>" if body else "<earlier/>"


def render_report(room: str, n: int, env: dict[str, Any], level1: list[Chunk],
                  level2: list[Chunk], dry_run: bool) -> str:
    out = [f"# Digest compaction: room {room}", "",
           f"- counted messages: {n}",
           f"- model: {env.get('model')} (digest {env.get('digest') or '?'}, "
           f"server {env.get('server_version') or '?'})",
           f"- level 1 chunks: {len(level1)}; level 2: {len(level2)}",
           f"- dry run: {'yes, no model was called' if dry_run else 'no'}", ""]
    for c in level1 + level2:
        out.append(f"## L{c.level} m{c.first}-{c.last} · {c.started} – {c.ended} · "
                   f"{c.exchanges} exchanges")
        out.append(f"- records: {outcome_text(c.runs, len(c.exact_repeats))}")
        if c.extended_repeats:
            pairs = ", ".join(f"m{a}←m{b}" for a, b in c.extended_repeats)
            out.append(f"- extended repeats (recording only, not rendered): {pairs}")
        if c.exact_repeats:
            pairs = ", ".join(f"m{a}←m{b}" for a, b in c.exact_repeats)
            out.append(f"- exact repeats: {pairs}")
        if c.level == 1:
            out.append(f"- asked: {c.asked if c.asked is not None else '(not generated)'}")
        out.append(f"- topics: {', '.join(c.topics) or '(none)'}")
        if c.parse_problems:
            out.append(f"- parse problems: {'; '.join(c.parse_problems)}")
        if c.error:
            out.append(f"- error: {c.error}")
        if c.timing:
            out.append(f"- timing: {c.timing}")
        out.append("")
    w, block = earlier_block(n, level1, level2)
    out += [f"## What the loop would see now",
            f"Window: m{w}-m{n - 1} verbatim ({n - w} messages). Before it:", "",
            "```xml", block, "```", ""]
    return "\n".join(out)


# --- database (read-only) ---------------------------------------------------

def load_room(database_url: str, room: str) -> tuple[list[Message], dict[str, Run]]:
    import psycopg  # the app's driver; imported here so the pure parts need no database

    dsn = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        rows = conn.execute(
            "select m.id, m.uuid::text, u.user_type, m.text, m.created_at, m.kind,"
            " m.streaming, m.meta->>'assistant_run_uuid'"
            " from chat_message m join chat_user u on u.uuid = m.sender_uuid"
            " where m.room_uuid = %s order by m.id", (room,)).fetchall()
        run_rows = conn.execute(
            "select uuid::text, status, summary->>'outcome' from assistant_run"
            " where room_uuid = %s", (room,)).fetchall()
    msgs = [Message(id=r[0], uuid=r[1], sender=r[2], text=r[3], created_at=r[4],
                    kind=r[5], streaming=r[6], run_uuid=r[7]) for r in rows]
    runs = {r[0]: Run(uuid=r[0], status=r[1], outcome=r[2]) for r in run_rows}
    return msgs, runs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    p.add_argument("room", help="chatroom uuid")
    p.add_argument("--database-url", required=True,
                   help="read-only source; no default, so the database is always explicit")
    p.add_argument("--out", required=True, type=Path, help="output directory, outside the repo")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--dry-run", action="store_true", help="build prompts and records; call no model")
    p.add_argument("--burst", type=int, default=10, help="generative calls per burst")
    p.add_argument("--cooldown", type=float, default=60, help="seconds between bursts")
    args = p.parse_args(argv)

    check_out(args.out)
    if not args.dry_run:
        check_local(args.url, args.model)
    raw, runs = load_room(args.database_url, args.room)
    msgs = counted(raw)
    level1 = build_level1(msgs, runs)
    print(f"{len(msgs)} counted messages, {len(level1)} level 1 chunks"
          f" -> {0 if args.dry_run else len(level1)} generative calls"
          f" in bursts of {args.burst} with {args.cooldown:.0f} s cool-down")
    env = {"model": args.model} if args.dry_run else environment(args.url, args.model)
    generate(msgs, level1, args.url, args.model, args.dry_run, args.burst, args.cooldown)
    level2 = build_level2(level1)

    args.out.mkdir(parents=True, exist_ok=True)
    doc = {"room": args.room, "created_at": datetime.now(UTC).isoformat(),
           "counted_messages": len(msgs), "dry_run": args.dry_run, "environment": env,
           "level1": [asdict(c) for c in level1], "level2": [asdict(c) for c in level2]}
    (args.out / "run.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    (args.out / "report.md").write_text(
        render_report(args.room, len(msgs), env, level1, level2, args.dry_run), encoding="utf-8")
    print(f"wrote {args.out / 'run.json'} and {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
