"""Opt-in test: does a turn decide better with the room's background?

The probes of notes/proposals/2026-11-21-chat-compaction.md (§6). One
invented room, dated 1999: 90 messages summarized as nine hand-written
chunks, then a window of 30 messages about something else. Twelve requests
are each put to the assistant's decide call twice, with the background off
and on, and the two decisions are written side by side for a person to
judge against what each probe passes on.

It is not a tournament between prompts: a run of twelve cannot rank
near-equal variants. It answers whether the background lets the assistant
use what the window no longer holds, and whether it breaks something that
worked.

- **The decide calls of one turn, and no tool.** A read the model chooses
  is answered by a fixed observation (`OBSERVATIONS`): the board as it
  stands, and nothing found anywhere else, which is what the memory holds
  of an invented room. The turn is followed to its reply, at most
  `MAX_STEPS` calls. The first call alone says little: the assistant
  reads before it answers, with the background and without.
- **The chunks are written by hand**, not by the summarizer. What is probed
  is the reading of the blocks, on rows that are known.
- **No database.** The prompt is built by the assistant's own builder on an
  agent filled by hand, and sent to a local Ollama server.

    python -m benchmarks.room_background_probes --out <dir> --dry-run
    python -m benchmarks.room_background_probes --out <dir>

`--dry-run` writes the prompts and calls no model. Without it, calls run in
bursts (8 by default) with a cool-down (60 s by default) between bursts,
because the operator's machine overheats on longer runs. Refuses `:cloud`
models and non-loopback servers.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e4b-mlx"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DAY0 = datetime(1999, 10, 4, 9, 0, tzinfo=UTC)
PROMPT_VERSION = 23


# --- the room --------------------------------------------------------------------

def _part(wanted: str, result: str, **more: Any) -> dict[str, Any]:
    part = {"summary": wanted, "result": result, "requests": 1,
            "reactions": ["moved_on"], "signals": {"runs": {"resolved": 1}}}
    part.update(more)
    return part


#: The nine chunks before the window, oldest first: `(subjects' parts,
#: statements)`. The last four are shown part by part.
CHUNKS: list[tuple[list[dict[str, Any]], list[str]]] = [
    ([_part("how the seed list is kept", "the seed list is a paper list in the shed"),
      _part("the watering rota for the plots", "the rota was written down as the user gave it")],
     ["I keep the seed list on paper because the shed has no power.",
      "The watering rota goes by plot number, odd plots on Mondays and even plots on Thursdays."]),
    ([_part("volunteers for the early shift", "three names were suggested for the early shift"),
      _part("tide times for the harbour slip", "high water was given for the weekend",
            grounding="tool")],
     ["The hard part of running the allotment is getting volunteers for the early shift."]),
    ([_part("the rowing club newsletter draft", "a first draft of the newsletter was written"),
      _part("tide times for the harbour slip", "high water was given for Tuesday",
            grounding="tool")], []),
    ([_part("open tasks on the garden board", "four open tasks were listed", grounding="tool"),
      _part("the rowing club newsletter headline", "two headlines were offered")], []),
    ([_part("tide times for the harbour slip", "high water was given for Friday",
            grounding="tool"),
      _part("a title for the tool shed inventory", "the inventory was titled by shelf")], []),
    ([_part("the year the Skerry Point lighthouse was built", "a year was given",
            signals={"runs": {"resolved": 1}, "down": 1}),
      _part("open tasks on the garden board", "three open tasks were listed",
            grounding="tool")], []),
    ([_part("move the two painting tasks on the garden board to done",
            "both painting tasks were moved to done", grounding="tool"),
      _part("a packing list for the shed clear-out", "a list of twelve items was written")], []),
    ([_part("how many plots are rented this season", "nineteen plots were counted as rented",
            grounding="tool"),
      _part("tide times for the harbour slip", "high water was given for Sunday",
            grounding="tool")], []),
    ([_part("a notice for the gate about the water being off",
            "a short notice was written for the gate"),
      _part("open tasks on the garden board", "two open tasks were listed",
            grounding="tool")], []),
]

#: `(name, explanation, parts, in the shown chunks)`.
TOPICS = [
    ("tide times for the harbour slip", "High water is looked up for a given day", 4, True),
    ("the garden board", "The board with the allotment's tasks", 4, True),
    ("the rowing club newsletter", "A newsletter being drafted for the rowing club", 2, True),
]

#: The window: 15 exchanges about one new subject, none of which answers a probe.
WINDOW = [
    ("Let's plan the autumn plant sale. When should it be?",
     "A Saturday in late October suits a plant sale: the perennials can still be lifted."),
    ("Saturday the 23rd then. What do we need?",
     "Tables, price labels, a cash box, and someone to mind the gate."),
    ("How many tables?", "Six trestle tables would hold what a sale of this size brings in."),
    ("Write a sign for the gate.", "Autumn plant sale, Saturday 23 October, ten until two."),
    ("Shorter.", "Plant sale, Sat 23 Oct, 10 to 2."),
    ("What should the cuttings cost?", "Fifty pence for a cutting and two pounds for a potted plant is usual."),
    ("And the bulbs?", "Bulbs sell well in bags of ten for a pound."),
    ("Who brings the cash box?", "You have not said who holds it. Shall I note it as open?"),
    ("Note it as open.", "Noted: who brings the cash box is still open."),
    ("Draft a line for the notice board.", "Bring your spare plants to the sale on the 23rd."),
    ("Add that donations are welcome.", "Bring your spare plants to the sale on the 23rd. Donations welcome."),
    ("What if it rains?", "The sale can move under the lean-to by the tool shed."),
    ("How long to set up?", "An hour, with four people."),
    ("So we start at nine.", "Yes: setting up at nine for a ten o'clock start."),
    ("Good. That is the plan for the sale.", "The plan stands: Saturday 23 October, ten until two."),
]

#: `(name, request, passes when)`.
PROBES = [
    ("coverage", "What have we mostly talked about in this conversation, all in all?",
     "recurring topics of the room are named (tides, the garden board, the newsletter), none invented"),
    ("before the window", "What were we working on before we started on the plant sale?",
     "it matches the near lines (the gate notice, the board's tasks, the plots, the painting tasks)"),
    ("recognition", "Let's get back to the rowing club newsletter.",
     "the newsletter is treated as known, not as something never mentioned"),
    ("new subject", "What are we in the middle of right now?",
     "the plant sale is named, not the room's oldest topics"),
    ("rejected answer", "What year was the Skerry Point lighthouse built?",
     "no year is given from memory of the conversation: a read, or it says the earlier answer was rejected"),
    ("what happened", "Did we move the painting tasks to done?",
     "answered from the line, as something done then, without a read"),
    ("current state", "Are the painting tasks done right now?",
     "the board is read this turn"),
    ("missing detail", "What was the phone number of the paint supplier I gave you?",
     "says it does not have it, or reads; no number invented"),
    ("never discussed", "What did we conclude about the beehives?",
     "says it was not discussed, or reads; no conclusion invented"),
    ("stated: why", "Why do I keep the seed list on paper?",
     "the shed has no power"),
    ("stated: how", "How does the watering rota work?",
     "by plot number, odd plots on Mondays and even plots on Thursdays"),
    ("stated: hard", "What do I find hardest about running the allotment?",
     "getting volunteers for the early shift"),
]


#: What a read returns here, by the action's name; `""` is every other read.
OBSERVATIONS = {
    "kanban_query": 'Board "garden board". Done: "paint the gate", "paint the shed door". '
                    'Open: "mend the water butt", "order compost".',
    "": "Nothing was found.",
}
MAX_STEPS = 3


def build_room() -> tuple[list[dict[str, Any]], Any]:
    """The room's messages up to the window's end, and its background."""
    from types import SimpleNamespace

    from agents import room_background
    from agents.room_chunks import content_hash

    agent_uuid = str(uuid4())
    messages: list[dict[str, Any]] = []

    def add(sender: str, text: str) -> None:
        n = len(messages) + 1
        at = DAY0 + timedelta(days=(n - 1) // 10, minutes=n)
        messages.append({"id": n, "uuid": str(uuid4()), "kind": "message", "text": text,
                         "sender": sender, "sender_type": sender,
                         "sender_uuid": agent_uuid if sender == "agent" else "user",
                         "sender_name": "assistant" if sender == "agent" else "user",
                         "created_at": at, "timestamp": at.isoformat(), "meta": {}})

    for seq in range(len(CHUNKS)):
        for k in range(5):
            add("human", f"an earlier request, chunk {seq} number {k}")
            add("agent", f"an earlier reply, chunk {seq} number {k}")
    chunks = []
    for seq, (parts, statements) in enumerate(CHUNKS):
        inside = messages[seq * 10:(seq + 1) * 10]
        chunks.append({"uuid": uuid4(), "first_id": inside[0]["id"], "last_id": inside[-1]["id"],
                       "content_hash": content_hash(inside), "prompt_version": PROMPT_VERSION,
                       "summary": ", ".join(p["summary"] for p in parts),
                       "parts": parts, "statements": statements})
    for asked, answered in WINDOW:
        add("human", asked)
        add("agent", answered)
    window_first_id = messages[-30]["id"]
    shown = chunks[-1]["uuid"]
    tallied = {"axes": {"task": [], "thing": [
        {"topic": SimpleNamespace(name=name, explanation=about), "parts": parts,
         "messages": parts * 2, "last": "1999-10-12",
         "members": [SimpleNamespace(chunk_uuid=shown if recent else uuid4(), part_index=100 + i)]}
        for i, (name, about, parts, recent) in enumerate(TOPICS)]}}
    background = room_background.build(chunks, messages[:90], window_first_id, tallied)
    return messages, background


# --- the call --------------------------------------------------------------------

def decide_prompt(messages: list[dict[str, Any]], request: str, background: Any,
                  steps: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """The next decide call of a turn whose request is `request`, as the
    assistant builds it, in the production layout. `steps` are the reads
    the turn has made, each a decision with its observation."""
    from agents.assistant import (
        EXCHANGE_TURNS,
        AssistantAgent,
        AssistantTurnStep,
        _base_enabled_capabilities,
    )

    agent = AssistantAgent.__new__(AssistantAgent)
    agent.agent_uuid = messages[1]["sender_uuid"]
    agent._caps = _base_enabled_capabilities()
    agent._identity_block = ""
    agent._persona_block = ""
    agent._calibration_block = ""
    agent._profile_block = ""
    agent._skill_block = ""
    agent._criteria_markdown = ""
    agent._reply_language_markdown = ""
    agent._long_request_summary_markdown = ""
    agent._long_request_summary = None
    agent.step_limit = 8
    agent._step_budget = agent.step_limit
    agent._criteria_profile = {"data": {}}
    agent._prompt_layout = EXCHANGE_TURNS
    agent._room_background = background
    at = messages[-1]["created_at"] + timedelta(minutes=5)
    current = dict(messages[0], id=len(messages) + 1, uuid=str(uuid4()), text=request,
                   created_at=at, timestamp=at.isoformat())
    scratchpad = [
        AssistantTurnStep(step_index=i, action=str(d.get("action")), args=d.get("args") or {},
                          status="ok", observation=d["observation"], is_read=True,
                          reason=str(d.get("reason") or ""))
        for i, d in enumerate(steps or [])]
    user, followups = agent._build_decide_turns(
        messages=[*messages, current], scratchpad=scratchpad, step_index=len(scratchpad))
    return [{"role": "system", "content": agent._system_prompt()},
            {"role": "user", "content": user}, *followups]


def check_local(url: str, model: str) -> None:
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in LOOPBACK_HOSTS:
        sys.exit(f"refusing a server that is not on this machine: {url}")
    if model.endswith(":cloud") or "-cloud" in model:
        sys.exit(f"refusing a cloud model: {model}")


def ask(url: str, model: str, prompt: list[dict[str, str]]) -> dict[str, Any]:
    from agents.assistant import AssistantStepDecision

    body = json.dumps({"model": model, "messages": prompt, "stream": False,
                       "format": AssistantStepDecision.model_json_schema()}).encode()
    request = urllib.request.Request(f"{url}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        reply = json.load(response)
    text = (reply.get("message") or {}).get("content") or ""
    try:
        decision = json.loads(text)
    except ValueError:
        decision = {"unparsed": text[:2000]}
    return {"decision": decision, "seconds": round(time.monotonic() - started, 1),
            "prompt_tokens": reply.get("prompt_eval_count")}


def _said(results: list[dict[str, Any]] | None) -> str:
    if results is None:
        return "(not run)"
    out = []
    for result in results:
        decision = result["decision"]
        if "unparsed" in decision:
            out.append("unparsed: " + decision["unparsed"])
            continue
        args = decision.get("args") or {}
        action = str(decision.get("action"))
        if action == "reply":
            out.append("reply: " + " ".join(str(args.get("message") or "").split()))
        else:
            out.append(f"{action} {json.dumps(args, ensure_ascii=False)}")
    return " -> ".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    p.add_argument("--out", required=True, type=Path, help="output directory")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--dry-run", action="store_true", help="build the prompts; call no model")
    p.add_argument("--burst", type=int, default=8, help="calls per burst")
    p.add_argument("--cooldown", type=float, default=60, help="seconds between bursts")
    p.add_argument("--only", action="append", help="a probe's name; may be given again")
    args = p.parse_args(argv)
    if not args.dry_run:
        check_local(args.url, args.model)

    messages, background = build_room()
    probes = [probe for probe in PROBES if not args.only or probe[0] in args.only]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "background.txt").write_text(
        f"<room_topics>\n{background.topics}\n</room_topics>\n"
        f"<room_earlier>\n{background.earlier}\n</room_earlier>\n")
    print(f"{len(probes)} probes, off and on -> "
          f"{0 if args.dry_run else f'up to {2 * MAX_STEPS * len(probes)}'} calls"
          f" in bursts of {args.burst} with {args.cooldown:.0f} s cool-down")

    rows, calls = [], 0
    for name, request, passes in probes:
        row: dict[str, Any] = {"probe": name, "request": request, "passes_when": passes}
        for arm, given in (("off", None), ("on", background)):
            prompt = decide_prompt(messages, request, given)
            row[f"{arm}_prompt_chars"] = sum(len(m["content"]) for m in prompt)
            if args.dry_run:
                (args.out / f"prompt-{name.replace(' ', '-').replace(':', '')}-{arm}.txt").write_text(
                    "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in prompt))
                row[arm] = None
                continue
            steps: list[dict[str, Any]] = []
            row[arm] = []
            while True:
                if calls and calls % args.burst == 0:
                    time.sleep(args.cooldown)
                calls += 1
                result = ask(args.url, args.model, prompt)
                row[arm].append(result)
                decision = result["decision"]
                action = str(decision.get("action"))
                print(f"  {name} / {arm}: {action}, {result['seconds']} s", flush=True)
                if action == "reply" or "unparsed" in decision or len(row[arm]) == MAX_STEPS:
                    break
                steps.append(dict(decision, observation=OBSERVATIONS.get(
                    action, OBSERVATIONS[""])))
                prompt = decide_prompt(messages, request, given, steps)
        rows.append(row)
        (args.out / "run.json").write_text(json.dumps(
            {"created_at": datetime.now(UTC).isoformat(), "model": args.model,
             "dry_run": args.dry_run, "rows": rows}, indent=1, ensure_ascii=False) + "\n")

    lines = [f"# Room background probes ({args.model})", ""]
    for row in rows:
        lines += [f"## {row['probe']}", "", f"Request: {row['request']}", "",
                  f"Passes when: {row['passes_when']}", "",
                  f"- **off** ({row['off_prompt_chars']:,} chars): {_said(row['off'])}",
                  f"- **on** ({row['on_prompt_chars']:,} chars): {_said(row['on'])}", ""]
    (args.out / "report.md").write_text("\n".join(lines))
    print(f"wrote {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
