# Per-step LLM token metrics — design (2026-06-24)

**Status:** approved-direction, complete spec (decisions made; implement
directly). Capture the input/output token counts of each assistant ReAct step's
LLM call and show them per-step in the `/assistant` timeline. Models the
instrumentation on PlanExe's structured-LLM token counting
(`worker_plan/.../llm_util/token_counter.py` +
`experiments/.../run_callback_handlers_on_structured_llm.py`).

## Decisions (made, with rationale)

- **Capture with LlamaIndex `TokenCountingHandler`, in the shared
  `_structured_completion` (`agents/base.py`).** For a *structured* LLM,
  `ChatResponse.raw` is the parsed Pydantic model (not the usage dict), so the
  reliable path is a `CallbackManager([TokenCountingHandler()])` passed to
  `the_llm.as_structured_llm(...)` — exactly the PlanExe structured experiment.
  After the stream, read `counter.prompt_llm_token_count` (input) and
  `counter.completion_llm_token_count` (output).
- **Expose via `self._last_usage` on the agent.** `_structured_completion` sets
  `self._last_usage = None` at entry and, on success,
  `{"input": …, "output": …}`. Generic (all `ModelGroupAgent`s get it); only the
  assistant consumes it. Initialized to `None` in `ModelGroupAgent.__init__` so a
  test that monkeypatches `_decide_next_step` (bypassing `_structured_completion`)
  still has the attribute.
- **Attribute to the step in `agents/assistant.py`.** Each loop step makes
  exactly one `_decide_next_step` → `_structured_completion` call (skill/profile
  blocks are retrieval, not LLM), so it's a clean 1:1. Right after `decide`, read
  `usage = self._last_usage` and thread it **explicitly** into the step that
  decision produces (open / record). Explicit threading — not reading the instance
  attr inside the recorders — so a `control` step (no decide call) never inherits a
  prior step's counts.
- **No run total.** Steps may use different models (reasoning vs not), so a sum is
  misleading. Per-step only.
- **Summarizer out of scope.** `assistant_run_summarizer` runs through the same
  `_structured_completion` (so `_last_usage` is set) but nothing stores or shows it
  — no schema/UI for it.
- **`input`/`output` only.** No reasoning/thinking-token column (YAGNI).

## Schema (`db/models.py` + migration)

`AssistantStep` gains two nullable columns:
```python
input_tokens:  Mapped[int | None] = mapped_column()
output_tokens: Mapped[int | None] = mapped_column()
```
Migration: `_add_column_if_missing("assistant_step", "input_tokens", "input_tokens INTEGER")`
and the same for `output_tokens`. Null = not captured (a `control` step, a
crash before the call returned, or a provider that reported nothing).

## Capture (`agents/base.py`)

In `_structured_completion`:
- `self._last_usage = None` at entry.
- One `counter = TokenCountingHandler()` per call (accumulates across the
  candidate-model fallback loop); pass
  `callback_manager=CallbackManager([counter])` to `as_structured_llm`.
- On the successful return path, set
  `self._last_usage = {"input": counter.prompt_llm_token_count,
  "output": counter.completion_llm_token_count}`.

## Attribute + store (`agents/assistant.py`, `db/assistant.py`)

- Loop: after `decision = self._decide_next_step(...)`, `usage = self._last_usage`.
- Pass `usage` into the step recorders for **this** step only:
  - non-terminal → `_open_step(..., usage=usage)` → `db.open_assistant_step(...,
    input_tokens=usage["input"], output_tokens=usage["output"])`.
  - validation-failed / `final` → `_record_step(..., usage=usage)` →
    `db.append_assistant_step(..., input_tokens=…, output_tokens=…)`.
  - `control` steps and the crash-`_fail_run` row → no usage (None).
- `db.open_assistant_step` / `append_assistant_step` gain
  `input_tokens=None, output_tokens=None` params. `settle_assistant_step` is
  unchanged (tokens were written at open).

## Display (`/assistant` timeline, `webapp/assistant_views.py`)

Each step card gains a small line when either count is present:
`in {{ step.input_tokens or 0 }} · out {{ step.output_tokens or 0 }} tok`.

## Caveat (surfaced)

`TokenCountingHandler` reports the provider's `usage` when the streamed response
carries it; for some local models (LM Studio / Ollama) over streaming it falls
back to a **tiktoken estimate**, so those numbers are approximate. Verify which
path the configured models take during implementation; if estimated, note it
(e.g. a `~` prefix or a tooltip). Same caveat as the PlanExe setup.

## Testing (model-free)

- `db/test_assistant_trace.py`: `open_assistant_step` / `append_assistant_step`
  persist `input_tokens`/`output_tokens`.
- `agents/test_assistant.py`: a decider that sets `agent._last_usage` before
  returning → the resulting step row stores those counts; a `control` step stores
  `None`.
- `webapp/test_assistant_views.py`: a step seeded with token counts renders the
  `in … · out … tok` line; a step without them does not.
- The base.py capture (handler wiring) is a thin use of a documented LlamaIndex
  feature mirrored from PlanExe; verified manually against a configured model
  rather than unit-tested (would need a faked streaming LLM + callback events).

## Out of scope

- Run/total aggregation, cost (USD), reasoning-token column, per-model rollups,
  and summarizer metrics.

## Acceptance

A finished assistant run shows, per step in the `/assistant` timeline, the input
and output token counts of that step's LLM call (null/blank where unavailable),
captured via `TokenCountingHandler` in the shared structured-completion path.
Suite green, model-free.
