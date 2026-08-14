# Bounding generation server-side

## Why this is the only real guarantee

When a run is abandoned — the supervisor's watchdog fires, the operator stops
the run, the process dies — everything the code can do is *ask* the inference
server to stop. It asks by closing the HTTP connection, and that only works if
the server notices and honours the disconnect. llama.cpp aborts a generation
when the client goes away only if it checks between tokens; if the connection
sits half-open, or the server is mid-batch, it keeps going. Ollama sits on the
same engine.

The observed failure: a heartbeat-timeout SIGKILL left a `llama-server`
generating at full GPU with the run already marked failed and nothing left in
the system that knew about the request. SIGTERM-then-SIGKILL (see `main.py`
`TERM_GRACE`) fixes the common case, because the worker now unwinds and closes
the stream. It does not fix the case where the unwind does not finish in time,
and it does not fix a server that ignores the disconnect.

A cap on the number of tokens the server will produce is the only bound that
holds without the server's cooperation on cleanup. The generation ends because
it hit the cap, not because anyone asked.

## Why the caps should differ per call

The assistant makes six kinds of model call and their output sizes are not
remotely comparable:

| call | what it emits | realistic ceiling |
|---|---|---|
| response-language classifier | a Likert score per declared language + a one-line reason | very small |
| acceptance criteria | three prose fields, one or two sentences each | small |
| recall filter | three scores per candidate + 1-3 sentences | small, scales with candidate count |
| second opinion | a verdict plus a problem list | small |
| reply audit | a verdict plus a defect list | small |
| decide | a step decision — but `reply` carries the whole answer | this is the only unbounded one |
| request summary | a summary of an over-long request | bounded by its own field descriptions |

Only `decide` can legitimately produce a long output, and only on the step that
emits the final `reply`. A classifier that has started generating a hundred
pages is not doing the job — it is looping, and a cap turns that from "GPU
pinned until someone notices" into "one failed step the loop can retry or fall
back from."

So a single global cap is the wrong shape. It has to be high enough for the
longest legitimate reply, which makes it useless as a runaway guard for the
five narrow calls.

## Where it would go

`db.resolved_model_kwargs(model_uuid)` returns the per-model arguments that
`llm.prepare_llm` splats into the client. The cap is a normal member of that
dict — `num_predict` on the native Ollama wrapper, `max_tokens` on the
OpenAI-compat path (`ThinkingAwareOpenAILike`). Both already pass through:
`_prepare_ollama_llm` filters to `Ollama.model_fields`, and `OpenAILike` takes
`max_tokens` directly.

That means the mechanism needs no new plumbing. What it needs is a decision
about where the number comes from:

- **Per model config** (a field on the model row, edited on `/model`). One
  number per model, applied to every call that model serves. Simple, and wrong
  for a model bound to both the decide loop and the classifier.
- **Per call site** (a constant in `assistant.py` merged into the args before
  `prepare_llm`). Matches the table above — each call caps at what its own job
  can need. More places to keep honest, but the numbers are stable because
  they follow the response schema, not the model.
- **Per agent binding** (a field alongside the model group on `/agentmodel`).
  Sits where the call/model pairing is already configured, so the classifier's
  cap follows the classifier binding whichever model it resolves to.

Per call site is the closest fit to the actual risk: the bound belongs to the
job, not to the hardware. Per agent binding is the closest fit to how the rest
of the system is configured.

## What a cap does when it is hit

Truncation, not an error. The provider stops emitting and the response comes
back short — which for a structured call means the JSON is incomplete and
parsing fails. That surfaces as a failed step, and the existing per-model
fallback in `_structured_call` tries the next member of the group.

That is the right failure for the five narrow calls: incomplete output from a
classifier is worthless anyway, and failing fast is better than waiting out a
loop. It is the wrong failure for a long `reply` — a truncated answer that
fails to parse loses work the model actually did. So the decide call's cap has
to sit above the longest reply worth producing, and that number is a product
decision rather than a safety one.

## Not done

No cap is set today. The runaway path is bounded only by the streaming
deadline in `_structured_call`, the httpx read timeout, and the supervisor
watchdog — all three of which bound *the client's waiting*, not the server's
working.
