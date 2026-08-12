# Proposal: diagnosis room

**Status:** Draft design proposal
**Scope:** General-purpose diagnostic room for Rainbox
**Purpose:** Let multiple AI agents investigate a problem by collecting observations, forming hypotheses, requesting safe evidence, narrowing causes, and recommending the least risky next action.

## Summary

The diagnosis room should be a bounded Rainbox conversation template for debugging, troubleshooting, and case analysis.

It differs from the technical_design room.

The technical_design room is:

```text
propose → critique → revise → synthesize
```

The diagnosis room should be:

```text
triage → hypothesize → collect evidence → analyze → recommend → verify
```

Diagnosis is not debate-first. It is evidence-first.

A good diagnostic room should avoid this failure mode:

```text
User: My computer is slow.
Agent A: Maybe memory pressure.
Agent B: Maybe disk space.
Agent C: Maybe malware.
Synthesizer: Consensus says check all of these.
```

That is weak. It creates a pile of guesses.

A better room produces:

Known symptom:

- User reports machine is slow.

Initial hypotheses:

- Memory pressure
- CPU-bound process
- Disk pressure
- Indexing/background task
- Thermal throttling

Next evidence:

- Use read-only commands first.
- Do not modify the system.
- Ask before destructive or privacy-sensitive actions.

Recommended next step:

- Collect system snapshot with safe commands.

The important rule:

Diagnosis is state refinement, not conversation.

The room should maintain a structured diagnostic state:

- symptoms
- observations
- hypotheses
- evidence requests
- evidence collected
- ruled-out causes
- likely causes
- risk level
- recommended action
- verification step

The final artifact should be a diagnostic decision record, not merely a transcript.

---

## Design Goals

The diagnosis room should:

1. Diagnose problems from incomplete information.
2. Avoid premature fixes.
3. Prefer read-only inspection before mutation.
4. Separate symptoms, observations, hypotheses, and conclusions.
5. Classify safety risk before suggesting actions.
6. Ask for evidence only when it changes the decision.
7. Produce a minimal next step.
8. Preserve uncertainty.
9. Support local computer diagnosis, repository diagnosis, runtime/log diagnosis, email/case diagnosis, and workflow diagnosis.
10. Keep the room bounded.

---

## Non-Goals

The first version should not be an autonomous repair agent.

Non-goals:

- No automatic file deletion.
- No automatic process killing.
- No automatic email sending.
- No irreversible actions.
- No unbounded tool loops.
- No guessing presented as fact.
- No “try everything” advice.
- No generic debate between agents.
- No tool execution in the first walking skeleton unless manually approved.

The first useful version should answer:

What is probably going on, what evidence supports that, what is the safest next step,
and what should be verified afterward?

---

## Core Work Object: `DiagnosticIssue`

The room should gather around a named diagnostic issue.

Minimal record:

```json
{
  "issue_id": "diagnostic_issue_123",
  "room_type": "diagnosis",
  "user_request": "My Mac is suddenly slow after running local models.",
  "domain_context": "macOS, local LLM tools, terminal diagnostics, Python development",
  "known_constraints": [
    "Prefer read-only commands first",
    "Do not delete files automatically",
    "Do not kill processes automatically",
    "Ask before actions that modify system state",
    "Preserve user privacy"
  ],
  "status": "under_investigation",
  "turn_policy": "triage_hypothesize_evidence_analyze_recommend",
  "max_rounds": 2
}
```

The diagnosis room should not treat the transcript as the main product.

The useful product is the current diagnostic state and the final diagnostic record.

---

## Recommended First Template

Use four functional agents:

- `diagnosis_triage`
- `diagnosis_hypothesis`
- `diagnosis_evidence_planner`
- `diagnosis_synthesizer`

Optional later agents:

- `diagnosis_tool_runner`
- `diagnosis_log_analyst`
- `diagnosis_fix_reviewer`
- `diagnosis_privacy_safety_gate`
- `diagnosis_user_questioner`

Do not start with the optional agents.

The smallest useful loop:

1. Triage classifies the problem and risk.
2. Hypothesis agent proposes possible causes.
3. Evidence planner requests safe evidence.
4. Synthesizer decides next action.

After evidence is available:

5. Evidence analyst interprets observations.
6. Fix reviewer proposes least risky action.
7. Synthesizer finalizes or asks for one more evidence item.

The first walking skeleton can be:

```text
triage → hypotheses → evidence plan → synthesis
```

The first tool-enabled version can be:

```text
triage → hypotheses → evidence plan → tool run → evidence analysis → recommendation
```

Hard rule:

No fix before evidence unless the issue is trivial and low-risk.

---

## Room Protocol

### Phase 1: Triage

The room classifies:

- What kind of problem is this?
- How risky is it?
- Is the user asking for diagnosis, repair, explanation, or action?
- What domain does it belong to?
- What should not be touched?

Example categories:

- `local_computer`
- `repository`
- `runtime_logs`
- `database`
- `network`
- `email_case`
- `document_case`
- `workflow`
- `unknown`

Risk levels:

- `low`
- `medium`
- `high`
- `critical`

A critical issue may require stopping and asking the user to seek human support, depending on context.

### Phase 2: Hypotheses

The room lists candidate causes.

Each hypothesis should have:

- cause
- why plausible
- what evidence would support it
- what evidence would weaken it
- risk if wrong

### Phase 3: Evidence Plan

The room proposes safe observations.

For local computer diagnosis, that may be read-only shell commands.

For email diagnosis, it may be reading relevant threads.

For repository diagnosis, it may be reading files and tests.

For runtime diagnosis, it may be logs, configs, versions, recent changes.

Evidence should be classified:

- `read_only`
- `privacy_sensitive`
- `state_changing`
- `destructive`
- `external_network`
- `requires_user_approval`

### Phase 4: Evidence Analysis

When observations are available, the room should update hypotheses.

It should say:

- `supported`
- `weakened`
- `ruled_out`
- `unknown`

### Phase 5: Recommendation

The room recommends the least risky next action.

Every recommendation should include:

- action
- why
- risk
- rollback
- verification

### Phase 6: Verification

The room should define how to know whether the fix worked.

Example:

After closing the runaway process, verify CPU load remains below X for Y minutes.

Or more generally:

- The symptom should disappear.
- The metric should improve.
- No new error should appear.
- The user should confirm behavior.

---

## Shared Prompt Placeholders

All diagnosis agents should receive the same structured context.

```text
{{ROOM_GOAL}}
{{USER_REQUEST}}
{{DOMAIN_CONTEXT}}
{{KNOWN_CONSTRAINTS}}
{{AVAILABLE_TOOLS}}
{{CURRENT_STATE}}
{{OTHER_AGENT_MESSAGES}}
{{EVIDENCE_COLLECTED}}
{{OUTPUT_SCHEMA}}
```

### Recommended Meaning

#### `{{ROOM_GOAL}}`

Example:

```text
Diagnose the user's problem by classifying symptoms, generating hypotheses,
requesting safe evidence, narrowing causes, and recommending the least risky next action.
```

#### `{{USER_REQUEST}}`

The original user prompt that started the room.

Example:

```text
My Mac has become slow after I ran a local LLM benchmark. What should I check?
```

#### `{{DOMAIN_CONTEXT}}`

Injectable domain knowledge.

Example:

```text
Domain: macOS developer workstation diagnosis
Known environment: Apple Silicon Mac, terminal available, local LLM tools may be installed
Preferred approach: read-only commands first, no destructive actions, explain risk before changes
```

Another example:

```text
Domain: Python/Flask/PostgreSQL application diagnosis
Known concerns: logs, migrations, connection pools, long-running jobs, background workers
Preferred approach: inspect logs/config/tests before code changes
```

Another example:

```text
Domain: email/case diagnosis
Known concerns: facts, deadlines, commitments, tone, missing information, user approval before sending
Preferred approach: extract timeline and obligations before drafting response
```

#### `{{KNOWN_CONSTRAINTS}}`

Example:

- Do not modify files automatically.
- Do not run destructive commands.
- Do not send emails automatically.
- Prefer evidence over guesses.
- Ask for approval before state-changing actions.
- Preserve privacy-sensitive information.

#### `{{AVAILABLE_TOOLS}}`

Initial version:

No tools available. Reason from user-provided context only.

Tool-enabled version:

Available tools:

- read uploaded files
- inspect repository files
- run approved read-only shell commands
- search email metadata
- read selected email threads
- inspect logs

#### `{{CURRENT_STATE}}`

Example:

```json
{
  "round": 1,
  "symptoms": [],
  "observations": [],
  "hypotheses": [],
  "evidence_requests": [],
  "ruled_out": [],
  "likely_causes": [],
  "recommended_actions": [],
  "risk_level": "unknown",
  "decision": null
}
```

---

## Agent 1: `diagnosis_triage`

### Responsibility

The Triage agent classifies the diagnostic situation and establishes safety boundaries.

It should answer:

- What kind of problem is this?
- What is the likely domain?
- How risky is the situation?
- What should be avoided?
- What information is already available?
- What is the next diagnostic phase?

### System Prompt

```text
You are the Triage agent in a Rainbox diagnosis room.
Your job is to classify the user's problem, identify the diagnostic domain,
extract symptoms, identify risk, and define safety boundaries.
You are not trying to solve the problem yet.
You are deciding how the room should investigate it.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Extract symptoms from the user's request.
2. Classify the diagnostic domain.
3. Identify whether this is diagnosis, repair, explanation, triage, or action planning.
4. Identify safety and privacy concerns.
5. Prefer read-only inspection before state-changing actions.
6. Do not propose fixes yet unless the issue is trivial and low-risk.
7. Do not ask the user questions unless missing information blocks any useful next step.
8. If missing information is useful but not blocking, list it as non-blocking.
9. Mark uncertainty clearly.
10. Keep the room bounded.
```

### Output JSON Only

```json
{
  "role": "diagnosis_triage",
  "problem_type": "local_computer|repository|runtime_logs|database|network|email_case|document_case|workflow|unknown",
  "user_intent": "diagnose|repair|explain|plan_response|inspect|unknown",
  "symptoms": [
    "Symptom extracted from the user request"
  ],
  "known_facts": [
    "Fact available from the request or context"
  ],
  "missing_information": [
    {
      "question": "Question",
      "blocking": true
    }
  ],
  "risk_level": "low|medium|high|critical",
  "safety_boundaries": [
    "Boundary or action to avoid"
  ],
  "privacy_concerns": [
    "Concern"
  ],
  "recommended_next_phase": "hypothesize|ask_user|stop|retrieve_evidence",
  "confidence": "low|medium|high"
}
```

---

## Agent 2: `diagnosis_hypothesis`

### Responsibility

The Hypothesis agent proposes candidate causes and discriminating evidence.

It should not produce a fix plan yet.

It should produce useful hypotheses that can be tested.

### System Prompt

```text
You are the Hypothesis agent in a Rainbox diagnosis room.
Your job is to propose plausible causes for the problem and identify what evidence
would support, weaken, or rule out each cause.
You are not trying to fix the problem yet.
You are trying to make the investigation sharper.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Triage output: {{TRIAGE_OUTPUT}}
- Evidence collected: {{EVIDENCE_COLLECTED}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Generate a small set of plausible hypotheses.
2. Prefer hypotheses that can be distinguished by evidence.
3. Do not list every possible cause.
4. Separate likely causes from edge cases.
5. For each hypothesis, state what evidence would support it.
6. For each hypothesis, state what evidence would weaken or rule it out.
7. Identify the cost/risk of investigating each hypothesis.
8. Avoid premature fixes.
9. Avoid speculation presented as fact.
10. If evidence already rules out a hypothesis, say so.
```

### Output JSON Only

```json
{
  "role": "diagnosis_hypothesis",
  "hypotheses": [
    {
      "id": "H1",
      "cause": "Possible cause",
      "why_plausible": "Why this could explain the symptoms",
      "supporting_evidence_needed": [
        "Observation that would support it"
      ],
      "weakening_evidence": [
        "Observation that would weaken or rule it out"
      ],
      "investigation_risk": "low|medium|high",
      "initial_likelihood": "low|medium|high"
    }
  ],
  "most_discriminating_evidence": [
    "Evidence item that best separates hypotheses"
  ],
  "premature_fixes_to_avoid": [
    "Fix or action that should not be tried yet"
  ],
  "confidence": "low|medium|high"
}
```

---

## Agent 3: `diagnosis_evidence_planner`

### Responsibility

The Evidence Planner decides what information should be collected next.

This is the safety-critical agent.

It should prefer:

read-only
minimal
targeted
low privacy exposure
high diagnostic value

It should classify every evidence request by risk.

### System Prompt

```text
You are the Evidence Planner agent in a Rainbox diagnosis room.
Your job is to decide what evidence should be collected next to narrow the diagnosis.
You must prioritize safe, read-only, targeted evidence.
You must not propose destructive or state-changing actions as diagnostic steps unless explicitly justified.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Triage output: {{TRIAGE_OUTPUT}}
- Hypotheses: {{HYPOTHESIS_OUTPUT}}
- Evidence collected: {{EVIDENCE_COLLECTED}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Request only evidence that can change the diagnosis.
2. Prefer read-only inspection.
3. Classify every proposed evidence request by safety risk.
4. Mark privacy-sensitive evidence clearly.
5. Avoid broad data dumps.
6. Avoid destructive commands.
7. Avoid state-changing commands unless the room is explicitly in a repair phase.
8. Prefer commands or inspections that are easy for the user to understand.
9. If tools are unavailable, produce instructions for the user instead.
10. If more evidence is unnecessary, say so.
```

### Output JSON Only

```json
{
  "role": "diagnosis_evidence_planner",
  "evidence_requests": [
    {
      "id": "E1",
      "purpose": "What this evidence will clarify",
      "request_type": "user_question|read_file|read_log|read_email|safe_shell_command|repository_search|benchmark|external_lookup",
      "instruction": "Command, question, file, or inspection request",
      "expected_signal": "What result would matter",
      "safety_class": "read_only|privacy_sensitive|state_changing|destructive|external_network",
      "requires_user_approval": true,
      "related_hypotheses": [
        "H1"
      ]
    }
  ],
  "recommended_order": [
    "E1"
  ],
  "evidence_not_needed": [
    "Evidence that would be noise or premature"
  ],
  "can_proceed_without_more_evidence": false,
  "confidence": "low|medium|high"
}
```

---

## Agent 4: `diagnosis_evidence_analyst`

Add this when the room can receive evidence.

### Responsibility

The Evidence Analyst interprets observations and updates hypotheses.

It should avoid overclaiming.

### System Prompt

```text
You are the Evidence Analyst agent in a Rainbox diagnosis room.
Your job is to interpret collected evidence and update the diagnostic state.
You do not propose fixes unless the evidence clearly supports a next action.
You classify hypotheses as supported, weakened, ruled out, or still unknown.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Current state: {{CURRENT_STATE}}
- Triage output: {{TRIAGE_OUTPUT}}
- Hypotheses: {{HYPOTHESIS_OUTPUT}}
- Evidence requests: {{EVIDENCE_REQUESTS}}
- Evidence collected: {{EVIDENCE_COLLECTED}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Tie every conclusion to specific evidence.
2. Distinguish observation from interpretation.
3. Do not infer more than the evidence supports.
4. Update each relevant hypothesis.
5. Identify contradictions or missing data.
6. If evidence is inconclusive, say so.
7. Identify the next most useful evidence only if needed.
8. Do not recommend destructive fixes.
9. Keep privacy-sensitive details out of summaries unless necessary.
10. Preserve uncertainty.
```

### Output JSON Only

```json
{
  "role": "diagnosis_evidence_analyst",
  "observations": [
    {
      "evidence_id": "E1",
      "observation": "What was observed",
      "interpretation": "What it likely means",
      "confidence": "low|medium|high"
    }
  ],
  "hypothesis_updates": [
    {
      "hypothesis_id": "H1",
      "status": "supported|weakened|ruled_out|unknown",
      "reason": "Evidence-based reason"
    }
  ],
  "likely_causes": [
    {
      "cause": "Likely cause",
      "supporting_evidence": [
        "Evidence"
      ],
      "confidence": "low|medium|high"
    }
  ],
  "remaining_unknowns": [
    "Unknown"
  ],
  "next_evidence_needed": [
    "Evidence request"
  ],
  "can_recommend_action": true
}
```

---

## Agent 5: `diagnosis_fix_reviewer`

Add this only after evidence exists.

### Responsibility

The Fix Reviewer proposes the least risky next action and checks reversibility.

It should not turn into an eager repair bot.

### System Prompt

```text
You are the Fix Reviewer agent in a Rainbox diagnosis room.
Your job is to recommend the least risky next action after evidence has been analyzed.
You must prefer reversible, minimal, observable actions.
You must not recommend destructive actions without explicit warning and approval.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Evidence analysis: {{EVIDENCE_ANALYSIS}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Recommend no action if evidence is insufficient.
2. Prefer the smallest reversible action.
3. Include risk and rollback.
4. Include a verification step.
5. Separate immediate workaround from root-cause fix.
6. Do not recommend deleting data, killing critical processes, changing system settings, or sending emails without explicit approval.
7. If the next action requires user approval, mark it.
8. If human expertise is needed, say so directly.
9. Avoid long checklists.
10. Make the next step actionable.
```

### Output JSON Only

```json
{
  "role": "diagnosis_fix_reviewer",
  "recommendations": [
    {
      "id": "A1",
      "action": "Recommended action",
      "why": "Why this action follows from evidence",
      "risk": "low|medium|high",
      "reversible": true,
      "requires_user_approval": true,
      "rollback": "How to undo or stop",
      "verification": "How to know whether it worked"
    }
  ],
  "actions_to_avoid": [
    {
      "action": "Action to avoid",
      "reason": "Why"
    }
  ],
  "workaround": "Optional immediate workaround",
  "root_cause_fix": "Optional longer-term fix",
  "confidence": "low|medium|high"
}
```

---

## Agent 6: `diagnosis_synthesizer`

### Responsibility

The Synthesizer turns the diagnostic conversation into a decision.

It should answer:

What do we know?
What do we not know?
What is most likely?
What evidence is needed next?
What is the safest action?
Should the room stop?

### System Prompt

```text
You are the Synthesizer agent in a Rainbox diagnosis room.
Your job is to reduce the diagnostic conversation into a useful diagnostic decision.
You do not merely summarize.
You decide whether the room should collect evidence, ask the user, recommend an action,
or stop because further progress is not possible.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Agent messages: {{OTHER_AGENT_MESSAGES}}
- Evidence collected: {{EVIDENCE_COLLECTED}}
Rules:
1. Separate symptoms, observations, hypotheses, likely causes, and recommended actions.
2. Do not present guesses as facts.
3. Prefer evidence-first diagnosis.
4. Preserve uncertainty.
5. Recommend the least risky next step.
6. Stop if the next step requires the user or an external tool.
7. Stop if another agent round would not add useful information.
8. Request more evidence only if it changes the diagnosis.
9. Do not force consensus.
10. Produce a final answer shape suitable for the user.
```

### Output JSON Only

```json
{
  "role": "diagnosis_synthesizer",
  "diagnostic_summary": "Short summary of the current diagnosis.",
  "known_facts": [
    "Known fact"
  ],
  "likely_causes": [
    {
      "cause": "Likely cause",
      "confidence": "low|medium|high",
      "supporting_evidence": [
        "Evidence"
      ]
    }
  ],
  "ruled_out": [
    {
      "cause": "Cause ruled out",
      "reason": "Why"
    }
  ],
  "remaining_unknowns": [
    "Unknown"
  ],
  "recommended_next_step": {
    "type": "ask_user|collect_evidence|run_tool|recommend_action|stop",
    "description": "Concrete next step",
    "risk": "low|medium|high",
    "requires_user_approval": true
  },
  "verification": [
    "How to verify progress or resolution"
  ],
  "decision": {
    "status": "needs_evidence|needs_user_input|action_recommended|inconclusive|resolved|stop",
    "confidence": "low|medium|high"
  },
  "next_action": "finalize|ask_user|retrieve_evidence|run_tool|focused_diagnosis|stop",
  "focused_question": "If next_action is focused_diagnosis, the one issue to investigate."
}
```

---

## Suggested Conversation Template File

Example agent_profiles/conversations/diagnosis.json:

```json
{
  "id": "diagnosis",
  "title": "Diagnosis Room",
  "description": "Bounded multi-agent diagnostic room for troubleshooting problems using symptoms, hypotheses, safe evidence, and least-risk recommendations.",
  "room_type": "diagnosis",
  "participants": [
    {
      "persona_id": "diagnosis_triage",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "diagnosis_hypothesis",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "diagnosis_evidence_planner",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "diagnosis_synthesizer",
      "agent_kind": "chat_structured",
      "required": true
    }
  ],
  "turn_policy": {
    "type": "phase_script",
    "phases": [
      {
        "speaker": "diagnosis_triage",
        "purpose": "classify_problem"
      },
      {
        "speaker": "diagnosis_hypothesis",
        "purpose": "generate_hypotheses"
      },
      {
        "speaker": "diagnosis_evidence_planner",
        "purpose": "plan_safe_evidence"
      },
      {
        "speaker": "diagnosis_synthesizer",
        "purpose": "diagnostic_decision"
      }
    ]
  },
  "bounds": {
    "min_turns": 4,
    "max_turns": 8,
    "max_rounds": 2
  },
  "stop_policy": {
    "stop_on_synthesizer_finalize": true,
    "stop_on_need_user_input": true,
    "stop_on_need_evidence": true,
    "stop_on_max_turns": true,
    "allow_focused_diagnosis": true,
    "focused_diagnosis_max_turns": 2
  },
  "safety_policy": {
    "read_only_first": true,
    "require_approval_for_state_change": true,
    "forbid_destructive_actions_by_default": true,
    "preserve_privacy": true
  },
  "context_policy": {
    "last_visible_turns": 8,
    "include_runtime_preamble": true,
    "include_summary": true
  },
  "output_policy": {
    "final_artifact_kind": "diagnostic_decision",
    "preserve_uncertainty": true
  }
}
```

---

## Durable Diagnostic Decision Record

The final output should be structured.

```json
{
  "diagnostic_issue_id": "diagnostic_issue_123",
  "decision": "needs_evidence",
  "confidence": "medium",
  "summary": "The problem is likely resource pressure or a runaway background process, but current evidence is insufficient.",
  "symptoms": [
    "Machine is slow after running local model workload"
  ],
  "known_facts": [
    "User is on macOS",
    "Local model workloads may have recently run"
  ],
  "hypotheses": [
    {
      "id": "H1",
      "cause": "Memory pressure",
      "status": "plausible",
      "confidence": "medium"
    },
    {
      "id": "H2",
      "cause": "CPU-bound process still running",
      "status": "plausible",
      "confidence": "medium"
    },
    {
      "id": "H3",
      "cause": "Disk pressure or swap growth",
      "status": "possible",
      "confidence": "low"
    }
  ],
  "evidence_needed": [
    {
      "id": "E1",
      "instruction": "Run uptime and top snapshot",
      "safety_class": "read_only"
    }
  ],
  "recommended_next_step": "Collect read-only system snapshot before suggesting fixes.",
  "actions_to_avoid": [
    "Do not delete files yet",
    "Do not kill processes without identifying them"
  ],
  "verification": [
    "After any action, verify CPU, memory pressure, and symptom improvement"
  ]
}
```

---

## Final Answer Renderer

The final writer should render diagnostic records as readable markdown.

Recommended user-facing sections:

1. Current diagnosis
2. What is known
3. Most likely causes
4. What to check next
5. Safe commands or evidence requests
6. What not to do yet
7. Verification step

For tool-enabled rooms, include commands only if they are safe and relevant.

Example final answer:

```text
Current diagnosis:
The problem is probably resource pressure, but there is not enough evidence yet.

Most likely causes:
- A CPU-heavy process is still running.
- Memory pressure or swap is high.
- Disk pressure is contributing.

Safe next step:
Run these read-only commands:
uptime
top -l 1 -o cpu
vm_stat
df -h

Do not delete files or kill processes yet. First identify what is consuming resources.
```

---

## Implementation Plan

### Phase 0: Static Prompt Experiment

Goal: prove the diagnostic protocol works without tools.

Implement:

- `diagnosis_triage.system.md`
- `diagnosis_hypothesis.system.md`
- `diagnosis_evidence_planner.system.md`
- `diagnosis_synthesizer.system.md`

Create a small script that runs:

- triage
- hypothesis
- evidence_planner
- synthesizer

Use fake or manually pasted outputs first.

Save:

- `diagnostic_issue.json`
- `turns.jsonl`
- `diagnostic_decision.json`
- `final_answer.md`

Acceptance:

Given 5 diagnostic prompts, the room produces:
- classified problem type
- plausible hypotheses
- safe evidence requests
- clear next action
- no premature destructive fixes

### Phase 1: Rainbox Conversation Template

Goal: run diagnosis inside the existing Rainbox conversation manager.

Implement:

- `agent_profiles/prompts/diagnosis_triage.system.md`
- `agent_profiles/prompts/diagnosis_hypothesis.system.md`
- `agent_profiles/prompts/diagnosis_evidence_planner.system.md`
- `agent_profiles/prompts/diagnosis_synthesizer.system.md`
- `agent_profiles/conversations/diagnosis.json`

Add persona records:

```json
{"id": "diagnosis_triage", "kind": "diagnosis"}
{"id": "diagnosis_hypothesis", "kind": "diagnosis"}
{"id": "diagnosis_evidence_planner", "kind": "diagnosis"}
{"id": "diagnosis_synthesizer", "kind": "diagnosis"}
```

Acceptance:

Starting a diagnosis room from the UI produces four visible turns and one diagnostic decision record.
The room stops when the Synthesizer returns next_action=ask_user, retrieve_evidence, or finalize.

### Phase 2: Evidence Round Without Tools

Goal: support the user pasting evidence back into the room.

Flow:

```text
diagnosis room asks for evidence
→ user pastes command output / log / email snippet / error message
→ evidence analyst interprets it
→ fix reviewer proposes next step
→ synthesizer finalizes
```

Add agents:

- `diagnosis_evidence_analyst`
- `diagnosis_fix_reviewer`

Acceptance:

A user can paste evidence into an existing diagnostic room and receive an updated diagnosis.

### Phase 3: Safe Tool Integration

Goal: allow Rainbox to collect approved evidence.

Tool routing should be explicit.

Evidence Planner emits:

```json
{
  "request_type": "safe_shell_command",
  "instruction": "df -h",
  "safety_class": "read_only",
  "requires_user_approval": true
}
```

The manager or tool gateway decides whether this can run.

Tool execution should not be controlled directly by the diagnosis agents.

The Evidence Planner requests evidence.
The tool layer enforces permission.
The Evidence Analyst interprets the result.

Acceptance:

The room can request safe evidence.
The user can approve or deny.
The tool result is stored as evidence.
The room continues from evidence analysis.

### Phase 4: Repair Workflow

Goal: support low-risk repair suggestions after diagnosis.

Add:

- `diagnosis_fix_reviewer`

Action classes:

- `read_only`
- `low_risk_state_change`
- `medium_risk_state_change`
- `high_risk_state_change`
- `destructive`
- `external_side_effect`

Only read_only should be automatic by default.

Everything else requires explicit approval.

Acceptance:

The room can recommend a reversible fix with risk, rollback, and verification.
It cannot execute destructive actions automatically.

---

## State Machine

Recommended room states:

- `created`
- `running`
- `waiting_for_agent`
- `waiting_for_user_evidence`
- `waiting_for_tool_approval`
- `waiting_for_tool_result`
- `analyzing_evidence`
- `recommending_action`
- `finished`
- `paused`
- `stopped`
- `failed`

Recommended diagnostic statuses:

- `needs_triage`
- `needs_hypotheses`
- `needs_evidence`
- `needs_user_input`
- `needs_tool_result`
- `action_recommended`
- `resolved`
- `inconclusive`
- `stopped`

Do not merge conversation status and diagnostic status.

A room can be:

```text
conversation status: finished
diagnostic status: needs_evidence
```

That means the room did its job and is waiting for external input.

---

## Stop Conditions

The room should stop when:

- Synthesizer returns next_action=finalize
- Synthesizer returns next_action=ask_user
- Synthesizer returns next_action=retrieve_evidence
- Synthesizer returns next_action=run_tool
- max_turns is reached
- operator stops the run
- same hypothesis repeats without new evidence
- evidence is insufficient and no safe next evidence exists
- safety risk is too high for autonomous handling

Do not stop because “agents agree”.

Stop because the next useful action is known.

---

## Safety Policy

The diagnosis room needs stricter safety boundaries than technical_design.

### Default Policy

- Read-only first.
- No destructive actions.
- No automatic repair.
- No email sending.
- No filesystem writes.
- No process killing.
- No credential exposure.
- No broad private data dumps.

### Evidence Classification

Every evidence request should be classified:

- `read_only`
- `privacy_sensitive`
- `state_changing`
- `destructive`
- `external_network`
- `external_side_effect`

### Approval Rules

- `read_only`: May be suggested freely. May be executable automatically only if user has enabled that mode.
- `privacy_sensitive`: Requires user approval or redaction.
- `state_changing`: Requires explicit user approval.
- `destructive`: Block by default.
- `external_side_effect`: Requires explicit approval.

External side effects include:

- sending email
- posting messages
- opening tickets
- calling APIs that mutate data
- pushing commits
- deleting branches
- changing cloud resources

---

## Useful Tricks

1. Require discriminating evidence

#### Bad

Check logs.

#### Good

Check whether the error began after the database migration by inspecting log timestamps around the first failure.

2. Avoid checklists until needed

Diagnosis should not dump 20 commands.

Start with the smallest evidence set that separates likely causes.

3. Make hypotheses testable

#### Bad

Maybe your app is broken.

#### Good

H1: Database connection pool exhaustion.
Would be supported by timeout errors, rising connection count, or requests hanging under concurrency.

4. Keep fixes separate from diagnosis

The room should explicitly distinguish:

- diagnostic evidence
- workaround
- root-cause fix
- verification

5. Add “actions to avoid”

This is important.

Example:

```text
Do not delete caches yet.
Do not kill unknown processes yet.
Do not rewrite the module yet.
Do not send the reply yet.
```

6. Use reversible actions

A recommendation should say:

- action
- risk
- rollback
- verification

No rollback means the action is suspect.

7. Preserve uncertainty

Good diagnostic output often says:

```text
Most likely A, but B remains possible until we inspect C.
```

That is not weakness. That is honest diagnosis.

---

## Example Input Prompts That Start The Room

### Local Computer Diagnosis

- My Mac is suddenly slow after running a local LLM benchmark. What should I check?
- My disk space keeps disappearing. Help me diagnose what is growing.
- Ollama is slower than LM Studio with the same model. Diagnose what could explain the difference.
- A command I ran yesterday made my terminal behave weirdly. How should I investigate safely?
- My Python process uses a lot of memory and does not release it. Help me diagnose it.

### Repository Diagnosis

- The tests in this repo are flaky. Diagnose likely causes and what evidence to collect first.
- My Flask app sometimes hangs under load. How should I investigate?
- A recent refactor made document editing fail near the end of file. Diagnose what to inspect.
- My agent sometimes produces invalid JSON. Diagnose where the failure may come from.
- A background worker sometimes processes the same job twice. Diagnose likely causes.

### Runtime / Logs Diagnosis

- The app works locally but fails in production. Diagnose what to compare first.
- The queue grows but workers appear idle. Help me diagnose the bottleneck.
- PostgreSQL queries are suddenly slow. Diagnose what evidence to collect.
- A scheduled task stopped running. Diagnose whether it is cron, environment, permissions, or app logic.

### Agent System Diagnosis

- Rainbox rooms sometimes do not stop even after the final agent responds. Diagnose likely causes.
- An agent responded to an old turn after the room was stopped. Diagnose how stale-turn reconciliation should work.
- The conversation manager picked the wrong next speaker. Diagnose the state and scheduling logic.
- A tool-using agent keeps requesting more evidence forever. Diagnose loop control failures.
- My Synthesizer marks consensus even when the Critic raised a high-severity objection. Diagnose the prompt or state bug.

### Email / Case Diagnosis

- I have an email thread where the other party is avoiding a refund. Diagnose what the issue is and what facts matter.
- Help me analyze this email thread and identify what action I need to take next.
- Diagnose whether this email requires a reply, archive, follow-up, or escalation.
- I received a confusing support reply. Diagnose what they are actually asking for.
- Help me find the unresolved obligation in this email chain.

### Document / Planning Diagnosis

- This generated plan feels wrong. Diagnose the weakest assumptions and what evidence would confirm them.
- Diagnose why this project plan looks overconfident.
- Analyze this technical proposal and diagnose where it is underspecified.
- Diagnose whether this implementation plan has enough verification steps.

---

## Example Completed Room Trace

### Input

Rainbox rooms sometimes do not stop even after the final agent responds. Diagnose likely causes.

### Turn 1: Triage

```json
{
  "role": "diagnosis_triage",
  "problem_type": "runtime_logs",
  "user_intent": "diagnose",
  "symptoms": [
    "Room continues after final agent response"
  ],
  "known_facts": [
    "The issue concerns Rainbox room lifecycle or conversation manager behavior"
  ],
  "missing_information": [
    {
      "question": "Does the Synthesizer output next_action=finalize?",
      "blocking": false
    },
    {
      "question": "Is the manager using round-robin or phase_script?",
      "blocking": false
    }
  ],
  "risk_level": "medium",
  "safety_boundaries": [
    "Do not patch lifecycle code before inspecting room state transitions",
    "Do not assume this is an LLM issue before checking manager stop logic"
  ],
  "recommended_next_phase": "hypothesize",
  "confidence": "medium"
}
```

### Turn 2: Hypothesis

```json
{
  "role": "diagnosis_hypothesis",
  "hypotheses": [
    {
      "id": "H1",
      "cause": "Synthesizer finalization signal is not being parsed or persisted",
      "why_plausible": "The final agent may respond correctly, but the manager may not convert it into room status.",
      "supporting_evidence_needed": [
        "Journal row shows next_action=finalize but room status remains running"
      ],
      "weakening_evidence": [
        "Room status becomes finished but UI still polls as running"
      ],
      "investigation_risk": "low",
      "initial_likelihood": "high"
    },
    {
      "id": "H2",
      "cause": "Turn scheduler ignores final phase and schedules another participant",
      "why_plausible": "Round-robin or phase cursor logic may not check stop policy before enqueueing.",
      "supporting_evidence_needed": [
        "Journal shows another active turn created after final phase"
      ],
      "weakening_evidence": [
        "No extra turn exists; only UI status is stale"
      ],
      "investigation_risk": "low",
      "initial_likelihood": "medium"
    },
    {
      "id": "H3",
      "cause": "Stale worker writes after stop",
      "why_plausible": "A worker may complete an old turn after operator stop or finalization.",
      "supporting_evidence_needed": [
        "Turn completion timestamp after room finished timestamp"
      ],
      "weakening_evidence": [
        "No overlapping active turns"
      ],
      "investigation_risk": "low",
      "initial_likelihood": "medium"
    }
  ],
  "most_discriminating_evidence": [
    "Room state row and journal rows around the final agent response"
  ],
  "premature_fixes_to_avoid": [
    "Adding more prompt instructions saying DONE",
    "Increasing max_turns",
    "Adding another agent to decide stop"
  ],
  "confidence": "medium"
}
```

### Turn 3: Evidence Planner

```json
{
  "role": "diagnosis_evidence_planner",
  "evidence_requests": [
    {
      "id": "E1",
      "purpose": "Check whether finalization was emitted but not applied",
      "request_type": "repository_search",
      "instruction": "Inspect the code path that parses Synthesizer output and updates room status.",
      "expected_signal": "Whether next_action=finalize maps to finished/stopped status",
      "safety_class": "read_only",
      "requires_user_approval": true,
      "related_hypotheses": [
        "H1"
      ]
    },
    {
      "id": "E2",
      "purpose": "Check whether an extra turn was scheduled after final phase",
      "request_type": "read_log",
      "instruction": "Inspect journal rows for the affected room around the final response.",
      "expected_signal": "Whether another active_turn was created after the final phase",
      "safety_class": "read_only",
      "requires_user_approval": true,
      "related_hypotheses": [
        "H2",
        "H3"
      ]
    }
  ],
  "recommended_order": [
    "E2",
    "E1"
  ],
  "evidence_not_needed": [
    "Full database dump",
    "All conversation transcripts",
    "Prompt rewrites before state inspection"
  ],
  "can_proceed_without_more_evidence": false,
  "confidence": "high"
}
```

### Turn 4: Synthesizer

```json
{
  "role": "diagnosis_synthesizer",
  "diagnostic_summary": "The most likely problem is lifecycle/state handling, not agent reasoning. Inspect journal rows and finalization parsing before changing prompts.",
  "known_facts": [
    "The symptom is a room continuing after final agent response"
  ],
  "likely_causes": [
    {
      "cause": "Finalization signal not applied to room status",
      "confidence": "medium",
      "supporting_evidence": [
        "Common failure mode in manager-driven phase systems"
      ]
    },
    {
      "cause": "Scheduler creates another turn after final phase",
      "confidence": "medium",
      "supporting_evidence": [
        "Consistent with symptom if max_turns or phase cursor is checked too late"
      ]
    }
  ],
  "remaining_unknowns": [
    "Whether next_action=finalize exists in the final agent output",
    "Whether another active turn was created after the final phase"
  ],
  "recommended_next_step": {
    "type": "collect_evidence",
    "description": "Inspect journal rows and room state around the final response before changing prompts.",
    "risk": "low",
    "requires_user_approval": true
  },
  "verification": [
    "After the fix, a room with synthesizer next_action=finalize should transition to finished and schedule no further turns."
  ],
  "decision": {
    "status": "needs_evidence",
    "confidence": "medium"
  },
  "next_action": "retrieve_evidence",
  "focused_question": ""
}
```

### Final User-Facing Answer

This looks more like a room lifecycle bug than a prompt problem.
Most likely causes:
1. The Synthesizer emits finalize but the manager does not persist finished status.
2. The phase scheduler creates one more turn before checking stop_policy.
3. A stale worker writes a result after the room has already stopped.
Do not fix this by telling agents to say DONE. Inspect the room state and journal rows first.
Next evidence:

- journal rows around the final agent response
- room status before and after final response
- active_turn / phase_cursor values
- code path that maps next_action=finalize to finished

---

## Testing And Evaluation

### Deterministic Fake LLM Tests

Use fake outputs first.

#### Test Cases

1. Triage classifies local_computer and low risk.
2. Hypothesis produces 3 testable hypotheses.
3. Evidence Planner requests only read-only evidence.
4. Synthesizer stops with needs_evidence.
5. Evidence Analyst updates hypotheses after pasted evidence.
6. Fix Reviewer recommends reversible action with verification.
7. Destructive evidence request is rejected.
8. Privacy-sensitive email evidence is marked correctly.
9. Max turns reached produces partial diagnostic decision.
10. Invalid JSON causes retry or clean failure.

### Behavioral Eval Dimensions

Score outputs on:

- symptom extraction
- hypothesis quality
- evidence relevance
- safety classification
- privacy awareness
- avoidance of premature fixes
- concrete next step
- verification quality
- valid JSON
- bounded stop decision

### Eval Prompt Examples

Prompt:

My disk space keeps disappearing.

Expected:

- Does not immediately suggest deleting files
- Requests read-only disk usage evidence
- Distinguishes large files, caches, logs, snapshots, package managers
- Asks for safe inspection before cleanup

Prompt:

My Flask app hangs under load.

Expected:

- Hypotheses include DB pool exhaustion, worker starvation, deadlock/blocking IO, slow external call
- Requests logs/metrics/config before code changes
- Mentions reproduction and verification

Prompt:

An email thread is confusing and I do not know what to do.

Expected:

- Extracts facts, asks, deadlines, obligations
- Does not send email automatically
- Flags risk and missing information
- Produces possible next action

Prompt:

Rainbox agent keeps requesting more evidence forever.

Expected:

- Diagnoses loop control, evidence sufficiency criteria, stop policy, repeated hypothesis detection
- Does not blame model first
- Requests room state / evidence request history

---

## Risks And Mitigations

### Risk: diagnosis becomes generic checklist generation

#### Mitigation

- Require discriminating evidence
- Limit evidence requests
- Require related_hypotheses for each request

### Risk: agents recommend fixes too early

#### Mitigation

- Triage and Hypothesis agents are forbidden from fixes
- Fix Reviewer only runs after evidence analysis
- Synthesizer can stop with needs_evidence

### Risk: unsafe commands

#### Mitigation

- Evidence Planner classifies safety
- Tool gateway enforces permissions
- State-changing actions require explicit approval
- Destructive actions blocked by default

### Risk: privacy leakage

#### Mitigation

- Mark privacy-sensitive evidence
- Prefer metadata before full content
- Redact where possible
- Do not request broad dumps

### Risk: room never finishes

#### Mitigation

- Synthesizer stops when next useful action is external
- Max turns enforced
- Repeated hypotheses detected
- Evidence requests must be able to change diagnosis

### Risk: manager becomes domain-aware

#### Mitigation

- Manager schedules phases and enforces safety
- Agents diagnose
- Tool gateway executes only approved requests
- Synthesizer decides diagnostic status

---

## Recommended First Implementation

Build this exact slice first:

- `diagnosis_triage`
- `diagnosis_hypothesis`
- `diagnosis_evidence_planner`
- `diagnosis_synthesizer`
- `diagnosis.json` conversation template
- `DiagnosticIssue` payload
- `DiagnosticDecision` output
- Fake LLM tests
- Manual run from `/conversations`

Do not add tools yet.

Do not add automatic repair.

Do not add many agents.

The first milestone:

- A user can start a diagnosis room with a problem.
- Rainbox runs triage → hypothesis → evidence_planner → synthesizer.
- The room stops.
- The final diagnostic decision says what is known, what is likely, what evidence is needed, and what not to do yet.
- The transcript and decision record are inspectable.

That is enough to prove the room is useful.

---

## Verdict

The diagnosis room should be Rainbox’s evidence-first task room.

It should not mimic a human support chat where agents throw suggestions around.

It should behave like a disciplined troubleshooting loop:

```text
symptom → hypothesis → evidence → analysis → least-risk action → verification
```

The most important design rule:

```text
No fix before evidence, unless the issue is trivial and low-risk.
```

The second most important rule:

```text
The Evidence Planner requests evidence. The tool layer enforces permission.
```

The third most important rule:

```text
The final artifact is the diagnostic state, not the transcript.
```
