# Docs index

One line per document. Design docs describe **current state** (git holds the
history); `proposals/` holds design-time intent and reviews.

## Operating rainbox

- [operator-guide.md](operator-guide.md) — start the app, pages map, chat
  workflow, memory commands, evals, backup, troubleshooting.
- [backup.md](backup.md) — encrypted database backups (age + zstd): keys,
  scheduling, git push, restore, threat model.
- [testing.md](testing.md) — running the suite, the sandbox database, marker
  tests, known failures.
- [deep-research-tryout.md](deep-research-tryout.md) — operator walkthrough
  for `python -m research`: model group setup, search keys, example runs,
  troubleshooting.

## Subsystem designs

- [supervisor-design.md](supervisor-design.md) — the core runtime: the
  inbox→journal queue, spawn-on-demand child processes, heartbeat watchdog,
  recovery, routing, the agent class hierarchy.
- [settings-design.md](settings-design.md) — typed operator settings: the
  code-owned registry, DB → env → default resolution with provenance, the
  /settings page, Q&A repopulate actions.
- [assistant-design.md](assistant-design.md) — the ReAct loop: capability
  registry, write tiers (log-and-undo / confirm), undo ledger, controls,
  trace, worker-failure recovery, chat notices, inspector.
- [second-opinion-design.md](second-opinion-design.md) — the independent LLM
  review that gates `python_run` before execution: verdict, prompts, model
  binding, fail-open policy, inspector rendering.
- [prompt-design.md](prompt-design.md) — versioned system prompts: folder
  tree, clone lineage + diff, explicit Edit → Save, direct-chat linking.
- [cron-design.md](cron-design.md) — the scheduler: folder tree, action
  types, firing, outcomes, retries, reminders.
- [kanban-design.md](kanban-design.md) — boards as a coordination ledger:
  worker leases, authority dispatcher, serializations, assistant
  capabilities.
- [find-uuid-design.md](find-uuid-design.md) — the cross-table uuid
  resolver behind /find and the assistant's `find_uuid` action:
  exact/substring/fuzzy/mention passes, sources, ranking, Q&A shields.
- [git-design.md](git-design.md) — the /git page: registered-repo pointers,
  the guarded tree save, read-only inspection (two rev-parse reads, fixed
  argv, 5 s timeout), deliberate non-goals.
- [profile-design.md](profile-design.md) — person profiles: the
  field-registry-driven form + validator, sparse data JSONB, the
  server-owned `dynamic` and `calibration` subtrees, built-in locale
  templates.
- [profile-guidance.md](profile-guidance.md) — the profile-driven prompt
  blocks (formatting guide + knowledge calibration): architecture, the
  default-off switches, and the verification/enablement runbook (tests →
  browser → prompt inspection → live evals → release gate).
- [conversation-design.md](conversation-design.md) — bounded
  persona-to-persona conversations: manager, CAS turn guards, pause/resume.
- [direct-chat.md](direct-chat.md) — one-to-one operator↔model rooms:
  turn lifecycle, model resolution (room pick → global default), linked
  system prompts, streaming rows, transcript editing.
- [skills-design.md](skills-design.md) — procedural skills: file format,
  overlay resolution, inert candidates, retrieval + injection budgets.
- [qa-system.md](qa-system.md) — the curated Q&A knowledge base: registry,
  retrieval, dynamic handlers, shields.
- [voice-and-services.md](voice-and-services.md) — the side services map:
  Whisper STT, Kokoro TTS, Telegram bridge, multimodal demo proxy.
- [llm-providers.md](llm-providers.md) — the provider registry (LM Studio /
  Jan / Ollama): sync, resolution, probes, adding a provider.
- [deep-research.md](deep-research.md) — the research pipeline: pluggable
  web search, SSRF-guarded fetching, subtask researchers, cited reports,
  injection posture.
- [benchmarks.md](benchmarks.md) — the benchmark harnesses and their
  killable subprocess runner shape.

## Memory

- [memory-architecture.md](memory-architecture.md) — the full memory system:
  claims/evidence/tombstones, governed writes, retrieval, embeddings,
  review UI, telemetry, eval loop.
- [memory-commands.md](memory-commands.md) — the operator chat commands
  (remember / forget / confirm / correct / recall / explain).
- [memory-trust-hardening-tryout.md](memory-trust-hardening-tryout.md) —
  hands-on verification guide for the trust guarantees.
- [relevance-telemetry.md](relevance-telemetry.md) — the retrieval_event
  log: producers, stages, rollups, interpretation.
- [eval-loop.md](eval-loop.md) — feedback → eval case → run → gate →
  optimizer.
- [eval-playbook.md](eval-playbook.md) — the practical eval workflow.
- [evals-design.md](evals-design.md) — the `evals/` framework internals:
  case model, run lifecycle, scoring math, comparison/gate/optimizer/monitor
  mechanics, extension points.
- `memory-systems/` — comparative survey of external memory systems.

## Data & frontend conventions

- [data-model.md](data-model.md) — the Postgres tables and design notes.
- [chat-frontend-rules.md](chat-frontend-rules.md) — the /chat idle-cost
  rules (no polling, SSE, streaming) and template-editing gotchas.
- [ui-left-panel-tree.md](ui-left-panel-tree.md) — the shared folder-tree
  pattern (/chat, /cron, /kanban, /git) and its porting checklist.
- [ui-modals.md](ui-modals.md) — the app-wide modal pattern.
- [ui-modal-rename.md](ui-modal-rename.md) — renaming is modal-confirmed
  (click-to-rename name display, Cancel/Rename only) and why.
- [ui-kebab-menu.md](ui-kebab-menu.md) — the 3-dot overflow menu pattern:
  fixed positioning, viewport clamping (flip above when below won't fit).

## Proposals & reviews

- `proposals/` — dated design proposals and reviews; start with
  [2026-06-25-security-review-mitigations.md](proposals/2026-06-25-security-review-mitigations.md)
  (the open control-plane findings referenced throughout the docs above).
