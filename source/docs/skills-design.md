# Skills — design

**Status: built and running.** Skills are reusable "how to" procedures stored
as markdown files with YAML frontmatter, retrieved per assistant turn and
injected into the prompt as procedural guidance. The subsystem is
`skills/loader.py` (files → `Skill` records, lifecycle rules) and
`skills/retrieval.py` (lexical ranking, prompt block, telemetry). The
assistant is the only consumer today.

Skills are a **trust boundary**: an active skill changes how the assistant
behaves on matching requests. The lifecycle is therefore the same
candidate-first shape as memory: the assistant may *propose* a skill
(inert), but only the operator's confirmation *activates* it.

## Skill files

A skill is one `<id>.md` file — YAML frontmatter + markdown body:

```markdown
---
id: answer-with-read-tools
status: active
created_by: human
retrieval_tags: [status, live, data]
---
# Answer live questions with read tools

First paragraph summarising when this applies…
```

- `id` — a lowercase kebab slug (`^[a-z0-9][a-z0-9-]*$`); ids with path
  separators are rejected. The filename is `<id>.md`.
- `status` — `candidate` | `active` | `superseded` | `rejected`.
- `created_by` — `human` or `assistant`.
- `retrieval_tags` — extra lexical hooks for retrieval.
- `supersedes` — optional predecessor id.
- `source_journal_id` / `source_step_id` — provenance for assistant-proposed
  skills (the journal + step that proposed it).
- The body's first `#` heading is the title; the paragraph after it is the
  retrieval summary.

## Two directories, overlay wins

Like the Q&A registry, skills merge from two locations:

- **Base** — `data/skills/*.md`, shipped with rainbox (publishable, no PII).
- **Operator overlay** — `<customize.dir>/skills/*.md` (the `customize.dir`
  setting; same overlay pattern as `question_answer.jsonl`). Assistant
  proposals are written here.

Resolution rules (`load_skills`):

1. Load base, then overlay; overlay wins over base for the same id.
2. A `rejected` overlay entry **suppresses** the base skill with that id (and
   rejected skills are never returned as available).
3. `candidate` skills load but are never injected — the retrieval layer
   filters to `active` ("candidates are inert").
4. `supersedes` hides the predecessor only while the successor is `active`.
5. A `supersedes` cycle invalidates every skill in the cycle.
6. Duplicate ids inside one directory are an error — **neither** file is
   chosen (never last-write-wins).

The loader is deliberately robust: a malformed or conflicting file is skipped
with a warning rather than breaking a live assistant turn. `lint_skills()`
reuses the same parser to report unparseable files — surfaced by
`tools/doctor.py`.

## Retrieval and injection

`retrieve_skills(query)` is **deliberately lexical** in v1: token overlap
(stopword-filtered) between the query — the latest human message — and the
skill's title + `retrieval_tags` + headings + first paragraph. Best overlap
first, id as a stable tiebreak. Do not build a second semantic retriever just
for skills; the plan is to upgrade facts and skills to hybrid retrieval
together.

`build_skill_block(query, …)` assembles the prompt block under explicit
budgets — at most `MAX_SKILLS_INJECTED = 3` skills and
`MAX_SKILL_BLOCK_CHARS = 2000` — and records retrieval telemetry: every
ranked skill gets a `considered` event, every skill that fits the budget an
`injected` event (`target_type="skill"`, `source="skills.retrieval"`; see
`relevance-telemetry.md`). Telemetry failures never break a turn.

The assistant calls this once per turn (`_build_skill_block`) and places the
block after the user-profile block and before the transcript — profile is
*who the operator is*, skills are *how to do the task*.

## Lifecycle: who may change what

| Transition | Path | Guard |
|---|---|---|
| create candidate | assistant `propose_skill` (log-and-undo) → `write_candidate_skill` | overlay must be configured; clean slug; **never overwrites** an existing id |
| candidate → active | assistant `activate_skill` (**confirm-tier**) or operator edits the file | `set_skill_status(..., if_current="candidate")` — only a genuine candidate is promoted |
| undo of propose | `skill_delete` (internal, via the undo ledger) → `delete_skill_file(if_status="candidate")` | only deletes a still-pending candidate — never a skill the operator activated since |
| reject / supersede | operator edits the file's frontmatter | rejected suppresses; supersedes hides predecessor while active |

The conditional guards (`if_current` / `if_status`) are the same
version-guard discipline as the assistant's other reversible writes: an undo
or activation can never clobber a state that changed since the original
action.

## Design principles

- **Candidates are inert.** A proposed skill has zero effect on behavior
  until an explicit activation. This is enforced in the retrieval layer, not
  by prompt discipline.
- **Files, not rows.** Skills live in the operator's customize checkout so
  they are diffable, versionable, and editable with a text editor; the DB
  holds only telemetry about their use.
- **Budgeted injection.** A skill that doesn't fit the block budget is
  `considered` but not `injected` — visible in telemetry, absent from the
  prompt. No silent truncation of a skill body.
- **Every influence is explainable.** The `considered`/`injected` events per
  turn answer "why did the assistant follow this procedure?".

## Reference

| Thing | Where |
|---|---|
| Loader + lifecycle rules | `skills/loader.py` (`load_skills`, `write_candidate_skill`, `set_skill_status`, `delete_skill_file`, `lint_skills`) |
| Retrieval + prompt block | `skills/retrieval.py` (`retrieve_skills`, `build_skill_block`, budgets) |
| Assistant integration | `agents/assistant.py` (`_build_skill_block`, `propose_skill` / `activate_skill` / `skill_delete` capabilities) |
| Base skills | `data/skills/` |
| Overlay | `<customize.dir>/skills/` |
| Doctor lint | `tools/doctor.py` |
| Tests | `skills/test_loader.py`, `skills/test_retrieval.py`, `agents/test_assistant_skills.py` |

## See also

- `assistant-design.md` — the propose/activate capabilities and the undo
  ledger they ride on.
- `qa-system.md` — the sibling overlay pattern for facts.
- `relevance-telemetry.md` — the skill retrieval events.
- `proposals/2026-06-19-improvements-v2.md` — the original resolution-rule
  draft.
