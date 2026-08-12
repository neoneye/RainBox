# Proposal: email_case room

**Status:** Draft design proposal
**Scope:** General-purpose email case-handling room for Rainbox
**Purpose:** Let multiple AI agents analyze email threads, extract facts, identify obligations, assess risk, propose next actions, and draft responses without sending anything automatically.

## Summary

The email_case room should be a bounded Rainbox conversation template for handling messy email situations.

It should answer questions like:

- What is this email thread actually about?
- What does the other party want?
- What do I owe them?
- What deadline matters?
- Should I reply, archive, follow up, escalate, or wait?
- What should the reply say?
- What should I avoid saying?

This room is different from both technical_design and diagnosis.

The technical_design room is:

```text
propose → critique → revise → synthesize
```

The diagnosis room is:

```text
triage → hypothesize → collect evidence → analyze → recommend → verify
```

The email_case room should be:

```text
locate → extract facts → classify case → assess risk → plan action → draft → review → user approval
```

It is not debate-first. It is case-file-first.

The core idea:

```text
User asks about an email problem
→ Room identifies relevant thread(s)
→ Extracts facts, timeline, asks, commitments, deadlines
→ Classifies the case
→ Assesses risk and missing information
→ Recommends next action
→ Optionally drafts a reply
→ Risk reviewer checks the draft
→ User approves before anything is sent
```

The important rule:

The final artifact is the case record and recommended action, not the transcript.

---

## Design Goals

The email_case room should:

1. Turn messy email threads into a clear case record.
2. Separate facts from interpretation.
3. Identify the other party’s ask.
4. Identify the user’s obligations, deadlines, and risks.
5. Recommend whether to reply, wait, archive, escalate, or gather more information.
6. Draft responses when useful.
7. Review drafts for tone, overcommitment, legal/financial risk, and missing facts.
8. Never send email automatically.
9. Preserve user approval as the boundary before side effects.
10. Keep the room bounded.

---

## Non-Goals

The first version should not be a fully autonomous inbox agent.

Non-goals:

- No automatic sending.
- No automatic forwarding.
- No automatic deleting.
- No automatic archiving.
- No automatic labeling unless explicitly approved.
- No legal advice.
- No pretending uncertain facts are known.
- No broad inbox scanning without a reason.
- No long generic email advice.
- No unbounded back-and-forth.

The first useful version should answer:

What is going on in this email case, what matters, what should I do next,
and what would a safe reply look like?

---

## Core Work Object: `EmailCase`

The room should gather around a named email case, not just a thread.

Minimal record:

```json
{
  "case_id": "email_case_123",
  "room_type": "email_case",
  "user_request": "Help me respond to this refund thread.",
  "domain_context": "Email case handling, consumer support, travel booking, refund dispute",
  "known_constraints": [
    "Do not send email automatically",
    "Do not make legal claims unless supported",
    "Preserve user approval before side effects",
    "Prefer concise professional tone"
  ],
  "status": "under_review",
  "turn_policy": "locate_extract_assess_draft_review_synthesize",
  "max_rounds": 2
}
```

The transcript is useful for auditability, but the case record is the product.

---

## Recommended First Template

Use five functional agents:

- `email_case_locator`
- `email_case_fact_extractor`
- `email_case_risk_reviewer`
- `email_case_action_planner`
- `email_case_synthesizer`

Add a draft agent only once the case-file step works:

- `email_case_response_drafter`

Optional later agents:

- `email_case_thread_reader`
- `email_case_deadline_tracker`
- `email_case_commitment_checker`
- `email_case_tone_editor`
- `email_case_tool_gatekeeper`

Do not start with all of them.

The smallest useful loop without email-tool integration:

1. Fact Extractor reads provided email text.
2. Risk Reviewer identifies risk, missing facts, and things to avoid.
3. Action Planner recommends next step.
4. Synthesizer creates case record and final answer.

The smallest useful loop with email-tool integration:

1. Locator finds likely relevant threads.
2. Fact Extractor extracts timeline, asks, commitments, deadlines.
3. Risk Reviewer assesses risk.
4. Action Planner recommends next action.
5. Response Drafter drafts reply if useful.
6. Synthesizer finalizes and asks for user approval.

Hard rule:

No outbound action without explicit user approval.

---

## Room Protocol

### Phase 1: Locate

The room identifies relevant email material.

Possible inputs:

- user pasted email text
- user selected a thread
- user gave sender/subject/date hints
- user asked generally about recent emails

The room should avoid broad searches unless needed.

The Locator should answer:

- What email evidence do we have?
- Do we need to search?
- What query would find the relevant thread?
- Is the evidence enough to proceed?

### Phase 2: Extract Facts

The Fact Extractor builds the case file.

It should extract:

- participants
- timeline
- claims
- requests
- commitments
- deadlines
- attachments
- amounts
- reference numbers
- unresolved issues
- user obligations
- other party obligations

It should not draft yet.

### Phase 3: Classify Case

The room classifies what kind of email case this is.

Example categories:

- `reply_needed`
- `waiting_for_other_party`
- `deadline_or_obligation`
- `support_dispute`
- `refund_or_billing`
- `scheduling`
- `document_request`
- `account_access`
- `project_coordination`
- `sales_or_vendor`
- `personal_admin`
- `spam_or_phishing`
- `unknown`

### Phase 4: Assess Risk

The Risk Reviewer identifies:

- tone risk
- legal risk
- financial risk
- privacy risk
- commitment risk
- deadline risk
- relationship risk
- phishing/security risk

The goal is not paranoia. The goal is to avoid accidental self-harm.

### Phase 5: Plan Action

The Action Planner recommends one or more next actions:

- reply
- wait
- ask for clarification
- escalate
- call support
- attach document
- create reminder
- archive
- label
- forward to someone
- do nothing

Every action should have:

- why
- urgency
- risk
- approval needed

### Phase 6: Draft

The Response Drafter writes a possible reply only when the action plan calls for it.

The draft should be:

- clear
- short
- specific
- professional
- not overcommitting
- not inventing facts
- not escalating unnecessarily

### Phase 7: Review Draft

The Risk Reviewer checks the draft for:

- unsupported claims
- unnecessary admissions
- unclear ask
- missing deadline
- wrong tone
- overcommitment
- privacy exposure
- legal/financial risk

### Phase 8: Synthesize

The Synthesizer produces:

- case summary
- recommended next action
- draft reply if applicable
- risks
- missing information
- approval boundary

---

## Shared Prompt Placeholders

All email-case agents should receive the same structured context.

```text
{{ROOM_GOAL}}
{{USER_REQUEST}}
{{DOMAIN_CONTEXT}}
{{KNOWN_CONSTRAINTS}}
{{AVAILABLE_TOOLS}}
{{CURRENT_STATE}}
{{EMAIL_EVIDENCE}}
{{OTHER_AGENT_MESSAGES}}
{{OUTPUT_SCHEMA}}
```

### Recommended Meaning

#### `{{ROOM_GOAL}}`

Example:

```text
Analyze the user's email case by extracting facts, identifying obligations,
assessing risk, recommending next action, and drafting a response only when useful.
```

#### `{{USER_REQUEST}}`

The original user prompt.

Example:

```text
Help me respond to this email thread about a refund.
```

#### `{{DOMAIN_CONTEXT}}`

Injectable domain knowledge.

Example:

```text
Domain: email case handling
Known concerns: facts, timeline, asks, obligations, deadlines, tone, overcommitment,
privacy, user approval before sending
Preferred approach: extract the case file before drafting
```

Another example:

```text
Domain: vendor/support dispute
Known concerns: reference numbers, refund rules, prior promises, escalation path,
clear ask, avoiding unsupported legal claims
Preferred tone: firm, concise, professional
```

Another example:

```text
Domain: project coordination email
Known concerns: deliverables, commitments, owners, deadlines, blockers,
meeting requests, follow-up actions
Preferred tone: direct and operational
```

#### `{{KNOWN_CONSTRAINTS}}`

Example:

- Do not send email automatically.
- Do not archive, delete, forward, or label emails automatically.
- Do not invent facts.
- Do not make unsupported legal claims.
- Do not include sensitive personal data unless necessary.
- User must approve any outbound message.

#### `{{AVAILABLE_TOOLS}}`

Initial version:

No tools available. Reason from user-provided email text only.

Tool-enabled version:

Available tools:

- search email metadata
- read selected email threads
- read attachments
- create draft
- update draft
- send draft only after explicit user approval
- archive only after explicit user approval
- label only after explicit user approval

#### `{{CURRENT_STATE}}`

Example:

```json
{
  "round": 1,
  "case_type": "unknown",
  "participants": [],
  "timeline": [],
  "facts": [],
  "claims": [],
  "asks": [],
  "obligations": [],
  "deadlines": [],
  "risks": [],
  "recommended_action": null,
  "draft_reply": null,
  "decision": null
}
```

#### `{{EMAIL_EVIDENCE}}`

Can be:

- pasted email text
- selected thread summary
- full thread
- metadata only
- attachment summary
- no evidence yet

---

## Agent 1: `email_case_locator`

### Responsibility

The Locator identifies what email material is needed and how to find it.

It should not analyze deeply. It should decide whether enough evidence exists.

### System Prompt

```text
You are the Locator agent in a Rainbox email_case room.
Your job is to identify the relevant email material for the user's case.
You may work from pasted email text, selected thread content, metadata, or a user-provided search hint.
If email tools are available, propose targeted searches or thread reads.
If the evidence is already sufficient, say so.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Email evidence: {{EMAIL_EVIDENCE}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Identify whether the room has enough email evidence to proceed.
2. Prefer targeted search over broad inbox search.
3. Use sender, subject, date, keywords, and reference numbers when available.
4. Do not request full inbox access unless the user’s request requires it.
5. Mark privacy-sensitive evidence.
6. Do not draft a reply.
7. Do not recommend sending, archiving, deleting, forwarding, or labeling.
8. If tools are unavailable, say what the user should paste or provide.
9. If multiple threads may be relevant, identify them as candidates.
10. Keep the next step concrete.
```

### Output JSON Only

```json
{
  "role": "email_case_locator",
  "evidence_status": "sufficient|insufficient|ambiguous",
  "known_email_material": [
    "Email material already available"
  ],
  "search_requests": [
    {
      "purpose": "What this search would find",
      "query": "Targeted email search query or user instruction",
      "privacy_sensitivity": "low|medium|high",
      "requires_user_approval": true
    }
  ],
  "thread_read_requests": [
    {
      "purpose": "Why this thread should be read",
      "thread_hint": "Sender, subject, date, or other hint",
      "privacy_sensitivity": "low|medium|high",
      "requires_user_approval": true
    }
  ],
  "missing_information": [
    {
      "question": "Question",
      "blocking": true
    }
  ],
  "recommended_next_phase": "extract_facts|ask_user|search_email|read_thread|stop",
  "confidence": "low|medium|high"
}
```

---

## Agent 2: `email_case_fact_extractor`

### Responsibility

The Fact Extractor turns email evidence into a case file.

It should not recommend action yet unless the action is obvious and low-risk.

It should separate what the email says from what it might mean.

### System Prompt

```text
You are the Fact Extractor agent in a Rainbox email_case room.
Your job is to extract a structured case file from the available email evidence.
You do not draft a reply yet.
You do not decide strategy yet.
You identify facts, timeline, asks, commitments, deadlines, and unresolved issues.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Email evidence: {{EMAIL_EVIDENCE}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Extract only facts supported by the email evidence.
2. Separate fact from interpretation.
3. Identify participants and their roles.
4. Build a timeline when dates or ordering are available.
5. Identify what each party is asking for.
6. Identify commitments already made.
7. Identify deadlines, amounts, reference numbers, and attachments.
8. Identify unresolved issues.
9. Identify missing evidence.
10. Do not invent facts.
11. Do not draft a response yet.
```

### Output JSON Only

```json
{
  "role": "email_case_fact_extractor",
  "participants": [
    {
      "name_or_email": "Person or address",
      "role": "user|other_party|third_party|unknown"
    }
  ],
  "case_type_candidates": [
    "reply_needed|waiting_for_other_party|deadline_or_obligation|support_dispute|refund_or_billing|scheduling|document_request|account_access|project_coordination|sales_or_vendor|personal_admin|spam_or_phishing|unknown"
  ],
  "timeline": [
    {
      "date_or_order": "Date, time, or relative order",
      "event": "What happened",
      "source": "Email evidence reference"
    }
  ],
  "facts": [
    "Fact supported by evidence"
  ],
  "claims": [
    {
      "claim": "Claim made by a party",
      "made_by": "participant",
      "supported_by_evidence": true
    }
  ],
  "asks": [
    {
      "ask": "What someone wants",
      "asked_by": "participant",
      "asked_of": "participant",
      "explicit": true
    }
  ],
  "commitments": [
    {
      "commitment": "Commitment or promise",
      "made_by": "participant",
      "deadline": "Deadline or empty string"
    }
  ],
  "deadlines": [
    {
      "deadline": "Date or time",
      "meaning": "What happens or is due"
    }
  ],
  "amounts_or_references": [
    "Money, booking number, invoice number, ticket number, order id, etc."
  ],
  "attachments": [
    {
      "name": "Attachment name or description",
      "relevance": "Why it matters"
    }
  ],
  "unresolved_issues": [
    "Issue not yet resolved"
  ],
  "missing_information": [
    {
      "question": "Question",
      "blocking": true
    }
  ],
  "confidence": "low|medium|high"
}
```

---

## Agent 3: `email_case_risk_reviewer`

### Responsibility

The Risk Reviewer identifies what could go wrong.

It checks the case and any draft.

It should protect the user from accidental commitments, bad tone, unsupported claims, privacy leakage, or security mistakes.

### System Prompt

```text
You are the Risk Reviewer agent in a Rainbox email_case room.
Your job is to identify risks in the email case and later review any proposed draft.
You are not trying to be alarmist.
You are trying to prevent avoidable mistakes.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Email evidence: {{EMAIL_EVIDENCE}}
- Extracted facts: {{FACT_EXTRACTOR_OUTPUT}}
- Draft reply, if any: {{DRAFT_REPLY}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Identify tone, legal, financial, privacy, deadline, relationship, and security risks.
2. Identify unsupported claims.
3. Identify accidental admissions or overcommitments.
4. Identify missing facts that should be clarified before replying.
5. Identify whether the email may be spam, phishing, or suspicious.
6. Do not exaggerate low-risk issues.
7. Do not draft the full reply unless asked by the room phase.
8. Recommend constraints for a safe response.
9. If reviewing a draft, identify concrete edits.
10. Preserve user approval before outbound action.
```

### Output JSON Only

```json
{
  "role": "email_case_risk_reviewer",
  "risk_level": "low|medium|high|critical",
  "risks": [
    {
      "risk": "Specific risk",
      "category": "tone|legal|financial|privacy|deadline|relationship|security|commitment|other",
      "severity": "low|medium|high|critical",
      "why_it_matters": "Concrete consequence",
      "mitigation": "How to reduce risk"
    }
  ],
  "unsupported_claims": [
    "Claim that should not be made without evidence"
  ],
  "things_to_avoid_saying": [
    "Phrase, claim, or concession to avoid"
  ],
  "safe_response_constraints": [
    "Constraint the response should follow"
  ],
  "draft_review": {
    "status": "not_reviewed|acceptable|revise|reject",
    "issues": [
      {
        "issue": "Problem in draft",
        "suggested_edit": "Concrete edit"
      }
    ]
  },
  "confidence": "low|medium|high"
}
```

---

## Agent 4: `email_case_action_planner`

### Responsibility

The Action Planner recommends what to do next.

It chooses between replying, waiting, asking for clarification, escalating, creating a reminder, archiving, labeling, or drafting.

It should not write the full email unless the room requests drafting.

### System Prompt

```text
You are the Action Planner agent in a Rainbox email_case room.
Your job is to recommend the next action for the email case.
You use the extracted facts and risk review to decide whether the user should reply,
wait, ask for clarification, escalate, create a reminder, archive, label, forward,
or take no action.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Email evidence: {{EMAIL_EVIDENCE}}
- Extracted facts: {{FACT_EXTRACTOR_OUTPUT}}
- Risk review: {{RISK_REVIEW_OUTPUT}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Recommend the smallest useful next action.
2. Do not recommend sending automatically.
3. Do not recommend archiving/deleting/forwarding/labeling automatically.
4. Mark actions that require user approval.
5. Identify urgency.
6. Distinguish reply-needed from no-reply-needed.
7. If a draft is useful, request a drafting phase.
8. If facts are missing, recommend a clarification question.
9. Avoid long action lists.
10. Make the recommendation operational.
```

### Output JSON Only

```json
{
  "role": "email_case_action_planner",
  "recommended_action": {
    "action_type": "reply|wait|ask_clarification|escalate|create_reminder|archive|label|forward|draft_only|no_action|needs_more_evidence",
    "description": "Concrete next action",
    "why": "Why this is the right next step",
    "urgency": "low|medium|high",
    "requires_user_approval": true
  },
  "alternative_actions": [
    {
      "action_type": "Alternative",
      "when_to_choose": "Condition where this is better"
    }
  ],
  "draft_needed": true,
  "draft_goal": "What the draft should accomplish",
  "key_points_to_include": [
    "Point"
  ],
  "points_to_avoid": [
    "Point"
  ],
  "follow_up_plan": [
    "Follow-up action or reminder"
  ],
  "confidence": "low|medium|high"
}
```

---

## Agent 5: `email_case_response_drafter`

### Responsibility

The Response Drafter writes a reply when the action plan calls for it.

It should not send the reply.

It should not invent facts.

It should produce a draft that can be reviewed.

### System Prompt

```text
You are the Response Drafter agent in a Rainbox email_case room.
Your job is to draft an email response that follows the case facts, action plan,
and risk constraints.
You do not send the email.
You write a draft for user review.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Email evidence: {{EMAIL_EVIDENCE}}
- Extracted facts: {{FACT_EXTRACTOR_OUTPUT}}
- Risk review: {{RISK_REVIEW_OUTPUT}}
- Action plan: {{ACTION_PLAN_OUTPUT}}
- Other agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Do not invent facts.
2. Do not include unsupported legal or financial claims.
3. Keep the draft concise.
4. Match the requested tone if one is specified.
5. Otherwise use a clear, professional, neutral tone.
6. State the ask explicitly.
7. Include relevant reference numbers or dates only if present in evidence.
8. Avoid unnecessary apologies or admissions.
9. Avoid escalation language unless the action plan requires it.
10. Do not say the email has been sent.
```

### Output JSON Only

```json
{
  "role": "email_case_response_drafter",
  "subject": "Suggested subject, or empty string if replying in existing thread",
  "draft_body": "Email draft body",
  "draft_intent": "What this draft is meant to accomplish",
  "facts_used": [
    "Fact used in draft"
  ],
  "assumptions": [
    "Assumption made in the draft"
  ],
  "placeholders": [
    {
      "placeholder": "[PLACEHOLDER]",
      "meaning": "What the user must fill in"
    }
  ],
  "confidence": "low|medium|high"
}
```

---

## Agent 6: `email_case_synthesizer`

### Responsibility

The Synthesizer produces the final case decision.

It should decide:

Do we know enough?
What is the case about?
What matters?
What should the user do?
Is a draft ready?
Does anything require approval?

### System Prompt

```text
You are the Synthesizer agent in a Rainbox email_case room.
Your job is to reduce the email-case conversation into a useful case decision.
You do not merely summarize.
You decide whether the user should reply, wait, ask for more information,
create a draft, review a draft, or approve an action.
Context:
- Room goal: {{ROOM_GOAL}}
- User request: {{USER_REQUEST}}
- Domain context: {{DOMAIN_CONTEXT}}
- Known constraints: {{KNOWN_CONSTRAINTS}}
- Available tools: {{AVAILABLE_TOOLS}}
- Current state: {{CURRENT_STATE}}
- Email evidence: {{EMAIL_EVIDENCE}}
- Agent messages: {{OTHER_AGENT_MESSAGES}}
Rules:
1. Separate facts from interpretation.
2. Identify the actual unresolved issue.
3. Identify the recommended next action.
4. Preserve risks and missing information.
5. If there is a draft, summarize what it does and whether it is safe to use.
6. Do not say anything has been sent.
7. Do not recommend outbound side effects without explicit user approval.
8. Stop if the next step requires the user.
9. Ask for more evidence only if it changes the decision.
10. Produce a final answer shape suitable for the user.
```

### Output JSON Only

```json
{
  "role": "email_case_synthesizer",
  "case_summary": "Short summary of what the email case is about.",
  "known_facts": [
    "Known fact"
  ],
  "unresolved_issue": "The main unresolved issue",
  "recommended_next_action": {
    "action_type": "reply|wait|ask_clarification|escalate|create_reminder|archive|label|forward|draft_only|no_action|needs_more_evidence",
    "description": "Concrete next step",
    "urgency": "low|medium|high",
    "requires_user_approval": true
  },
  "risks": [
    {
      "risk": "Risk",
      "severity": "low|medium|high|critical",
      "mitigation": "Mitigation"
    }
  ],
  "missing_information": [
    {
      "question": "Question",
      "blocking": true
    }
  ],
  "draft": {
    "available": true,
    "status": "not_needed|needs_review|ready_for_user_review|unsafe|missing_facts",
    "subject": "Subject or empty string",
    "body": "Draft body or empty string"
  },
  "approval_boundary": [
    "Action requiring explicit user approval"
  ],
  "decision": {
    "status": "needs_evidence|needs_user_input|draft_ready|action_recommended|no_action_needed|inconclusive|stop",
    "confidence": "low|medium|high"
  },
  "next_action": "finalize|ask_user|retrieve_email|read_thread|create_draft|review_draft|stop"
}
```

---

## Suggested Conversation Template File

Example agent_profiles/conversations/email-case.json:

```json
{
  "id": "email_case",
  "title": "Email Case",
  "description": "Bounded multi-agent room for analyzing email threads, extracting facts, assessing risk, planning action, and drafting replies.",
  "room_type": "email_case",
  "participants": [
    {
      "persona_id": "email_case_locator",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "email_case_fact_extractor",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "email_case_risk_reviewer",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "email_case_action_planner",
      "agent_kind": "chat_structured",
      "required": true
    },
    {
      "persona_id": "email_case_synthesizer",
      "agent_kind": "chat_structured",
      "required": true
    }
  ],
  "turn_policy": {
    "type": "phase_script",
    "phases": [
      {
        "speaker": "email_case_locator",
        "purpose": "locate_email_evidence"
      },
      {
        "speaker": "email_case_fact_extractor",
        "purpose": "extract_case_file"
      },
      {
        "speaker": "email_case_risk_reviewer",
        "purpose": "assess_case_risk"
      },
      {
        "speaker": "email_case_action_planner",
        "purpose": "plan_next_action"
      },
      {
        "speaker": "email_case_synthesizer",
        "purpose": "case_decision"
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
    "stop_on_need_email_evidence": true,
    "stop_on_draft_ready": true,
    "stop_on_max_turns": true
  },
  "safety_policy": {
    "no_auto_send": true,
    "no_auto_archive": true,
    "no_auto_delete": true,
    "no_auto_forward": true,
    "require_approval_for_outbound_actions": true,
    "preserve_privacy": true
  },
  "context_policy": {
    "last_visible_turns": 8,
    "include_runtime_preamble": true,
    "include_summary": true
  },
  "output_policy": {
    "final_artifact_kind": "email_case_decision",
    "preserve_uncertainty": true,
    "include_draft_when_available": true
  }
}
```

When drafting is enabled, use an expanded template:

```text
locator → fact_extractor → risk_reviewer → action_planner → response_drafter → risk_reviewer → synthesizer
```

The second Risk Reviewer pass is important. Drafts are where accidental commitments happen.

---

## Durable Email Case Decision Record

The final output should be structured.

```json
{
  "email_case_id": "email_case_123",
  "decision": "draft_ready",
  "confidence": "medium",
  "case_summary": "The other party appears to be asking for missing booking details before handling the refund request.",
  "case_type": "refund_or_billing",
  "participants": [
    {
      "name_or_email": "support@example.com",
      "role": "other_party"
    },
    {
      "name_or_email": "user",
      "role": "user"
    }
  ],
  "known_facts": [
    "Support requested the booking reference.",
    "The user wants a refund.",
    "No refund confirmation has been provided yet."
  ],
  "unresolved_issue": "Support needs enough identifying information to locate the booking.",
  "risks": [
    {
      "risk": "Sending unsupported accusations may reduce cooperation.",
      "severity": "medium",
      "mitigation": "Use firm but factual wording."
    }
  ],
  "recommended_next_action": {
    "action_type": "reply",
    "description": "Reply with the booking reference and a clear refund request.",
    "urgency": "medium",
    "requires_user_approval": true
  },
  "draft": {
    "subject": "",
    "body": "Hello,\n\nMy booking reference is [BOOKING_REFERENCE].\n\nPlease confirm whether this booking is eligible for cancellation and refund, and let me know the next step.\n\nBest regards,"
  },
  "approval_boundary": [
    "User must review and approve before creating or sending the draft."
  ],
  "missing_information": [
    "Booking reference"
  ]
}
```

---

## Final Answer Renderer

The final writer should render email case records as readable markdown.

Recommended sections:

1. Case summary
2. What is known
3. What is unresolved
4. Recommended next action
5. Risks / things to avoid
6. Draft reply
7. Approval needed

Example final answer:

```text
Case summary:
They are not rejecting the refund yet. They are asking for enough information to locate the booking.

Recommended next action:
Reply with the booking reference and ask them to confirm refund eligibility and next steps.

Avoid:
- Accusing them of refusing the refund.
- Making legal claims unless you want that escalation.
- Sending without checking the booking reference.

Draft:
...
```

---

## Implementation Plan

### Phase 0: Static Prompt Experiment

Goal: prove the email-case protocol works without email tools.

Implement prompt files:

- `email_case_fact_extractor.system.md`
- `email_case_risk_reviewer.system.md`
- `email_case_action_planner.system.md`
- `email_case_synthesizer.system.md`

Run this sequence on pasted email text:

- fact_extractor
- risk_reviewer
- action_planner
- synthesizer

Save:

- `email_case.json`
- `turns.jsonl`
- `email_case_decision.json`
- `final_answer.md`

Acceptance:

Given 5 pasted email cases, the room produces:
- case summary
- participants
- timeline or ordering
- asks and obligations
- risks
- recommended next action
- draft only when useful
- no claim that email was sent

### Phase 1: Rainbox Conversation Template

Goal: run email_case inside the Rainbox conversation manager.

Implement:

- `agent_profiles/prompts/email_case_locator.system.md`
- `agent_profiles/prompts/email_case_fact_extractor.system.md`
- `agent_profiles/prompts/email_case_risk_reviewer.system.md`
- `agent_profiles/prompts/email_case_action_planner.system.md`
- `agent_profiles/prompts/email_case_synthesizer.system.md`
- `agent_profiles/conversations/email-case.json`

Add persona records:

```json
{"id": "email_case_locator", "kind": "email_case"}
{"id": "email_case_fact_extractor", "kind": "email_case"}
{"id": "email_case_risk_reviewer", "kind": "email_case"}
{"id": "email_case_action_planner", "kind": "email_case"}
{"id": "email_case_synthesizer", "kind": "email_case"}
```

Acceptance:

Starting an email_case room from the UI produces structured turns and one email case decision record.
The room stops when the Synthesizer returns next_action=ask_user, retrieve_email, draft_ready, or finalize.

### Phase 2: Draft Generation

Goal: produce reviewable drafts.

Add:

- `email_case_response_drafter.system.md`

Expanded phase script:

```text
locator
fact_extractor
risk_reviewer
action_planner
response_drafter
risk_reviewer
synthesizer
```

Acceptance:

The room can produce a draft reply, review it for risk, and present it for user approval.
It never says the draft was sent.

### Phase 3: Email Tool Integration

Goal: allow Rainbox to search and read selected email threads.

The Locator should emit tool requests, not directly access email.

Example:

```json
{
  "request_type": "search_email",
  "query": "from:support@example.com subject:refund newer_than:30d",
  "privacy_sensitivity": "medium",
  "requires_user_approval": true
}
```

Tool routing should be explicit:

- Locator requests email evidence.
- Tool gateway asks approval if needed.
- Email tool returns metadata or selected thread.
- Fact Extractor processes the evidence.

Acceptance:

The room can request a targeted email search.
The user can approve or deny.
The selected thread becomes EMAIL_EVIDENCE.
The room continues from fact extraction.

### Phase 4: Draft Creation In Email Client

Goal: optionally create a saved draft, not send it.

Action classes:

- `read_email_metadata`
- `read_email_thread`
- `create_draft`
- `update_draft`
- `send_draft`
- `archive`
- `delete`
- `label`
- `forward`

Default permissions:

- `read_email_metadata`: requires approval unless user has enabled email-agent access.
- `read_email_thread`: requires approval or explicit user request.
- `create_draft`: requires approval.
- `update_draft`: requires approval.
- `send_draft`: requires explicit send approval.
- `archive` / `delete` / `forward` / `label`: requires explicit approval.

Acceptance:

The room can create a draft only after the user approves.
The room cannot send the draft unless the user explicitly says to send it.

### Phase 5: Follow-Up And Reminders

Goal: support “follow up if no reply” workflows.

The Action Planner may recommend:

```text
Create reminder in 3 days if no reply.
```

But this should be a separate approval-gated action.

Acceptance:

The room can suggest a reminder, but does not create it without explicit approval.

---

## State Machine

Recommended room states:

- `created`
- `running`
- `waiting_for_agent`
- `waiting_for_email_search_approval`
- `waiting_for_thread_read_approval`
- `waiting_for_user_input`
- `waiting_for_draft_approval`
- `waiting_for_send_approval`
- `draft_ready`
- `finished`
- `paused`
- `stopped`
- `failed`

Recommended email-case statuses:

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

Do not merge conversation status and case status.

A room can be:

```text
conversation status: finished
case status: draft_ready
```

That means the room did its job. The user must decide whether to use the draft.

---

## Stop Conditions

The room should stop when:

- Synthesizer returns next_action=finalize
- Synthesizer returns next_action=ask_user
- Synthesizer returns next_action=retrieve_email
- Synthesizer returns next_action=create_draft
- Synthesizer returns decision.status=draft_ready
- max_turns is reached
- operator stops the run
- evidence is insufficient and no targeted search is available
- risk is too high for automated drafting

Do not stop because agents agree.

Stop because the next useful action is known.

---

## Safety Policy

The email_case room needs strict side-effect boundaries.

### Default Policy

- No automatic send.
- No automatic archive.
- No automatic delete.
- No automatic forward.
- No automatic label.
- No automatic commitment.
- No unsupported legal claims.
- No broad inbox scan without reason.

### Evidence Classification

Every email evidence request should be classified:

- `metadata_only`
- `thread_content`
- `attachment_content`
- `privacy_sensitive`
- `external_side_effect`
- `requires_user_approval`

### Approval Rules

- `metadata_search`: allowed only when user asked for email help or approves the search.
- `thread_read`: requires targeted relevance.
- `attachment_read`: requires explicit relevance.
- `create_draft`: requires approval.
- `send`: requires explicit send instruction.
- `archive` / `delete` / `forward` / `label`: requires explicit approval.

The room may recommend actions, but the tool gateway enforces them.

---

## Useful Tricks

1. Extract before drafting

#### Bad

Write a reply based on vibes.

#### Good

Extract facts, asks, commitments, and unresolved issue first.
Then draft.

2. Identify the real ask

Many emails are vague. The room should answer:

What are they actually asking the user to do?

3. Identify user obligations

The most useful output is often:

You owe them X.
They owe you Y.
The unresolved issue is Z.

4. Add “things to avoid saying”

This is crucial for disputes.

Example:

```text
Do not say "I accept the fee" unless you actually do.
Do not threaten legal action unless you want escalation.
Do not apologize for fault unless fault is established.
```

5. Draft with placeholders

If a fact is missing, use placeholders instead of inventing.

Example:

```text
[BOOKING_REFERENCE]
[ORDER_ID]
[DATE]
[ATTACHMENT_NAME]
```

6. Do not over-escalate

A support email often needs firm clarity, not aggression.

The Risk Reviewer should catch excessive tone.

7. Separate draft from action

A draft is not a sent email.

The case record should say:

```text
draft_ready
requires_user_approval
not_sent
```

8. Prefer targeted email search

#### Bad

Search all emails for refund.

#### Good

Search from:booking.com OR from:customer.service subject:(refund OR cancellation) newer_than:90d

9. Preserve uncertainty

#### Good

The thread suggests they are waiting for your booking reference, but the actual refund policy is not included in the available emails.

10. Classify suspicious messages

The room should support:

- Could this be phishing?
- Should I click this link?
- Should I provide these details?

In that case, the answer may be:

- Do not click.
- Verify through official site.
- Do not reply with credentials or payment details.

---

## Example Input Prompts That Start The Room

### General Email Analysis

- Help me understand what this email thread is actually asking me to do.
- Analyze this email and tell me whether I need to reply.
- What is the unresolved issue in this email chain?
- Summarize this thread into facts, obligations, and next action.
- This email is confusing. Tell me what they want and what I should do next.

### Drafting Replies

- Help me draft a concise reply to this email.
- Write a firm but polite response to this support thread.
- Draft a reply that asks for clarification without overcommitting.
- Draft a follow-up email because they have not replied.
- Help me respond without sounding aggressive.

### Disputes / Refunds / Support Cases

- I have an email thread where the company is avoiding a refund. Analyze the case and draft a reply.
- They rejected my refund request. Tell me what facts matter and what I should say next.
- Support keeps asking for more information. Diagnose what they still need.
- Help me escalate this support case professionally.
- Analyze whether this reply actually answers my question.

### Scheduling / Coordination

- Analyze this scheduling email and tell me what I need to confirm.
- Draft a reply proposing two alternative meeting times.
- This project email has many moving parts. Extract owners, tasks, and deadlines.
- Tell me who owes what in this coordination thread.
- Summarize the action items from this email chain.

### Risk / Tone Review

- Review this draft before I send it. Is it too harsh?
- Does this email accidentally commit me to something?
- Check this reply for unsupported claims or risky wording.
- Make this email shorter and safer.
- What should I avoid saying in this dispute reply?

### Phishing / Security

- Does this email look suspicious?
- Should I click this link from the email?
- This email asks for account details. Analyze whether it is safe.
- Help me verify whether this sender is legitimate.
- Draft a safe reply that does not reveal sensitive information.

### Rainbox-Specific Email Room Prompts

- Design an email_case room where agents extract facts from a thread, assess risk, draft a reply, and wait for approval.
- Diagnose why an email_case room drafted a reply before extracting the timeline.
- Design the approval boundary between an email_case room and Gmail send actions.
- How should Rainbox represent an email case decision record?
- Design a room that can read a selected thread, extract obligations, and create a draft without sending it.

---

## Example Completed Room Trace

### Input

Help me respond to this refund email thread. They keep asking for more information.

### Turn 1: Locator

```json
{
  "role": "email_case_locator",
  "evidence_status": "insufficient",
  "known_email_material": [
    "User says the thread concerns a refund and repeated requests for more information"
  ],
  "search_requests": [
    {
      "purpose": "Find the refund thread",
      "query": "subject:(refund OR cancellation) newer_than:90d",
      "privacy_sensitivity": "medium",
      "requires_user_approval": true
    }
  ],
  "missing_information": [
    {
      "question": "Please provide the thread or approve a targeted search for the refund conversation.",
      "blocking": true
    }
  ],
  "recommended_next_phase": "ask_user",
  "confidence": "medium"
}
```

If the user provides the thread, the room continues.

### Turn 2: Fact Extractor

```json
{
  "role": "email_case_fact_extractor",
  "participants": [
    {
      "name_or_email": "support@example.com",
      "role": "other_party"
    },
    {
      "name_or_email": "user",
      "role": "user"
    }
  ],
  "case_type_candidates": [
    "refund_or_billing",
    "support_dispute"
  ],
  "facts": [
    "The user requested a refund.",
    "Support asked for the booking reference.",
    "Support has not yet confirmed refund eligibility."
  ],
  "asks": [
    {
      "ask": "Provide booking reference",
      "asked_by": "support@example.com",
      "asked_of": "user",
      "explicit": true
    }
  ],
  "unresolved_issues": [
    "Whether the booking is eligible for refund",
    "Whether support has enough information to locate the booking"
  ],
  "missing_information": [
    {
      "question": "Booking reference",
      "blocking": true
    }
  ],
  "confidence": "medium"
}
```

### Turn 3: Risk Reviewer

```json
{
  "role": "email_case_risk_reviewer",
  "risk_level": "medium",
  "risks": [
    {
      "risk": "Accusing support of refusing the refund before eligibility is confirmed",
      "category": "tone",
      "severity": "medium",
      "why_it_matters": "It may escalate unnecessarily and reduce cooperation.",
      "mitigation": "Use firm factual language and ask for confirmation."
    }
  ],
  "unsupported_claims": [
    "They are refusing the refund"
  ],
  "things_to_avoid_saying": [
    "You are deliberately avoiding the refund",
    "I will take legal action"
  ],
  "safe_response_constraints": [
    "Provide the requested reference",
    "Ask for refund eligibility and next step",
    "Keep the tone professional"
  ],
  "draft_review": {
    "status": "not_reviewed",
    "issues": []
  },
  "confidence": "medium"
}
```

### Turn 4: Action Planner

```json
{
  "role": "email_case_action_planner",
  "recommended_action": {
    "action_type": "reply",
    "description": "Reply with the booking reference and ask support to confirm refund eligibility and next steps.",
    "why": "Support appears to need identifying information before processing the case.",
    "urgency": "medium",
    "requires_user_approval": true
  },
  "draft_needed": true,
  "draft_goal": "Provide requested information and obtain a clear refund decision.",
  "key_points_to_include": [
    "Booking reference",
    "Clear refund request",
    "Request for next step or confirmation"
  ],
  "points_to_avoid": [
    "Unsupported accusation",
    "Legal threat",
    "Overly long history"
  ],
  "confidence": "medium"
}
```

### Turn 5: Response Drafter

```json
{
  "role": "email_case_response_drafter",
  "subject": "",
  "draft_body": "Hello,\n\nMy booking reference is [BOOKING_REFERENCE].\n\nPlease confirm whether this booking is eligible for cancellation and refund, and let me know the next step.\n\nBest regards,",
  "draft_intent": "Provide the requested information and ask for a clear refund decision.",
  "facts_used": [
    "Support requested the booking reference",
    "User wants a refund"
  ],
  "assumptions": [],
  "placeholders": [
    {
      "placeholder": "[BOOKING_REFERENCE]",
      "meaning": "The user's actual booking reference"
    }
  ],
  "confidence": "medium"
}
```

### Turn 6: Synthesizer

```json
{
  "role": "email_case_synthesizer",
  "case_summary": "Support appears to be asking for the booking reference before they can handle the refund request.",
  "known_facts": [
    "The user requested a refund",
    "Support asked for a booking reference"
  ],
  "unresolved_issue": "Whether the booking is eligible for refund",
  "recommended_next_action": {
    "action_type": "reply",
    "description": "Reply with the booking reference and ask for refund eligibility and next step.",
    "urgency": "medium",
    "requires_user_approval": true
  },
  "risks": [
    {
      "risk": "Escalating tone before eligibility is confirmed",
      "severity": "medium",
      "mitigation": "Use concise factual wording"
    }
  ],
  "missing_information": [
    {
      "question": "Booking reference",
      "blocking": true
    }
  ],
  "draft": {
    "available": true,
    "status": "ready_for_user_review",
    "subject": "",
    "body": "Hello,\n\nMy booking reference is [BOOKING_REFERENCE].\n\nPlease confirm whether this booking is eligible for cancellation and refund, and let me know the next step.\n\nBest regards,"
  },
  "approval_boundary": [
    "User must fill in booking reference and approve before sending."
  ],
  "decision": {
    "status": "draft_ready",
    "confidence": "medium"
  },
  "next_action": "finalize"
}
```

### Final User-Facing Answer

```text
Case summary:
They are asking for your booking reference before deciding or processing the refund.

Recommended next action:
Reply with the booking reference and ask them to confirm refund eligibility and the next step.

Avoid:
- Accusing them of refusing the refund.
- Making legal threats.
- Sending without replacing the placeholder.

Draft:
Hello,
My booking reference is [BOOKING_REFERENCE].
Please confirm whether this booking is eligible for cancellation and refund, and let me know the next step.
Best regards,

Approval needed:
Fill in the booking reference and review before sending.
```

---

## Testing And Evaluation

### Deterministic Fake LLM Tests

Use fake outputs first.

#### Test Cases

1. Pasted email with clear ask → fact extraction succeeds.
2. Missing email evidence → Locator asks for thread or targeted search.
3. Refund dispute → risks include unsupported accusations.
4. Scheduling email → action planner identifies confirmation needed.
5. Phishing-like email → risk reviewer flags security risk.
6. Draft requested → drafter produces concise reply with placeholders.
7. Draft has risky wording → risk reviewer rejects or revises.
8. Missing deadline → synthesizer asks user or marks missing information.
9. User asks to send → system requires explicit approval after draft review.
10. Invalid JSON → retry or fail cleanly.

### Behavioral Eval Dimensions

Score outputs on:

- fact extraction quality
- ask identification
- obligation identification
- deadline extraction
- risk awareness
- tone appropriateness
- draft usefulness
- placeholder usage instead of invented facts
- approval boundary clarity
- valid JSON
- bounded stop decision

### Eval Prompt Examples

Prompt:

Analyze this email and tell me if I need to reply.

Expected:

- Identifies explicit or implied ask
- Distinguishes reply_needed vs no_action_needed
- Identifies missing information
- Does not draft unless useful

Prompt:

Review this draft before I send it. Is it too harsh?

Expected:

- Reviews tone
- Identifies risky wording
- Suggests concrete edits
- Does not send

Prompt:

This email asks me to click a link and verify payment details.

Expected:

- Flags phishing/security risk
- Advises verification through official channel
- Avoids clicking or replying with sensitive data

Prompt:

Extract action items from this project email chain.

Expected:

- Participants
- Tasks
- Owners
- Deadlines
- Open questions
- Recommended follow-up

---

## Risks And Mitigations

### Risk: room drafts before understanding the case

#### Mitigation

- Fact Extractor must run before Drafter
- Drafter input must include action plan
- Synthesizer rejects drafts with missing facts

### Risk: accidental sending

#### Mitigation

- No send tool in the room by default
- Send requires explicit user instruction
- Draft creation and sending are separate actions
- Synthesizer must list approval boundary

### Risk: unsupported claims

#### Mitigation

- Risk Reviewer identifies unsupported claims
- Drafter must list facts used
- Placeholders required for missing information

### Risk: privacy leakage

#### Mitigation

- Locator prefers targeted thread search
- Evidence requests classify privacy sensitivity
- Avoid broad inbox scans
- Do not include sensitive content in summaries unless needed

### Risk: over-escalation

#### Mitigation

- Risk Reviewer checks tone
- Action Planner distinguishes firm from aggressive
- Draft defaults to concise professional tone

### Risk: room never finishes

#### Mitigation

- Synthesizer stops when next user action is known
- Max turns enforced
- Draft-ready is a stop state
- Missing evidence becomes needs_user_input

---

## Recommended First Implementation

Build this exact slice first:

- `email_case_fact_extractor`
- `email_case_risk_reviewer`
- `email_case_action_planner`
- `email_case_synthesizer`
- `email-case.json` conversation template
- `EmailCase` payload
- `EmailCaseDecision` output
- Fake LLM tests
- Manual run from pasted email text

Do not add Gmail integration yet.

Do not add automatic draft creation yet.

Do not add send actions.

The first milestone:

- A user can paste an email thread into an email_case room.
- Rainbox runs fact_extractor → risk_reviewer → action_planner → synthesizer.
- The room stops.
- The final case decision says what is known, what is unresolved, what the user should do next, and what not to say.
- The transcript and decision record are inspectable.

The second milestone:

- Add response_drafter and a second risk review pass.
- The room produces a draft for user review, but never sends it.

The third milestone:

- Add targeted email search/read tooling behind explicit approval.
- The room can locate a thread, read it, build a case file, and draft a reply.

---

## Verdict

The email_case room should be Rainbox’s case-handling room.

It should not behave like a generic email assistant that immediately writes a reply.

It should behave like a disciplined case worker:

```text
locate → extract facts → classify case → assess risk → plan action → draft → review → approval
```

The most important design rule:

```text
Extract the case before drafting the reply.
```

The second most important rule:

```text
A draft is not an action. Sending is a separate approval-gated side effect.
```

The third most important rule:

```text
The final artifact is the email case decision, not the transcript.
```
