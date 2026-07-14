# Kanban — design

The kanban board at `GET /kanban` is the coordination primitive from
docs/plan.md ("Kanban board"): agents keep track of progress in Postgres
because editing markdown todo lists is too fragile for small models — instead
of "rewrite the document correctly", an agent calls **narrow, uuid-addressed
operations** that succeed atomically or fail loudly.

## Architecture

- **Tables** (`db/models.py`, created by `init_db`'s `create_all()`):
  `kanban_board`, `kanban_board_folder`, `kanban_column`, `kanban_task`,
  `kanban_task_event` (append-only audit trail). Plain uuid reference columns,
  no FKs — the cron-tables pattern, integrity enforced app-side. New boards
  get the default columns `To do` / `In progress` / `Done`
  (`KANBAN_DEFAULT_COLUMNS`).
- **Backend** (`db/kanban.py`, re-exported from the `db` facade): board and
  folder CRUD, board duplication, the guarded bulk per-board and per-tree
  saves for the page, the markdown/JSON serializers, and the agent
  operations. **API** (`webapp/kanban_api.py`).
- **Page** (`webapp/kanban_views.py` shell + `static/kanban.js` logic): a
  left-panel folder tree (folders → boards; the shared pattern in
  `docs/ui-left-panel-tree.md`, mirroring /cron) with per-node kebab menus
  (rename, new subfolder, duplicate board, copy id, delete), tree
  drag-and-drop (folder 3-zone, board 2-zone, root strip), columns/cards with
  drag-and-drop, modal CRUD (shared `ui-modal.css`), the task's audit trail +
  lease state in the edit modal, a serialization view/copy sidebar, and a
  single `?id=<uuid>` deep link that accepts a **board, folder, or task**
  uuid — a task uuid selects its board and opens that task's overlay (the
  edit modal's "Copy link" button produces these). House rules:
  desktop-first, no native dialogs, mtime cache-buster.
- **Agent side**: `tools/kanban_dispatcher.py` (authority enforcement),
  `tools/kanban_runner.py` (the claim→work→complete protocol),
  `agents/kanban_worker.py` (the LLM worker), `benchmarks/kanban.py`
  (the /benchmark_kanban eval page + CLI).

## Three write surfaces

The boundaries are strict: **humans use the bulk saves; workers use only the
row-level operations; the personal assistant uses only its log-and-undo
capabilities.**

**1. The page's bulk saves** — whole-document PUTs for human editing:

- `PUT /kanban/api/board/<uuid>` with the whole board: validation before any
  mutation (uuid shape/uniqueness, every task references a column in the
  payload, `agentUuid` null-or-uuid); an optimistic-concurrency **version
  token** (`kanban_board_version`, a digest of user-managed fields) — GET
  returns it, PUT must echo it, stale → **409** and the page re-hydrates
  instead of clobbering (so an agent's write can never be silently
  overwritten by a stale tab, and vice versa); and a **declared-deletes
  tripwire** (`deletes` count) so a truncated payload can't wipe a board.
- `PUT /kanban/api/tree` with the whole folder/board placement — a
  **placement-only** save (names and content are not editable through it),
  with its own version token (`kanban_tree_version`) and the same
  409-and-rehydrate contract. Folder delete reparents children; it never
  cascades to boards.

UI saves also append audit events ('created', 'moved' with column names) so
human edits land in the same trail as agent operations. Saves are debounced
(250 ms) and serialized — one in-flight PUT at a time.

Adjacent human-surface endpoints: `POST /kanban/api/boards` (create, accepts
`folderId`), `DELETE /kanban/api/board/<uuid>` (cascade),
`POST /kanban/api/board/<uuid>/duplicate` (deep clone with fresh uuids; the
audit trail is not copied), `POST /kanban/api/folders`,
`DELETE /kanban/api/folders/<uuid>`.

**2. Worker operations** — row-level, uuid-addressed, each recorded in
`kanban_task_event`.

**Assignee vs lease:** `agent_uuid` is the *responsible* agent (set by humans
via the bulk save); `claimed_by` + `claimed_at` + `claim_expires_at` are the
*current worker's lease* (set only by the claim operations). Leases default
to 15 min (`KANBAN_CLAIM_LEASE`) and expire at read time — no sweep: a
crashed agent stops renewing and the task becomes claimable again, including
by another instance of the same agent. A working agent heartbeats via renew;
completing (ok or failed) releases the lease, so the retry path is
`complete(ok=false)` → claim again.

| Operation | Endpoint | Semantics |
|---|---|---|
| claim-next | `POST /kanban/api/claim-next {agentUuid, boardId?, includeUnassigned?}` | the DB atomically finds + LEASES one eligible task (my live-leased task first — a worker restart resumes its work — then my assigned, then unassigned; never another agent's live lease, never the board's last column; earlier boards/columns first) via `FOR UPDATE SKIP LOCKED`. `task: null` when nothing is eligible |
| claim | `POST /kanban/api/tasks/<uuid>/claim {agentUuid}` | lease a SPECIFIC task; **409** while another agent's lease is live; an EXPIRED lease is taken over (recorded); re-claiming one's own lease renews it |
| release | `POST /kanban/api/tasks/<uuid>/release {agentUuid}` | give the lease back (done / bowing out); a live lease only by its holder (**409**), an expired one by anyone; unclaimed = no-op |
| renew | `POST /kanban/api/tasks/<uuid>/renew {agentUuid}` | extend one's own lease (long-runner heartbeat); no event |
| move | `POST …/move {columnUuid, actor?, note?}` | move within the task's board (appended at the end); 400 for a foreign column |
| move to board | `POST …/move-to-board {boardId, actor?}` | move to another board keeping uuid, audit trail, assignee, and lease; the column **carries over** (same name case-insensitively, else same position, else the target's first — a board move is not a state change) and the 'moved' event names both boards. Same board = no-op. The task modal's Board select uses this — the page's per-board bulk save cannot express a cross-board move without delete + recreate |
| append event | `POST …/events {kind, actor?, detail?}` | progress notes / arbitrary audit entries; `GET` lists them |
| complete | `POST …/complete {ok, actor?, detail?}` | ok → move to the board's **last column** (done by convention) + 'done' event; not ok → **stays put** + 'failed' event with the reason (failure is information for the operator, not an automatic state change). Both outcomes **release the lease**. The dispatcher passes `review=True` for unverified agents, routing ok=true to a Review column (see Execution) |
| get task | `GET /kanban/api/tasks/<uuid>` | one task's brief incl. its `boardUuid` (used by the `?id=<task>` deep link) |

**Structured output vs function calling:** the primitives are
mechanism-agnostic — expose them as function-calling tools, or have a
structured-output reply name `{op, taskUuid, …}` and dispatch to the same
`db.kanban_*` functions. The /benchmark_kanban 2×2 matrix settled the
default empirically: **markdown context + structured output** — more
reliable (every model solved the md+struct spec; the tools variants broke
most models) and faster on every target. Function calling stays available
behind the same dispatcher, but only nemotron-3-nano and qwen3-coder handled
it flawlessly; granite4 is the speed pick on the struct path; supergemma4
fails the JSON-context path.

**3. The personal assistant's log-and-undo capabilities** — see the next
section. These are code-owned writes whose safety model is operator
reversibility + trace, not the worker authority ceiling; they do **not**
route through observe/work/shape.

## Assistant capabilities (log-and-undo)

The personal assistant (`agents/assistant.py`) exposes kanban to chat. One
read action and five prompt-exposed write families; every write returns an
`undo` descriptor `{capability, payload}` that is recorded as a completed row
in the undo ledger, and the `/undo` endpoint replays it. Two capabilities
exist only as undo inverses and are never prompt-exposed.

| Capability | Semantics | Undo |
|---|---|---|
| `kanban_read` | read without writing events, every observation JSON: `task_uuid` → task detail + board columns (move targets) + 10 recent events; `board_uuid` → the board's `kanban_board_llm_json` document; neither → every board in its folder tree (nested nodes, folder + board uuids, task counts) | — (read) |
| `kanban_move_task` | move a task; `column_uuid` accepts the column's **name (case-insensitive) or uuid** (operators say "In progress", not a uuid); a no-op move (destination = source) is refused with the available columns listed, so the model can't claim a move that never happened | inverse move |
| `kanban_complete` | mark a task done — operator-proxy intent, so it goes straight to Done (`review=False`), not worker review-routing | move back to the prior column |
| `kanban_comment` | append a 'comment' event | the trail is append-only, so undo posts a `↩ retracted: …` comment (which itself needs no further undo) |
| `kanban_create_task` | create a task; an omitted/unresolvable `column_uuid` falls back to the board's **first column** | `kanban_delete_task` (internal) |
| `kanban_create_board` | create a board with the default columns; the store assigns the uuid | `kanban_delete_board` (internal) |

Undo is **position-aware**: a move-undo carries `expect_column` (where the
original write left the task) and refuses if the task has since moved —
don't yank it from where it now sits. Guard rails around the write loop:
duplicate same-run writes are blocked, the assistant may not claim a write it
never performed, and every kanban write appends a `/kanban?id=<task-or-board>`
link to the reply so the operator can jump straight to what changed. A
test locks the prompt-exposed action surface so internal inverses can't leak
into the prompt.

Separately, the query subsystem's `get_kanban_overview` handler
(`agents/query_handlers.py`) renders every board grouped by folder with
per-column task counts, for "what's on the boards?"-style questions.

## Agent permission model

Agents should not all receive the same write authority. Three
**authorities** match the actual API surfaces instead of a large policy
matrix nobody needs yet:

| Authority | Allowed behavior |
|---|---|
| observe | Read board serializations, task metadata, and task events; append comment/suggestion-kind events. |
| work | Claim, renew, release, append progress, and complete/fail tasks the agent is allowed to claim. |
| shape | Create, edit, move, route, close, delete, or otherwise reshape board state. Human-only by default. |

Per-agent `kanban_authority` (default **observe**) and `kanban_verified` live
in the agent_config registry and are enforced in
`tools/kanban_dispatcher.py`: every runner write dispatches through
`kanban_dispatch(agent_uuid, op)`, and denials land in the trail as
'permission-denied' events. The enforcement point is the dispatcher, not the
model prompt — a model can propose an operation, but code decides whether it
is permitted before calling `db.kanban_*`.

The finer permission names from `docs/plan.md` remain vocabulary, not
implementation targets:

| Planned split | Fits under | When to split it out |
|---|---|---|
| read_only, suggest_only, comment_only | observe | When suggestions/comments need different rate limits, review paths, or UI treatment. |
| close_card | work / shape | When "complete my claimed task" and "close arbitrary task" need different grants. |
| draft_create, move_card, edit_card, admin | shape | When a real non-human agent earns one part of shape without earning the rest. |

> The assistant's kanban capabilities deliberately sit outside this model —
> log-and-undo writes whose safety is operator reversibility + trace, not the
> worker authority ceiling.

**Known gap (follow-up):** the dispatcher chokepoint is in-process only. The
HTTP operation endpoints (`/kanban/api/claim-next`, `…/claim`, `…/release`,
`…/renew`, `…/move`, `…/complete`, `…/events` POST) predate it, take
`agentUuid`/`actor` from the request body, and call `db.kanban_*` directly —
no authority check, `complete` defaults `review=False`. Until they are routed
through `kanban_dispatch` (or formally declared operator-only), treat them as
a trusted/operator surface, not an agent surface. The whole kanban API is
also unauthenticated HTTP like the rest of the control plane — Finding 8d of
`docs/proposals/2026-06-25-security-review-mitigations.md` folds it into the
planned Phase 1 auth boundary.

## Execution (enqueue-on-command)

`POST /kanban/api/tasks/<uuid>/enqueue` (the task modal's **Run** button)
enqueues the task's ASSIGNED agent with `{task_uuid, board_uuid, source:
"kanban"}` via the supervisor inbox — 400 for unassigned / not-runnable
assignees, 409 while a live lease holds the task. No polling: execution is
operator-triggered.

The board protocol lives in ONE adapter, `tools/kanban_runner.py`, so agents
don't each reimplement it:

    claim (conflict/vanished task → clean skip) → 'started' event
      → work(task, board_markdown) → complete/fail

The first obligation after claim is readiness: can the worker verify what
"done" means for this card? If not, it should fail the task with a precise
reason such as `unclear acceptance criteria: ...` rather than producing
confident nonsense. That failure is a quality signal and belongs in the
ledger.

The lease is released on every path, including a crashed `work` fn. An agent
adopts kanban by writing only its `work(task, board_markdown) -> (ok,
detail)` callback. Two consumers exist:

- **workspace_shell** (`tools/workspace_shell_chat.py`) runs the task's
  description as its command (validated argv, no shell,
  workspace-confined), output into the event trail as 'progress', ok =
  exit 0. Its exit codes are real verification, which is why it is the
  verified consumer.
- **kanban_worker** (`agents/kanban_worker.py`, `KanbanWorkerAgent`,
  `kanban_worker` in agent_config) — the LLM worker. One structured call per
  card → `{status: done|unclear|failed, deliverable, comment}`; the
  deliverable lands in the event trail as a 'progress' event; readiness is
  folded into the call (`status=unclear` → `complete(ok=false, "unclear
  acceptance criteria: …")`). All its board writes go through the
  dispatcher; the runner serves it the `focus=in-progress` board view.

For unverified LLM workers (`kanban_verified` false), the dispatcher routes
successful work to a **"Review"-named column** (case-insensitive, fallback
the last column) with a 'review' event, instead of treating `ok=true` as
directly done. A human, and later a verifier agent, moves Review to Done.
Direct-to-Done is an earned capability based on ledger evidence: review pass
rate, done/failed ratio, lease-takeover count, event quality, and absence of
permission violations.

To run the LLM worker for real (tests fake the LLM call): bind
`kanban_worker` to a model group on `/agent_models` (granite4 per the
benchmark verdict), add a column named "Review" to a board, assign the worker
a card with a clear description, press the card's **Run** button, then read
the card's event trail and the Review column.

## Serialization contracts (the LLM-facing read views)

Two equivalent representations of one board, both generated **server-side
from DB state** and both meant as LLM context input — markdown reads
naturally inside a chat-style prompt, JSON suits structured prompts and tool
results; they carry the same ids, so either works as the read side of the
operations. The page's Developer sidebar shows both. **Markdown is the
default LLM context** (the /benchmark_kanban verdict: faster than JSON in
every mode and the only format every model read ids from correctly).

`GET /kanban/api/board/<uuid>/markdown` (text/markdown):

```markdown
# Kanban board: Website relaunch

Board id: `boardId`

Optional board description (escaped, see below).

## To do (`columnId`)

- **Write copy** (`taskId`) — @persona_egon (`agentId`)
  Optional task description, indented under the bullet.
- **Pick a font** (`taskId`) — _unassigned_

## In progress (`columnId`)

_(empty)_
```

Rules: the markdown carries the **same ids as the JSON twin** under the same
role names (boardId / columnId / taskId / agentId), so a model can quote any
of them when invoking operations — e.g. a move needs the target columnId,
which it reads off the heading. Columns are `##` headings in board order,
each ending with its backticked columnId; a task is one bullet — bold title,
backticked full taskId, `@agent (\`agentId\`)` (display name resolved from
agent_config) or `_unassigned_`; description lines indented two spaces; empty
columns render `_(empty)_`. Done-ness is the column, not checkboxes.

The JSON twin (`GET /kanban/api/board/<uuid>/json`) nests tasks inside their
column, resolves `agentName` next to `agentId`, and omits the version token
(a read snapshot, not a save payload).

**`focus=in-progress`** (query param on both endpoints, and on the
`kanban_board_markdown`/`kanban_board_llm_json` functions) is the asymmetric
view matching what an executing agent needs: the first column shrinks to a
title+id list, the last column to a summary, and the middle columns get full
content plus lease state and the 5 most recent task events as working
memory. The default (no params) is the full symmetric document. A
`detail=full|brief` knob (brief = titles + ids only, for tight context
budgets) is designed but deferred.

**Spoof resistance:** board/task text cannot forge structure. Inline values
(titles, names) are flattened to one line with `` \` * _ [ ] ( ) ``
backslash-escaped; description lines that start like a heading/bullet/quote/
fence get their first token escaped. Parsing contract: only *unindented*
`- **…**` lines are tasks and only `##` lines are columns — everything
indented is content. (A description can still *mention* a uuid, but it cannot
mint a structurally valid task or column line.)

## Wire shapes

```js
// GET/PUT /kanban/api/board/<uuid>
{ uuid, name, description,
  columns: [{uuid, name}],                       // order = position
  tasks:   [{uuid, columnUuid, title, description, agentUuid|null,
             claimedBy|null, claimExpiresAt|null}],  // lease fields read-only
  version }                                      // PUT also sends `deletes`

// GET/PUT /kanban/api/tree — placement only
{ folders: [{uuid, name, description, parentId|null, position}],
  boards:  [{uuid, name, folderId|null, position, taskCount}],  // taskCount read-only
  version }
```

`agentUuid` is the agent_config uuid — stable across role renames; names are
display-only (the page is rendered with `{name, uuid}` pairs for the picker).
The bulk save ignores the lease fields; only the claim operations write them.

## Task links

Planned: tasks may reference other tasks, including tasks on other boards.
Links let coordination agents express structure without moving cards
prematurely.

Possible table:

```text
kanban_task_link  id, uuid, source_task_uuid, target_task_uuid,
                  relation, created_by, created_at
```

Candidate relations: `duplicates`, `blocked_by`, `related`, `split_from`,
`supersedes`.

Early cross-board agents should default to suggesting links rather than
moving cards. Before this table exists, represent a proposed link as a
`kind='suggestion'` event with structured detail and a UI affordance for a
human to accept it. Direct link creation can graduate later once the relation
taxonomy and UI are stable.

## Design principles

- **The board is a coordination ledger, not a workflow engine.** Its value is
  that agent work is *legible, interruptible, and recoverable*: every unit of
  work is a row with an id, an owner, a lease, and an append-only history.
  Columns are states; the moment they become pipeline stages with per-column
  behavior, the board is BPMN and the robustness argument is lost.
- **Models propose, code disposes.** Authority and ground truth live in the
  dispatcher, never the prompt.
- **Verification beats permission.** Permissions bound blast radius *before*
  acting; verification catches wrong work *after*. "Done" from an LLM is a
  claim, not a fact (workspace_shell's exit codes are the exception — which
  is why it went first). Autonomy is *earned from the ledger*: review pass
  rate, done/failed ratio, lease takeovers, and event quality justify each
  promotion (review-required → direct-to-done → board-level run → cron).
- **Events are the working memory.** The trail is how a small model resumes
  work it doesn't remember doing — after a lease expiry, a restart, or a
  takeover by a sibling instance — and the substrate suggestions start on.
- **The serialization is the context budget.** A board that no longer fits in
  a small model's context is one agents can't reason about whole; that is the
  real WIP limit. Boards per project; archive Done eventually (events kept).
- **Reversibility is an alternative authority model.** The worker path bounds
  blast radius with permissions; the assistant path bounds it with undo +
  trace. Both keep the human able to see and reverse what an agent did.

## Roadmap

Built: the board page with folder tree and deep links; the bulk-save and
tree-save surfaces with version tokens; the row-level worker operations with
leases; the authority dispatcher with Review-before-Done routing; the runner
adapter with workspace_shell and kanban_worker as consumers; the
`focus=in-progress` serialization; the assistant's log-and-undo capability
family with the undo ledger; and the /benchmark_kanban first slice that
settled markdown + structured output as the defaults.

Remaining, in order:

1. **Board-level run.** A "run assigned tasks" sweep that enqueues runnable
   cards while respecting assignment, live leases, readiness, and authority.
   Groundwork worth doing with it: `focus=in-progress` events currently
   render newest-first with details clipped to 200 chars; for "resume from
   the trail", oldest→newest and a larger budget for the last deliverable may
   read better for small models.
2. **Secretary + suggestion workflow.** The first observe-authority secretary
   prompt modes (intake, triage, duplicates, staleness, board health,
   summaries) writing `kind='suggestion'` events, with human accept/reject
   affordances in the UI — before any first-class draft/link tables. The
   conservative workflow: intake → duplicate detection → board routing →
   triage → quality check → human approves through the normal UI. Do not let
   early agents freely move cards across boards — cross-board movement is
   high blast radius; start with comments, links, and suggestions.
3. **Task links.** Graduate the link relations from suggestion events to the
   `kanban_task_link` table once the taxonomy has proven itself in use.
4. **Benchmark, full suite.** The complete comparison incl. markdown
   todo-editing via `EditDocumentAgentV6` and the autonomy-promotion metrics
   (see "Benchmark plan").
5. **Live updates + UI polish.** Focus-or-interval refresh (or SSE like
   /chat) so agent writes appear without a manual reload; reload-on-409 is
   good enough until agents actively run. A browser-level test for the save
   lifecycle (edit → switch board → duplicate) belongs here too — the current
   frontend tests are marker tests.
6. **Column CRUD / WIP limits.** The bulk save already accepts arbitrary
   columns; the UI deliberately doesn't expose editing them yet.

Also deferred: the `detail=brief` serialization variant; routing the HTTP
operation endpoints through the dispatcher (the known gap above).

## Benchmark plan

The central hypothesis from `docs/plan.md`: a dedicated database-backed
kanban board is more robust than asking models to edit markdown todo lists
with `EditDocumentAgentV6` or similar document patchers. The first slice
(**/benchmark_kanban**, `benchmarks/kanban.py`, also a CLI) tests the 2×2
matrix `kanban_{md,json}_{struct,tools}`: synthetic boards serialized with
the production renderers plus one move/claim/complete/fail/note instruction;
correct iff the op and the uuids match exactly. Its verdict (markdown +
structured output) is a default, not a permanent commitment — the adapter
keeps the context format and invocation mechanism swappable.

The full suite should measure whether models can:

- identify the correct `taskId`, `columnId`, `boardId`, and `agentId` from
  markdown context, and the same ids from JSON context;
- choose the correct operation for a requested change;
- avoid invalid or overpowered operations when their authority is low;
- create a useful draft card from rough input;
- detect likely duplicates without deleting or merging them;
- suggest a board/column/assignee without directly moving the card;
- report progress and complete/fail using the row-level operations;
- avoid moving cards across boards unless explicitly authorized;
- decide whether a claimed task has verifiable acceptance criteria before
  doing work;
- use recent task events to resume after lease expiry, restart, or takeover.

Compare at least: markdown todo-list editing via `EditDocumentAgentV6`;
kanban markdown context + structured-output operation; kanban JSON context +
structured-output operation; kanban context + function-calling tool
invocation.

Success criteria should emphasize correctness and blast radius over elegance:
wrong-card moves, wrong-board moves, accidental deletes, malformed ids, and
permission violations are more important than prose quality. For autonomous
workers, also track review pass rate, done/failed ratio, lease takeovers,
quality of event trails, and how often the board serialization exceeds the
target model's comfortable context budget.

## Tests

- `webapp/test_kanban_api.py` — behavior against the real DB: round trips,
  ordering, validation rejections, the 409 stale-version path, the
  declared-deletes tripwire, markdown structure and a spoofing case,
  claim/claim-next (incl. cross-board ordering, expired-lease takeover,
  conflict, idempotent re-claim), release/renew, enqueue preconditions and
  payload contract, move, complete ok/failed, the events endpoints, and
  cascade delete.
- `webapp/test_kanban_tree.py` — the folder tree: CRUD, reparenting delete,
  placement-only save, tree-version conflicts.
- `webapp/test_kanban_task_get_api.py` + `db/test_kanban_get_task.py` — the
  single-task reader behind the `?id=<task>` deep link.
- `webapp/test_kanban_views.py` — page-shell markers for the wiring.
- `webapp/test_kanban_admin.py` — the Flask-Admin views (incl.
  KanbanBoardFolder) render with real rows.
- `tools/test_kanban_dispatcher.py` — authority enforcement
  (observe/work/shape), denial events, Review routing for unverified agents.
- `tools/test_workspace_shell_chat.py` — the execution loop end to end
  through the real agent handler (success/failure/blocked/skip paths, lease
  released).
- `agents/test_kanban_worker.py` — the LLM worker with a faked model call
  (done/unclear/failed paths, deliverable event, Review routing).
- `agents/test_kanban_move_action.py`, `agents/test_kanban_writes_s2.py`,
  `agents/test_kanban_create.py`, `agents/test_kanban_create_board.py` — the
  assistant's log-and-undo capabilities: column-by-name resolution, no-op
  guard, position-aware undo, retraction comments, first-column default,
  create/delete inverses, and the locked prompt-exposed action surface.
- `benchmarks/test_kanban.py` — the /benchmark_kanban harness.
