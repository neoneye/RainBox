# Single Assistant Prompt Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put tier 0 of every assistant prompt (`current_user_request` + `conversation_history_xml`) behind one builder with one history window, so the six calls of a turn share a cacheable prefix instead of diverging on their first history message.

**Architecture:** A new `AssistantPromptBuilder` class in `agents/assistant.py`, defined after `AssistantAgent`, emits tier 0 and tier 1 in its `__init__` and exposes `append_*` methods for each call's own sections. It holds a reference to the agent and delegates to the agent's existing `_append_*` helpers, which read agent state — the builder owns the *order*, the agent keeps owning its *blocks*. Six of the seven prompt builders are rewritten to go through it; `_build_request_summary_prompt` is deliberately left alone.

**Tech Stack:** Python 3, `xml.etree.ElementTree`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-23-single-prompt-builder-design.md`

## Global Constraints

- All commands run from `/Users/neoneye/git/rainbox/source`.
- Test runner is `./venv/bin/python -m pytest`. Never a bare `pytest`.
- Never run ad-hoc scripts or `psql` against `rainbox_production`. The pytest path is already forced onto `rainbox_claude` by `rainbox/conftest.py`; nothing extra is needed for tests.
- Every section must be created through `ET.SubElement`. ElementTree escaping is the security property that stops untrusted content forging a section tag. The single exception is `turn_instructions`, which opts into raw rendering via `_RAW_RENDER_ATTR` and must only ever be set by `_append_turn_instructions`.
- `_render_sections(root)` serializes `root`'s **children** as top-level siblings. The root element's own tag never reaches the model.
- Section text is never reworded in this plan. Sections move; their contents do not change.
- Comments and docstrings describe how the code works now. No "renamed from", "previously", or migration notes — git holds the history.
- Commit after every task. Never amend; each revision is its own commit.

---

### Task 1: The failing contract test

The property the whole change exists to create, written first so it can be seen failing. Six assistant prompts built from one turn state must share a byte-identical prefix through the end of `<user_settings_json>`.

**Files:**
- Modify: `agents/test_assistant_prompt_tiers.py`

**Interfaces:**
- Consumes: `fully_populated_agent` fixture and `common_prefix_len(a, b)`, both already in this file.
- Produces: `all_turn_prompts(agent, messages) -> dict[str, str]`, mapping a call name to its rendered user prompt, and `TURN_MESSAGES: list[dict]`, a 21-message turn. Tasks 3 and 4 reuse `TURN_MESSAGES`.

- [ ] **Step 1: Write the failing test**

Append to `agents/test_assistant_prompt_tiers.py`:

```python
def all_turn_prompts(agent, messages: list[dict]) -> dict[str, str]:
    """Every assistant user prompt for one turn state, by call name.

    request_summary is absent by design: it runs before the turn proper, at
    its own much larger request budget, and carries no turn context to share.
    """
    from agents.assistant import _build_recall_filter_prompt

    decision = AssistantStepDecision(
        reason="run the sum",
        action=AssistantActionName.PYTHON_RUN,
        args={"code": "print(2 + 2)"},
    )
    return {
        "decide": agent._build_user_prompt(
            messages=messages, scratchpad=[], step_index=0),
        "acceptance_criteria": agent._build_acceptance_criteria_prompt(messages),
        "reply_audit": agent._build_reply_audit_prompt(
            "here is the answer", messages=messages, scratchpad=[]),
        "second_opinion": agent._build_second_opinion_prompt(
            decision, reasoning="because", messages=messages),
        "response_language_classifier":
            agent._build_response_language_classifier_prompt(messages, None),
        "recall_filter": _build_recall_filter_prompt(
            "what is 2+2", [{"id": "qa-1"}],
            prompt_prefix=agent._recall_filter_prefix(messages)),
    }


# Long enough that a six-message window and a thirty-message one cannot
# coincide. Distinct text per message so a wrong slice is visible in the diff
# rather than hiding behind repeated filler.
TURN_MESSAGES = [
    {"sender_type": "human" if i % 2 == 0 else "agent",
     "text": f"turn message number {i}"}
    for i in range(20)
] + [{"sender_type": "human", "text": "what is 2+2"}]


def test_every_assistant_call_shares_the_turn_prefix(fully_populated_agent):
    """The property the single prompt builder exists to create.

    A KV cache reuses a prefix, counting from token 0. conversation_history_xml
    sits at tier 0, so any two calls slicing history differently diverge on
    their first <message> and share nothing past it. Every call therefore
    renders the same window, and every call carries `identity` — the one
    tier-1 block they all have — so the shared run reaches the end of
    user_settings_json.

    Written as a loop over all six calls rather than as pairs, so a seventh
    call added later cannot quietly opt out.
    """
    prompts = all_turn_prompts(fully_populated_agent, TURN_MESSAGES)
    required = "<user_settings_json>identity</user_settings_json>"

    for name, prompt in prompts.items():
        assert required in prompt, f"{name} carries no identity block"

    for name, prompt in prompts.items():
        for other_name, other in prompts.items():
            if name >= other_name:
                continue
            shared = common_prefix_len(prompt, other)
            # A slice shorter than the target cannot contain it, so this pins
            # a lower bound on `shared` rather than merely asserting overlap.
            assert required in prompt[:shared], (
                f"{name} x {other_name} share only {shared} chars, "
                f"which does not reach the end of user_settings_json"
            )
```

Add `AssistantActionName` and `AssistantStepDecision` to the existing
`from agents.assistant import (...)` block at the top of the file, keeping
the list alphabetically ordered.

- [ ] **Step 2: Run the test to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py::test_every_assistant_call_shares_the_turn_prefix -q`

Expected: FAIL. It fails first on the `identity` loop, at `response_language_classifier carries no identity block`. Comment that loop out temporarily and the pair loop reports shared prefixes of 58-530 characters against prompts of 3-20k.

Measured on this fixture before any change, so you can recognise a correct failure:

```
decide               x acceptance_criteria    shared= 127
decide               x reply_audit            shared= 127
decide               x second_opinion         shared=  58
decide               x classifier             shared= 127
decide               x recall_filter          shared=1323   <- already aligned
acceptance_criteria  x reply_audit            shared= 530
```

`decide x recall_filter` is the one pair that already reaches `identity`: those two are the only calls sharing a history window today. It is the shape every other pair should end up with.

- [ ] **Step 3: Commit the failing test**

```bash
git add agents/test_assistant_prompt_tiers.py
git commit -m "test(assistant): pin the cross-call shared prefix all six calls need

Fails today: the calls slice conversation history with different windows,
so a six-message prompt is a suffix of a thirty-message one where a prefix
cache needs a prefix."
```

---

### Task 2: The builder class

Add the class. Nothing calls it yet, so this task ends green with the Task 1 test still red.

**Files:**
- Modify: `agents/assistant.py` (append after the `AssistantAgent` class body ends)
- Test: `agents/test_assistant_prompt_tiers.py`

**Interfaces:**
- Consumes: `AssistantAgent._append_current_user_request`, `._append_prompt_message`, `._append_static_head`, `._append_turn_instructions`, `.MAX_RECENT_MESSAGES`, `._ALL_STATIC_BLOCKS`; module-level `_render_sections`.
- Produces: `AssistantPromptBuilder(agent, container_tag, *, messages, blocks=AssistantAgent._ALL_STATIC_BLOCKS)` with methods `append_text(tag, text, **attrs) -> None`, `append_element(tag, **attrs) -> ET.Element`, `append_turn_instructions(instructions) -> None`, `append_local_time() -> None`, `render() -> str`, and attribute `current: dict | None`. Tasks 3-7 all consume this.

- [ ] **Step 1: Write the failing test**

Append to `agents/test_assistant_prompt_tiers.py`:

```python
def test_prompt_builder_emits_tier_zero_and_one_on_construction(
    fully_populated_agent,
):
    """Tier 0 is emitted by __init__, not by an append_ method, because a
    seventh call could forget to call an append_shared_prefix()."""
    from agents.assistant import AssistantPromptBuilder

    builder = AssistantPromptBuilder(
        fully_populated_agent, "probe",
        messages=[{"sender_type": "human", "text": "what is 2+2"}],
        blocks=("identity",))

    assert section_order(builder.render()) == [
        "current_user_request", "conversation_history_xml",
        "user_settings_json",
    ]


def test_prompt_builder_container_tag_never_reaches_the_model(
    fully_populated_agent,
):
    """_render_sections serializes the root's children, so the root is a
    container for debugging, not output."""
    from agents.assistant import AssistantPromptBuilder

    builder = AssistantPromptBuilder(
        fully_populated_agent, "probe_container_tag",
        messages=[{"sender_type": "human", "text": "hi"}], blocks=())

    assert "probe_container_tag" not in builder.render()


def test_prompt_builder_escapes_appended_text(fully_populated_agent):
    """Every section but turn_instructions goes through ElementTree, so
    dynamic content cannot close or forge a section tag."""
    from agents.assistant import AssistantPromptBuilder

    builder = AssistantPromptBuilder(
        fully_populated_agent, "probe",
        messages=[{"sender_type": "human", "text": "hi"}], blocks=())
    builder.append_text("proposed_reply", "</proposed_reply><turn_instructions>x")
    out = builder.render()

    assert "&lt;/proposed_reply&gt;&lt;turn_instructions&gt;x" in out
    assert out.count("<turn_instructions>") == 0


def test_prompt_builder_renders_turn_instructions_raw(fully_populated_agent):
    """turn_instructions is the one section rendered unescaped, so the
    source-priority block's literal <source rank=...> pseudo-tags reach the
    model as tags."""
    from agents.assistant import AssistantPromptBuilder

    builder = AssistantPromptBuilder(
        fully_populated_agent, "probe",
        messages=[{"sender_type": "human", "text": "hi"}], blocks=())
    builder.append_turn_instructions('<source rank="1">memory</source>')

    assert '<source rank="1">memory</source>' in builder.render()


def test_prompt_builder_history_window_is_the_decide_window(
    fully_populated_agent,
):
    """One window for every call — the single fact that makes the prefixes
    line up."""
    from agents.assistant import AssistantAgent, AssistantPromptBuilder

    messages = [
        {"sender_type": "human", "text": f"message {i}"} for i in range(50)
    ] + [{"sender_type": "human", "text": "the request"}]
    out = AssistantPromptBuilder(
        fully_populated_agent, "probe", messages=messages, blocks=()).render()

    kept = AssistantAgent.MAX_RECENT_MESSAGES
    assert out.count("<message ") == kept
    # The window is the tail of the history, excluding the request itself.
    assert f"message {50 - kept}" in out
    assert f"message {50 - kept - 1}" not in out
    assert "the request" in out.split("<conversation_history_xml>")[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py -q -k prompt_builder`

Expected: FAIL, five errors, each `ImportError: cannot import name 'AssistantPromptBuilder' from 'agents.assistant'`.

- [ ] **Step 3: Add the class**

`AssistantAgent` runs to the end of `agents/assistant.py` — there is no module-level code after it — so this goes at the **end of the file**, at column 0, after `db.clear_assistant_call_checkpoint(self._run)`.

Defining it after `AssistantAgent` is what lets `blocks` default to `AssistantAgent._ALL_STATIC_BLOCKS` without moving anything. Methods on `AssistantAgent` can still construct it, because Python resolves the name at call time — the same forward reference `_build_recall_filter_prompt` already uses for `AssistantAgent._append_turn_instructions`.

```python
class AssistantPromptBuilder:
    """Every assistant call's user prompt, assembled in one place.

    Tier 0 (the request and the conversation history) and tier 1 (the static
    head) are emitted on construction; the caller appends its own tier-1
    extras, its turn_instructions and its tier-3 tail, then renders.

    Tier 0 being emitted by __init__ rather than by an append_ method is the
    point of the class. A prefix cache reuses a matched run counting from
    token 0, and conversation_history_xml sits second in the prompt, so two
    calls slicing history differently share nothing past their first
    <message>. The window lives here and nowhere else, which is what makes
    tier 0 byte-identical across the calls.

    The builder owns the ORDER; the agent keeps owning its BLOCKS. The
    _append_* helpers stay on AssistantAgent because each reads agent state —
    the five _*_block attributes, the long-request summary — and this class
    calls them rather than reaching into those attributes itself.
    """

    def __init__(
        self,
        agent: "AssistantAgent",
        container_tag: str,
        *,
        messages: list[dict[str, Any]],
        blocks: tuple[str, ...] = AssistantAgent._ALL_STATIC_BLOCKS,
    ) -> None:
        """`container_tag` names the tree for a reader and never reaches the
        model: _render_sections serializes the root's children as top-level
        siblings, so the root itself is a container, not output.

        `blocks` selects this call's tier-1 head. Keep the per-call sets
        nested (see _ALL_STATIC_BLOCKS): a call taking a block another call
        skips ends the shared prefix there.
        """
        self._agent = agent
        self._root = ET.Element(container_tag)
        # The turn's request, kept as an attribute because several callers
        # need it again for their closing re-anchor.
        self.current: dict[str, Any] | None = messages[-1] if messages else None

        agent._append_current_user_request(self._root, self.current)

        context = messages[:-1][-agent.MAX_RECENT_MESSAGES:] if messages else []
        history = ET.SubElement(self._root, "conversation_history_xml")
        if context:
            for message in context:
                agent._append_prompt_message(history, message)
        else:
            ET.SubElement(history, "none")

        agent._append_static_head(self._root, blocks=blocks)

    def append_text(self, tag: str, text: str, **attrs: str) -> None:
        """A leaf section. Goes through ET.SubElement, so `text` cannot close
        or forge a section tag however hostile it is."""
        ET.SubElement(self._root, tag, attrs).text = text

    def append_element(self, tag: str, **attrs: str) -> ET.Element:
        """An empty section, returned so the caller can nest children into it.
        For the three tails that carry trees rather than text:
        proposed_step, current_turn_steps and turn_observations."""
        return ET.SubElement(self._root, tag, attrs)

    def append_turn_instructions(self, instructions: str) -> None:
        """Tier 2: this call's job description, from module constants only."""
        self._agent._append_turn_instructions(self._root, instructions)

    def append_local_time(self) -> None:
        """The operator's clock, which the model needs because the only other
        time anchor is the conversation's UTC timestamps. Last in any prompt
        that carries it: it changes every minute, so anywhere else it would
        invalidate the cached prefix of every section after it."""
        now_local = datetime.now().astimezone()
        self.append_text(
            "current_local_time", now_local.strftime("%Y-%m-%d %H:%M %Z"))

    def render(self) -> str:
        return _render_sections(self._root)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py -q -k prompt_builder`

Expected: PASS, 5 passed.

- [ ] **Step 5: Confirm nothing else moved**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py -q`

Expected: the five new builder tests pass, `test_every_assistant_call_shares_the_turn_prefix` still FAILS (no call uses the builder yet), everything else passes.

- [ ] **Step 6: Commit**

```bash
git add agents/assistant.py agents/test_assistant_prompt_tiers.py
git commit -m "feat(assistant): add AssistantPromptBuilder owning tiers 0 and 1

__init__ emits the request, the history and the static head, so an assistant
prompt cannot be built without them and the history window has one home."
```

---

### Task 3: Move the decide prompt onto the builder

The reference call: it takes every static block and its section order is already the canonical one, so moving it first proves the builder reproduces existing output byte for byte.

**Files:**
- Modify: `agents/assistant.py` — `_build_user_prompt` (around line 4528) and `_recall_filter_prefix` (around line 4500)

**Interfaces:**
- Consumes: `AssistantPromptBuilder` from Task 2.
- Produces: no signature changes. `_build_user_prompt(*, messages, scratchpad, step_index) -> str` and `_recall_filter_prefix(messages) -> str` keep their signatures.

- [ ] **Step 1: Write the characterization test**

The existing `DECIDE_EXPECTED` order test already covers this call, and it must stay green through the move. Add one test that pins the byte-level equivalence explicitly, in `agents/test_assistant_prompt_tiers.py`:

```python
def test_recall_filter_prefix_is_a_prefix_of_the_decide_prompt(
    fully_populated_agent,
):
    """Not merely overlapping: the filter's whole rendered prefix is the
    opening of the decide prompt, so the nested call reuses the loop's own
    cached run instead of warming a second one."""
    agent = fully_populated_agent
    decide = agent._build_user_prompt(
        messages=TURN_MESSAGES, scratchpad=[], step_index=0)

    assert decide.startswith(agent._recall_filter_prefix(TURN_MESSAGES))
```

- [ ] **Step 2: Run it to see it pass before the change**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py::test_recall_filter_prefix_is_a_prefix_of_the_decide_prompt -q`

Expected: PASS. This one starts green on purpose — it is a characterization test, guarding a property the refactor must not break.

- [ ] **Step 3: Rewrite `_recall_filter_prefix`**

Replace its body (keep the docstring exactly as it is) so that everything from `root = ET.Element("recall_filter_call")` through `return _render_sections(root)` becomes:

```python
        return AssistantPromptBuilder(
            self, "recall_filter_call", messages=messages,
            blocks=("identity",)).render()
```

- [ ] **Step 4: Rewrite `_build_user_prompt`**

Replace everything from `root = ET.Element("assistant_turn")` down to and including the `self._append_static_head(root)` call with:

```python
        prompt = AssistantPromptBuilder(
            self, "assistant_turn", messages=messages)
        current = prompt.current
```

Keep the tier-0 and tier-1 explanatory comments by moving them onto
`AssistantPromptBuilder.__init__` — they are now facts about the builder, not
about this call. Then convert the rest of the method body:

```python
        # Tier 2: what this call is for.
        prompt.append_turn_instructions(self._decide_turn_instructions())

        # Tier 3: dynamic tail, least volatile first. active_skills is
        # retrieved per request, so it is dynamic despite reading as static.
        if self._skill_block:
            prompt.append_text(
                "active_skills", self._skill_block, authority="instructions")

        # The classifier's score-free Markdown follows the history into every
        # reasoning step. It is model-derived context, while turn_instructions
        # owns the instruction explaining how its ranked list is interpreted.
        if self._reply_language_markdown:
            prompt.append_text(
                "reply_language_markdown", self._reply_language_markdown)

        # Only the LATEST criteria render — a revision replaces this section,
        # never appends (the trace keeps the history). A bare suffixed tag:
        # the `_markdown` suffix states the format, so a `format` attribute
        # would only repeat it, and the content is model-generated, so its
        # authority lives in the code-owned sentence in turn_instructions
        # rather than in an attribute here.
        if self._criteria_markdown:
            prompt.append_text(
                "acceptance_criteria_markdown", self._criteria_markdown)

        # Ahead of the request because it grows append-only within a turn:
        # step N+1 shares its whole prefix through step N's entry.
        turn_steps = prompt.append_element(
            "current_turn_steps", authority="fresh_evidence")
        kept, omitted = self._bounded_turn_events(scratchpad)
        if omitted:
            ET.SubElement(turn_steps, "omitted", {"count": str(omitted)})
        if kept:
            for event in kept:
                self._append_turn_event(turn_steps, event)
        else:
            ET.SubElement(turn_steps, "none")

        prompt.append_text(
            "decision_request",
            f"{self._request_anchor(current)} "
            "Choose exactly one next action. If current_turn_steps already "
            "answer that request, choose reply now. Never repeat "
            "an identical successful or failed action.",
            step=str(step_index + 1), max_steps=str(self.step_limit))

        prompt.append_local_time()
        return prompt.render()
```

- [ ] **Step 5: Run the decide tests**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py -q`

Expected: `test_decide_prompt_follows_tier_order`, `test_recall_filter_prompt_follows_tier_order`, `test_recall_filter_shares_the_decide_prompts_opening_bytes`, `test_recall_filter_prefix_is_a_prefix_of_the_decide_prompt` and the consecutive-steps prefix test all PASS. `test_every_assistant_call_shares_the_turn_prefix` still FAILS.

- [ ] **Step 6: Run the wider assistant suite**

Run: `./venv/bin/python -m pytest agents/ -q -k assistant`

Expected: no new failures relative to the baseline. Record the baseline first if you have not: `git stash && ./venv/bin/python -m pytest agents/ -q -k assistant | tail -3 && git stash pop`.

- [ ] **Step 7: Commit**

```bash
git add agents/assistant.py agents/test_assistant_prompt_tiers.py
git commit -m "refactor(assistant): build the decide and recall-filter prompts with the builder"
```

---

### Task 4: Move the acceptance-criteria prompt onto the builder

First call whose history window actually changes: six messages become thirty.

**Files:**
- Modify: `agents/assistant.py` — delete `ACCEPTANCE_CRITERIA_MAX_MESSAGES` (around line 5689 with its two comment lines above it) and rewrite `_build_acceptance_criteria_prompt` (around line 5697)
- Test: `agents/test_assistant_prompt_tiers.py`

**Interfaces:**
- Consumes: `AssistantPromptBuilder` from Task 2.
- Produces: `_build_acceptance_criteria_prompt(messages, *, prior_criteria=None, scratchpad=None) -> str` — signature unchanged.

- [ ] **Step 1: Write the failing test**

Append to `agents/test_assistant_prompt_tiers.py`:

```python
def test_criteria_prompt_shares_the_decide_prompts_history(
    fully_populated_agent,
):
    """The criteria call is the one that runs immediately before the first
    decide call, so its history slice is what decides whether decide starts
    warm or cold."""
    agent = fully_populated_agent
    decide = agent._build_user_prompt(
        messages=TURN_MESSAGES, scratchpad=[], step_index=0)
    criteria = agent._build_acceptance_criteria_prompt(TURN_MESSAGES)

    def history(prompt: str) -> str:
        start = prompt.index("<conversation_history_xml>")
        return prompt[start:prompt.index("</conversation_history_xml>") + 27]

    assert history(criteria) == history(decide)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py::test_criteria_prompt_shares_the_decide_prompts_history -q`

Expected: FAIL on the final assertion — criteria's history holds 6 `<message>` elements against decide's 20.

- [ ] **Step 3: Delete the constant**

Remove these three lines from `agents/assistant.py` (around 5687-5689):

```python
    # How many prior conversation messages the criteria call sees: constraint
    # planning can need operator context but not the decide loop's full
    # MAX_RECENT_MESSAGES window.
    ACCEPTANCE_CRITERIA_MAX_MESSAGES: int = 6
```

- [ ] **Step 4: Rewrite the builder body**

In `_build_acceptance_criteria_prompt`, replace everything from
`root = ET.Element("acceptance_criteria_call")` through
`self._append_static_head(root, blocks=("identity",))` with:

```python
        prompt = AssistantPromptBuilder(
            self, "acceptance_criteria_call", messages=messages,
            blocks=("identity",))
        current = prompt.current
```

Keep the comment explaining why the formatting guide here is bespoke, and
convert the remainder:

```python
        guide = self._criteria_formatting_guide()
        if guide:
            prompt.append_text("formatting_guide", guide)
        prompt.append_turn_instructions(ACCEPTANCE_CRITERIA_TURN_INSTRUCTIONS)
        revising = prior_criteria is not None
        if revising:
            prompt.append_text(
                "prior_acceptance_criteria",
                self._format_criteria_markdown(prior_criteria),
                format="markdown")
            steps = prompt.append_element(
                "current_turn_steps", authority="fresh_evidence")
            kept, omitted = self._bounded_turn_events(scratchpad or [])
            if omitted:
                ET.SubElement(steps, "omitted", {"count": str(omitted)})
            if kept:
                for event in kept:
                    self._append_turn_event(steps, event)
            else:
                ET.SubElement(steps, "none")
        prompt.append_text(
            "criteria_request",
            "Revise the acceptance criteria: compare the prior criteria "
            "with the steps so far — what changed, and which criteria does "
            "it invalidate? Emit the full revised criteria; keep everything "
            "the change does not touch."
            if revising else
            f"{self._request_anchor(current)} Establish the acceptance "
            "criteria the reply to that request must satisfy.")
        return prompt.render()
```

Update the method's docstring: it currently says "a short operator history
tail". Change that phrase to "the turn's conversation history" — the tail is
no longer short, and the docstring must describe what the code does now.

- [ ] **Step 5: Run the criteria tests**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py agents/test_assistant_acceptance_criteria.py -q`

Expected: PASS, including `test_criteria_prompt_follows_tier_order` and the new history test. `test_every_assistant_call_shares_the_turn_prefix` still FAILS — audit, second_opinion and the classifier have not moved yet.

- [ ] **Step 6: Commit**

```bash
git add agents/assistant.py agents/test_assistant_prompt_tiers.py
git commit -m "refactor(assistant): build the criteria prompt with the builder

Its history window becomes the decide window, which is what lets the first
decide call of a turn start against a warm prefix instead of a cold one."
```

---

### Task 5: Move the reply-audit prompt onto the builder

**Files:**
- Modify: `agents/assistant.py` — delete `REPLY_AUDIT_MAX_MESSAGES` (around line 4868) and rewrite `_build_reply_audit_prompt` (around line 4870)

**Interfaces:**
- Consumes: `AssistantPromptBuilder` from Task 2.
- Produces: `_build_reply_audit_prompt(message, *, messages, scratchpad) -> str` — signature unchanged.

- [ ] **Step 1: Delete the constant**

Remove these five lines from `agents/assistant.py` (around 4864-4868). Leave `REPLY_AUDIT_MAX_OBSERVATION_CHARS` and its own comment, directly above, alone:

```python
    # Enough prior turns to resolve who a follow-up is about, and no more: the
    # auditor checks the message, and a long transcript both dilutes that and
    # invites checking the reply against remembered facts read off the
    # history instead of against the turn's observations.
    REPLY_AUDIT_MAX_MESSAGES: int = 6
```

That comment states a real concern, and deleting the constant does not answer it — the auditor now sees thirty messages. What answers it is `REPLY_AUDIT_TURN_INSTRUCTIONS`, which already tells the auditor the history is there to "resolve what the request refers to" and is "not evidence for what is true", and which `test_reply_audit_prompt_names_the_history_as_referent_only` pins. Do not weaken that instruction.

- [ ] **Step 2: Rewrite the builder body**

In `_build_reply_audit_prompt`, replace everything from
`root = ET.Element("reply_audit")` through
`self._append_static_head(root, blocks=("identity", "formatting"))` with:

```python
        prompt = AssistantPromptBuilder(
            self, "reply_audit", messages=messages,
            blocks=("identity", "formatting"))
```

`current` is not used by the rest of this method — the audit has no closing
re-anchor, because `proposed_reply` at the tail is what it reads last — so do
not bind it. Convert the remainder:

```python
        prompt.append_turn_instructions(REPLY_AUDIT_TURN_INSTRUCTIONS)
        if self._criteria_markdown:
            prompt.append_text(
                "acceptance_criteria_markdown", self._criteria_markdown)
        if self._reply_language_markdown:
            prompt.append_text(
                "reply_language_markdown", self._reply_language_markdown)
        steps = [e for e in scratchpad if isinstance(e, AssistantTurnStep)]
        if steps:
            observations = prompt.append_element(
                "turn_observations", authority="fresh_evidence")
            for step in steps:
                # action + args + result. No `reason`: see the docstring.
                entry = ET.SubElement(
                    observations, "observation",
                    {"action": step.action, "status": step.status})
                entry.text = step.observation[
                    : self.REPLY_AUDIT_MAX_OBSERVATION_CHARS]
        # The message under audit closes the prompt — the last thing the
        # auditor reads is the thing it is judging.
        prompt.append_text("proposed_reply", message)
        prompt.append_local_time()
        return prompt.render()
```

Update the docstring: "A bounded slice of the conversation rides along" becomes
"The turn's conversation history rides along". The rest of that paragraph — the
subject check, the "who is her mom" example — stays exactly as written; it is
still why the history is there.

- [ ] **Step 3: Run the audit tests**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py agents/test_assistant_acceptance_criteria.py -q`

Expected: PASS, including `test_reply_audit_prompt_follows_tier_order` and `test_reply_audit_sees_the_conversation_it_needs_to_resolve_a_referent`.

- [ ] **Step 4: Commit**

```bash
git add agents/assistant.py
git commit -m "refactor(assistant): build the reply-audit prompt with the builder"
```

---

### Task 6: Move the second-opinion prompt onto the builder

This call gains conversation history, which it does not carry today.

**Files:**
- Modify: `agents/assistant.py` — `_build_second_opinion_prompt` (around line 4797)
- Test: `agents/test_assistant_prompt_tiers.py`

**Interfaces:**
- Consumes: `AssistantPromptBuilder` from Task 2.
- Produces: `_build_second_opinion_prompt(decision, *, reasoning, messages) -> str` — signature unchanged.

- [ ] **Step 1: Update the expected section order**

In `agents/test_assistant_prompt_tiers.py`, change `SECOND_OPINION_EXPECTED` to
insert `conversation_history_xml` after the request, and add it to
`SECOND_OPINION_ALWAYS`:

```python
SECOND_OPINION_EXPECTED = [
    "current_user_request", "conversation_history_xml",
    "user_settings_json", "formatting_guide", "user_profile",
    "turn_instructions",
    "reply_language_markdown", "acceptance_criteria_markdown",
    "proposed_step", "verdict_request", "current_local_time",
]

SECOND_OPINION_ALWAYS = [
    "user_settings_json", "turn_instructions", "proposed_step",
    "conversation_history_xml", "current_user_request", "verdict_request",
    "current_local_time",
]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py::test_second_opinion_prompt_follows_tier_order -q`

Expected: FAIL on `set(SECOND_OPINION_ALWAYS) <= set(order)` — the prompt has no `conversation_history_xml` section.

- [ ] **Step 3: Rewrite the builder body**

In `_build_second_opinion_prompt`, replace everything from
`root = ET.Element("second_opinion_review")` through
`self._append_static_head(root, blocks=("identity", "formatting", "profile"))`
with:

```python
        prompt = AssistantPromptBuilder(
            self, "second_opinion_review", messages=messages,
            blocks=("identity", "formatting", "profile"))
```

Convert the remainder:

```python
        prompt.append_turn_instructions(SECOND_OPINION_TURN_INSTRUCTIONS)

        if self._reply_language_markdown:
            prompt.append_text(
                "reply_language_markdown", self._reply_language_markdown)
        # The criteria are part of what "serves the request" means: a program
        # converting to yards should fail review when the criteria say meters.
        if self._criteria_markdown:
            prompt.append_text(
                "acceptance_criteria_markdown", self._criteria_markdown)
        proposed = prompt.append_element(
            "proposed_step", action=decision.action.value)
        ET.SubElement(proposed, "stated_reason").text = decision.reason
        if reasoning:
            ET.SubElement(proposed, "model_reasoning").text = reasoning[
                : self.SECOND_OPINION_MAX_REASONING_CHARS
            ]
        code = str(decision.args.get("code", ""))
        ET.SubElement(proposed, "python_program").text = code[
            : self.SECOND_OPINION_MAX_CODE_CHARS
        ]
        prompt.append_text(
            "verdict_request",
            "Review the proposed_step against the current_user_request and "
            "the user context above. List real problems (or none), then "
            "set approved.")
        prompt.append_local_time()
        return prompt.render()
```

Replace the docstring's closing clause — "leaf sections only, no conversation
history (the current request is the contract the program is judged against)" —
with:

```
        The turn's conversation history rides along as reference, the same as
        the other calls carry it: what is AUTHORITATIVE here is the request
        and the criteria, which turn_instructions states, and carrying no
        history would only cost this call the shared prefix every other call
        of the turn reuses.
```

- [ ] **Step 4: Run the second-opinion tests**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py agents/test_assistant_second_opinion.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/assistant.py agents/test_assistant_prompt_tiers.py
git commit -m "refactor(assistant): build the second-opinion prompt with the builder

It gains the turn's history: carrying none meant diverging from the shared
prefix immediately after current_user_request."
```

---

### Task 7: Move the language-classifier prompt onto the builder

This call gains `user_settings_json`. It runs first in the turn, so it is the call that warms the prefix, and it can only warm what it carries.

**Files:**
- Modify: `agents/assistant.py` — delete `RESPONSE_LANGUAGE_CLASSIFIER_MAX_MESSAGES` (around line 3347) and rewrite `_build_response_language_classifier_prompt` (around line 5114)
- Test: `agents/test_assistant_prompt_tiers.py`

**Interfaces:**
- Consumes: `AssistantPromptBuilder` from Task 2.
- Produces: `_build_response_language_classifier_prompt(messages, profile) -> str` — signature unchanged.

- [ ] **Step 1: Update the expected section order**

In `agents/test_assistant_prompt_tiers.py`, replace `CLASSIFIER_EXPECTED` and
`CLASSIFIER_ALWAYS`, including the comment above them:

```python
# user_settings_languages_json is this call's own tier-1 block, built from the
# profile rather than by _append_static_head. It follows the shared identity
# block rather than standing in for it: the classifier runs first in the turn,
# so it is the call that warms the prefix, and it can only warm what it
# carries.
CLASSIFIER_EXPECTED = [
    "current_user_request", "conversation_history_xml",
    "user_settings_json", "user_settings_languages_json",
    "turn_instructions", "classification_request",
]

CLASSIFIER_ALWAYS = [
    "user_settings_json", "user_settings_languages_json",
    "turn_instructions", "conversation_history_xml", "current_user_request",
    "classification_request",
]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py::test_response_language_classifier_prompt_follows_tier_order -q`

Expected: FAIL — `user_settings_json` is absent from the rendered order.

- [ ] **Step 3: Delete the constant**

Remove from `agents/assistant.py` (around line 3347):

```python
    RESPONSE_LANGUAGE_CLASSIFIER_MAX_MESSAGES: int = 6
```

- [ ] **Step 4: Rewrite the builder body**

In `_build_response_language_classifier_prompt`, replace everything from
`root = ET.Element("response_language_classifier_call")` through the
`ET.SubElement(history, "none")` branch with:

```python
        prompt = AssistantPromptBuilder(
            self, "response_language_classifier_call", messages=messages,
            blocks=("identity",))
```

Convert the remainder:

```python
        prompt.append_text(
            "user_settings_languages_json",
            json.dumps(
                user_profile.declared_language_candidates(profile),
                ensure_ascii=False, indent=1))

        prompt.append_turn_instructions(RESPONSE_LANGUAGE_TURN_INSTRUCTIONS)

        prompt.append_text(
            "classification_request",
            "Predict the language or languages the next reply should use. "
            "First copy every declared profile-language code exactly into the "
            "result and score it, even when its score is negative. A broad "
            "target language in current_user_request is refined by a "
            "compatible preferred profile variant; it does not replace that "
            "exact code with a broad tag. Then add any language or dialect "
            "required by current_user_request that is absent from the "
            "declared rows. If there are no declared rows, include the "
            "candidates supported by the request.")

        return prompt.render()
```

Replace the docstring's second paragraph, which currently says no static head
is taken, with:

```
        Takes the shared `identity` block and then its own tier-1
        user_settings_languages_json, built here from the profile. This call
        runs first in the turn, so it is the one that warms the prefix the
        other five reuse — and it can only warm the blocks it carries.
```

Also delete the stray double blank line left inside this method after the
`append_turn_instructions` call.

- [ ] **Step 5: Run the classifier tests**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py agents/test_response_language_classifier.py -q`

Expected: PASS.

- [ ] **Step 6: Run the Task 1 contract test — it should now go green**

Run: `./venv/bin/python -m pytest agents/test_assistant_prompt_tiers.py::test_every_assistant_call_shares_the_turn_prefix -q`

Expected: PASS. Every call now renders the same tier 0 and carries `identity`. If it still fails, the failure message names the offending pair — go back to that call's task rather than weakening the assertion.

- [ ] **Step 7: Commit**

```bash
git add agents/assistant.py agents/test_assistant_prompt_tiers.py
git commit -m "refactor(assistant): build the classifier prompt with the builder

Last of the six. Every assistant call now shares tier 0 and the identity
block, which is the property test_every_assistant_call_shares_the_turn_prefix
pins."
```

---

### Task 8: Update the module docstring and run everything

The tier-test module's docstring records measured numbers from the previous refactor and now understates what holds. Docs describe current behavior.

**Files:**
- Modify: `agents/test_assistant_prompt_tiers.py` (module docstring)
- Modify: `agents/assistant.py` (the `_ALL_STATIC_BLOCKS` comment, if it still claims cross-call sharing is limited to the blocks two calls have in common)

- [ ] **Step 1: Rewrite the module docstring's second paragraph**

Replace the paragraph beginning "Two wins, measured." with:

```
Two wins. Within one call, consecutive steps of a decide loop share their
whole prefix through the previous step's own entry. Across the six calls of
one turn, AssistantPromptBuilder emits an identical tier 0 — the request,
then the conversation history at one window for every call — followed by the
identity block every call carries, so the shared run reaches the end of
user_settings_json before the per-call static heads diverge. Behind that,
_ALL_STATIC_BLOCKS is ordered so the per-call block sets nest (criteria then
audit then second_opinion then decide), extending the overlap further for the
calls that have more blocks in common.
```

- [ ] **Step 2: Run the whole assistant suite**

Run: `./venv/bin/python -m pytest agents/ -q`

Expected: no failures introduced by this branch. Compare against the baseline recorded in Task 3 Step 6 — this repo has pre-existing failures unrelated to this work, and those stay as they were.

- [ ] **Step 3: Run the webapp and db suites that touch prompts**

Run: `./venv/bin/python -m pytest webapp/ db/ -q`

Expected: unchanged from baseline.

- [ ] **Step 4: Verify no dead constants remain**

Run: `grep -rn "ACCEPTANCE_CRITERIA_MAX_MESSAGES\|REPLY_AUDIT_MAX_MESSAGES\|RESPONSE_LANGUAGE_CLASSIFIER_MAX_MESSAGES" agents/ webapp/ evals/`

Expected: no output.

- [ ] **Step 5: Verify every call goes through the builder**

Run: `grep -n "ET.Element(" agents/assistant.py`

Expected: exactly two hits at module/class level for prompt roots —
`_build_request_summary_prompt`'s `ET.Element("request_summary_call")` and
`_build_recall_filter_prompt`'s `ET.Element("recall_filter_call")`, both
deliberately outside the builder. Any other `ET.Element(` creating a prompt
root is a call that did not get moved.

- [ ] **Step 6: Commit**

```bash
git add agents/assistant.py agents/test_assistant_prompt_tiers.py
git commit -m "docs(assistant): describe the cross-call prefix the builder now produces"
```

---

### Task 9: Measure it live

The spec's prediction is falsifiable. This task checks it and is the one that decides whether the change worked.

**Files:** none — measurement only.

- [ ] **Step 1: Restart the app so the new code is loaded**

The operator runs the server. Ask them to restart it, then confirm it is up:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/activity
```

Expected: `200`.

- [ ] **Step 2: Ask the operator to run one assistant turn**

Any request that exercises the full turn — one that makes the assistant read memory rather than answer instantly.

- [ ] **Step 3: Read the numbers back**

```bash
curl -s "http://127.0.0.1:5000/activity?range=1h&metric=cached_tokens&by=model"
```

Find the turn's first `assistant.decide` row.

Expected: its **Reusable** column is close to its whole Prompt column, not
467. Before the change the turn read:

```
response_language_classifier  ~2.0k prompt   reusable 232
acceptance_criteria           ~2.3k          reusable ~740-1200
decide #1                    ~11-12k         reusable 467-471
```

- [ ] **Step 4: Read the criteria the turn produced**

The cache win and the quality question are separate, and the /activity numbers
only answer the first. The criteria call now reads five times more transcript,
and its own instructions spend two paragraphs telling it not to nominate the
transcript as a source — spec assumption 2, recorded there as unverified.

Open the `assistant.acceptance_criteria` row for the turn:

```bash
curl -s "http://127.0.0.1:5000/activity?range=1h&metric=cached_tokens&by=model"
```

Follow its `inspect` link and read the `assumptions` field of the response.
What you are looking for is whether it has started settling the scope of the
answer from what the transcript happens to mention, instead of recording the
ambiguity as unresolved. If it has, say so — the fix is a narrower window for
that one call, at the cost of its share of the prefix, and that is the
operator's call to make, not a thing to quietly absorb.

- [ ] **Step 5: Record the result in the spec**

Append a short "Measured outcome" section to
`docs/superpowers/specs/2026-08-23-single-prompt-builder-design.md` with the
before and after figures, in the style of the 2026-08-13 spec's own measured
outcome. **Report what the numbers actually say.** If Reusable did not move,
say so and stop — do not adjust the test to match a disappointing result.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-08-23-single-prompt-builder-design.md
git commit -m "docs(spec): record the measured cross-call prefix reuse"
```

---

## Notes for the implementer

**What must not change.** Every section's text. This refactor moves sections
between builders and widens a window; it does not reword a single instruction.
If a diff shows changed prose inside `turn_instructions`, a
`*_request` section, or any block, that is a mistake.

**The escaping guarantee.** `append_text` and `append_element` both go through
`ET.SubElement`. Never build a section by string concatenation, and never set
`_RAW_RENDER_ATTR` outside `_append_turn_instructions`. The reason is in that
method's docstring: `turn_instructions` carries only code-owned module
constants, so it is the one section where raw rendering is safe.

**The nesting rule.** `_ALL_STATIC_BLOCKS` is ordered identity → formatting →
profile → persona → calibration, and each call's `blocks` tuple must be a
prefix of that order, or a prefix plus its own bespoke sections appended
after. A call that skips a block another call takes ends the shared prefix at
that point.

**If the contract test fails after Task 7**, its message names the pair. Take
the two prompts, diff them at the reported offset, and fix the call — the
assertion is the specification, not the obstacle.
