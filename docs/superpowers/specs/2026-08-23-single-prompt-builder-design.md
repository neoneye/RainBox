# One prompt builder for every assistant call

## Problem

The assistant's calls share a system prompt and a tier order, but each of the
seven user-prompt builders assembles those tiers by hand. The history section
is the piece nobody shares: every builder slices it from the tail of the
message list with its own constant.

| Builder | Constant | Value |
|---|---|---|
| `_build_user_prompt` (decide) | `MAX_RECENT_MESSAGES` | 30 |
| `_recall_filter_prefix` | `MAX_RECENT_MESSAGES` | 30 |
| `_build_acceptance_criteria_prompt` | `ACCEPTANCE_CRITERIA_MAX_MESSAGES` | 6 |
| `_build_reply_audit_prompt` | `REPLY_AUDIT_MAX_MESSAGES` | 6 |
| `_build_response_language_classifier_prompt` | `RESPONSE_LANGUAGE_CLASSIFIER_MAX_MESSAGES` | 6 |

`messages[:-1][-6:]` starts at message −6; `messages[:-1][-30:]` starts at −30.
History renders oldest-first, so a six-message window is a *suffix* of a
thirty-message one, never a prefix. A KV cache needs a prefix. The two windows
diverge on their first `<message>` element, and since `conversation_history_xml`
sits at tier 0 — second section of the prompt — everything after that point is
re-prefilled on every call.

The prior spec, `2026-08-13-assistant-shared-prompt-design.md`, measured
cross-call sharing at 51 bytes and attributed it to non-nested tier-1 block
sets. That half was fixed: `_ALL_STATIC_BLOCKS` now orders the blocks so the
per-call subsets nest. The history window was not a factor then, because
history sat in tier 3. Moving it to tier 0 made the window mismatch the
binding constraint.

### Evidence

Two consecutive calls on one turn, from the `/activity` dashboard. Both send
the same 1,916-character system prompt.

| | acceptance_criteria | decide |
|---|---|---|
| user prompt | 9,227 chars | 45,629 chars |
| first history message | six back | thirty back |
| prompt tokens | 2,727 | 11,112 |
| Reusable | 1,222 | **467** |

`reusable_prefix_tokens` hashes in 1000-char blocks, so the shared region is
2 blocks: the system prompt plus `<current_user_request>…</current_user_request>`
and the opening history tag. Working it back:
`11112 × 2000 / 47560 = 467`. That is the reported figure exactly, which
confirms the mechanism rather than merely being consistent with it.

Across a full turn the pattern is structural, not incidental:

```
response_language_classifier  ~2.0k prompt   reusable 232
acceptance_criteria           ~2.3k          reusable ~740-1200
decide #1                    ~11-12k         reusable 467-471   <- cold, 16-18s
memory_filter                 ~8-11k         reusable 5.2-5.9k
decide #2                    ~13-14k         reusable 11-12k
reply_audit                   ~4k            reusable ~1.0-1.5k
```

The first `decide` of every turn prefills ~11k tokens cold and spends 16-18s
of the turn's wall clock doing it, because the two small calls that ran
immediately before it carried a different slice of the same conversation.

## Goal

Make tier 0 byte-identical across every assistant call, and put its assembly
in one place so a future call cannot opt out by accident.

Two things follow from that, and only the first is the refactor:

1. One `AssistantPromptBuilder` that emits tiers 0 and 1 on construction.
2. One history window. `MAX_RECENT_MESSAGES = 30` survives;
   `ACCEPTANCE_CRITERIA_MAX_MESSAGES`, `REPLY_AUDIT_MAX_MESSAGES` and
   `RESPONSE_LANGUAGE_CLASSIFIER_MAX_MESSAGES` are deleted.

## Tier numbering

The code's current numbering, which differs from the 2026-08-13 spec's (that
one counted the system prompt as tier 0):

- **tier 0** — `current_user_request`, its summary when the request was
  truncated, then `conversation_history_xml`
- **tier 1** — the static head, in `_ALL_STATIC_BLOCKS` order:
  identity, formatting, profile, persona, calibration
- **tier 2** — `turn_instructions`
- **tier 3** — the per-call dynamic tail, least volatile first, closing with
  `current_local_time` where the call carries one

## `AssistantPromptBuilder`

Lives in `agents/assistant.py` beside `_render_sections`. Modelled on the
`ReportGenerator` builder in PlanExe: a stateful object, typed `append_*`
methods pushing onto an ordered internal list, a terminal `render()`. No
fluent chaining — the reference does not chain, and chaining buys nothing
here.

One deviation from the reference: **tiers 0 and 1 are emitted by `__init__`,
not by an `append_` method.** Tier 0 being identical everywhere is the entire
point of the change, so it must be impossible to construct an assistant prompt
without it. An `append_shared_prefix()` is a thing a seventh call could forget
to call.

```python
class AssistantPromptBuilder:
    """Every assistant call's user prompt.

    Tier 0 (request + history) and tier 1 (static head) are emitted on
    construction; the caller appends its own tier-1 extras, its
    turn_instructions and its tier-3 tail, then renders. The history window
    lives here and nowhere else — that is what makes tier 0 byte-identical
    across the calls, which is what a prefix cache needs.
    """

    def __init__(
        self,
        agent: "AssistantAgent",
        container_tag: str,
        *,
        messages: list[dict[str, Any]],
        blocks: tuple[str, ...] = _ALL_STATIC_BLOCKS,
    ) -> None: ...

    def append_text(self, tag: str, text: str, **attrs: str) -> None: ...
    def append_element(self, tag: str, **attrs: str) -> ET.Element: ...
    def append_turn_instructions(self, instructions: str) -> None: ...
    def append_local_time(self) -> None: ...
    def render(self) -> str: ...
```

`append_element` returns the created element, for the three tails that hold
nested trees: `proposed_step` (second opinion), `current_turn_steps` (decide
and a criteria revision), and `turn_observations` (audit). A purely
declarative section list would have had to carry ElementTree fragments as
data to serve those, which is indirection without payoff.

`append_text` and `append_element` write through `ET.SubElement`, preserving
the escaping guarantee that is the existing security property: dynamic content
cannot close or forge a section tag.

`append_turn_instructions` keeps the `_RAW_RENDER_ATTR` opt-in unchanged.
It stays the one section rendered raw, and the builder is the only thing that
sets the marker — matching on the tag name would let any future
`append_text("turn_instructions", …)` silently lose escaping.

`container_tag` never reaches the model. `_render_sections` iterates
`for section in root` and serializes children only, so the root element is a
container, not output. It is kept and named because it documents which call a
tree belongs to when read in a debugger.

`__init__` holds a reference to the agent rather than copying the seven
`_*_block` attributes out of it; those are already built once per turn and
read-only by the time a prompt is assembled.

**The builder owns the order; the agent keeps owning its blocks.** The four
existing `_append_*` helpers stay on `AssistantAgent`, because each reads
agent state — `_append_static_head` reads five `_*_block` attributes,
`_append_current_user_request` reads `_long_request_summary_markdown` — and
moving them would mean one class reaching into another's privates. The
builder holds a reference to the agent and calls them in the fixed tier
order.

The builder class is defined after `AssistantAgent` in the module, so
`blocks` can default to `AssistantAgent._ALL_STATIC_BLOCKS` and nothing has to
move. Python resolves the name at call time, so methods on `AssistantAgent`
can construct it despite being defined earlier — the same forward reference
`_build_recall_filter_prompt` already uses for
`AssistantAgent._append_turn_instructions`.

### Call site shape

```python
def _build_reply_audit_prompt(self, message, *, messages, scratchpad) -> str:
    prompt = AssistantPromptBuilder(
        self, "reply_audit", messages=messages,
        blocks=("identity", "formatting"))
    prompt.append_turn_instructions(REPLY_AUDIT_TURN_INSTRUCTIONS)
    if self._criteria_markdown:
        prompt.append_text("acceptance_criteria_markdown", self._criteria_markdown)
    ...
    prompt.append_text("proposed_reply", message)
    prompt.append_local_time()
    return prompt.render()
```

## Per-builder mapping

| Call | Static blocks | Change |
|---|---|---|
| decide | all five | none |
| recall_filter prefix | identity | none |
| reply_audit | identity, formatting | history 6 → 30 |
| acceptance_criteria | identity + bespoke `formatting_guide` | history 6 → 30 |
| response_language_classifier | identity + bespoke `user_settings_languages_json` | history 6 → 30; gains `user_settings_json` |
| second_opinion | identity, formatting, profile | gains history |
| request_summary | — | unchanged, stays outside the builder |

Three of those are judgment, not mechanics.

**The classifier gains `user_settings_json`.** It emits no standard static
head today; its docstring records that `user_settings_languages_json` is its
own tier-1 block, standing in that role. But the classifier runs *first* in
the turn, which makes it the call that warms the prefix — and it can only warm
what it carries. Giving it `identity` makes the chain nest, so tier 0 plus
identity becomes the shared run for all six calls. The cost is the settings
JSON in a narrow classifier; it is one line to revert if classification
degrades.

**second_opinion gains history.** Its docstring's reason — "no conversation
history (the current request is the contract the program is judged against)" —
is a statement about what is *authoritative*, which `turn_instructions`
already makes, not about what the reviewer may see. Carrying no history means
diverging from the shared prefix immediately after `current_user_request`, so
opting out costs the call its whole prefix.

**request_summary stays out.** It keeps its own small builder. It leads with
`turn_instructions` rather than the request, reads the request at its own
much larger `REQUEST_SUMMARY_INPUT_MAX_CHARS` budget, and exists to describe
an oversized paste before the turn proper begins. It has no turn context to
share, and rendering a thirty-message history into the largest prompt of the
turn for a cache alignment it runs too early to use would be a cost with no
return.

### Nesting after the change

```
tier 0 (request + history)            all six
  + identity                          all six
    + bespoke formatting_guide        criteria
    + user_settings_languages_json    classifier
    + formatting                      audit, second_opinion, decide
      + profile                       second_opinion, decide
        + persona + calibration       decide
```

criteria and the classifier diverge one block after identity, because their
bespoke tier-1 blocks differ from each other and from `formatting`. That is
inherent — two calls cannot both append a different section at the same
position — and it costs little, because tier 0 is where the tokens are.

## What this does not fix

The `/activity` data holds a second, independent failure: rows where Reusable
is high and Cached is 0, meaning the prefix was available and the runtime did
not serve it.

```
00:21:47  decide         reusable 11.3k  cached 0  prefill 22.0s
00:21:15  memory_filter  reusable  5.2k  cached 0  prefill 12.1s
00:00:00  decide         reusable 11.4k  cached 8.1k  prefill  6.3s
```

Same prompt shape, cache hit on some turns and not others. `memory_filter`
resolves its own model slot (`assistant.memory_filter`), so it may not even
share an Ollama slot with `decide`. This is a runtime/slot question, not a
prompt-assembly one, and it is out of scope here. This change should make it
easier to see, by removing the prompt-side noise from the same columns.

## Trust boundary

Unchanged, and the builder is what enforces it rather than convention:

1. `turn_instructions` is assembled from module constants only. Nothing
   derived from user data, the profile, or a model response is interpolated
   into it. `append_turn_instructions` is the only method that sets
   `_RAW_RENDER_ATTR`.
2. Every other section goes through `ET.SubElement` inside the builder, so
   ElementTree escaping still applies to all of them.
3. The shared system prompt still names `turn_instructions` as the sole
   instruction-bearing section.
4. `formatting_guide` keeps `authority="instructions"`; its imperatives are
   code-owned and its interpolated values pass the prompt-boundary validation
   in `user_profile.formatting`.

Widening the history window does not widen this boundary. The extra messages
are `conversation_history_xml` content, which the system prompt already
declares data, and each one is still capped at `HISTORY_MESSAGE_MAX_CHARS`.

## Verification

**The contract test.** `test_assistant_prompt_tiers.py` already has
`common_prefix_len` and per-call expected section orders. Add one test that
states the property directly: for a single turn state, every pair of the six
assistant prompts shares a prefix through the end of `<user_settings_json>`.
Written as a loop over all six builders, so a seventh call added later cannot
quietly opt out. **This test fails today** — that is where the work starts.

**Order tests.** Update `CRITERIA_EXPECTED`, `CLASSIFIER_EXPECTED`,
`SECOND_OPINION_EXPECTED` and `SECOND_OPINION_ALWAYS` for the sections those
calls gain. `DECIDE_EXPECTED`, `AUDIT_EXPECTED`, `RECALL_FILTER_EXPECTED` and
`SUMMARY_EXPECTED` are unchanged.

**Window assertions.** `test_assistant_acceptance_criteria.py`,
`test_response_language_classifier.py`, `test_assistant_actions.py` and
`test_assistant_long_request.py` assert on section presence and content;
anything asserting on the six-message window changes to thirty.

**Live measurement, as its own commit after the refactor.** The prediction is
falsifiable on `/activity`: the first `decide` of a turn moves from Reusable
467 to approximately its whole prompt, and its prefill drops out of the
16-18s band. If that number does not move, the change did not work.

## Out of scope

- Rewriting any job description. `turn_instructions` bodies move verbatim.
- `request_summary`'s builder.
- The runtime/slot cache-eviction question described above.
- Reordering the turn so the widest call runs first. Land the builder and
  measure before deciding whether that is needed.
- Any change to `is_function_calling_model`. Tool definitions render into the
  prompt head by the chat template, which would put per-call-varying content
  at token 0 and destroy every tier below it.

## Assumptions

1. The backends reuse KV cache for a shared prompt prefix, positionally, and
   do not care about message boundaries. Verified on Ollama by
   `tools/measure_prefix_cache.py` and recorded in the 2026-08-13 spec: a
   novel ~2000-token prefix costs 2271 ms, the same prefix with a different
   tail costs 55.6 ms.
2. Widening the narrow calls' history window does not degrade their output.
   **Unverified, and the real risk in this change.** The criteria call's
   instructions spend two paragraphs warning it not to nominate the
   transcript as a source, and it will now see five times more transcript.
   The measurement above shows whether the cache win landed; it does not show
   whether criteria quality held. Watch the criteria text on the first turns
   after the change.
