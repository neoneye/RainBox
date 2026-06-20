# Kanban — design

The kanban board at `GET /kanban` is the coordination primitive from
docs/plan.md ("Kanban board"): agents keep track of progress in Postgres
because editing markdown todo lists is too fragile for small models — instead
of "rewrite the document correctly", an agent calls **narrow, uuid-addressed
operations** that succeed atomically or fail loudly.

## Status — read this first (updated 2026-06-11)

**Where we are.** Roadmap items 1 and 2 are DONE (details inline in the
Roadmap section below):

1. */benchmark_kanban* settled the format question empirically: **markdown
   context + structured output** beat JSON and function calling on both
   reliability and speed. granite4 is the speed pick; only nemotron-3-nano
   and qwen3-coder were flawless in tools mode; supergemma4 fails JSON.
   (Also: the old /benchmark page was renamed to **/benchmark_basic**.)
2. The **first LLM worker is shipped**: `agents/kanban_worker.py`
   (`kanban_worker` in agent_config). One structured call per card →
   `{status: done|unclear|failed, deliverable, comment}`; the deliverable
   lands in the task's event trail as a 'progress' event; ok=true completes
   into a **Review** column (it is unverified); no acceptance criteria →
   `complete(ok=false, "unclear acceptance criteria: …")`. Authority
   (observe/work/shape) is enforced in `tools/kanban_dispatcher.py`; the
   runner serves workers the `focus=in-progress` board view (lease state +
   recent events inline). Spec: docs/superpowers/specs/
   2026-06-11-kanban-llm-worker-design.md. Plan executed:
   docs/superpowers/plans/2026-06-11-kanban-llm-worker.md. Commits
   `251d58c7..7a8d0383` on main.

**The worker has NOT yet run against a real model** — tests fake the LLM
call. To try it for real: bind `kanban_worker` to a model group on
`/agent_models` (granite4 per the verdict), add a column named "Review" to a
board, assign the worker a card with a clear description, press the card's
**Run** button, then read the card's event trail and the Review column.

**Where we're going.** Next is roadmap item 3, the **board-level run**: a
"run assigned tasks" sweep that enqueues runnable cards while respecting
assignment, live leases, readiness, and authority. After that: item 4
secretary (observe-only suggestion events with human accept/reject), item 5
task links, item 6 the full benchmark suite, items 7–8 UI polish and column
CRUD.

**Known gaps / loose ends.**

- The HTTP operation endpoints bypass the authority dispatcher — see
  "Known gap (follow-up)" under the permission model below. Route them
  through `kanban_dispatch` or declare them operator-only before exposing
  them to anything untrusted.
- Focus events render newest-first and details are clipped to 200 chars in
  the markdown; for "resume from the trail" (item 3) oldest→newest and a
  larger budget for the last deliverable may read better for small models.
- 2 pre-existing test failures in `agents/test_query_filter_router_memory_ops.py`
  (`QueryFilterRouterAgent` missing `model_group_uuid`) — unrelated to
  kanban, predate this work, still unfixed.
- `init_db` migrations were rewritten (commit `b3e8d7fa`) to skip
  already-applied DDL: every process start used to take ACCESS EXCLUSIVE
  locks and could deadlock against any open read transaction (this is what
  froze `pytest test_kanban_api.py` runs). New migrations should follow the
  `_add_column_if_missing` pattern in db.py.

## Architecture

- **Tables** (`db/models.py`, created by `init_db`'s `create_all()`):
  `kanban_board`, `kanban_column`, `kanban_task`, `kanban_task_event`
  (append-only audit trail). Plain uuid reference columns, no FKs — the
  cron-tables pattern, integrity enforced app-side.
- **Backend** (`db_kanban.py`, re-exported from `db`): board CRUD, a guarded
  bulk per-board save for the page, the markdown/JSON serializers, and the
  agent operations. **API** (`webapp/kanban_api.py`).
- **Page** (`webapp/kanban_views.py` shell + `static/kanban.js` logic):
  a left-panel folder tree (folders → boards; the shared pattern in
  `docs/ui-left-panel-tree.md`) with per-node kebab menus, columns/cards with
  drag-and-drop, modal CRUD, the task's audit trail + lease state in the edit
  modal, serialization view/copy, and a `?id=<uuid>` deep link (board or
  folder). House rules: desktop-first, no native dialogs, mtime cache-buster.

## Two write surfaces

The boundary is strict: **humans use the bulk save; agents never do** —
agents only use the row-level operations.

**1. The page's bulk save** — `PUT /kanban/api/board/<uuid>` with the whole
board:

- validation before any mutation (uuid shape/uniqueness, every task references
  a column in the payload, `agentUuid` null-or-uuid);
- an optimistic-concurrency **version token** (`kanban_board_version`, a
  digest of user-managed fields) — GET returns it, PUT must echo it, stale →
  **409** and the page re-hydrates instead of clobbering (so an agent's write
  can never be silently overwritten by a stale tab, and vice versa);
- a **declared-deletes tripwire** (`deletes` count) so a truncated payload
  can't wipe a board.

UI saves also append audit events ('created', 'moved' with column names) so
human edits land in the same trail as agent operations.

**2. Agent operations** — row-level, uuid-addressed, each recorded in
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
| append event | `POST …/events {kind, actor?, detail?}` | progress notes / arbitrary audit entries; `GET` lists them |
| complete | `POST …/complete {ok, actor?, detail?}` | ok → move to the board's **last column** (done by convention) + 'done' event; not ok → **stays put** + 'failed' event with the reason (failure is information for the operator, not an automatic state change). Both outcomes **release the lease**. Planned: the dispatcher/runner should route successful unverified LLM work to Review before Done |

**Structured output vs function calling:** the primitives are
mechanism-agnostic — expose them as function-calling tools, or have a
structured-output reply name `{op, taskUuid, …}` and dispatch to the same
`db.kanban_*` functions. The DB layer is identical either way; pick per model
capability when wiring an LLM agent.
*Resolved (2026-06-11, /benchmark_kanban first slice):* **structured output
is the default.** It was both more reliable (every model solved
`kanban_md_struct`; the tools variants broke most models) and faster on every
target. Function calling stays available behind the same dispatcher, but only
nemotron-3-nano and qwen3-coder handled it flawlessly.

## Agent permission model

Agents should not all receive the same write authority. Start with **three
authorities** that match the actual API surfaces instead of implementing a large
policy matrix before any agent needs it:

| Authority | Allowed behavior |
|---|---|
| observe | Read board serializations, task metadata, and task events; append comment/suggestion-kind events. |
| work | Claim, renew, release, append progress, and complete/fail tasks the agent is allowed to claim. |
| shape | Create, edit, move, route, close, delete, or otherwise reshape board state. Human-only by default. |

The finer permission names from `docs/plan.md` are still useful vocabulary, but
they are not the first implementation target:

| Planned split | Fits under | When to split it out |
|---|---|---|
| read_only, suggest_only, comment_only | observe | When suggestions/comments need different rate limits, review paths, or UI treatment. |
| close_card | work / shape | When "complete my claimed task" and "close arbitrary task" need different grants. |
| draft_create, move_card, edit_card, admin | shape | When a real non-human agent earns one part of shape without earning the rest. |

The enforcement point is the agent-facing operation dispatcher, not the model
prompt. A model can propose an operation, but code decides whether that operation
is permitted before calling `db.kanban_*`. This keeps the orchestrator
deterministic: call agents, collect verdicts, resolve conflicts, apply
permissions, emit proposed actions, and write the audit log.

> Note: the personal **assistant**'s `kanban_move` capability is code-owned and
> does not pass through this observe/work/shape model; it is a log-and-undo write
> whose safety is operator reversibility + trace, not the worker authority ceiling.

**Known gap (follow-up):** the dispatcher chokepoint is in-process only. The
HTTP operation endpoints (`/kanban/api/claim-next`, `…/claim`, `…/release`,
`…/renew`, `…/move`, `…/complete`, `…/events` POST) predate it, take
`agentUuid`/`actor` from the request body, and call `db.kanban_*` directly —
no authority check, `complete` defaults `review=False`. Until they are routed
through `kanban_dispatch` (or formally declared operator-only), treat them as
a trusted/operator surface, not an agent surface.

## Agent roles and workflow

The first implementation should use **two running agents/processes**, with the
specialist names from `docs/plan.md` treated as prompts and schedules inside
those processes:

| Process | Authority | Responsibility |
|---|---|---|
| worker | work | Execute claimed tasks. Current consumer: `workspace_shell`; later consumers are LLM workers with the same claim/event/complete protocol. |
| secretary | observe | Run read/comment prompts for intake, triage, duplicate detection, dependency finding, staleness, board health, cross-board coordination, and daily summaries. |

Promote a prompt into its own agent only when its permission, schedule, context
window, or failure mode genuinely diverges. Until then, a fleet of narrow agents
mostly moves coordination burden back to the human.

The conservative review workflow still applies, but initially as secretary
prompt modes:

1. **Intake** turns rough input into suggested cards.
2. **Duplicate detection** comments on likely existing cards.
3. **Board routing** suggests the target board or cross-board link.
4. **Triage** suggests priority, column, and assignee.
5. **Quality** checks whether the card has enough context and acceptance
   criteria to execute.
6. **Human approves** creation, routing, edits, or closure through the normal UI.

Do not let early agents freely move cards across boards. Cross-board movement is
high blast radius: it changes ownership context, board semantics, and what other
agents see as available work. Start with comments, links, and suggestions; then
graduate specific shape operations only after benchmarks and ledger history show
that the agent reliably chooses the right card and destination.

## Task links

Planned: tasks may reference other tasks, including tasks on other boards. Links
let coordination agents express structure without moving cards prematurely.

Possible table:

```text
kanban_task_link  id, uuid, source_task_uuid, target_task_uuid,
                  relation, created_by, created_at
```

Candidate relations:

- `duplicates`
- `blocked_by`
- `related`
- `split_from`
- `supersedes`

Early cross-board agents should default to suggesting links rather than moving
cards. Before this table exists, represent a proposed link as a
`kind='suggestion'` event with structured detail and a UI affordance for a human
to accept it. Direct link creation can graduate later once the relation taxonomy
and UI are stable.

## Design principles (consensus)

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

The first obligation after claim is readiness: can the worker verify what "done"
means for this card? If not, it should fail the task with a precise reason such
as `unclear acceptance criteria: ...` rather than producing confident nonsense.
That failure is a quality signal and belongs in the ledger.

The lease is released on every path, including a crashed `work` fn. An agent
adopts kanban by writing only its `work(task, board_markdown) -> (ok, detail)`
callback. Current consumer: **workspace_shell** runs the task's description
as its command (validated argv, no shell, workspace-confined), output into
the event trail as 'progress', ok = exit 0.

Second consumer: **kanban_worker** (`agents/kanban_worker.py`) — the first LLM
worker. One structured call per card produces a text deliverable into the
event trail; unverified, so ok=true completes into Review. All its board
writes go through `tools/kanban_dispatcher.py`.

For unverified LLM workers, the dispatcher/runner should route successful work
to a **Review** column before Done instead of treating `ok=true` as directly
done. A human, and later a verifier agent, moves Review to Done. Direct-to-Done
is an earned capability based on ledger evidence: review pass rate, done/failed
ratio, lease-takeover count, event quality, and absence of permission
violations.

## Serialization contracts (the LLM-facing read views)

Two equivalent representations of one board, both generated **server-side
from DB state** and both meant as LLM context input — markdown reads
naturally inside a chat-style prompt, JSON suits structured prompts and tool
results; they carry the same ids, so either works as the read side of the
operations. The page's Developer sidebar shows both.
*Benchmarked (2026-06-11, /benchmark_kanban):* **markdown is the default LLM
context.** It was faster than JSON in every mode and the only format every
model read ids from correctly (supergemma4 failed `kanban_json_struct`).

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

**Planned variants** — one knob set, applying identically to both formats
(query params on both endpoints), for fitting the context budget to the job:

- `detail=full|brief` — `brief` drops descriptions: titles + ids only, for
  tight context budgets or boards used as a high-level map.
- `focus=in-progress` — asymmetric detail matching what an executing agent
  needs: in-progress columns get full content (descriptions, lease state, and
  recent task events as working memory); *To do* shrinks to a title+id list;
  *Done* to a count or titles-only. The default (no params) stays the full
  symmetric document above.

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
```

`agentUuid` is the agent_config uuid — stable across role renames; names are
display-only (the page is rendered with `{name, uuid}` pairs for the picker).
The bulk save ignores the lease fields; only the claim operations write them.

## Roadmap

Claude and codex agree on both the *content* and the *sequence* below. The
decisive change from codex's earlier ordering is accepted: **measure before
prompting**. The benchmark's first slice must run before the worker prompt it
is supposed to inform, because which serialization small models read ids from
reliably — and whether structured output or function calling yields fewer bad
operations — are empirical inputs to the worker design. The other accepted
change is that **the first LLM worker is one vertical slice, not four separate
policy projects**: authority enforcement, readiness-on-claim,
Review-before-Done, the worker prompt, and `focus=in-progress` only become
testable together.

Codex's only guardrail on that agreement: item 1 must stay small and
decision-oriented. It chooses defaults for item 2; it must not become the full
benchmark suite or delay the worker until every model/mode has been exhausted.
If the first slice is inconclusive, build item 2 behind the same adapter so the
context format and invocation mechanism can be swapped without touching the
board protocol.

1. **Benchmark, first slice.** The cheap, decisive subset: can the target
   local models read boardId/columnId/taskId/agentId out of the markdown vs
   the JSON, and pick the correct operation — via structured output vs
   function calling? Reuses the existing eval infrastructure; the result is a
   default, not a permanent commitment, for item 2.
   *Harness built:* `benchmarks/kanban.py` — the 2×2 matrix as four specs on
   its own page, **/benchmark_kanban** (`kanban_{md,json}_{struct,tools}`;
   also a CLI). Each trial:
   synthetic board serialized with the production renderers
   (`kanban_render_markdown`/`kanban_render_llm_json`) + one move/claim/
   complete/fail/note instruction; correct iff the op and the uuids match
   exactly. Run it against the local models to pick item 2's defaults.
   **DONE — verdict (2026-06-11):** item 2 defaults to **markdown context +
   structured-output operations**. Results across the 2×2 matrix:
   - `kanban_md_struct`: all models solve it (fastest: granite4 t0.15 c8k,
     1.70s).
   - `kanban_json_struct`: all but supergemma4 t0.15 c8k (fastest: granite4,
     1.96s).
   - `kanban_md_tools` / `kanban_json_tools`: most function-calling models
     struggle; only nemotron-3-nano and qwen3-coder (both t0.15 c8k) are
     flawless (fastest md_tools: granite4, 2.53s — fastest ≠ flawless).
   - Structured output beats function calling and markdown beats JSON on
     both reliability AND speed, so there is no trade-off to weigh.
   Model notes for item 2: granite4 is the speed default on the struct path;
   nemotron-3-nano / qwen3-coder are the only qualified models if a worker
   ever needs tools mode; exclude supergemma4 from any JSON-context path.
2. **The first LLM worker (one milestone).** Per-agent observe/work/shape
   config enforced in the operation dispatcher; the readiness protocol
   (no verifiable acceptance criteria → `complete(ok=false, "unclear
   acceptance criteria: …")`); Review-before-Done routing for unverified
   workers; the worker prompt for `work(task, context)`; and the
   smallest useful `detail`/`focus` serialization variants — especially
   `focus=in-progress` with recent task events, because the trail is the
   worker's resumable memory.
   **DONE (2026-06-11):** shipped as one slice — `kanban_authority`
   (observe/work/shape, default observe) + `kanban_verified` in the
   agent_config registry, enforced in `tools/kanban_dispatcher.py` (every
   runner write dispatches through it; denials land as 'permission-denied'
   events); `kanban_complete_task(review=)` routes unverified ok=true to a
   "Review"-named column (case-insensitive, fallback Done) with a 'review'
   event; `focus=in-progress` on both serializers and API endpoints (first
   column brief, last summarized, middle full + lease + 5 recent events);
   `agent_kanban_worker.KanbanWorkerAgent` — one structured call per card
   ({status: done|unclear|failed, deliverable, comment}), deliverable as a
   'progress' event, readiness folded into the call
   (status=unclear → complete(ok=false, "unclear acceptance criteria: …")).
   `detail=brief` deferred.
3. **Board-level run.** A "run assigned tasks" sweep that enqueues runnable
   cards while respecting assignment, live leases, readiness, and authority.
4. **Secretary + suggestion workflow.** The first observe-authority secretary
   prompt modes (intake, triage, duplicates, staleness, board health,
   summaries) writing `kind='suggestion'` events, with human accept/reject
   affordances in the UI — before any first-class draft/link tables.
5. **Task links.** Graduate the link relations (duplicates, blocked_by,
   related, split_from, supersedes) from suggestion events to the
   `kanban_task_link` table once the taxonomy has proven itself in use.
6. **Benchmark, full suite.** The complete comparison incl. markdown
   todo-editing via `EditDocumentAgentV6` and the autonomy-promotion metrics
   (see "Benchmark plan").
7. **Live updates + UI polish.** Focus-or-interval refresh (or SSE like
   /chat) so agent writes appear without a manual reload; reload-on-409 is
   good enough until agents actively run. A browser-level test for the save
   lifecycle (edit → switch board → duplicate) belongs here too — the current
   frontend tests are marker tests.
8. **Column CRUD / WIP limits.** The bulk save already accepts arbitrary
   columns; the UI deliberately doesn't expose editing them yet.

## Benchmark plan

The central hypothesis from `docs/plan.md`: a dedicated database-backed kanban
board is more robust than asking models to edit markdown todo lists with
`EditDocumentAgentV6` or similar document patchers. Test that directly.

Evaluation cases should measure whether models can:

- identify the correct `taskId`, `columnId`, `boardId`, and `agentId` from
  markdown context;
- identify the same ids from JSON context;
- choose the correct operation for a requested change;
- avoid invalid or overpowered operations when their authority is low;
- create a useful draft card from rough input;
- detect likely duplicates without deleting or merging them;
- suggest a board/column/assignee without directly moving the card;
- report progress and complete/fail using the row-level operations;
- avoid moving cards across boards unless explicitly authorized;
- decide whether a claimed task has verifiable acceptance criteria before doing
  work;
- use recent task events to resume after lease expiry, restart, or takeover.

Compare at least these modes:

- markdown todo-list editing via `EditDocumentAgentV6`;
- kanban markdown context + structured-output operation;
- kanban JSON context + structured-output operation;
- kanban context + function-calling tool invocation.

Success criteria should emphasize correctness and blast radius over elegance:
wrong-card moves, wrong-board moves, accidental deletes, malformed ids, and
permission violations are more important than prose quality. For autonomous
workers, also track review pass rate, done/failed ratio, lease takeovers,
quality of event trails, and how often the board serialization exceeds the
target model's comfortable context budget.

## Tests

`test_kanban_api.py` — behavior against the real DB: round trips, ordering,
validation rejections, the 409 stale-version path, the declared-deletes
tripwire, markdown structure and a spoofing case, claim/claim-next (incl.
cross-board ordering, expired-lease takeover, conflict, idempotent re-claim),
release/renew, enqueue preconditions and payload contract, move, complete
ok/failed, the events endpoints, and cascade delete.
`tools/test_workspace_shell_chat.py` — the execution loop end to end through
the real agent handler (success/failure/blocked/skip paths, lease released).
`test_kanban_views.py` — page-shell markers for the wiring.
`test_kanban_admin.py` — the Flask-Admin views render with real rows.
