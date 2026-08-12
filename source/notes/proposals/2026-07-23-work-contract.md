# The work contract — consequences of side-effectful requests

**Status:** Seed, deliberately deferred. The acceptance-criteria proposal
(`2026-07-23-reply-acceptance-criteria.md`) is scoped to the REPLY
contract; this document parks the work-contract half so its drafted
material and open questions aren't lost.
**Date:** 2026-07-23

## The idea

A request like "move kanban task X to DONE" delivers a state change, not
an answer — the reply is only the receipt. Modifying stuff is rarely the
sunshine scenario, so the contract for the WORK needs its own
considerations, established before the first mutation runs:

- **side_effects** — the measurable outcome ("kanban task X is in the
  DONE column"), and ONLY the named changes: anything beyond them is out
  of scope for the request.
- **consequences** — the pre-mortem per effect: reversibility (its write
  tier — log-and-undo is undoable, confirm-tier waits for the operator),
  the blast radius of a wrong target, and the failure stance: on a
  failed mutation the reply reports the ACTUAL state, never the intended
  one.

Drafted example for "move kanban task X to DONE":

```json
{"side_effects": ["kanban task X is in the DONE column"],
 "consequences": [
   "kanban_move is log-and-undo — reversible via the undo ledger",
   "a wrong target task would misstate project status; resolve 'X' via find_uuid before moving",
   "on failure: report the task's actual column; do not claim the move"]}
```

What this buys, wired to existing machinery:

- The audit gains the non-sunshine branch: the message passes the
  acceptance test only by claiming exactly the side effects whose steps
  returned ok — the anti-fabrication rule as a per-run named checklist.
- Write tiers become visible in the contract: a confirm-tier effect is
  specified as "a proposal awaiting confirmation", never "done".
- Scope is bounded for the second opinion and the summariser: moving X
  must not also touch Y.
- Mid-run revision covers the no-op: a read revealing X already in DONE
  revises the contract, so the reply reports the true state.

The contract complements the mechanical safety machinery (write tiers,
undo ledger, duplicate-write blocks, second-opinion gate) — it makes the
stakes visible before the first mutation; it never replaces enforcement.

## Why deferred

Risk analysis for mutations opens into a much larger toolkit than two
list fields — PlanExe's planning pipeline demonstrates the depth: SMART
criteria, risk assessment and mitigation, decision levers
(primary/secondary decisions), scenarios, premise attack, premortem,
stakeholder analysis. Bolting a shallow version onto the reply-contract
step risks doing to the criteria call what the typed-reply union did to
the decision format: more constraint surface than a small model can
carry. The right scope for the work contract — which of those
instruments earn their tokens in a personal-assistant loop, and whether
they run per-request or only for confirm-tier writes — is its own design
conversation.

## The governing constraint: localhost latency

Everything runs on localhost against a small local model, and a chat
turn must answer in seconds — not 15 minutes. That constraint does most
of the scoping by itself:

- **In budget:** at most one extra small structured call in the loop. A
  `side_effects` + `consequences` pair (a few short lines) fits; that is
  the ceiling for anything that runs per-request.
- **Out of budget:** the deep PlanExe instruments (scenarios, levers,
  premise attack, full premortem) as loop steps. They are minutes of
  model time each; they cannot live on the conversational path.
- **The one latency-free slot:** a confirm-tier write already pauses the
  run and waits for the operator — the conversation is stopped anyway.
  If a deeper analysis ever earns its place, it runs asynchronously
  while the confirm card is pending and attaches its findings to the
  card; the operator reads them when deciding. Latency there costs
  nothing, which makes the confirm card the only plausible home for
  heavyweight risk analysis.

## Open questions for the full proposal

- Does the lightweight work contract ride the same step-0 call as the
  reply contract, or run only when the request implies a write (cheaper,
  but needs a code-side trigger)?
- Should the confirm-card UI show the contract (side effects +
  consequences) so the operator approves against the same yardstick the
  audit uses?
- Evals: "move task X to DONE" with a scripted failed move — the reply
  must NOT claim completion; over-reach cases (a second task touched)
  must fail the acceptance test.
