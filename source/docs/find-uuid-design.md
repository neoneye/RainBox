# find_uuid — resolve any uuid, however badly quoted

Nearly everything in rainbox is uuid-addressed: kanban boards, columns,
tasks and their folders; cron folders, jobs and runs; chat folders, rooms,
messages and users; git repos; prompts; profiles; model configs and groups;
memory claims; assistant runs and steps; journal rows; Q&A registry entries.
Those uuids leak into chats, logs, and half-remembered pastes as *fragments*
— a prefix, a suffix, a typo'd character — and a weak LLM (or a human with a
scrap of hex) cannot reproduce them exactly. `db.find_uuid(query)` answers
**"what is this uuid?"** without the caller knowing which table to look in
or having the string exactly right.

Implementation: `db/find_uuid.py`. Read-only — no events are written, no
lease is touched.

## Consumers

| Consumer | Where | Notes |
|---|---|---|
| `/find` page | `webapp/find_views.py` | paste a uuid/fragment, results as you type, Open links jump to the entity. The search syncs into the url (`/find?q=2f70`) both ways, so the address bar is always a shareable permalink. |
| `GET /find/api/search?q=` | `webapp/find_views.py` | the page's JSON backend; 400 for a too-short query |
| `find_uuid` assistant action | `agents/assistant.py` | same resolver as a capability (family `lookup`), so the assistant resolves half-remembered ids into exact ones instead of guessing. The system prompt directs it here whenever it holds a uuid it is not sure about. |

## Query normalization

The query is lowercased and stripped of uuid punctuation and wrapping —
whitespace, dashes, braces, brackets, quotes, commas, and a `urn:uuid:`
prefix. Non-hex characters that remain (typos) are **kept**: the fuzzy pass
must see them as the mismatches they are. All matching happens against each
uuid's 32-char dashless hex, so a fragment spanning a dash boundary of the
quoted form still matches. Queries under `FIND_UUID_MIN_QUERY` (4) useful
characters are refused with a `ValueError` (the API maps it to a 400) —
shorter fragments would match half the database.

## Matching passes

Strictest first; a weaker pass only widens the net, never overrides:

| Pass | Runs when | Score | Semantics |
|---|---|---|---|
| exact | always | 1.0 | the query is the full 32-hex uuid |
| substring | always | 0.70 + 0.25·len/32, +0.05 prefix / +0.02 suffix | the query is a contiguous fragment. The position bonus ranks a uuid that *starts* with the fragment above one that merely contains it — people quote the beginning of a uuid far more often than the end, and the end more often than the middle. |
| fuzzy | query ≥ `FIND_UUID_MIN_FUZZY_QUERY` (8) **and** nothing matched exact/substring | 0.9 · ratio, threshold 0.78 | best `SequenceMatcher` ratio of the query against every same-length window of each uuid's hex — catches one or two typo'd characters wherever the fragment sits |
| mention | query ≥ `FIND_UUID_MIN_MENTION_QUERY` (8), always runs in addition | 0.5 flat | the fragment appears inside a TEXT field (see below); reported as the **containing** entity, ranked below every direct uuid match, deduped against entities that already matched directly |

Results are sorted by score (ties by kind) and capped by `limit`
(default 20).

## Row sources (uuid columns)

One `_Source` per kind: the model, its uuid attribute (`id` for Journal),
and a describer producing `{name, url, parents}`. Registered kinds:

kanban board / folder / column / task · cron folder / job / run ·
chat folder / room / message / user · git folder / repo · prompt folder /
prompt · profile folder / profile · model config / config override / group ·
memory claim · assistant run / step · journal.

Conventions the describers follow:

- **url** is the `?id=` deep link of the page that shows the entity
  (`/kanban?id=…`, `/cron?id=…`, `/chat?id=…`, `/git?id=…`, `/prompt?id=…`,
  `/profile?id=…`, `/memory?id=…`, `/model?id=…`, `/modelgroup?id=…`,
  `/assistant?id=…`). Entities without their own deep link borrow the
  nearest page that shows them: a kanban column links to its board, a chat
  message and an assistant run link to their room's page (`/chat?id=<room>`
  and `/assistant?id=<run>` respectively; a step deep-links
  `/assistant?id=<run>#step-<uuid>`), a cron run links to its job. Chat
  users, journal rows, and agents have no page — url is null.
- **parents** is the chain inner → outer: a task lists its column, board,
  then the board's folder chain to the root; folder chains are cycle-safe.
- **name** is the entity's own label; entities without one get a readable
  synthesis (a chat message its text excerpt, a cron run
  `"<trigger> @ <fired_at>"`, an assistant step `"step N: <action>"`).

## Text sources (mentions)

A `_TextSource` names the model, its searched text columns, and which
entity a hit is reported AS — usually the row itself, but a kanban task
*event* hit reports the event's task (the entity a caller can act on, not
the log line):

| Searched | Columns | Reported as |
|---|---|---|
| chat_message | text | chat message |
| kanban_task | title, description | kanban task |
| kanban_task_event | detail | the event's **task** |
| cron_job | message, command, description | cron job |
| prompt | content | prompt |
| memory_claim | text | memory claim |
| journal | payload, result | journal |

The SQL strips dashes from both sides
(`replace(lower(col),'-','') LIKE '%<fragment>%'`, wildcards escaped), so a
fragment spanning a dash boundary of a uuid quoted in prose still hits.
Each source contributes at most its `_MENTION_ROWS_PER_SOURCE` (10) newest
rows, so a heavily-quoted uuid cannot flood the results.

Assistant step observations are deliberately NOT a text source: every
`kanban_read` observation quotes whole boards, so including them would bury
real results under a step mention for any live uuid.

## The Q&A registry

The Q&A knowledge base (see `qa-system.md`) is searched at its SOURCE — the
merged jsonl of base `data/question_answer.jsonl` plus the operator overlay
`<customize.dir>/question_answer.jsonl` — not the derived pgvector table, so
an edit is findable before a repopulate. Two rules distinguish it:

- **An entry's `id` is its identity** — overlay entries commonly use a uuid
  as their id — so an id hit scores like a direct uuid match: full id →
  exact, fragment → substring (down to the 4-char minimum). Hits in the
  questions / answer / handler / path text are mentions (8+ chars).
- **Shields are honored exactly like retrieval honors them**: an entry
  carrying a `shield` appears only when that shield is in the
  `qa.unlocked_shields` setting — fail closed, on the operator page and the
  assistant action alike (the assistant is who shields exist to gate). A
  malformed overlay also fails closed: no Q&A matches rather than an error.

A Q&A match carries the qa id in its `uuid` field, the entry's first
question as its name, and the source file path as its parent.

## Match shape

```json
{
  "kind": "kanban task",
  "uuid": "2f70dead-0000-4000-8000-000000000001",
  "match": "substring",
  "confidence": 0.786,
  "name": "ship the thing",
  "url": "/kanban?id=2f70dead-0000-4000-8000-000000000001",
  "parents": [
    {"kind": "kanban column", "uuid": "…", "name": "In progress"},
    {"kind": "kanban board",  "uuid": "…", "name": "Sprint"},
    {"kind": "kanban folder", "uuid": "…", "name": "Work"}
  ]
}
```

`match` is `exact` | `substring` | `fuzzy` | `mention`; `confidence` is the
score rounded to 3 places; `uuid` always carries the FULL id — the string a
caller should use in subsequent operations.

## Adding a source

- A new uuid-bearing table: write a describer (`row → {name, url,
  parents}`) and append a `_Source` to `_SOURCES`. Reuse `_folder_chain`
  for folder parents and `_parent_ref` for single links.
- A new text field worth searching: append a `_TextSource` — pick the
  entity kind a hit should be reported as (it must already be in
  `_SOURCES`).

Performance is a full scan of every registered uuid column per query (plus
one LIKE per text column); at this app's scale (thousands of rows) a search
including the fuzzy pass stays well under half a second. If a table ever
grows past that, the pool scan is the place to add caching — not the
describers, which only run for the top `limit` matches.

## Tests

`db/test_find_uuid.py` (matching semantics, ranking, parents, shields, Q&A
ids), `webapp/test_find_views.py` (page + API), and the `find_uuid` cases
in `agents/test_assistant_actions.py` (the capability).
