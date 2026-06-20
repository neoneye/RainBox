---
id: answer-with-read-tools
status: active
created_by: human
source_journal_id:
source_step_id:
supersedes:
retrieval_tags: [tools, query, status, git, memory, kanban, knowledge]
updated_at: "2026-06-20T00:00:00Z"
---

# Answer using read-only tools

When the operator asks about project or git status, the knowledge base, kanban
boards, or remembered facts, gather the answer with a read action before
replying:

- `query_qa` for project/git status and knowledge-base questions.
- `query_memory` for remembered facts and preferences.
- `kanban_read` for board and card state.
- `workspace_read_command` for inspecting files (read-only commands only).

Prefer one focused query, then `reply`. Do not guess when a tool can confirm.
