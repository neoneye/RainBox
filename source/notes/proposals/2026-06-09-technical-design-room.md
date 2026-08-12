# Proposal: technical_design room

**Status:** Draft design proposal
**Scope:** General-purpose technical design deliberation room for Rainbox
**Purpose:** Let multiple AI agents debate, refine, and converge on implementation designs for programming, systems, architecture, debugging strategy, and other technical problems.

## Summary

The technical_design room should be a bounded Rainbox conversation template where agents collaborate around a specific technical design question.

It should not be specific to atomic clocks, backend engineering, Python, or any one domain. Instead, the room should use a stable deliberation protocol with injectable domain context.

The core idea:

```text
User asks a technical design question
→ Proposer creates a concrete design
→ Critic attacks the design
→ Proposer revises
→ Synthesizer extracts the decision
→ Room stops, asks for evidence, or runs one focused extra round
```

This room is not “agents chatting until they agree.” It is a structured design review loop.

The important distinction:

```text
Conversation runtime:
  who speaks next, routing, journal rows, max turns, stop/resume/reconcile

Technical design protocol:
  what problem is being solved, what design was proposed, what objections exist,
  what tradeoffs remain, and what recommendation should be given to the user
```

Rainbox already has the conversation runtime direction: personas, templates, bounded turns, dynamic return addresses, journal provenance, operator controls, and restart/reconcile behavior.

The missing part is the task protocol for technical design.

This document proposes that protocol.

---

## Design Goals

The technical_design room should:

1. Produce useful implementation guidance.
2. Avoid vague “architecture astronaut” answers.
3. Preserve important dissent instead of forcing fake consensus.
4. Work across domains using placeholder-injected domain context.
5. Keep the conversation bounded.
6. Prefer small prototypes and reversible steps.
7. Separate facts, assumptions, risks, and recommendations.
8. Make the final answer useful even when agents disagree.
9. Support later tool use: repository inspection, file reading, benchmarks, docs lookup, or shell diagnostics.
10. Stay compatible with Rainbox’s existing conversation manager model.

---

## Non-Goals

The first version should not try to be a full autonomous software engineer.

Non-goals:

- No automatic code changes.
- No direct filesystem modification.
- No unbounded debate.
- No swarm.
- No majority vote as a substitute for correctness.
- No hidden consensus.
- No tool calls inside the first walking skeleton.
- No attempt to solve every technical domain equally well without injected context.

The first useful version should answer:

Given this technical problem, what is a good implementation approach,
what are the risks, and what should be tried first?

---

## Core Work Object: `DesignIssue`

The room should gather around a named design issue, not just a chat message.

Minimal record:

```json
{
  "issue_id": "design_issue_123",
  "room_type": "technical_design",
  "user_request": "How should I implement append-only document patch history?",
  "domain_context": "Python, Flask, PostgreSQL, local-first development, inspectable systems",
  "known_constraints": [
    "Prefer simple implementation first",
    "Avoid React/TypeScript unless already required",
    "Do not mutate user files without explicit approval"
  ],
  "status": "under_review",
  "turn_policy": "propose_critique_revise_synthesize",
  "max_rounds": 2
}
```

The transcript is valuable for auditability, but the useful product is the design decision record.

---

## Recommended First Template

Use exactly three functional agents:

- `technical_design_proposer`
- `technical_design_critic`
- `technical_design_synthesizer`

Optional later agents:

- `technical_design_implementer`
- `technical_design_evidence_checker`
- `technical_design_constraint_keeper`
- `technical_design_test_designer`

Do not start with the optional agents. They will make the first version noisy.

The smallest good loop:

1. Proposer creates a design
2. Critic reviews it
3. Proposer revises it
4. Synthesizer emits decision

Possible extra loop:

5. If needed, Synthesizer asks one focused debate question
6. Proposer and Critic answer only that question
7. Synthesizer finalizes

Hard rule:

No open-ended "continue the discussion" turns.

If another round is needed, it must have a narrow question.

Example:

Focused debate question:

```text
Should this use append-only event storage or mutable current-state rows?
```

---

## Shared Prompt Placeholders

All agents should receive the same structured context.

```text
{{ROOM_GOAL}}
{{USER_REQUEST}}
{{DOMAIN_CONTEXT}}
{{KNOWN_CONSTRAINTS}}
{{AVAILABLE_TOOLS}}
{{CURRENT_STATE}}
{{OTHER_AGENT_MESSAGES}}
{{OUTPUT_SCHEMA}}
```

### Recommended Meaning

#### `{{ROOM_GOAL}}`

The task being performed by the room.

Example:

```text
Produce a practical implementation design for the user's technical problem.
Identify assumptions, risks, tradeoffs, smallest useful prototype, and final recommendation.
```

#### `{{USER_REQUEST}}`

The original user prompt that started the room.

Example:

```text
How should I implement a multi-agent technical design room in Rainbox?
```

#### `{{DOMAIN_CONTEXT}}`

Injectable domain knowledge.

Example:

```text
Domain: backend software engineering
Preferred stack: Python, Flask, PostgreSQL, SQLite for local prototypes
Style preference: simple, inspectable, testable, minimal dependencies
Avoid: React/TypeScript unless the existing codebase already uses it
```

Another example:

```text
Domain: command execution for LLM agents
Known concerns: shell injection, destructive commands, audit logs, subprocess isolation,
permission boundaries, command allow/maybe/block classification
```

#### `{{KNOWN_CONSTRAINTS}}`

Hard or soft constraints.

Example:

- Must fit existing Rainbox conversation manager design
- Must use file-backed persona prompts first
- Must preserve auditability in journal rows
- Must stop after bounded turns
- Must not require Postgres schema changes in the first prototype unless necessary

#### `{{AVAILABLE_TOOLS}}`

Useful later. Initially:

No tools available. Reason from the provided context only.

Later:

Available tools:

- read repository files
- search uploaded documents
- run safe read-only shell commands
- inspect database schema
- search official documentation

#### `{{CURRENT_STATE}}`

The durable state of the design discussion.

Example:

```json
{
  "round": 1,
  "proposal": null,
  "critic_objections": [],
  "resolved_changes": [],
  "open_questions": [],
  "decision": null
}
```

---

## Agent 1: `technical_design_proposer`

### Responsibility

The Proposer creates the initial technical design and later revises it after criticism.

The Proposer should optimize for:

- clarity
- simplicity
- feasibility
- testability
- incremental delivery
- explicit assumptions
- minimal useful prototype

It should not optimize for sounding impressive.

### System Prompt

```text
You are the Proposer agent in a Rainbox technical_design room.
Your job is to produce a concrete technical proposal for the user's request.
You are not a cheerleader. You must identify a practical design that could actually be implemented.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Prefer simple, inspectable designs over clever designs.
2. State assumptions explicitly.
3. Separate facts from guesses.
4. Do not pretend certainty where the domain is unclear.
5. Do not overfit to one technology unless the domain context requires it.
6. Prefer designs that can be tested incrementally.
7. Mention the smallest useful prototype.
8. Mention failure modes and operational risks.
9. Avoid vague advice.
10. Do not ask the user questions unless missing information blocks useful progress.
11. When information is missing but not blocking, make a reasonable assumption and label it.
12. If reviewing a previous critique, revise the proposal directly instead of defending it rhetorically.
13. Do not ignore high-severity objections.
```

### Output JSON Only

```json
{
  "role": "technical_design_proposer",
  "summary": "One paragraph summary of the proposed design.",
  "assumptions": [
    "Assumption 1"
  ],
  "proposal": {
    "architecture": [
      "Major component or step"
    ],
    "data_model": [
      "Relevant data structure, schema, state, message, or file format"
    ],
    "control_flow": [
      "Step-by-step runtime behavior"
    ],
    "interfaces": [
      "APIs, files, commands, protocols, or contracts"
    ]
  },
  "smallest_useful_prototype": [
    "Step 1",
    "Step 2"
  ],
  "tests": [
    "Test or validation to run"
  ],
  "risks": [
    {
      "risk": "What can go wrong",
      "severity": "low|medium|high",
      "mitigation": "How to reduce the risk"
    }
  ],
  "open_questions": [
    {
      "question": "Question",
      "blocking": true
    }
  ],
  "confidence": "low|medium|high"
}
```

---

## Agent 2: `technical_design_critic`

### Responsibility

The Critic attacks the proposal.

The Critic should optimize for:

- finding hidden assumptions
- identifying under-specified interfaces
- spotting unnecessary complexity
- revealing operational failures
- challenging weak abstractions
- demanding tests and observability
- preventing premature consensus

The Critic should not become a second proposer too early.

### System Prompt

```text
You are the Critic agent in a Rainbox technical_design room.
Your job is to find flaws, hidden assumptions, missing constraints, bad abstractions,
weak interfaces, implementation traps, and operational risks in the current proposal.
You are not trying to be polite. You are trying to prevent bad engineering decisions.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Proposal to review: {{PROPOSER_OUTPUT}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Attack the design, not the author.
2. Prefer specific objections over broad skepticism.
3. Identify where the proposal is underspecified.
4. Identify where the design may fail in real use.
5. Identify unnecessary complexity.
6. Identify missing tests, observability, rollback, and safety mechanisms.
7. Identify claims that need evidence.
8. Do not invent requirements that are not implied by the task.
9. Do not force consensus.
10. If the proposal is mostly good, say so, but still identify the sharpest remaining risks.
11. Do not propose a full replacement design unless the current proposal is structurally wrong.
12. Prefer actionable fixes over abstract criticism.
```

### Output JSON Only

```json
{
  "role": "technical_design_critic",
  "overall_assessment": "Short assessment.",
  "strong_points": [
    "What the proposal gets right"
  ],
  "objections": [
    {
      "issue": "Specific issue",
      "severity": "low|medium|high",
      "why_it_matters": "Concrete consequence",
      "suggested_fix": "Concrete fix"
    }
  ],
  "bad_assumptions": [
    {
      "assumption": "Assumption being challenged",
      "reason": "Why it may be wrong",
      "impact": "What breaks if wrong"
    }
  ],
  "missing_tests": [
    "Test or validation missing"
  ],
  "evidence_needed": [
    {
      "claim": "Claim that needs evidence",
      "evidence_type": "docs|benchmark|source_code|local_inspection|user_confirmation"
    }
  ],
  "verdict": "accept|revise|reject",
  "confidence": "low|medium|high"
}
```

---

## Agent 3: `technical_design_synthesizer`

### Responsibility

The Synthesizer reduces the conversation into a decision.

It is not a summarizer only. It decides whether the room has produced something useful.

The Synthesizer should answer:

What did the agents agree on?
What changed because of criticism?
What risks remain?
What should the user do next?
Should the room stop?

### System Prompt

```text
You are the Synthesizer agent in a Rainbox technical_design room.
Your job is to reduce the agent conversation into a useful engineering decision.
You do not merely summarize. You decide what is agreed, what remains disputed,
what should be changed, and whether another round is needed.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Extract the actual technical decision, not every detail.
2. Separate consensus from unresolved disagreement.
3. Preserve important dissent instead of forcing fake agreement.
4. Prefer actionable next steps.
5. Stop the debate if another round is unlikely to improve the answer.
6. Request another round only for a specific issue.
7. Mark assumptions clearly.
8. Mark evidence gaps clearly.
9. Produce a final answer shape suitable for the user.
10. Be concise but not shallow.
11. If the agents agree only because the critique was weak, do not mark strong consensus.
12. If implementation should proceed, identify the smallest useful next step.
```

### Output JSON Only

```json
{
  "role": "technical_design_synthesizer",
  "consensus": [
    "Point the agents agree on"
  ],
  "resolved_changes": [
    {
      "change": "Design change to make",
      "reason": "Why"
    }
  ],
  "bounded_disagreements": [
    {
      "issue": "Disagreement",
      "positions": [
        "Position A",
        "Position B"
      ],
      "recommended_handling": "How the final answer should present this"
    }
  ],
  "remaining_risks": [
    {
      "risk": "Risk",
      "severity": "low|medium|high",
      "owner": "proposer|critic|user|unknown"
    }
  ],
  "decision": {
    "status": "accepted|accepted_with_risks|needs_revision|needs_evidence|needs_user_input|rejected",
    "confidence": "low|medium|high",
    "recommended_next_step": "Concrete next action"
  },
  "next_action": "finalize|revise_proposal|focused_debate|ask_user|retrieve_evidence",
  "focused_question": "If next_action is focused_debate, the one issue to debate.",
  "final_answer_outline": [
    "Section or point to include"
  ]
}
```

---

## Optional Later Agent: `technical_design_implementer`

Add this only after the basic room works.

### Responsibility

The Implementer turns an accepted design into implementation tasks.

It should not decide whether the design is correct. It should make the design buildable.

### System Prompt

```text
You are the Implementer agent in a Rainbox technical_design room.
Your job is to turn an accepted or nearly accepted technical design into concrete implementation steps.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Accepted design or current proposal: {{CURRENT_DESIGN}}
- Critic objections: {{CRITIC_OUTPUT}}
- Synthesizer decision: {{SYNTHESIZER_OUTPUT}}
Rules:
1. Do not reopen settled design arguments unless implementation exposes a blocker.
2. Produce small, ordered steps.
3. Identify files, modules, schemas, APIs, tests, and migration needs where possible.
4. Prefer reversible changes.
5. Include rollback or recovery if the change touches persistent state.
6. Do not invent codebase details not provided in context.
7. Mark unknown files or modules as placeholders.
```

### Output JSON Only

```json
{
  "role": "technical_design_implementer",
  "implementation_plan": [
    {
      "step": "Step name",
      "description": "What to do",
      "files_or_modules": [
        "File or module placeholder"
      ],
      "tests": [
        "Test to add or run"
      ],
      "risk": "low|medium|high"
    }
  ],
  "data_migrations": [
    "Migration or schema change"
  ],
  "rollback_plan": [
    "Rollback step"
  ],
  "definition_of_done": [
    "Observable completion criterion"
  ]
}
```

---

## Optional Later Agent: `technical_design_evidence_checker`

Useful when the proposal makes claims about libraries, APIs, protocols, performance, security, model behavior, or external systems.

### System Prompt

```text
You are the Evidence Checker agent in a Rainbox technical_design room.
Your job is to identify which claims need evidence before the design should be trusted.
You do not need to retrieve evidence unless tools are available. Your primary job is to classify evidence needs.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current proposal: {{PROPOSER_OUTPUT}}
- Critique: {{CRITIC_OUTPUT}}
Rules:
1. Identify claims that are factual, version-sensitive, benchmark-sensitive, security-sensitive, or API-sensitive.
2. Do not demand evidence for ordinary engineering judgment unless the risk is material.
3. Prefer official documentation, source code, local inspection, and benchmarks over blog posts.
4. If tools are unavailable, say what evidence should be collected.
5. If evidence is not required, say so.
```

### Output JSON Only

```json
{
  "role": "technical_design_evidence_checker",
  "evidence_required": true,
  "claims": [
    {
      "claim": "Claim needing evidence",
      "reason": "Why evidence is needed",
      "evidence_type": "official_docs|source_code|benchmark|local_command|user_confirmation|uploaded_file",
      "priority": "low|medium|high"
    }
  ],
  "can_finalize_without_evidence": true,
  "notes": [
    "Additional note"
  ]
}
```

---

## Suggested Conversation Template File

Example agent_profiles/conversations/technical-design.json:

```json
{
  "id": "technical_design",
  "title": "Technical Design Review",
  "description": "Bounded multi-agent design review for technical implementation questions.",
  "room_type": "technical_design",
  "participants": [
    {
      "persona_id": "technical_design_proposer",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "technical_design_critic",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "technical_design_synthesizer",
      "agent_kind": "chat_structured",
      "required": true
    }
  ],
  "turn_policy": {
    "type": "phase_script",
    "phases": [
      {
        "speaker": "technical_design_proposer",
        "purpose": "initial_proposal"
      },
      {
        "speaker": "technical_design_critic",
        "purpose": "critique"
      },
      {
        "speaker": "technical_design_proposer",
        "purpose": "revision"
      },
      {
        "speaker": "technical_design_synthesizer",
        "purpose": "decision"
      }
    ]
  },
  "bounds": {
    "min_turns": 4,
    "max_turns": 6,
    "max_rounds": 2
  },
  "stop_policy": {
    "stop_on_synthesizer_finalize": true,
    "stop_on_max_turns": true,
    "allow_focused_debate": true,
    "focused_debate_max_turns": 2
  },
  "context_policy": {
    "last_visible_turns": 8,
    "include_runtime_preamble": true,
    "include_summary": true
  },
  "output_policy": {
    "final_artifact_kind": "technical_design_decision",
    "preserve_dissent": true
  }
}
```

The important part is phase_script.

Round-robin is fine for demo personas. It is not ideal for real technical design. Technical design has a natural sequence:

propose → critique → revise → synthesize

Use that.

---

## Durable Decision Record

The final output of the room should be a structured record.

```json
{
  "design_issue_id": "design_issue_123",
  "decision": "accepted_with_risks",
  "confidence": "medium",
  "summary": "Use a file-backed technical_design conversation template with Proposer, Critic, and Synthesizer agents. Keep the manager mechanical and encode the design-review logic in the template and prompts.",
  "assumptions": [
    "The first implementation does not need tools.",
    "Structured JSON output is available for these agents."
  ],
  "recommended_design": {
    "components": [
      "conversation template",
      "three functional personas",
      "DesignIssue state record",
      "decision record",
      "final answer renderer"
    ],
    "control_flow": [
      "Proposer creates initial design",
      "Critic reviews",
      "Proposer revises",
      "Synthesizer decides"
    ]
  },
  "remaining_risks": [
    {
      "risk": "Agents may produce valid JSON but weak criticism.",
      "severity": "medium",
      "mitigation": "Add behavioral evals with intentionally flawed proposals."
    }
  ],
  "bounded_dissent": [],
  "next_step": "Implement the template with fake LLM tests before using real models."
}
```

This should be the artifact trusted by later systems, not the raw transcript.

---

## Final Answer Renderer

After the room finishes, a final writer can render the decision record for the user.

### Recommended Sections

1. Recommended design
2. Why this design
3. Smallest useful prototype
4. Risks and mitigations
5. Open questions
6. What to do next
7. Dissent / alternatives

This writer can be deterministic code at first. Do not make another agent unless needed.

---

## Implementation Plan

### Phase 0: Static Prompt Experiment

Goal: prove the room produces useful output outside the Rainbox runtime.

Implement:

- Three prompt files:
  - technical_design_proposer.system.md
  - technical_design_critic.system.md
  - technical_design_synthesizer.system.md
- One Python script that runs:
  - proposer
  - critic
  - proposer revision
  - synthesizer
- Use fake or manually pasted outputs first.
- Validate JSON with Pydantic.
- Save all turns to a local .jsonl transcript.

Acceptance:

Given 5 technical prompts, the room produces:
- a concrete design
- at least one useful objection
- a revised design
- a final decision record

### Phase 1: Rainbox Conversation Template

Goal: run technical_design inside the existing bounded conversation runtime.

Implement:

- Add personas to agent_profiles/personas.jsonl.
- Add prompt files under agent_profiles/prompts/.
- Add agent_profiles/conversations/technical-design.json.
- Use phase_script instead of round-robin.
- Add DesignIssue payload in the manager-created first turn.
- Store final synthesizer output as a decision artifact.

Acceptance:

Starting a technical_design run from the UI produces four visible turns and one final decision record.
The room stops automatically after the synthesizer returns next_action=finalize.

### Phase 2: Focused Debate

Goal: support one additional narrow debate when the Synthesizer requests it.

Implement:

- Synthesizer may return:
  - next_action = "focused_debate"
  - focused_question = "..."
- Manager appends a focused debate phase:
  - proposer answers focused question
  - critic responds
  - synthesizer finalizes
- Hard cap: one focused debate extension.

Acceptance:

The room can continue only when the Synthesizer emits a specific focused_question.
The room cannot continue with vague "discuss more" instructions.

### Phase 3: Evidence And Tool Integration

Goal: allow the room to request evidence instead of guessing.

Implement:

- Add optional technical_design_evidence_checker.
- Let Synthesizer emit:
  - next_action = "retrieve_evidence"
- Evidence type classification:
  - official_docs
  - source_code
  - benchmark
  - local_command
  - uploaded_file
  - user_confirmation
- Route evidence requests to appropriate Rainbox tool agents.

Acceptance:

If a design depends on version-sensitive or source-code-sensitive claims,
the room records that evidence is needed instead of pretending certainty.

### Phase 4: Implementation-Plan Expansion

Goal: turn accepted designs into actionable implementation plans.

Implement:

- Add optional technical_design_implementer.
- Input: final decision record.
- Output:
  - files/modules to change
  - schemas
  - tests
  - rollout
  - rollback
  - definition of done

Acceptance:

For a programming prompt, the room can produce a concrete implementation plan
without automatically editing files.

---

## State Machine

Recommended room states:

- `created`
- `running`
- `waiting_for_agent`
- `waiting_for_evidence`
- `waiting_for_user`
- `synthesizing`
- `finished`
- `paused`
- `stopped`
- `failed`

Recommended design decision statuses:

- `accepted`
- `accepted_with_risks`
- `needs_revision`
- `needs_evidence`
- `needs_user_input`
- `rejected`

Do not overload conversation run status with design decision status. They are separate.

A conversation can be finished while the design decision is needs_user_input.

---

## Stop Conditions

The room should stop when:

- Synthesizer returns next_action=finalize
- Synthesizer returns next_action=ask_user
- Synthesizer returns next_action=retrieve_evidence
- max_turns is reached
- operator stops the run
- same high-level objection repeats without new information
- an agent fails repeatedly

Do not use “all agents agree” as the only stop condition.

Better:

Stop when the Synthesizer has enough structure to produce a useful decision.

---

## Consensus Policy

The technical_design room should use qualified consensus by default.

Meaning:

The design can be accepted if:
- the Proposer has produced a concrete design,
- the Critic has no unresolved high-severity objection,
- the Synthesizer marks remaining disagreement as bounded.

Important:

Consensus does not mean every agent likes the design.
Consensus means the system can responsibly recommend the design while preserving known risks.

Supported consensus states:

- `strong_consensus`
- `qualified_consensus`
- `bounded_disagreement`
- `needs_evidence`
- `needs_user_input`
- `no_consensus`

Example:

```json
{
  "consensus_state": "bounded_disagreement",
  "recommendation": "Use SQLite for the prototype, but design the storage layer so PostgreSQL can replace it later.",
  "dissent": [
    {
      "role": "critic",
      "concern": "SQLite may become a bottleneck if concurrent multi-room writes are required."
    }
  ],
  "tripwire": "Revisit PostgreSQL if concurrent write contention appears in testing."
}
```

---

## Useful Tricks

1. Ask for the smallest useful prototype

Every proposal should answer:

What is the smallest thing we can build to validate this design?

This prevents overengineering.

2. Add tripwires

A design can be simple now if it has a clear trigger for revisiting.

Example:

```text
Use file-backed prompts first.
Tripwire: move prompts to Postgres when multiple users need UI editing or prompt revision history.
```

3. Preserve dissent

The final answer should sometimes say:

```text
Recommended path: A.
Dissent: B may be better if condition X becomes true.
Trigger to revisit: Y.
```

This is much more useful than forced agreement.

4. Critic should not be too creative

A common failure mode is that the Critic invents a completely different system.

The Critic should primarily answer:

What is wrong with this proposal?
What breaks?
What is missing?
What is too complex?
What should be fixed?

5. Use explicit verdicts

The Critic must output:

accept | revise | reject

The Synthesizer must output:

finalize | revise_proposal | focused_debate | ask_user | retrieve_evidence

No verdict means no control signal.

6. Use phase scripts, not generic round-robin

Round-robin creates conversation.

Phase scripts create work.

For technical_design, use:

propose → critique → revise → synthesize

7. Keep the manager boring

The manager should not understand engineering.

The manager should understand:

- current phase
- next speaker
- turn budget
- active turn
- stop flags
- retry/reconcile
- final artifact presence

The Synthesizer understands the design decision.

Do not merge those roles.

8. Use JSON for agent turns, markdown for user output

Agent-to-agent communication should be structured.

User-facing output should be readable markdown.

Do not make the user read raw agent JSON unless debugging.

---

## Example Input Prompts That Start The Room

These are user prompts that should create a technical_design room.

### Rainbox / Agent System Examples

- Design a technical_design room for Rainbox where agents propose, critique, revise, and synthesize implementation plans.
- How should I implement a bounded multi-agent conversation manager that supports pause, resume, stop, and stale-turn reconciliation?
- Design the data model for storing persona prompts, prompt revisions, conversation templates, and conversation runs.
- How should Rainbox decide when an agent-to-agent conversation is done without relying on the agents saying DONE?
- Design a safe way for one Rainbox agent to request evidence from another tool-using agent without creating runaway loops.
- How should I implement phase_script turn scheduling instead of round-robin?
- Design an issue-ledger system where agents debate a specific technical issue and produce a durable decision record.

### Programming Architecture Examples

- How should I implement append-only document history using unified diffs and periodic checkpoints?
- Design a Python module that lets an LLM edit a document by replacing line ranges, while preserving trailing newline behavior.
- How should I structure a Flask app so feature modules register routes without turning main.py into a giant file?
- Design a safe command execution layer for an LLM agent that accepts shell-like syntax but executes argv directly.
- How should I store and query Q&A pairs in PostgreSQL using pgvector and reranking?
- Design a benchmark framework for comparing LLM tool-calling behavior across local models.

### Debugging / Diagnosis Design Examples

- How should I design a room that diagnoses why a local model runner is slower in one app than another?
- Design a safe diagnostic workflow for analysing why a macOS machine is slow, using only read-only commands first.
- How should an agent system inspect logs, classify likely causes, and propose the least invasive fix?

### Email / Case-Handling Adjacent Examples

These are not pure technical_design, but can use the same room if the question is about implementation.

- Design an email-case room where agents extract facts from a thread, identify obligations, draft a response, and review risk.
- How should I implement an inbox triage agent that detects whether an email needs action, a reply, archive, or human review?

### General Technical Design Examples

- Design a plugin system where tools can be discovered, permissioned, invoked, and audited.
- How should I implement a local-first task queue with retries, idempotency, and observability?
- Design a system for comparing multiple implementation alternatives and preserving dissent in the final recommendation.
- How should I implement a cache for expensive LLM calls without accidentally reusing stale or unsafe outputs?

---

## Example Completed Room Trace

### Input

How should I implement phase_script turn scheduling instead of round-robin?

### Turn 1: Proposer

#### Produces

```json
{
  "summary": "Add a phase_script policy to conversation templates where each phase names a speaker and purpose. The manager advances by phase index instead of modulo participant count.",
  "proposal": {
    "architecture": [
      "Extend conversation template schema with turn_policy.type = phase_script",
      "Store current_phase_index on ConversationRun or derived run state",
      "Manager chooses speaker from phases[current_phase_index]",
      "Advance only after terminal journal result for active turn"
    ],
    "data_model": [
      "turn_policy JSON in template",
      "current_phase_index integer",
      "active_turn UUID",
      "final_artifact flag"
    ],
    "control_flow": [
      "Create run",
      "Manager enqueues phase 0 speaker",
      "Speaker completes",
      "Manager validates expected active turn",
      "Manager increments phase index",
      "Stop when final phase completes or Synthesizer finalizes"
    ]
  },
  "smallest_useful_prototype": [
    "Hardcode one technical_design template with four phases",
    "Run with fake LLM outputs",
    "Assert exact speaker order"
  ]
}
```

### Turn 2: Critic

#### Finds

```json
{
  "objections": [
    {
      "issue": "The proposal does not define how focused_debate inserts extra phases.",
      "severity": "medium",
      "suggested_fix": "Represent phase_script as a mutable runtime phase queue copied from the template at run creation."
    },
    {
      "issue": "The proposal risks duplicating turns if manager ticks are retried.",
      "severity": "high",
      "suggested_fix": "Use compare-and-swap on tick_count and active_turn fields."
    }
  ],
  "verdict": "revise"
}
```

### Turn 3: Proposer Revision

#### Updates

```json
{
  "resolved_changes": [
    "Copy template phases into runtime phase_queue at run creation",
    "Use phase_cursor instead of template index only",
    "Focused debate appends two temporary phases before final synthesis",
    "Manager tick must be idempotent using expected_tick_count"
  ]
}
```

### Turn 4: Synthesizer

#### Finalizes

```json
{
  "decision": {
    "status": "accepted_with_risks",
    "recommended_next_step": "Implement phase_queue and phase_cursor with fake LLM tests before using real agents."
  },
  "remaining_risks": [
    {
      "risk": "Schema migration may be premature if phase_queue can live in existing run metadata.",
      "severity": "medium"
    }
  ],
  "next_action": "finalize"
}
```

### Final User-Facing Answer

Recommended design: implement phase_script as a runtime phase_queue copied from the template at run creation. Use phase_cursor and active_turn CAS to keep manager ticks idempotent. Add focused debate by appending temporary phases, not by changing the base template. Prototype with fake LLM outputs and assert exact speaker order.

---

## Testing And Evaluation

### Deterministic Fake LLM Tests

Use fake outputs to test the room machinery.

#### Test Cases

1. Critic accepts initial proposal → Synthesizer finalizes
2. Critic requests revision → Proposer revises → Synthesizer finalizes
3. Critic rejects proposal → Synthesizer marks needs_revision
4. Synthesizer requests focused_debate → two extra turns → finalizes
5. Agent emits invalid JSON → retry or fail cleanly
6. Proposer ignores high-severity objection → Synthesizer refuses acceptance
7. Human interrupts → room pauses before next speaker
8. Operator stop → room stops within one turn
9. Stale active turn → reconcile retries once
10. Max turns reached → room stops with partial decision

### Behavioral Eval Prompts

Use 10–20 prompts with expected properties.

#### Example Score Dimensions

- concreteness
- useful criticism
- realistic prototype
- clear risks
- preserved dissent
- no fake consensus
- no unnecessary abstractions
- valid JSON
- correct stop decision

### Eval Examples

Prompt:

Design a safe command execution layer for an LLM agent that accepts shell-like syntax but executes argv directly.

Expected:

- Mentions parser vs shell execution
- Mentions allow/maybe/block classification
- Mentions audit log
- Mentions destructive command handling
- Mentions no bash -c for simple commands
- Mentions tests for quoting, pipes, redirects, find -exec, rm, delete

Prompt:

How should I implement a cache for LLM calls?

Expected:

- Mentions cache key includes model, prompt, system prompt, tool schema, temperature, relevant config
- Mentions invalidation
- Mentions privacy/safety
- Mentions stale output risk
- Does not blindly cache all calls

Prompt:

Design a document edit agent using line ranges.

Expected:

- Mentions line-numbered input
- Mentions replace range
- Mentions EOF newline handling
- Mentions patch validation
- Mentions weak model failure modes
- Mentions benchmark tests

---

## Risks And Mitigations

### Risk: agents produce agreement theater

#### Mitigation

- Critic must provide verdict
- Synthesizer must preserve dissent
- No majority vote for correctness
- Focused debate only on named disagreement

### Risk: room becomes too verbose

#### Mitigation

- Agent turns are JSON
- User sees final rendered markdown
- Context window uses summary + recent turns
- Max turns enforced

### Risk: Critic is weak

#### Mitigation

- Add evals with intentionally flawed proposals
- Score whether high-severity flaws are caught
- Add "no new abstractions" criticism rule

### Risk: Proposer ignores criticism

#### Mitigation

- Synthesizer checks whether high-severity objections were addressed
- If not, decision status becomes needs_revision

### Risk: too many optional agents

#### Mitigation

- Start with 3 agents only
- Add Evidence Checker only when evidence routing exists
- Add Implementer only after decision records are stable

### Risk: the manager becomes domain-aware

#### Mitigation

- Manager only controls turns and state
- Synthesizer owns design decision
- Template owns deliberation protocol

---

## Recommended First Implementation

Build this exact slice first:

- `technical_design_proposer`
- `technical_design_critic`
- `technical_design_synthesizer`
- `technical-design.json` template
- `DesignIssue` payload
- `TechnicalDesignDecision` output
- Fake LLM tests
- Manual run from `/conversations`

Do not add tools yet.

Do not add more roles yet.

Do not auto-edit files yet.

The first milestone is simple:

- A user can start a technical_design room with a programming question.
- Rainbox runs proposer → critic → proposer → synthesizer.
- The room stops.
- The final decision is rendered as a useful markdown answer.
- The transcript and decision record are inspectable.

That is enough to prove the room is real.

---

## Verdict

The technical_design room should be Rainbox’s first serious task-centric room after the demo personas.

It is general-purpose because the protocol is stable:

propose → critique → revise → synthesize

It is domain-aware because {{DOMAIN_CONTEXT}} is injected.

It is bounded because the manager controls turns.

It is useful because the final product is not the transcript. The product is a technical design decision with assumptions, risks, dissent, and next steps.

The most important design rule:

Treat the conversation as state refinement, not chat.

The second most important rule:

The manager schedules turns. The Synthesizer decides meaning.
