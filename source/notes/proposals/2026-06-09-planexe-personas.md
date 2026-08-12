# Feedback on the Rainbox Persona / Agent Conversation Design

## Executive View

The Rainbox document is strong as an implementation design for bounded persona-to-persona conversations. It is concrete, code-aware, and correctly avoids the common trap of building a vague “multi-agent swarm” before the runtime has deterministic turn-taking, identity, cancellation, recovery, and observability.

The central design choice is right: Rainbox should not invent a second orchestration runtime. It should reuse the existing queue, journal, supervisor, child-process isolation, chat messages, and routing machinery. The proposed missing primitive — a bounded conversation manager — is the correct minimal addition.

However, the document is currently more mature as a conversation runtime design than as a design for fact-checking and improving PlanExe-style reports. It explains how agents talk, but it needs a stronger model for why they talk, what artifacts they produce, what counts as agreement, what counts as unresolved disagreement, and how their conclusions become safe edits to a report.

The next conceptual step is to separate two layers:

text Conversation runtime:   Who speaks next?   How is the run bounded?   How is it stopped, resumed, recovered, and observed?  Deliberation protocol:   What issue is being decided?   Which agents must participate?   What evidence is required?   What does consensus mean?   What artifact is produced?   What happens when consensus fails? 

The current document handles the first layer well. Rainbox needs the second layer to become useful for serious report iteration.

---

## What the Document Gets Right

### 1. It correctly treats persona as data

The split between agent_kind, persona, conversation_template, and conversation_run is the right foundation.

This avoids a common design failure where “Egon”, “critic”, “researcher”, and “shell agent” all become tangled inside one config object. The proposed split is clean:

text agent_kind = implementation and capability persona = behavior and prompt template = who participates and how turns are scheduled run = one bounded execution instance 

That is a good abstraction boundary.

### 2. The manager-as-agent design is sound

Making the conversation manager an ordinary Rainbox agent is a good architectural decision.

It means the manager benefits from:

- inbox/journal persistence
- existing supervisor process isolation
- failure visibility
- SIGKILL watchdog behavior
- routing and retry semantics
- the same operational model as every other agent

Putting this logic inside the supervisor would be worse. The supervisor should remain boring and hard to kill. The manager can crash, fail, or be improved independently.

### 3. Dynamic return-address routing is the right move

The document correctly rejects static next = manager.

That matters. A persona should be usable in a normal chatroom and also inside a managed conversation. Static routing would permanently couple the persona to group-chat execution.

The dynamic return_to_agent_uuid mechanism is a good design because it makes the return path specific to one queued job, not to the agent’s whole identity.

### 4. The bounded loop is necessary

The document is appropriately paranoid about runaway loops.

The combination of:

- max_turns
- min_turns
- stop phrases
- operator stop
- pause/resume
- stale-turn reconciliation
- one active turn per run
- compare-and-set advancement

is exactly the kind of boring machinery that makes agent systems usable. Without it, Rainbox would become another system where agents politely agree forever or silently wedge.

### 5. The document has unusually good operational realism

The notes about restart behavior, stale transcript carryover, watchdog heartbeats, slow reasoning models, prompt SHA provenance, and room reuse are valuable. These are the kinds of details most multi-agent designs ignore until the system fails in production.

This is one of the strongest parts of the document.

---

## Main Weakness

The document is too focused on persona conversation mechanics and not yet focused enough on artifact improvement mechanics.

For Rainbox’s stated purpose — agents that fact-check, obtain documents, and iterate on PlanExe reports — the important unit is not a “conversation”. The important unit is an issue under review.

A good Rainbox run should not be:

text Egon talks to Benny until DONE. 

It should be:

text Issue: The report claims X. Required agents: Claim Extractor, Source Finder, Domain Reviewer, Skeptic, Editor. Required evidence: at least one primary source or explicit uncertainty. Decision rule: consensus, qualified consensus, or unresolved dissent. Output: verified claim, rejected claim, patch, or open question. 

The current runtime can support that, but the design document should make it explicit.

---

## Recommended Conceptual Model

Rainbox should be built around deliberation rooms, not just chatrooms.

A chatroom is the visible interface. A deliberation room is the structured process underneath.

text Chatroom:   transcript, messages, visible agent discussion  Deliberation room:   issue, participants, evidence, objections, decision rule, final artifact 

The missing core object is something like:

json {   "issue_id": "issue_123",   "artifact_id": "report_456",   "section_id": "risk_assessment",   "issue_type": "claim_verification",   "statement": "The plan assumes binding grid capacity can be secured within 36 months.",   "required_agents": [     "claim_extractor",     "source_finder",     "domain_reviewer",     "skeptic",     "editor"   ],   "evidence_required": true,   "consensus_policy": "qualified_consensus",   "status": "under_review" } 

This is the object agents should gather around.

The conversation manager schedules turns. The deliberation protocol decides what they are trying to resolve.

---

## The Agent Types Rainbox Actually Needs

For PlanExe report iteration, I would not start with playful personas. They are useful for testing the runtime, but the production system needs functional agents with clear responsibilities.

### 1. Artifact Mapper

Purpose: Understand the report structure.

Responsibilities:

- identify sections
- identify generated artifacts inside the report
- map claims to sections
- detect cross-section dependencies
- identify which sections are editable versus informational

For PlanExe reports, this agent should understand concepts such as:

- executive summary
- assumptions
- scenarios
- strategic decisions
- risks
- premortem
- documents to create/find
- WBS
- dependencies
- timeline
- budget
- compliance
- governance
- KPIs
- self-audit

This agent does not fact-check. It creates the map.

### 2. Claim Extractor

Purpose: Convert prose into reviewable claims.

Responsibilities:

- extract factual claims
- extract numeric claims
- extract timeline claims
- extract legal/regulatory claims
- extract stakeholder claims
- extract assumptions disguised as facts
- classify claims by verification difficulty

Example output:

json {   "claim": "Phase 1 can reach 1 GW operational capacity within 36 months.",   "type": "timeline/feasibility",   "section": "Executive Summary",   "verification_need": "external evidence and expert review",   "risk_if_wrong": "high" } 

This is one of the most important agents. Without claim extraction, fact-checking stays vague.

### 3. Source Finder / Document Retriever

Purpose: Obtain relevant source material.

Responsibilities:

- search for primary documents
- identify official regulations
- find standards
- find market data
- find technical references
- retrieve PDFs or web pages
- rank source quality
- distinguish primary, secondary, and weak sources

This agent should not decide whether the report is correct. It gathers material.

### 4. Evidence Assessor

Purpose: Evaluate source quality.

Responsibilities:

- determine whether a source supports a claim
- detect outdated sources
- detect jurisdiction mismatch
- distinguish explicit support from weak inference
- flag missing primary sources
- assign evidence strength

This should be separate from Source Finder. Retrieval and evidence judgment are different jobs.

### 5. Domain Reviewer

Purpose: Bring domain-specific reasoning.

This is not one agent. It is a slot filled according to the report domain.

Examples:

text Rail ticketing report:   rail interoperability reviewer   passenger rights reviewer   clearing/settlement reviewer   EU transport regulation reviewer  Datacenter report:   grid/power reviewer   land/permitting reviewer   datacenter engineering reviewer   financing reviewer   security/sovereignty reviewer 

Rainbox should select domain reviewers dynamically from the report’s detected disciplines.

### 6. Skeptic / Red-Team Reviewer

Purpose: Attack feasibility.

Responsibilities:

- identify optimistic assumptions
- find hidden dependencies
- detect uncosted work
- challenge timelines
- challenge stakeholder cooperation
- identify “sounds plausible but unsupported” claims
- produce concrete objections, not vibes

This agent should be required for any serious PlanExe report review.

### 7. Stakeholder Proxy

Purpose: Simulate the perspective of affected or resisting parties.

Examples:

text Rail report:   incumbent rail operator   independent distributor   regulator   passenger rights group   accessibility advocate  Datacenter report:   grid operator   local municipality   environmental authority   anchor tenant   national security authority 

This is important because many plans fail through stakeholder refusal, not technical impossibility.

### 8. Consistency Checker

Purpose: Find contradictions inside the report.

Responsibilities:

- detect inconsistent numbers
- compare executive summary against detailed sections
- check timeline vs WBS
- check risk mitigations against assumptions
- check scenario choice against strategic decisions
- check budget references for mismatch
- detect repeated or conflicting claims

This agent does not need external research. It works on internal coherence.

### 9. Patch Author / Report Editor

Purpose: Convert accepted findings into edits.

Responsibilities:

- write proposed patches
- preserve document style
- avoid rewriting unrelated sections
- link each patch to issue IDs
- include rationale and evidence references
- produce minimal diffs where possible

This should be the only agent allowed to propose final report text.

### 10. Consensus Chair

Purpose: Manage the decision process.

Responsibilities:

- state the issue being decided
- list agent positions
- identify agreement and disagreement
- ask for missing evidence
- determine whether consensus threshold has been met
- escalate unresolved issues to a human
- produce the final decision record

This role is distinct from the conversation manager.

The conversation manager says:

text Benny speaks next. 

The consensus chair says:

text The evidence supports revising the claim, but not deleting it. 

Do not merge these roles.

---

## What “Consensus” Should Mean

Rainbox should not treat consensus as “agents stopped arguing”.

That is too weak. Small models will agree too easily. Strong models may continue debating after enough evidence exists. Consensus must be a structured state.

Recommended consensus states:

text verified partially_verified unsupported contradicted needs_human_review not_enough_evidence out_of_scope 

Each reviewed issue should end with a decision record:

json {   "issue_id": "issue_123",   "decision": "partially_verified",   "confidence": "medium",   "consensus": {     "reached": true,     "policy": "qualified_consensus",     "agreeing_agents": [       "evidence_assessor",       "domain_reviewer",       "skeptic"     ],     "dissenting_agents": []   },   "evidence": [     {       "source_id": "source_abc",       "support": "partial",       "notes": "Supports the regulatory direction but not the proposed timeline."     }   ],   "recommended_action": "revise_claim",   "patch_required": true } 

Consensus should require more than one role.

For serious report changes, I would require at least:

text Evidence Assessor + Domain Reviewer + Skeptic 

For edits, additionally require:

text Patch Author + Consistency Checker 

For high-risk domains, add:

text Compliance/Legal Reviewer or Safety Reviewer 

---

## Consensus Policies

Rainbox should support several consensus policies, not one global rule.

### 1. Unanimous Consensus

Use when:

- legal claims
- safety claims
- financial claims with high impact
- report changes that remove major warnings
- claims that could mislead decision-makers

Rule:

text All required reviewers must agree, or the issue escalates. 

### 2. Qualified Consensus

Use for most report improvements.

Rule:

text A decision can pass if the Evidence Assessor, Domain Reviewer, and Skeptic all agree, even if a lower-priority participant has unresolved reservations. 

This is probably the default.

### 3. Majority Consensus

Use only for low-risk editorial questions.

Examples:

- wording
- section ordering
- summary clarity
- tone
- minor duplication

Do not use majority voting for factual correctness.

### 4. Chair Decides After Dissent

Use when the system must produce a result but disagreement remains.

The dissent must be preserved:

json {   "decision": "revise_claim",   "dissent": [     {       "agent": "stakeholder_proxy",       "objection": "The proposed wording may still overstate regulator willingness."     }   ] } 

This is useful because some disagreements are informative rather than blocking.

### 5. Human Arbitration

Use when:

- agents cannot resolve evidence conflict
- source quality is poor
- the proposed edit changes strategic direction
- the finding affects legal, financial, medical, safety, or compliance-sensitive content
- the system detects circular debate

Human arbitration should pause the run, not fail it.

---

## Required Agent Conversations by Task Type

### Task: Fact-check a claim

Required agents:

text Claim Extractor Source Finder Evidence Assessor Domain Reviewer Skeptic Consensus Chair 

Optional:

text Stakeholder Proxy Compliance Reviewer Patch Author 

Consensus requirement:

text Evidence Assessor and Domain Reviewer must agree. Skeptic must either agree or file a bounded dissent. If evidence is missing, the issue cannot be marked verified. 

### Task: Find missing documents

Required agents:

text Artifact Mapper Source Finder Domain Reviewer Evidence Assessor Consensus Chair 

Consensus requirement:

text Domain Reviewer confirms the document is relevant. Evidence Assessor classifies source quality. Consensus Chair records whether the document satisfies the original need. 

### Task: Improve a report section

Required agents:

text Artifact Mapper Skeptic Domain Reviewer Patch Author Consistency Checker Consensus Chair 

Consensus requirement:

text Patch Author proposes. Domain Reviewer approves substance. Consistency Checker approves internal consistency. Skeptic confirms no major objection remains. 

### Task: Rework assumptions

Required agents:

text Claim Extractor Skeptic Domain Reviewer Stakeholder Proxy Consensus Chair Patch Author 

Consensus requirement:

text At least one skeptical objection must be answered before the assumption is accepted. If the assumption remains uncertain, it should be marked as an assumption, not rewritten as fact. 

### Task: Update plan after new evidence

Required agents:

text Source Finder Evidence Assessor Domain Reviewer Impact Analyst Patch Author Consistency Checker Consensus Chair 

Consensus requirement:

text Evidence must be accepted before impact analysis. Impact analysis must identify affected sections. Patch Author must not edit only the local sentence if the change invalidates timeline, budget, risk, or WBS sections. 

### Task: Produce final revised report

Required agents:

text Patch Author Consistency Checker Citation / Evidence Checker Skeptic Consensus Chair 

Consensus requirement:

text No unresolved high-severity issue remains. All factual upgrades have evidence. All unresolved claims are explicitly labeled as assumptions or open questions. 

---

## How This Should Apply to PlanExe Reports

PlanExe reports are structured and broad. Rainbox should exploit that structure instead of treating the report as one blob.

A good PlanExe review run should probably create these review queues:

text claims_to_verify documents_to_find assumptions_to_recheck internal_consistency_issues stakeholder_objections risk_model_objections patches_proposed patches_accepted human_review_required 

The report sections then become work surfaces.

Example:

text Executive Summary:   Check whether it overstates certainty.  Assumptions:   Check whether assumptions are explicit, testable, and not disguised as facts.  Scenarios:   Check whether the selected scenario follows from the constraints.  Strategic Decisions:   Check whether decision trade-offs are realistic and non-duplicative.  Risks:   Check whether mitigations actually address root causes.  WBS / Timeline:   Check whether dependencies, durations, and critical path are plausible.  Budget:   Check whether major cost drivers are missing or inconsistent.  Documents to Create / Find:   Turn these into retrieval tasks for Rainbox agents. 

This is where Rainbox can become genuinely useful: not “agents chat about a report”, but “agents decompose a report into verifiable and editable units”.

---

## Proposed Rainbox Review Object Model

The document already has Artifact, Section, Claim, Source, Evidence, Question, Issue, AgentMessage, ProposedPatch, Decision, and Revision as a direction. I would make that central.

Suggested core model:

text Artifact   id   type   title   source_uri   version  Section   id   artifact_id   title   path   text_hash  Issue   id   artifact_id   section_id   type   severity   statement   status  Claim   id   issue_id   text   claim_type   verification_status  Source   id   uri   title   source_type   retrieval_date   quality_rating  Evidence   id   claim_id   source_id   support_level   notes  AgentPosition   id   issue_id   agent_id   stance   rationale   confidence  ConsensusDecision   id   issue_id   policy   result   confidence   dissent_summary  ProposedPatch   id   issue_id   target_section_id   patch_type   diff_or_replacement   rationale   status 

This is more important than the chat transcript. The transcript is auditability. The issue ledger is the product.

---

## Strong Recommendation: Separate Chat Messages from Decision Records

A chat transcript is not a decision log.

Rainbox should store both.

Bad:

text Agent A says it agrees, Agent B says DONE, so the report changes. 

Good:

text Issue #42:   Claim unsupported.   Evidence insufficient.   Domain reviewer agrees.   Skeptic agrees.   Patch proposed.   Consistency checker approved.   Consensus chair marked accepted. 

The report editor should consume the decision record, not scrape the chat.

---

## Where the Current Document Should Be Extended

### 1. Add a “Deliberation Protocol” section

The document should define:

text issue lifecycle required participants consensus policies decision states dissent handling human arbitration patch approval 

This is currently missing.

### 2. Add task-specific conversation templates

Current templates are persona-centric:

text egon-benny 

Rainbox needs task-centric templates:

text claim_fact_check source_retrieval assumption_review risk_review section_patch_review full_report_review 

Personas/agents are assigned to the template because the task demands them, not because the room wants two named characters.

### 3. Add evidence requirements

For fact-checking, every claim should have an evidence policy.

Example:

json {   "evidence_policy": {     "minimum_sources": 1,     "prefer_primary_sources": true,     "allow_unsourced_result": false,     "stale_after_days": 180,     "require_retrieval_date": true   } } 

For some domains, one primary source is enough. For others, the agent should explicitly say that the claim cannot be verified.

### 4. Add patch governance

Report edits should require a structured approval flow.

Suggested patch states:

text draft needs_review accepted rejected superseded applied 

Suggested patch rule:

text No patch is applied merely because one agent proposed it. 

Minimum approval:

text Patch Author proposes. Domain Reviewer approves substance. Consistency Checker approves local/global consistency. Consensus Chair accepts. 

### 5. Add unresolved-dissent handling

A good report-review system should not force false agreement.

Sometimes the correct outcome is:

text The agents disagree. The evidence is incomplete. The report should preserve uncertainty. 

That is not failure. That is good epistemic hygiene.

---

## Concern: “Consensus” Can Become Theater

Multi-agent systems often produce fake rigor. Three agents agree because they share the same model, same blind spots, and same prompt style.

Rainbox should defend against that.

Recommended mitigations:

1. Use role-specific evidence obligations.
2. Require agents to quote or reference evidence IDs, not just opinions.
3. Make the Skeptic file at least one objection before agreeing.
4. Preserve dissent instead of smoothing it away.
5. Use different model groups for high-value reviews when possible.
6. Use source-grounded agents for factual decisions.
7. Separate retrieval from judgment.
8. Separate judgment from editing.
9. Require the Consensus Chair to state why consensus is valid.
10. Escalate when evidence is missing.

Consensus should mean:

text The required roles have independently satisfied their obligations. 

Not:

text The conversation sounds harmonious. 

---

## Recommended Agent Council for PlanExe Report Iteration

For a serious PlanExe report review, I would use this default council:

text 1. Artifact Mapper 2. Claim Extractor 3. Source Finder 4. Evidence Assessor 5. Domain Reviewer 6. Skeptic / Red-Team Reviewer 7. Stakeholder Proxy 8. Consistency Checker 9. Patch Author 10. Consensus Chair 

But not all of them need to speak on every issue.

For each issue, Rainbox should select a minimal required subset.

Example:

text Minor wording issue:   Patch Author   Consistency Checker  Unsupported factual claim:   Source Finder   Evidence Assessor   Domain Reviewer   Skeptic   Consensus Chair  Strategic feasibility issue:   Domain Reviewer   Skeptic   Stakeholder Proxy   Patch Author   Consensus Chair  Internal contradiction:   Artifact Mapper   Consistency Checker   Patch Author   Consensus Chair 

This avoids turning every small edit into a committee meeting.

---

## Suggested Conversation Template: Claim Fact-Check

json {   "id": "claim_fact_check",   "goal": "Decide whether a specific report claim is supported, unsupported, contradicted, or requires human review.",   "participants": [     { "role": "source_finder", "required": true },     { "role": "evidence_assessor", "required": true },     { "role": "domain_reviewer", "required": true },     { "role": "skeptic", "required": true },     { "role": "consensus_chair", "required": true }   ],   "turn_policy": {     "mode": "phase_script",     "max_turns": 8   },   "phases": [     {       "speaker": "source_finder",       "task": "Find candidate sources and classify them."     },     {       "speaker": "evidence_assessor",       "task": "Assess whether the sources support the claim."     },     {       "speaker": "domain_reviewer",       "task": "Evaluate domain plausibility and missing context."     },     {       "speaker": "skeptic",       "task": "Challenge the strongest apparent conclusion."     },     {       "speaker": "consensus_chair",       "task": "Record decision, confidence, dissent, and recommended action."     }   ],   "done_when": {     "decision_states": [       "verified",       "partially_verified",       "unsupported",       "contradicted",       "needs_human_review"     ],     "requires_evidence_ids": true   } } 

This is much stronger than round-robin for serious review work.

Round-robin is good for Phase 0 testing. Phase-scripted deliberation is better for report improvement.

---

## Suggested Conversation Template: Patch Review

json {   "id": "patch_review",   "goal": "Decide whether a proposed report patch should be accepted, revised, rejected, or escalated.",   "participants": [     { "role": "patch_author", "required": true },     { "role": "domain_reviewer", "required": true },     { "role": "consistency_checker", "required": true },     { "role": "skeptic", "required": true },     { "role": "consensus_chair", "required": true }   ],   "turn_policy": {     "mode": "phase_script",     "max_turns": 7   },   "consensus_policy": {     "mode": "qualified_consensus",     "required_approvals": [       "domain_reviewer",       "consistency_checker",       "consensus_chair"     ],     "skeptic_must_have_no_high_severity_objection": true   },   "done_when": {     "artifact": "patch_decision",     "required_fields": [       "decision",       "rationale",       "remaining_risks",       "patch_status"     ]   } } 

---

## Suggested Conversation Template: Full Report Review

json {   "id": "full_report_review",   "goal": "Review a structured planning report and produce a prioritized issue ledger plus accepted patches.",   "participants": [     { "role": "artifact_mapper", "required": true },     { "role": "claim_extractor", "required": true },     { "role": "source_finder", "required": false },     { "role": "evidence_assessor", "required": false },     { "role": "domain_reviewer", "required": true },     { "role": "skeptic", "required": true },     { "role": "stakeholder_proxy", "required": true },     { "role": "consistency_checker", "required": true },     { "role": "patch_author", "required": true },     { "role": "consensus_chair", "required": true }   ],   "stages": [     "map_artifact",     "extract_issues",     "triage_issues",     "review_high_severity_issues",     "retrieve_sources_where_needed",     "propose_patches",     "approve_or_reject_patches",     "produce_review_summary"   ],   "outputs": [     "issue_ledger",     "source_ledger",     "consensus_decisions",     "accepted_patches",     "human_review_queue"   ] } 

This is probably the real Rainbox target.

---

## Product Boundary: PlanExe vs Rainbox

PlanExe should remain general-purpose plan generation.

Rainbox should be general-purpose artifact investigation and iteration.

The boundary should be:

text PlanExe:   Generate the first structured plan/report.  Rainbox:   Review, verify, debate, source, patch, and evolve the report. 

Rainbox should not assume every artifact is a PlanExe report. But PlanExe reports are an ideal first-class adapter because they are structured, sectioned, and rich in claims.

Recommended adapter model:

text PlanExeReportAdapter:   parse sections   identify report modules   extract claims and assumptions   identify documents-to-find   identify WBS and timeline objects   map risks and mitigations   generate review issues 

Rainbox core should stay artifact-generic.

---

## Revised Design Principle

The document’s implicit principle is:

text Get the boring bounded loop right first. 

That is correct.

But for Rainbox’s larger purpose, I would extend it:

text Get the boring bounded loop right first. Then make every conversation produce a durable decision artifact. 

Agents talking is not the product.

Agents producing inspectable, evidence-linked, consensus-backed report improvements is the product.

---

## Concrete Demands for Agent Consensus

Rainbox should enforce these demands before accepting a consensus result:

### 1. The issue must be explicit

No consensus without a named issue.

Bad:

text The agents discussed the report and agreed it is good. 

Good:

text Issue #17: The 36-month grid-capacity assumption may be unsupported. 

### 2. Required roles must be present

A factual claim cannot be verified without evidence roles.

A patch cannot be accepted without a consistency role.

A strategic assumption should not pass without a skeptic.

### 3. Each required agent must submit a structured position

Example:

json {   "agent": "domain_reviewer",   "stance": "partially_supports",   "confidence": "medium",   "rationale": "The direction is plausible, but the timeline is not supported by the retrieved sources.",   "blocking_objection": false } 

### 4. Evidence must be attached where relevant

For factual verification, consensus without evidence is invalid.

The allowed result should be:

text unsupported not_enough_evidence needs_human_review 

not:

text verified 

### 5. Dissent must be preserved

If an agent disagrees, the decision record must include the disagreement.

Dissent is not noise. It is often the most valuable output.

### 6. Consensus Chair must produce the final decision record

The chair should not silently decide. It must explain:

text what was decided why it was decided which agents agreed which agents dissented what evidence was used what patch/action follows 

### 7. Patches require separate approval

A fact-check decision and an edit decision are not the same.

The system may decide:

text Claim is unsupported. 

But the edit still requires:

text How should the report change? Should the claim be removed, softened, sourced, or moved to assumptions? 

### 8. High-risk issues must allow human escalation

Rainbox should not force closure on uncertain high-impact issues.

Valid terminal state:

text needs_human_review 

That should be considered success, not failure.

---

## Final Assessment

The Rainbox document is a strong Phase 0 / Phase 1 engineering design for bounded agent conversations. It is unusually grounded in the actual runtime constraints: queue semantics, journal routing, process isolation, watchdogs, prompt provenance, and UI control.

The main gap is that it currently treats conversation as the main object. For Rainbox to become useful for PlanExe report iteration, the main object should become the review issue.

The agents should not merely talk and reach a vague agreement. They should gather around explicit issues, produce evidence-linked positions, preserve dissent, and emit durable decision records and patches.

The architecture should therefore evolve from:

text bounded persona conversation 

to:

text bounded evidence-producing deliberation around artifact issues 

That is the leap from an interesting multi-agent chat system to a serious report-improvement system.

The conversation manager gives you control.

The issue ledger gives you usefulness.

The consensus protocol gives you trust.

The patch workflow gives you iteration.