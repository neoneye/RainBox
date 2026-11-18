# The request as its own turn

**Status:** Built behind the `prompt.layout` setting (/config):
`single_turn` is the prior behaviour and the default, `request_turn` is this
design. The evaluation below has not been run yet.
**Date:** 2026-10-19
**Related:** `2026-08-31-turn-latency-and-prompt-redundancy.md` (prefix
sharing and the nested block sets; read before moving any section),
`2026-07-24-operator-locale-and-language.md` (read before anything that can
pull the reply language), `2026-10-17-conversation-working-set.md` (places
new blocks into the same prompt).

## The problem

Every model call sends two messages: a system prompt and one user message.
The user message holds everything — history, settings, profile, memories,
this call's instructions, the evidence gathered so far — and somewhere in it
is what the operator actually typed. The model has to find the request
among sections that all look the same and carry the same role.

### The assistant

`AssistantPromptBuilder` renders one user message of XML sections:

1. `current_user_request` (tier 0)
2. `conversation_history_xml`, the last `MAX_RECENT_MESSAGES` (30)
3. the static head: `user_settings_yaml`, `user_expertise_yaml`,
   `user_profile`, `assistant_persona`
4. `turn_instructions`
5. the dynamic tail: `active_skills`, `reply_language_markdown`,
   `acceptance_criteria_markdown`, `current_turn_steps`
6. `decision_request`, which quotes the request again
7. `current_local_time`

The request leads for cache reasons. It is byte-identical across every call
of a turn, and the calls' heads differ in length. The re-quote at the tail
is a patch, and `_request_anchor`'s docstring records why it was needed. A
step read the request at the top, then thirty messages of history, then a
closing instruction that only named the section. It answered the newest
history message instead, a near-identical question from the previous turn,
and every later step inherited the substitution.

The patch has limits. Past `REQUEST_ANCHOR_MAX_CHARS` (400) the anchor only
points ("too long to repeat here, and it opens: …"). So a long request —
exactly the kind where getting it right matters most — ends the prompt with
a pointer to text about 13k tokens back.

### The chat agents

`format_history` renders an IRC transcript with the latest message split out
under `Current message:` at the bottom. That position is right, but the line
is mangled. `_format_irc_line` collapses the message onto one line (real line
breaks become literal `\n`) and gives it the same `[ts] <sender>` prefix
every history line has. A pasted multi-line message reaches the model as one
escaped line, formatted exactly like the thirty lines above it.

The context block (profile, curated facts, recalled memories) sits above the
transcript. Recalled memories are retrieved per message, so the prompt
changes right after the system prompt on every turn, and the previous turn's
cached prefix is worth nothing.

## The design

Send the request as its own user turn, after a fixed assistant
acknowledgement:

```
system     fixed instructions, unchanged
user       the context: everything the call carries today, minus the request
assistant  a fixed acknowledgement
user       the request, verbatim
```

The model's chat template already marks "the last user turn" as the
instruction. That is the strongest positional signal these models were
trained on, and today it goes to a 13k-token blob. In this design it goes
to the operator's own words. The fix stops depending on wording ("never a
message from conversation_history_xml, however recent") and comes from
structure.

The context stays in a user turn. The other obvious option was moving the
context into the system prompt and sending the request as the only user
message. That gives recalled memories, other agents' replies and old
assistant answers system-level authority, which is the wrong direction:
`turn_instructions` exists to say that old answers are never authoritative.

Five decisions shape it.

### 1. The acknowledgement commits; it does not ask

The assistant turn is written in the model's own voice, and models stay
consistent with what they have already said. So the acknowledgement states
what the model will do with the material, not a request for more:

> Context received. I'll treat it as reference material and act on your
> next message.

It is one module constant shared by every call that uses this shape. It
says "message", not "prompt": the person on the other end writes a message,
and "prompt" frames the turn as a task to execute. Each call's own job stays
in `turn_instructions`, so one acknowledgement fits the decide, criteria and
recall-filter calls alike. "Act on" covers replying, choosing an action and
establishing criteria.

The acknowledgement is English and sits directly before the request. That
risk is measured, not assumed away (see Evaluation). If it pulls replies
toward English, the first fallback is "Context received." alone, and the
second adds "in the language of your message".

The decide and criteria calls answer in structured output, and a prose
assistant turn just before a JSON answer could weaken schema adherence. The
schema is not a guarantee on its own: `_structured_completion`'s rejection
retries exist because models do return `{"reason": null, "action": null}`.
So the evaluation counts rejected responses per call.

### 2. The request turn carries the operator's words and nothing else

No tag, no attributes, no timestamp, no sender prefix, line breaks intact.
Anything the code knows *about* the request goes in the context turn:

- a cut request (over `CURRENT_REQUEST_MAX_CHARS`) still arrives middle-cut
  in the request turn. The code-owned facts about the cut (`original_chars`,
  `included_chars`) move to a short `current_user_request_cut` section at
  the end of the context turn.
- `current_user_request_summary_markdown` (the long-request summary) stays
  in the context turn. It is model-derived material about the request, not
  the request.

`decision_request` loses its quote. Its anchor sentence becomes "The request
to answer is your next message, never a message from
conversation_history_xml." It keeps the step count and "Choose exactly one
next action…". `_request_anchor` and `REQUEST_ANCHOR_MAX_CHARS` go away, and
so does the long-request pointer, because there is nothing left to point
across.

### 3. Every call drops the request from tier 0 together

Calls that share a turn's prefix must keep their heads nested (see
`_ALL_STATIC_BLOCKS`). If decide stopped leading with the request while
`reply_audit` still did, they would diverge at token one, and the measured
audit win (9.5k prompt, 2.9s prefill behind decide) would be gone. So the
request leaves tier 0 in every `AssistantPromptBuilder` call at once, and
tier 0 becomes the history alone. History is byte-identical across a turn's
calls, which is the property tier 0 needs.

Where the request goes then depends on what the call is about:

| call | subject | request goes |
|---|---|---|
| decide | the request | its own final turn |
| acceptance_criteria | the request | its own final turn |
| reply_audit | the draft reply | `current_user_request` section at the end of the single user turn, before the draft |
| second_opinion | the proposed step | same, before the proposed step |
| recall filter (`_recall_filter_prefix`) | the recalled candidates, scored against the query | `current_user_request` section at the end of its prefix |
| response-language classifier, request summary, run summarizer | their own inputs | unchanged apart from the shared head |

The second group judges something other than the request, so the request is
material there and stays a section. The recall filter belongs there: it scores
candidates against the memory query, not against the request. The same principle would later give
*their* subject (the draft, the proposed step) a turn of its own. That is a
separate change, measured separately.

### 4. The chat agents get the same shape and a stable-first context

For `UnstructuredChatAgent` and the structured chat agent:

```
user       profile block, curated facts            (stable across turns)
           transcript without the current message  (grows by append)
           recalled memories                       (per message)
           "The next message is from <sender>, <ts>."
assistant  the acknowledgement
user       the current message, verbatim
```

`format_history` gains a sibling that returns the history and the current
message separately. The existing function stays for the tool and MCP agents
until they adopt the shape. The sender and timestamp move to one code-owned
line at the end of the context turn, so the request turn stays verbatim. The
system prompt's description ("An optional Relevant remembered facts block …
A clearly marked Current message at the bottom") is rewritten to describe
the turns.

Reordering the context is the cache half of this change. These agents make
one model call per turn, so the runtime still holds the previous turn's
prompt when the next one arrives. With stable blocks first, the next turn
diverges at the end of the old transcript instead of right after the system
prompt. That holds until the room passes the 20-message window. After that,
the window drops its oldest line every turn and the divergence moves back to
the first transcript line. Dropping history in chunks rather than one line
at a time would fix that; it is out of scope here.

### 5. The trace stores the turns that were sent

`assistant_step.user_prompt` keeps the first user turn, the context, which
is the bulk. (`second_opinion_review` needs nothing: that call stays
single-turn.) A new nullable JSON column,
`followup_turns`, holds what came after it as `[{role, content}, …]`. That
is the acknowledgement and the request, in the same `{role, content}` shape
the rejected-attempt `feedback` already uses. The acknowledgement is stored
even though it is a constant: a record stands as written, and a later change
to the constant must not rewrite what an old row says was sent. Rows from
before the change keep `followup_turns` NULL and render as they do today.

The /assistant log renders the turns in order under the existing prompt
view, with each turn labelled by role. `build_turn_prompts` (the eval seam)
returns the system prompt and the turn list instead of a `(system, user)`
pair. `_structured_completion` takes the turns after the system prompt
instead of one `user_prompt`, and appends rejection corrections after them
as it does now.

### 6. One setting, read once per turn

`prompt.layout` (`db/settings.py`) holds the layout; its choices are
`PROMPT_LAYOUTS` in `chat/prompt_layout.py`, so a future layout reaches the
/config dropdown by being added there. The assistant reads it once at the
start of a turn, beside the response-language gate, and records it in the
turn log every step row carries — a refresh of the acceptance criteria
mid-turn re-captures the profile, never the layout. The chat agents read it
once per handled message. An unreadable value reads as `single_turn`.

The shared assistant system prompt is the same in both layouts. Every call of
a turn sends one byte-identical system prompt, and the calls that still carry
the request section are described by it exactly as before; the decide and
criteria calls learn where the request went from `REQUEST_MESSAGE_SECTION` at
the end of their `turn_instructions`. The decide instructions' references to
`current_user_request` are rewritten for the layout, each fragment required to
occur once so a reworded constant fails loudly. The chat agents' system
prompts swap their message-shape paragraph instead (`chat_system_prompt`):
they make one call per turn, so nothing is shared across calls.

The eval harness (`evals/profile_guidance.py`) builds and sends the turns in
the layout the setting names, and its prompt hash covers them, so results
from the two layouts never share a hash.

## Cost

The request now comes after each call's own tail, so it is prefilled once
per call instead of once per turn. A short request costs nothing worth
measuring. At `CURRENT_REQUEST_MAX_CHARS` (8000 chars, about 2k tokens)
across a five-call turn, that is about 8k extra prefill tokens, roughly 12s
at the measured 650 tok/s cold rate. The long-request path pays for being
right. If that turns out too steep, the trade is at that threshold, not in
the design.

For the assistant, reuse across turns stays unlikely in practice. The last
call of a turn (the run summarizer) does not share the decide head, so the
runtime's single slot holds a prompt that does not match the next turn. This
change makes cross-turn reuse possible for the assistant without delivering
it. The chat agents get it for real (decision 4).

## Evaluation

Run the eval set twice on the same model, before and after. Report:

- **Wrong request answered.** The `_request_anchor` failure: a follow-up
  that closely resembles an earlier message. Add fixture cases built for it,
  short and long (over 400 chars, where today's anchor only points).
- **Reply language.** Non-English requests with an English-heavy context,
  scored the way the language classifier's cases are. This is the
  acknowledgement's one known risk.
- **Rejected responses** per decide and criteria call.
- **Prefill.** Per call from /activity (reusable and cached), split by
  request length. The long-request rows are where the cost above shows up.

Ship if the first three do not regress. Report the prefill numbers either
way.

## Out of scope

- A turn of its own for the draft under audit and the step under second
  opinion (decision 3).
- A chunked history window for the chat agents (decision 4).
- Replaying history as real user/assistant turns. `agents/direct_chat.py`
  does that for one-to-one chat. Group rooms with several agents have no
  faithful mapping onto two roles, which is why the transcript exists.
