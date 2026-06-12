# Proposal: task room implementation roadmap

**Status:** Draft implementation roadmap
**Date:** 2026-06-09
**Scope:** How Rainbox should move from the shipped agent-to-agent conversation runtime to useful task rooms, starting with `technical_design`, `diagnosis`, and `email_case`.
**Related documents:**

- `2026-06-08-persona-prompts-and-agent-conversations.md`
- `2026-06-09-technical-design-room.md`
- `2026-06-09-diagnosis-room.md`
- `2026-06-09-email-case-room.md`

## Summary

Rainbox now has the beginning of a general agent-to-agent conversation runtime:
personas as data, conversation templates, bounded turns, manager-controlled
routing, pause/stop/resume/reconcile, journal provenance, and operator UI.

The next step should not be more open-ended conversation. The next step should
be task rooms: bounded templates where agents produce a durable artifact for a
specific kind of work.

The first three task rooms should be:

- `technical_design`: propose, critique, revise, synthesize a technical design.
- `diagnosis`: triage symptoms, form hypotheses, request evidence, recommend
  the least risky next action.
- `email_case`: extract facts from email evidence, assess risk, recommend next
  action, and optionally draft a reply that waits for user approval.

These rooms test different but complementary capabilities:

- `technical_design` tests structured deliberation and bounded dissent.
- `diagnosis` tests evidence-first reasoning and safety classification.
- `email_case` tests case-file extraction, approval-gated side effects, and
  draft generation without pretending anything has been sent.

The recommended order is to implement `technical_design` first because it is
smaller and less safety-sensitive. Then implement the no-tool version of
`diagnosis`. Then implement `email_case` from pasted email text, without Gmail
or send integration. Tool-enabled diagnosis and email integration should come
later, after the shared runtime contracts are stable.

---

## Key Finding

The main design boundary is now clear:

```text
Conversation runtime:
  who speaks next, queue/journal routing, bounds, retries, stop/resume,
  stale-turn reconciliation, transcript visibility, final artifact storage

Task room protocol:
  what work is being performed, which roles speak, what JSON they produce,
  what stop signals mean, and what durable decision record is trusted
```

The manager should stay mechanical. It should not know engineering or diagnosis.
It should know how to run a phase script, validate a turn result, enforce bounds,
route terminal outputs, and store final artifacts.

The room template and agent prompts should define the work.

---

## Readiness Assessment

### `technical_design`

The `technical_design` room is ready for implementation.

It has a small, clear first slice:

```text
proposer -> critic -> proposer revision -> synthesizer
```

It can be implemented without tools, without repository inspection, and without
automatic code edits. The useful output is a `TechnicalDesignDecision` record
rendered to markdown for the user.

The main implementation risk is weak critique or fake consensus. That should be
handled with schema validation and behavioral evals, not with more agents.

### `diagnosis`

The `diagnosis` room is conceptually strong but should be implemented more
carefully.

It has a larger workflow:

```text
triage -> hypotheses -> evidence plan -> synthesis
```

The first version should stop at `needs_evidence`, `needs_user_input`, or
`finalize`. It should not run tools yet.

The main implementation risks are premature fixes, unsafe evidence requests,
privacy leaks, and runaway evidence loops. Those are best addressed by making
the first implementation produce a diagnostic decision and evidence request only.

### `email_case`

The `email_case` room is a good third task room after the first two patterns are
working.

It has a case-file-first workflow:

```text
fact extraction -> risk review -> action plan -> synthesis
```

The first version should work from pasted email text only. It should not search
mailboxes, create drafts in an email client, send messages, archive messages, or
label messages.

The main implementation risks are drafting before understanding the case,
unsupported claims, privacy leakage, accidental commitments, and accidental
sending. Those are best handled by making "draft" and "send" separate concepts:
the room may propose or render a draft, but sending is always a separate
approval-gated side effect outside the room.

---

## Shared Contracts To Build First

Before implementing room-specific logic, define shared contracts once.

### Conversation Template

Add or formalize `phase_script` templates:

```json
{
  "turn_policy": {
    "type": "phase_script",
    "phases": [
      {
        "speaker": "agent_id",
        "purpose": "phase_purpose"
      }
    ]
  },
  "bounds": {
    "min_turns": 4,
    "max_turns": 6,
    "max_rounds": 1
  },
  "stop_policy": {
    "stop_on_synthesizer_finalize": true,
    "stop_on_max_turns": true
  },
  "output_policy": {
    "final_artifact_kind": "technical_design_decision"
  }
}
```

Round-robin can remain for demo conversations. Task rooms should use
`phase_script`.

### Agent Turn Result

Every structured task-room agent should return:

- valid JSON
- a `role`
- phase-specific payload
- `confidence`
- optionally a control signal such as `verdict` or `next_action`

Do not rely on prose phrases like `DONE` for task-room control.

### Synthesizer Control Signal

The synthesizer should be the only agent that can finalize a task room.

Shared `next_action` values:

- `finalize`
- `ask_user`
- `retrieve_evidence`
- `retrieve_email`
- `revise_proposal`
- `focused_debate`
- `focused_diagnosis`
- `create_draft`
- `review_draft`
- `run_tool`
- `stop`

Not every room supports every value. Unsupported values should fail validation
or become `needs_revision`.

### Final Artifact

The trusted product should be a durable record, not the transcript.

Examples:

- `TechnicalDesignDecision`
- `DiagnosticDecision`
- `EmailCaseDecision`

The transcript remains useful for audit and debugging, but later systems should
consume the structured artifact.

---

## Schema Work

Define Pydantic schemas before prompt tuning.

Minimum shared schemas:

- `PhaseScriptTemplate`
- `RoomBounds`
- `StopPolicy`
- `TaskRoomRunState`
- `TaskRoomFinalArtifact`
- `AgentTurnEnvelope`

Minimum `technical_design` schemas:

- `DesignIssue`
- `TechnicalDesignProposal`
- `TechnicalDesignCritique`
- `TechnicalDesignSynthesis`
- `TechnicalDesignDecision`

Minimum `diagnosis` schemas:

- `DiagnosticIssue`
- `DiagnosisTriage`
- `DiagnosisHypotheses`
- `DiagnosisEvidencePlan`
- `DiagnosisSynthesis`
- `DiagnosticDecision`

Minimum `email_case` schemas:

- `EmailCase`
- `EmailCaseFactExtraction`
- `EmailCaseRiskReview`
- `EmailCaseActionPlan`
- `EmailCaseSynthesis`
- `EmailCaseDecision`

Optional later `email_case` schemas:

- `EmailCaseLocator`
- `EmailCaseDraft`
- `EmailCaseDraftReview`
- `EmailToolRequest`

Use validation and retry for malformed model output. "JSON only" should be a
prompt instruction, but correctness should come from schema validation.

---

## Enum Alignment

The current proposals are close, but the first implementation should align enum
names before coding.

### Evidence Safety Classes

Use one shared set:

- `read_only`
- `privacy_sensitive`
- `state_changing`
- `destructive`
- `external_network`
- `external_side_effect`

`requires_user_approval` should remain a separate boolean, not a safety class.

### Decision Statuses

Technical design:

- `accepted`
- `accepted_with_risks`
- `needs_revision`
- `needs_evidence`
- `needs_user_input`
- `rejected`

Diagnosis:

- `needs_evidence`
- `needs_user_input`
- `action_recommended`
- `inconclusive`
- `resolved`
- `stop`

Email case:

- `needs_evidence`
- `needs_fact_extraction`
- `needs_risk_review`
- `needs_action_plan`
- `needs_draft`
- `draft_needs_review`
- `draft_ready`
- `needs_user_input`
- `action_recommended`
- `no_action_needed`
- `inconclusive`
- `stopped`

### Consensus States

Only the `technical_design` room needs consensus language:

- `strong_consensus`
- `qualified_consensus`
- `bounded_disagreement`
- `needs_evidence`
- `needs_user_input`
- `no_consensus`

Diagnosis should avoid consensus framing. It should use evidence strength and
hypothesis status instead.

Email case should also avoid consensus framing. It should use case status,
approval boundaries, and draft readiness instead.

### Email Side-Effect Classes

Email tools need a separate side-effect model because even harmless-looking
email operations can expose private content or change user workflow state.

Use these action classes:

- `read_email_metadata`
- `read_email_thread`
- `read_attachment`
- `create_draft`
- `update_draft`
- `send_draft`
- `archive`
- `delete`
- `label`
- `forward`
- `create_reminder`

Default rule: the room may recommend these actions, but the tool gateway
enforces approval. `send_draft`, `archive`, `delete`, `label`, `forward`, and
`create_reminder` require explicit user approval. The first `email_case`
implementation should not call these tools at all.

---

## Implementation Roadmap

### Phase 1: Shared Phase-Script Runner

Goal: run deterministic task-room flows with fake agent outputs.

Implement:

- Load a conversation template with `turn_policy.type = phase_script`.
- Copy template phases into runtime state at run creation.
- Track `phase_cursor`.
- Enforce one active turn per run.
- Advance only after a terminal result from the expected speaker.
- Store structured turn payloads.
- Stop on supported synthesizer `next_action` values.

Acceptance:

- A fake four-phase room runs in exact speaker order.
- A stale or duplicate turn cannot advance the room twice.
- Invalid speaker output fails cleanly.
- Max turns stops the room.
- Final artifact is stored separately from the transcript.

### Phase 2: `technical_design` Without Tools

Goal: ship the first useful task room.

Implement:

- `technical_design_proposer.system.md`
- `technical_design_critic.system.md`
- `technical_design_synthesizer.system.md`
- `technical-design.json`
- `DesignIssue`
- `TechnicalDesignDecision`
- deterministic final markdown renderer
- fake LLM tests

Flow:

```text
proposer -> critic -> proposer revision -> synthesizer
```

Acceptance:

- A user can start a `technical_design` room from the UI.
- The room runs four visible turns.
- The synthesizer emits a valid `TechnicalDesignDecision`.
- The room stops automatically on `next_action = finalize`.
- The final markdown answer includes recommendation, risks, dissent, and next
  step.

### Phase 3: Technical Design Evals

Goal: prevent fake usefulness.

Add behavioral evals with intentionally flawed prompts.

Score:

- concrete design
- useful criticism
- high-severity flaw detection
- preserved dissent
- realistic smallest prototype
- valid JSON
- correct stop decision

Acceptance:

- A weak proposal with a serious flaw is not accepted as strong consensus.
- A critic objection with high severity must be addressed or preserved as risk.
- The synthesizer can return `needs_revision` or `needs_evidence`.

### Phase 4: `diagnosis` Without Tools

Goal: ship evidence-first diagnostic reasoning without executing tools.

Implement:

- `diagnosis_triage.system.md`
- `diagnosis_hypothesis.system.md`
- `diagnosis_evidence_planner.system.md`
- `diagnosis_synthesizer.system.md`
- `diagnosis.json`
- `DiagnosticIssue`
- `DiagnosticDecision`
- deterministic final markdown renderer
- fake LLM tests

Flow:

```text
triage -> hypotheses -> evidence plan -> synthesizer
```

Acceptance:

- A user can start a `diagnosis` room from the UI.
- The room classifies problem type and risk.
- The room proposes testable hypotheses.
- The room emits safe evidence requests.
- The room stops at `needs_evidence`, `needs_user_input`, or `finalize`.
- It does not recommend destructive or state-changing actions.

### Phase 5: `email_case` Without Tools

Goal: ship case-file extraction and action recommendation from pasted email
text, without email-client integration.

Implement:

- `email_case_fact_extractor.system.md`
- `email_case_risk_reviewer.system.md`
- `email_case_action_planner.system.md`
- `email_case_synthesizer.system.md`
- `email-case.json`
- `EmailCase`
- `EmailCaseDecision`
- deterministic final markdown renderer
- fake LLM tests

Flow:

```text
fact_extractor -> risk_reviewer -> action_planner -> synthesizer
```

Acceptance:

- A user can paste an email thread into an `email_case` room.
- The room extracts participants, facts, asks, obligations, deadlines, and
  unresolved issues.
- The room identifies unsupported claims and things to avoid saying.
- The room recommends a next action such as `reply`, `wait`,
  `ask_clarification`, `no_action`, or `needs_more_evidence`.
- The room stops with an `EmailCaseDecision`.
- It never says anything was sent.

### Phase 6: Email Draft Generation

Goal: produce reviewable drafts without creating or sending anything in an
email client.

Implement:

- `email_case_response_drafter.system.md`
- second pass through `email_case_risk_reviewer`
- draft fields on `EmailCaseDecision`
- tests for placeholders and risky draft rejection

Flow:

```text
fact_extractor
-> risk_reviewer
-> action_planner
-> response_drafter
-> risk_reviewer
-> synthesizer
```

Acceptance:

- The room produces a concise draft only when the action plan says a draft is
  useful.
- The draft uses placeholders instead of inventing missing facts.
- The second risk review can mark a draft `acceptable`, `revise`, or `reject`.
- The final answer clearly says the draft is for user review and has not been
  sent.

### Phase 7: Diagnosis Evidence Round Without Tools

Goal: allow the user to paste evidence back into a diagnostic room.

Implement:

- `diagnosis_evidence_analyst`
- `diagnosis_fix_reviewer`
- resume a diagnostic room from user-provided evidence

Flow:

```text
user pastes evidence
-> evidence analyst
-> fix reviewer
-> synthesizer
```

Acceptance:

- The room updates hypothesis statuses from pasted evidence.
- Any recommended action includes risk, rollback, and verification.
- The room can still say evidence is inconclusive.

### Phase 8: Diagnostic Tool Integration

Goal: allow approved evidence collection.

Do this only after the no-tool flows are stable.

Rules:

- Agents request evidence.
- The tool gateway enforces permissions.
- The user approves state-changing, privacy-sensitive, or external-side-effect
  actions.
- Tool results are stored as evidence records.
- Evidence Analyst interprets tool results.

Acceptance:

- `read_only` evidence can be suggested safely.
- `privacy_sensitive` evidence requires approval or redaction.
- `state_changing` and `external_side_effect` actions require explicit approval.
- `destructive` actions are blocked by default.

### Phase 9: Email Tool Integration

Goal: allow targeted email search, selected thread reads, and optional draft
creation behind explicit approval.

Do this only after pasted-email `email_case` and draft generation are stable.

Rules:

- Locator requests email evidence.
- The tool gateway asks for approval when needed.
- Email tools return metadata, selected thread content, or attachment summaries.
- Fact Extractor processes email evidence.
- Draft creation is separate from draft generation.
- Sending is separate from draft creation.

Acceptance:

- The room can request a targeted email search.
- The user can approve or deny the search.
- The selected thread becomes `EMAIL_EVIDENCE`.
- The room can create a saved draft only after explicit approval.
- The room cannot send the draft unless the user explicitly approves sending.
- Archive, delete, label, forward, and reminder actions require explicit
  approval.

---

## What Not To Build Yet

Do not start with:

- automatic code edits
- automatic repair
- automatic sending
- automatic email archiving, deletion, forwarding, or labeling
- swarms
- majority voting
- tool loops
- a general planner agent
- a generic "agents discuss until done" room
- database-backed prompt editing UI

These can come later. The first win is reliable bounded task-room execution with
inspectable artifacts.

---

## Relationship To Egon And Benny

Keep Egon and Benny as demo personas and regression fixtures.

They should remain useful for:

- smoke-testing the conversation runtime
- checking local model behavior
- demonstrating the UI
- verifying pause/stop/resume/reconcile

They should not be the default personas for serious work.

Task rooms should use functional roles named after the work they perform:

- `technical_design_proposer`
- `technical_design_critic`
- `technical_design_synthesizer`
- `diagnosis_triage`
- `diagnosis_hypothesis`
- `diagnosis_evidence_planner`
- `diagnosis_synthesizer`
- `email_case_fact_extractor`
- `email_case_risk_reviewer`
- `email_case_action_planner`
- `email_case_synthesizer`
- `email_case_response_drafter`

---

## Recommended Next Step

Start implementation with the shared `phase_script` runner and fake-agent tests.

Then ship `technical_design` without tools.

Only after that should Rainbox implement `diagnosis`, and only in a no-tool form
at first.

Then implement `email_case` from pasted email text. Add draft generation only
after the case-file flow works. Add email search/read/draft tooling last, behind
explicit approval.

This order keeps the next milestone small, useful, and testable while preserving
the safety boundary needed for later diagnostic and email tool use.
