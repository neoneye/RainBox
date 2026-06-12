# agent_profiles

File-backed persona / conversation data for the persona + agent-to-agent
conversation feature (see `docs/proposals/2026-06-08-persona-prompts-and-agent-conversations.md`).

- `personas.jsonl` — one persona record per line. Each maps a runnable
  `agent_uuid` (declared in `agents/config.py`) to a display name and a system
  prompt file. Fields: `id` (stable persona id), `slug`, `name`, `description`,
  `system_prompt_path` (relative to this dir), `agent_kind` (the Python agent
  class to run), `agent_role` (the supervisor role name in `agents/config.py`),
  `agent_uuid` (runnable identity), `tags`, `enabled`.
- `prompts/*.system.md` — the persona system prompts, plain Markdown so they
  diff and review well. Keep a literal stop token (`DONE`) and a termination
  rule at the end; the conversation manager enforces `max_turns` regardless.
- `conversations/*.json` — conversation templates: which personas participate,
  their turn order, and the turn policy (`max_turns`, `stop_phrases`, …).

The loader is `persona.py`. Phase 0 reads these files directly; a later phase
imports them into Postgres while keeping file import/export.
