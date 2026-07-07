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

## Subsystem designs

- [assistant-design.md](assistant-design.md) — the ReAct loop: capability
  registry, write tiers (log-and-undo / confirm), undo ledger, controls,
  trace, inspector.
- [cron-design.md](cron-design.md) — the scheduler: folder tree, action
  types, firing, outcomes, retries, reminders.
- [kanban-design.md](kanban-design.md) — boards as a coordination ledger:
  worker leases, authority dispatcher, serializations, assistant
  capabilities.
- [conversation-design.md](conversation-design.md) — bounded
  persona-to-persona conversations: manager, CAS turn guards, pause/resume.
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
- `memory-systems/` — comparative survey of external memory systems.

## Data & frontend conventions

- [data-model.md](data-model.md) — the Postgres tables and design notes.
- [chat-frontend-rules.md](chat-frontend-rules.md) — the /chat idle-cost
  rules (no polling, SSE, streaming) and template-editing gotchas.
- [ui-left-panel-tree.md](ui-left-panel-tree.md) — the shared folder-tree
  pattern (/chat, /cron, /kanban, /git) and its porting checklist.
- [ui-modals.md](ui-modals.md) — the app-wide modal pattern.

## Proposals & reviews

- `proposals/` — dated design proposals and reviews; start with
  [2026-06-25-security-review-mitigations.md](proposals/2026-06-25-security-review-mitigations.md)
  (the open control-plane findings referenced throughout the docs above).
