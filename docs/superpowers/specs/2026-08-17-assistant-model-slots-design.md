# Assistant model slots

One /agentmodel binding per assistant model call, each falling back to a single
`assistant.default`.

## Why

The assistant makes eight distinct model calls, but only four are separately
bindable and the other four ride the assistant's own group. That makes
"experiment with a non-reasoning model" an all-or-nothing switch: the ReAct
loop, the acceptance criteria, the reply audit and the recall filter all move
together, so a regression cannot be attributed to the step that caused it.

Per-step bindings turn the question into an experiment. `assistant.decide` runs
many times per turn and is where latency is felt; the one-shot checks around it
run once. Being able to move them independently is the point.

## The name is already decided

`agents/base.py:_caller_tag` builds this vocabulary today for /activity
attribution: `assistant.decide`, `assistant.memory_filter`,
`assistant.second_opinion`, `assistant.reply_audit`,
`assistant.response_language_classifier`, `assistant.acceptance_criteria`,
`assistant.request_summary`, and — via `caller_name` —
`assistant.run_summarizer`. /agentmodel uses a different, older vocabulary for
a subset of the same calls.

**The /agentmodel slot names are the /activity caller tags.** One name per
model call, on both pages, with no translation table between them.

## Slots

Nine binding-only `agent_config` entries:

| Slot | Replaces | Calls per run |
| --- | --- | --- |
| `assistant.default` | — (new) | fallback for all |
| `assistant.decide` | the `assistant` entry's own binding | one per step |
| `assistant.acceptance_criteria` | assistant's own group | one, plus revisions |
| `assistant.request_summary` | assistant's own group | zero or one |
| `assistant.memory_filter` | `memory_filter` (shared) | one per memory_query |
| `assistant.second_opinion` | `second_opinion` | one per gated action |
| `assistant.reply_audit` | `reply_audit` | one |
| `assistant.response_language_classifier` | `response_language_classifier` | one |
| `assistant.run_summarizer` | `assistant_run_summarizer` | one, off the critical path |

Every slot keeps `requires_structured_output: True` — every call listed here
parses into a Pydantic model.

## Resolution

Exactly two links, identical for every call:

```
assistant.<step>  ->  assistant.default
```

Resolved through the existing `agents.query_filter_router.resolve_model_group`,
which already walks a `[(agent_uuid, label), ...]` chain and skips a binding
whose group is empty. No third level, no per-step special case. When neither is
bound the call has no candidates, which each call site already handles: the
loop raises, the optional calls record a skipped step.

## The `assistant` entry stops being a model binding

`assistant` remains the runnable agent — class path, chat identity, room
membership, the sender name on /chat. It gains `uses_model_group = False`, so
it disappears from /agentmodel: nothing on that page is named `assistant` any
more, and every row names a specific call.

`AssistantAgent.setup()` overrides the base binding read and resolves
`assistant.default` into `self.candidate_model_uuids` / `self.model_group_uuid`
— the value used by any path that has not asked for a specific slot. Each call
site resolves its own slot at call time and passes the result to
`_structured_completion(candidate_model_uuids=...)` (a parameter that already
exists) or to `structured_llm_call`.

## `memory_filter` stops being shared

`resolve_filter_model_group` currently prepends the `memory_filter` binding for
both the assistant's recall filter and the `query_filter_router` chat agent, so
keep/drop decisions come from one model identity. That coupling is now the
thing in the way: it is a shared knob standing between the operator and a
per-step choice.

`assistant.memory_filter` becomes assistant-only. `query_filter_router` scores
on its own bound group, through the same generic chain as everything else, and
`resolve_filter_model_group` / `resolve_filter_model_uuids` are deleted.

## Trace and page

- Every model call records which slot answered (`group_from`) on its step row,
  uniformly. This is how the experiment is read afterwards: which model ran
  which step, from the run itself rather than from what was bound at the time.
- /agentmodel groups rows by dotted prefix under a subheading, so the assistant
  block reads as a unit.
- An unbound `assistant.*` row reads `-> assistant.default` rather than
  `none assigned`, so the fallback is visible without inferring it.

## Out of scope

- Which steps run at all (the response-language gating proposal is a separate
  question).
- New model-group machinery. Groups already express a prioritized fallback
  list, which is everything the experiment needs.
- Preserving existing bindings. `assistant.default` and the renamed slots are
  bound once on /agentmodel after the change.
