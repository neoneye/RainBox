# Typed reply decision — a two-branch union for the assistant step

**Status:** Withdrawn after a live trial on the `typed-reply-decision`
branch (never merged). Two by-products of the trial were kept and merged:
the structured-stream re-validation fix in `agents/base.py` and the
literal audit verdict in the self-audit gate. This document records the
design, what the trial actually showed, and why the union was dropped.
**Date:** 2026-07-22

## The idea

The reply's formatting fidelity work (user_settings_json, the self-audit,
the `1_message`/`2_audit` prefixes) kept fighting one constraint: `args`
is a free-form dict, and key order inside a free-form dict is not
schema-enforceable. The proposal: make the decision a two-branch union —

```json
{"reason": "...", "args": {"message": "...", "audit": "..."}}          ← reply, no action field
{"reason": "...", "action": "memory_query", "args": {"query": "..."}}  ← everything else
```

— with `ReplyDecision` typing `message` before `audit` as real schema
properties (both required, extra keys forbidden on both branches, `reply`
excluded from the generic branch's action enum). If grammar-constrained
decoding enforces the schema, the writing order becomes physically
unviolatable and the prefixes, the raw-text order check, and the
bounce-teaching all become unnecessary.

## What the live trial showed

The union was implemented, adopted on the branch, and run against the
production model (nemotron-3-nano:4b via Ollama, non-function-calling,
thinking on). Three findings:

1. **The core premise does not hold on this stack.** The traces showed
   the model emitting `"action": "reply"` and audit-before-message —
   shapes the union schema forbids. On the Ollama non-function-calling
   path, llama-index embeds the schema in the prompt; it is *advisory*,
   not grammar-enforced. "The grammar physically forces the order" was
   the entire justification for the union, and it is false here. Schema
   structure is, on this stack, just another prompt instruction — and a
   more confusing one than prose.

2. **The union's schema complexity broke the streaming parser** (found
   while debugging a run that rejected its own python_run six times for
   a missing `code` argument it had in fact written): llama-index's
   streaming partial-parser returned the final `.raw` with the free-form
   args dict emptied (`{}`) while the provider's true text carried the
   arguments, and a structured stream's `message.content` is a dump of
   the partially parsed object — so the corruption also masqueraded as
   the model's output in the trace. The trigger was the union itself
   (RootModel anyOf with extra-forbidden branches): the flat classic
   schema had streamed through the same parser for weeks without ever
   emptying args. The **fix is kept on main anyway**
   (`ModelGroupAgent._settle_structured_result`: re-validate the
   provider's true text after the stream; it wins whenever it parses)
   because the same parser was separately caught rewriting flat-schema
   output too — it normalizes args key order, which is what silently
   defeated the audit-order check earlier — and because the true-text
   preference keeps the trace honest about what the model wrote. The
   guard is fail-open and behavior-identical when the parser is healthy.

3. **The union confuses smaller models.** With two shapes in play, a 4B
   model produced illegal hybrids, burned bounce caps, and finished runs
   the summariser flagged Unresolved (run `4c024056`). Every extra
   constraint is one more thing a small model must keep in working
   memory across a 4-5k-token prompt; the union added constraints while
   — see finding 1 — delivering no enforcement in return.

## Why it was withdrawn (the operator's judgment)

- **Partial type safety is inconsistent.** Typing the reply while the
  other ~19 actions keep open dicts creates two systems in one contract.
  Type safety is attractive, but reply-only typing is a special case the
  model (and the reader of the code) must remember.
- **The constraint burden lands on the model.** For small local models,
  one uniform, simple decision shape beats a cleverer format that needs
  branch selection. The uniform shape plus post-hoc validation (which
  rejects with a corrective message the model can act on) is the
  mechanism that demonstrably works on this stack.
- If typed decisions ever return, they should be **all actions or
  none**, and only on a stack where the schema is genuinely enforced
  (function-calling / grammar-backed structured output), verified first
  with a live probe — the branch's `tools/typed_reply_probe.py` shows
  the technique: stream a reply-shaped and a tool-shaped prompt, assert
  branch *and* args content.

## What was kept on main

- `_settle_structured_result` + preferring the instrumentation capture
  for the response snapshot (finding 2) — with unit tests in
  `agents/test_structured_result.py`.
- The self-audit gate's **literal verdict** rule: the audit passes only
  as exactly `OK` (any case, nothing else); a narration of the checks
  ending in "OK" is a rejection. The trial showed the model narrating
  its audits and exiting every reply through the bounce cap.
- The `1_message`/`2_audit` numbered-prefix contract stays: with the
  schema advisory on this stack, the prefixes and the raw-text order
  check are the working defense for message-before-audit.
