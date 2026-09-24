# Diary Memory: First-Release Implementation Specification

**Status:** Ready to implement. No diary components are built.
**Date:** 2026-09-21
**Related:** [memory architecture](../memory-architecture.md),
[Q&A](../qa-system.md), [retrieval granularity](2026-08-17-recall-filter-and-retrieval-granularity.md),
[eval loop](../eval-loop.md), [testing](../testing.md),
[assistant design](../assistant-design.md).

**Build the first release as a read-only diary search service:** import configured UTF-8 files,
retain immutable snapshots, parse their historical formats, search original
passages, and return bounded citations through a dedicated `diary_query` read
capability. Test it on synthetic data, then pilot one real month in the sandbox.

## 1. Release boundary and decisions

| In scope | Deferred |
|---|---|
| Directory-selected `timed`, `daily`, `changelog`, `plain` parsers | Automatic dialect classification, non-UTF-8 conversion |
| Immutable bytes, versioned parses, bounded passages and citations | Cross-file entity resolution and causal relationships |
| Literal/identifier search, FTS, optional local passage embeddings | Event/addressee extraction and generated sensitivity classification |
| Search, literal lookup, dated timeline and citation read | A dedicated comparison planner; use a timeline or successive reads meanwhile |
| Room isolation, optional agent restriction, source/file exclusions | Entry-level access rules and inferred policy changes |
| CLI import, inspect, exclude, disable, prune and source purge | Web management/viewer routes and automatic scheduled ingestion |
| A `diary_query` read capability, visible only where usable | Mixing diary candidates into the existing claim/Q&A scorer |
| Deterministic evals and a private pilot report | Belief writes, assistant-diary export, always-on profile/chat injection |

Decisions are code defaults unless marked as configuration:

- **Encoding:** strict UTF-8 only. Preserve BOM/CRLF bytes. A leading BOM is
  bytes `[0,3)` of the revision, tagged `separator` in coverage, never inside
  an entry, and never emitted in an excerpt; the parser decodes from byte 3
  and every byte offset it emits is shifted by 3 for that file. Offset 0 is
  the BOM. This is stated once so a header at the start of a BOM file and a
  search hit at the start of a file cannot disagree by three bytes. Invalid UTF-8 or NUL quarantines that
  file. No automatic conversion or replacement characters.
- **Dialect:** longest matching directory prefix in the source manifest, using
  path-component boundaries; unmatched files are `plain`. No glob precedence.
- **Timezone:** a required IANA timezone copied from the operator's profile into
  source configuration. Apply it as an *assumed* zone to all eras. A later profile
  edit does not change the source; an explicit source edit creates new parses.
  Written local dates/times remain the primary values.
- **Access:** each source belongs to one room, optionally one agent in that room.
  No global/project diary sources. `private` does not establish isolation.
- **Search:** exact filtered vectors first. HNSW is an optional, measured
  optimization with a defined fallback; it is not needed to run the pilot.
- **Reranking:** deterministic rank fusion, with no generative recall
  filter. Preserve the existing claim/Q&A branch and its configured backend.
- **Units:** a *character* is a Unicode code point (Python `len`) everywhere
  in this document. Stored locators are UTF-8 byte offsets; every length,
  cap and window is in characters.
- **Storage:** bytes are kept forever, derived rows only for the current
  parse. A revision that is a byte prefix of its successor (the append case)
  stores no bytes of its own. Superseded generations are deleted on
  publish, because they are a cache of a pure function of bytes and
  configuration. Vectors are keyed by content, so an unchanged passage
  never re-embeds. Storage therefore grows with the corpus, not with the
  number of syncs (§4).
- **Budget:** use explicit character limits compatible with today's assistant.
  A token budget is not enforceable by the current character-based prompt
  pipeline; do not describe a character count as a token guarantee. The diary
  observation budget is derived from the assistant's scratchpad (§8). It is
  sized to answer most requests in one step, because a turn has six steps
  and each decide costs about ten seconds. An evicted diary observation
  leaves a compact citation stand-in instead of vanishing.
- **Model locality:** diary text reaches only models on a loopback provider.
  When every member of every `assistant.*` slot's model group resolves to a
  loopback base URL, all of the room's enabled private sources are eligible.
  Otherwise only sources that opt in with `allow_remote_models` are, and
  `diary_query` is not offered at all if none does. The whole roster counts,
  fallbacks included. The reply audit and the run summarizer read step
  observations, every later call reads a quoting reply as history, and a
  group falls back across its members when one fails.
- **Human control:** source files are never edited. Import, embedding and
  source-policy changes run outside the request path. The first release writes
  no claims.

The first release answers “find this error,” “what did I record that day?” and
“show the passages about this design.” It can present a historical sequence.
It must not infer current truth from the latest passage, treat chronology as
causation, or treat an old command as authorization to act now. Confirmed current
state continues to come from the existing belief store.

## 2. Repository integration and ownership

Paths below are relative to `source/`; names marked **new** are proposed files.

| Area | Implementation location | Boundary |
|---|---|---|
| Models and bootstrap | [`db/models.py`](../../db/models.py), [`db/__init__.py`](../../db/__init__.py) | New tables and additive, idempotent constraint changes; no new migration framework |
| DB operations | `db/diary.py` **new**, re-exported by `db/__init__.py` | Transactions, eligibility query, exclusions, cursor persistence |
| Configuration/parser | `diary/config.py`, `diary/parsing.py` **new** | Pure validation/parsing; no DB, model or file writes |
| Reconciliation | `diary/ingest.py` **new** | Stable reads, generation publication, filesystem reconciliation |
| Retrieval/embeddings | `diary/retrieval.py`, `diary/embeddings.py` **new** | Injectable embedding client and deterministic candidate selection |
| Rendering/action adapter | `diary/render.py`, `diary/action.py` **new** | Schema validation, bounded original excerpts, observation metadata |
| Assistant integration | [`agents/assistant.py`](../../agents/assistant.py) | `DIARY_QUERY` capability and per-turn availability, `AssistantActionContext.models_local`, compact observations in `_bounded_turn_events`, fence and audit handling by action name |
| Model locality | [`agents/model_groups.py`](../../agents/model_groups.py) | `assistant_models_all_local()` over every `assistant.*` slot's resolved members |
| Fence | [`memory/retrieval.py`](../../memory/retrieval.py) | A second code-owned fence constant for diary passages; `split_recalled_fence` recognizes both |
| CLI | `tools/diary.py` **new** | Import/inspect/pilot; explicit database selection before importing `db` |
| Evaluation | `evals/diary.py` **new**, [`evals/runner.py`](../../evals/runner.py) | `diary_recall` cases with deterministic scoring and hard failures |
| Fixtures | `data/diary_fixture/` **new** | Synthetic bytes, expected ranges and versioned query cases |

Use a `diary/` package with `__init__.py`. Do not put ingestion in
`memory/seed_memory.py` or overload `memory_embedding`: its foreign key points
to claims. Reuse the local embedding client pattern, not its singleton cache
without a model-version key. No new HTTP routes or authentication assumptions
are needed for this release; the CLI is the source viewer.

Default-off is a property of the data, not of a settings flag: the
capability is invisible until a source is registered and explicitly
enabled. The global kill switch is the
existing `assistant.disabled_capabilities` setting, which removes
`diary_query` from prompt and dispatch at once, like any other capability.

## 3. Source configuration

The CLI imports a private JSON manifest into `diary_source`. The database is the
runtime configuration; agents cannot supply roots, room identities or policy.
A manifest update is explicit, validated in full before writing, and atomic.
This is a synthetic example, not an existing installation:

```json
{
  "schema_version": 1,
  "name": "diary-pilot",
  "root": "/private/tmp/rainbox-diary-pilot/input",
  "room_uuid": "10000000-0000-4000-8000-000000000001",
  "agent_uuid": null,
  "timezone": "Europe/Copenhagen",
  "sensitivity": "private",
  "allow_remote_models": false,
  "include_suffixes": [".txt"],
  "dialect_rules": [
    {"prefix": "current/", "dialect": "timed"},
    {"prefix": "daily/", "dialect": "daily"},
    {"prefix": "archive/", "dialect": "changelog"}
  ],
  "month_languages": ["en", "da", "de"],
  "command_tokens": ["/goal"],
  "author_tokens": {"kreese": "operator"},
  "file_overrides": {}
}
```

Validation and defaults:

- Require an absolute existing directory, an existing room, and a valid agent
  identity when supplied. Canonical roots are unique and may not overlap.
  Sources start disabled. `sensitivity` is `private` or `secret`; secret sources
  can be inspected locally but are never available to the assistant.
  `allow_remote_models` defaults to false and is a policy field: changing it
  bumps `policy_version`. A secret source rejects `true`.
- Prefixes are normalized relative POSIX directory paths, including a trailing
  slash; `""` is the root prefix. Reject `..`, absolute prefixes, duplicates and
  unknown dialects/languages. Suffix comparison is case-sensitive; `""` explicitly
  allows extensionless files. Skip symlinks, including symlinked directories.
- Month names come from `babel.dates.get_month_names` (`wide` and
  `abbreviated`, trailing period stripped, matched case-insensitively) for
  each language tag in `month_languages`; `babel` 2.18 is already in the
  venv and covers the operator's languages (`juli`, `Juli`, `jul.`). The
  tables are materialized into the parser configuration at register time,
  so the fingerprint covers them and a Babel upgrade cannot silently change
  a parse. Unknown month tokens remain undated with a diagnostic. Adding a
  language is a manifest edit; the fixture pins English, Danish and German.
- `file_overrides` keys are relative file paths. Each value contains
  `content_sha256`, `force_boundary_offsets: []`, `suppress_boundary_offsets: []`
  and `pasted_ranges: []`. Offsets are revision byte offsets (BOM included,
  as `show` prints them) at line starts; pasted ranges are `[start,end)`.
  `register` validates shape, sorting and overlap. Bounds and line-start
  positions can only be checked against the bytes, so the parser converts
  each offset to a character position at its input edge; an offset past EOF
  or off a line start skips the whole override with `override_invalid`.
  A hash mismatch does **not** quarantine the file either: the override is skipped,
  the file is parsed without it, and `override_stale` is emitted with the
  expected and actual hashes — the text stays searchable with a possibly
  imperfect boundary, which is strictly better than a file the assistant
  cannot see because a typo was fixed on line five. `parse --dry-run` lists
  stale overrides so they can be re-anchored. This is the sole file-level
  escape hatch for genuinely ambiguous syntax, and it is expected to be
  rare: the fence and terminal rules in §5 are meant to make most files
  need none.
- `command_tokens` supply hints only when the first body token exactly matches
  and is outside pasted text. `/tmp` is not a command unless configured. `IDEA:`
  is an idea marker, not proof of an addressee. Neither hint activates behavior.

Keep immutable parser configuration on each generation. Its SHA-256 fingerprint
covers canonical JSON, parser/identifier versions, chunk cap and the
materialized month tables. Policy fields have their own monotonically increasing `policy_version`;
changing access does not require re-embedding content. Root changes require a
new source rather than rebinding existing citations to another tree.

## 4. Persistence contract

Use UUID primary keys generated in Python. All columns are NOT NULL unless
marked `?`; timestamps are UTC `timestamptz`, hashes are 64-character lowercase
hex text, and JSON is JSONB. New dependent foreign keys use `ON DELETE CASCADE`
except the current-generation pointer and the references that point *at* a
revision's bytes (`bytes_in_revision_uuid`, `cite_revision_uuid`), which are
plain references `DEFERRABLE INITIALLY DEFERRED`, so a revision that other
rows depend on cannot vanish underneath them. Deferred because the check
must run after cascades: Postgres may queue a statement-level check on a
deleted revision ahead of the cascade that deletes the entries citing it,
so only a commit-time check lets a purge or prune remove a revision and its
dependents together, while one that would leave a dangling reference still
fails. A source record is retained on purge.

| Table | Columns in addition to `uuid` | Uniqueness / checks |
|---|---|---|
| `diary_source` | `name text`, `root_path text`, `room_uuid uuid`, `agent_uuid uuid?`, `timezone text`, `sensitivity text`, `config jsonb`, `enabled bool=false`, `vector_mode text=off`, `embedding_spec jsonb?`, `policy_version bigint=1`, `catalog_version bigint=1`, `created_at`, `updated_at` | Unique name and canonical root; sensitivity in private/secret; vector_mode off/exact/hnsw; positive versions |
| `diary_file` | `source_uuid FK`, `relative_path text`, `current_generation_uuid uuid?`, `availability text`, `last_seen_at`, `diagnostics jsonb=[]` | Unique `(source_uuid,relative_path)`; availability pending/ready/quarantined/missing |
| `diary_revision` | `file_uuid FK`, `sha256 text`, `byte_length bigint`, `raw_bytes bytea?`, `bytes_in_revision_uuid FK?`, `extends_revision_uuid uuid?`, `first_ingested_at` | Unique `(file_uuid,sha256)`; exactly one of `raw_bytes`/`bytes_in_revision_uuid`; `byte_length = length(raw_bytes)` when stored |
| `diary_generation` | `file_uuid FK`, `revision_uuid FK`, `parser_fingerprint text`, `parser_config jsonb`, `dialect text`, `diagnostics jsonb=[]`, `coverage jsonb`, `created_at` | Unique `(revision_uuid,parser_fingerprint)` and `(uuid,file_uuid)`; plain FK to revision; that the revision belongs to the same file is checked in the publisher's transaction; dialect enum |
| `diary_entry` | `generation_uuid FK`, `ordinal int`, `byte_start bigint`, `byte_end bigint`, `cite_revision_uuid FK`, `text text`, `date_local date?`, `clock_start time?`, `clock_end time?`, `date_basis text`, `time_status text`, `author_token text?`, `context_ranges jsonb=[]` | Unique `(generation_uuid,ordinal)`; positive nonempty range; ordinal ≥ 0 |
| `diary_passage` | `entry_uuid FK`, `part_index int`, `byte_start bigint`, `byte_end bigint`, `cite_revision_uuid FK`, `text text`, `text_hash text`, `search_vector tsvector` | Unique `(entry_uuid,part_index)`; positive range; part_index ≥ 0 |
| `diary_annotation` | `entry_uuid FK`, `kind text`, `subtype text`, `value text`, `byte_start bigint`, `byte_end bigint`, `basis text` | Unique `(entry_uuid,kind,subtype,byte_start,byte_end,value)`; kinds identifier/command_hint/idea_hint/pasted; basis rule/explicit_override |
| `diary_embedding` | `source_uuid FK`, `model_epoch text`, `text_hash text`, `embedding vector(768)`, `created_at` | Unique `(source_uuid,model_epoch,text_hash)`; reject nonfinite/zero vectors in adapter |
| `diary_exclusion` | `source_uuid FK`, `relative_path text`, `created_at` | Unique `(source_uuid,relative_path)`; excludes the entire file, all revisions |
| `diary_cursor` | `room_uuid uuid`, `agent_uuid uuid?`, `request jsonb`, `catalog_manifest jsonb`, `position jsonb`, `expires_at` | Opaque UUID; expires after 30 minutes; never contains copied source text |

`date_basis` is `header`, `filename` or `unknown`.
`time_status` is `date_only`, `minute`, `range`, `unknown`, `ambiguous` or
`invalid_range`. For identifier annotations, subtype is `url`, `hash`, `path`,
`symbol`, `issue` or `model`; other kinds use `none`. `value` is the original
literal string, never a concatenated compound key. Pasted annotations use value
`terminal_likely` or `fenced` (basis `rule`), or `explicit` for an override's
pasted range (basis `explicit_override`). All annotation ranges must be nonempty and within
the entry. Identifier rules are conservative and fixture-pinned: HTTP(S) URLs;
7–40 lowercase hex runs with token boundaries that mix digits and letters (so
a date is not a hash); paths with at least two segments that are rooted
(`/`, `~/`, `./`, `../`), end in `/` or have an extension (so `true/false`
and `/goal` are not paths); code-span contents and `::`/`#`-qualified names
as symbols; `#` plus digits; and model `name:tag` tokens. Overlapping kinds
are allowed. Coverage of unusual identifiers comes from literal mode.
Keep the schema narrow; no event/extraction/promotion tables exist.

Add a deferred composite FK from
`diary_file(current_generation_uuid,uuid)` to
`diary_generation(uuid,file_uuid)` after table creation. Clear the pointer before
purging descendants. The pointer can reference only a generation of that file.
It is the publication record: there is no separate “active” flag on passages.

### Retention: what is kept, and why storage is linear

A diary file is appended to daily, and each sync that sees a change creates
a revision. Keeping every revision's bytes, every generation's rows and a
vector per passage per generation would grow with *size × syncs*: a
hypothetical 4 MB file synced daily would add tens of megabytes a day. Bytes,
entry text and passage text are 4 MB each, and about 11,000 passage vectors
at roughly 3 KB each come to another 34 MB. Three rules keep storage proportional
to the corpus instead:

1. **Prefix-shared bytes.** When a newly inserted revision R′ begins with the full
   bytes of the file's previous current revision R (checked on the bytes,
   not inferred from sizes), the publisher records `R′.extends_revision_uuid = R`
   (provenance for `show`, a plain UUID that `prune` clears).
   It then repoints R, and every revision whose bytes were stored in R, at
   R′ (`bytes_in_revision_uuid = R′`, `raw_bytes = NULL`), in the same
   transaction. A revision's bytes are always its own `raw_bytes` or the
   first `byte_length` bytes of exactly one other revision's `raw_bytes`, so
   resolving them is one hop. A revision that is reused (the file went back to
   bytes it had before) is already resolvable and triggers no repointing.
   What a revision *denotes* never changes; only
   where its bytes are stored does. An edit that is not an append stores the
   new revision whole and leaves the older ones as they are. This covers the
   formats written today: `timed` files are appended and `daily` files are
   written once. A newest-first ChangeLog grows at the top, but those files
   are archival; if one were still being prepended to, each revision would
   be stored whole.
2. **One generation per file.** Publishing a generation deletes the
   superseded one, and its entries, passages and annotations with it, in the
   same transaction. A generation is a pure function of bytes and parser
   configuration, so nothing is lost that `parse_file` cannot rebuild, and
   every citation is a revision byte range that needs no parse rows. Every
   retrieval query therefore reads current rows only, without a
   current-generation join, and the trigram and FTS indexes hold no dead
   parses.
3. **Content-keyed vectors.** A vector is keyed by source, epoch and passage
   `text_hash` (the input format is part of the epoch, so the hash names the
   exact input). An unchanged passage in a new generation joins to its
   existing vector at no cost. Identical routine entries (“Physiotherapy
   exercises.”, written on hundreds of days) share one vector. `embed` ends by
   deleting this source's vectors that no current passage references, so
   text edited out of a file does not linger as a vector.

**Stable citations.** Every entry and passage carries `cite_revision_uuid`,
the oldest revision known to contain its exact bytes at the same offsets.
The publisher computes it from the superseded generation before deleting it.
If the new revision equals or extends that generation's revision R, a range
identical to one there (same offsets and hash) inherits that range's citation
revision; any other range ending within `R.byte_length` cites R. Every
remaining range cites the new revision. So a passage untouched by an append keeps its citation
string from day to day. The model sees one locator for one piece of text,
and telemetry rolls up per passage instead of fragmenting at every sync.
Citations are `diary:<cite_revision_uuid>:<byte_start>-<byte_end>`.

Nothing here deletes a revision. `prune --source UUID --path PATH` is the
operator's tool for dropping a file's non-current revisions, for example
after deleting a line they no longer want readable. In one transaction it
gives the current revision its own `raw_bytes`, repoints the current rows'
`cite_revision_uuid` at it, bumps `catalog_version` and deletes the rest.
Citations into the pruned revisions return `not_found`.

### Integrity and indexes

DB checks enforce nonempty ranges and enumerations. The publisher additionally
validates child ranges against parent/snapshot bytes, UTF-8 boundaries, exact
materialized text, hash matches and complete parser coverage before any switch.
`coverage` stores nonoverlapping structural ranges tagged entry/header/separator;
their union must be `[0,byte_length)`. Header context may be referenced by
several entries but has one owner in coverage. Empty files have empty coverage.

Required indexes:

- B-tree on file source/path; generation file; entry generation/date/ordinal;
  passage entry/part and `text_hash`; annotation entry and `(kind,subtype,value)`;
  revision `bytes_in_revision_uuid`; exclusions source/path; cursors expiry.
  Unique constraints provide overlapping indexes (the embedding key among
  them); do not duplicate those.
- GIN on `diary_passage.search_vector`, populated with
  `to_tsvector('simple', text)`. Searchable text is the exact passage, not a summary.
- Optional HNSW on `diary_embedding.embedding` using `vector_cosine_ops`,
  `m=16`, `ef_construction=64`, created by the CLI after bulk embedding. It is not
  an automatic startup migration. Exact mode must remain exact even if it exists.

### Migration and rollback

Follow [`init_db`](../../db/__init__.py): add models, then explicit idempotent
DDL for constraints on existing tables. Update both model declarations and
bootstrap checks for `eval_case_case_type_check` (`diary_recall`) and
`ck_retrieval_event_target_type` (`diary_citation`). Existing telemetry stages
`retrieved` and `injected` suffice; no stage migration is needed.

Change the existing bootstrap membership checks to recognize the complete
expected target-type set. Otherwise its current “contains skill” check may leave
an older constraint unchanged. Test fresh creation, upgrading a populated prior
schema and running bootstrap twice. Do not alter claim/evidence schemas.

Rollback adds `diary_query` to `assistant.disabled_capabilities` and disables
all sources before reverting runtime code.
Retain tables, citations and exclusions; do not run a destructive downgrade or
narrow enum constraints over existing diary rows. Purging a source is a separate
CLI operation that disables it, clears file pointers, deletes its imported
content/cursors and retains source configuration plus exclusions. It does not
edit originals or erase past chat traces/backups.

## 5. Parser algorithm and byte fidelity

The three synthetic format examples are at the end of this document. Implement
`parse_file(raw_bytes, relative_path, parser_config) -> ParsedFile` as a pure
function. It emits entries, passages, annotations, coverage and diagnostics.
No model participates. The parser works in **one unit**: it decodes the file
once and reasons in Python string (code point) offsets throughout —
boundaries, chunk caps, override positions, match spans. Byte offsets exist
only at the edge: when an entry, passage, annotation or coverage span is
emitted, its code-point range is converted to a **half-open UTF-8 byte
range** into the revision (plus the BOM shift), computed incrementally in
one forward pass so no per-character index is held. A code-point boundary
is a valid UTF-8 boundary by construction, so no split can land inside a
multi-byte character. Stored offsets are bytes; nothing downstream converts
back except the citation reader, which slices raw bytes and decodes. Do not
normalize stored newlines or indentation.

### Precedence

1. Strict decoding; failure quarantines the file. This is the only parse
   failure that hides a file. An override that is stale or invalid is
   skipped with its diagnostic and the file parses without it.
2. Explicit pasted-range and boundary overrides. Forced boundaries must still
   have a valid header shape; they resolve context ambiguity, not invalid clocks.
   Suppressed boundaries stay body text. Reject forced boundaries inside explicit
   pasted ranges.
3. Fenced regions: an opening run of at least three backticks/tildes, closed by
   the same character with at least that length. Ignore time/bullet syntax
   inside. A matching closer within 16,384 characters wins outright, so a
   closed fence protects everything in it, date-shaped lines included. An
   unmatched opener does **not** protect to EOF — one stray fence would
   otherwise erase every boundary in the rest of a decade-long file. It
   closes before the first of: a forced boundary; the next valid **date**
   header of the file's dialect (a date line, a ChangeLog header — time
   lines and bullets do not close it, since fenced output legitimately
   contains four-digit lines); or the 16,384-character cap. The forced close
   emits `fence_unterminated` with both offsets, and the region is still
   annotated as pasted. Every one of these closes has a fixture.
4. Likely terminal regions, then dialect boundary recognition outside them.
5. Date/author context assignment, coverage validation, passage chunking.

The terminal-start regex is
`^(?:\$[ \t]|[A-Za-z0-9_.@-]+:[~/][^\r\n]*?[#$](?:[ \t]|$))`.
It matches a leading `$ ` prompt or a host/path prompt such as
`www:/srv/skynet/www#`, optionally followed by a command. Pin positive/negative
fixtures; verified against the synthetic samples on 2026-09-21:

```text
match     www:/srv/skynet/www# svn st
match     www:/srv/skynet/www#
match     $ ls
no match  https://example.org/notes/threat-model#top   (the # is not followed by whitespace or end of line)
no match  note: /tmp is full # really                   (a space follows the colon)
match     host:/path#                                   (KNOWN FALSE POSITIVE: a URL-like token whose fragment marker ends the line)
```

The last case is the one shape prose can produce that the regex cannot
tell from a prompt; it is rare, it only affects boundary suppression inside
the region that follows, and a `pasted_ranges` override on that file
resolves it. Add it to the fixture as a documented limitation, not a bug.

Once started, the likely terminal region includes successive prompts/output and ends before two
consecutive empty lines, an explicit override boundary, the next valid date
header, the 16,384-character cap, or EOF. In `timed`, an `HHhMM` line right
after an empty line also ends it: that shape does not occur in shell output
the way four-digit lines do, and without the rule a single pasted command
would swallow the rest of the day's entries. One empty line inside it does
not end it. A header-shaped line suppressed there produces a
`possible_boundary_in_terminal` diagnostic. The region is a parsing heuristic,
not a verified speaker label; the following prose is not automatically quoted.

Two empty lines can also occur in terminal output. The parser makes this choice
explicit rather than claiming perfect recognition: retain the raw bytes, flag boundary
candidates immediately after such a region, and use hash-bound overrides when
inspection shows a false split. No content is discarded to resolve ambiguity.

### Dialect rules

| Dialect | Exact boundary rule | Other text |
|---|---|---|
| `timed` | Valid `YYYYMMDD` line at column zero; valid `HHhMM` or `HHhMM - HHhMM` time line at column zero | Text after a date before the first time is a date-only entry; text before any date is undated |
| `daily` | Filename stem exactly `YYYY_MM_DD`; valid four-digit `HHMM` at column zero at file start or immediately after an empty line | Blank lines within a record remain in it; a candidate inside a protected span is suppressed |
| `changelog` | Valid `DD-month-YYYY` plus whitespace and one author token; top-level `*` followed by whitespace opens a bullet | Retain indented continuations and internal blanks until next bullet/header; pre-bullet text is a date-only entry |
| `plain` | Nonempty blank-line-separated blocks | No inferred clock; use an unambiguous filename date if present |

For recognition only, remove the line ending and trailing spaces/tabs. An empty
line contains only spaces/tabs after removing its ending. Never remove those
bytes from stored text. Validate days/months/leap years and hours `00–23`, minutes
`00–59`; malformed headers remain text plus a diagnostic. Filename dates accept
only complete stems `YYYYMMDD`, `YYYY_MM_DD`, or `DD-MM-YYYY`. Month-name headers
use the configured tables. Do not find dates by scanning arbitrary body prose.

For `timed` and `changelog`, a valid header takes precedence over a filename date;
a mismatch emits `filename_header_mismatch`. For `daily`, an invalid filename
falls back to plain undated blocks with `invalid_daily_filename`. Unknown author
tokens remain literal tokens and never default to operator authorship.

Order is the parser's cheapest ambiguity signal. A four-digit prose line
such as a year (`2027` is a valid `20h27`) or an eight-digit order number
can pass the header grammar, but it rarely also fits the file's sequence.
The parser therefore checks each accepted header against its predecessor in
the same file: in `timed` and `daily`, a clock earlier than the previous
entry's start on the same date emits `clock_regression`; in `timed`, a date
earlier than the previous date emits `date_regression`, and in `changelog`
(newest first) a date *later* than the previous one does. The boundary is
still accepted, because diaries are occasionally written out of order, and
the diagnostic carries both offsets so `parse --dry-run` lists exactly the
places worth inspecting. A regression inside a likely terminal or fenced
region cannot occur, since those headers are already suppressed.

### Clocks, order and chunks

Keep local dates and minute values as written. Time ranges do not prove when the
author typed a record or when its event occurred. An end earlier than a start is
`invalid_range`; do not infer midnight rollover. DST gaps/overlaps are
`ambiguous`; retain local values without inventing a unique UTC instant.
Date-only entries do not acquire midnight timestamps. The generation records
its assumed source timezone. Filters apply to local calendar dates, not UTC
instants.

Source order controls neighbors. Timeline order is
`(date_local, clock_start NULLS FIRST, source_uuid, relative_path, ordinal,
part_index)`; newest-first ChangeLogs therefore read chronologically in timelines
while preserving source order within a date. With differing assumed zones, label
the timeline as local diary dates, not a single absolute event chronology.

Each entry owns its time/bullet marker and body; date/author headers are separate
context ranges. Chunk the body, including its entry marker, at the last line end
within **600 characters**. If none fits, split at the cap on a character
boundary; a line that must be split this way carries the pending text
(usually just the entry marker) with it rather than leaving a marker-only
passage. Two things set the cap. Four whole passages plus their labels must
fit one observation (§8), and about 170 tokens keeps a long entry's vectors
focused on one topic each. Most diary entries are shorter and stay a single
passage. No overlap at ingestion. Keep part indexes and map ranges to bytes.
Attach inherited date/author context to every part. Every short entry is one
passage. Whitespace-only structural spans are retained in coverage, not embedded.

Diagnostics contain code, file ID/path and byte offset, never copied body text.
Every diagnostic code has a fixture. Expected free-form body lines are not errors.

## 6. Import, exclusion and publication state machine

`sync_source(source_uuid)` is CLI-only. Acquire a session-level PostgreSQL
advisory lock on a dedicated connection for that source; another sync returns
`busy`. Hold it across per-file transactions and release it in `finally`.
Model/network calls never run under that lock or an open write transaction.
Policy changes do not take this long-lived lock. Each publisher briefly locks
the source row, checks the captured policy version and commits its pointer/version
update atomically; a policy change can therefore invalidate work between files.

1. Enumerate included regular files below the canonical root, excluding symlinks.
   Use a stable read (file identity/size/mtime checked before and after); retry a
   changing file twice, then mark it quarantined. Refuse a root/config mismatch.
   Excluded paths are never opened, so an exclusion placed before the first
   sync keeps the bytes out of the database entirely.
2. Hash each file's bytes. A file whose hash and the source's current parser
   fingerprint both match its current generation is unchanged and costs
   nothing more. Otherwise reuse or insert the immutable revision and parse it
   with the captured configuration.
3. In one transaction: insert the complete generation with all deterministic
   search rows, validate it, compute citation revisions, apply prefix-shared
   storage, delete the superseded generation, and switch the file pointer
   with `availability=ready` (§4, retention). Failed publication rolls back
   that file. On a known changed file's failure, set it quarantined in a
   separate transaction; do not serve old content as current. A source sync
   may succeed for other files and report partial failure.
4. Increment `catalog_version` whenever visible content changes: a
   publication, an availability change, an exclusion, enable/disable or a
   policy edit. Embeddings and vector-mode changes do not bump it. They
   change ranking, not what exists, and a backfill writing batches of 32
   would otherwise invalidate every open cursor hundreds of times. Missing
   files become `missing` only after a complete successful directory
   enumeration. A root scan failure does not mark everything missing.
   Re-read source policy/version before each commit.
5. Embed in a separate resumable command: vectors for current passages'
   text hashes that lack one in the current epoch. Before saving a batch,
   recheck the source's policy version. A vector whose text meanwhile
   vanished is harmless and removed by the closing sweep (§4). A failed
   embedding leaves literal/FTS available.

Register/update preserves source UUID and exclusions, disables the source, and
requires an explicit enable after sync. Background sync/embedding may operate on
an initially disabled source; they require unchanged captured policy and current
input, not assistant visibility. Any subsequent disable/purge/config edit bumps
the policy version and invalidates in-flight work.

Normal search requires ready files in current generations whose parser
fingerprint matches current source configuration. Older revisions remain
reachable only by citation, under current policy. Passage UUIDs change at
every publication; citation strings, which are what the model, the trace and
telemetry carry, survive an append (§4). No general occurrence-lineage
algorithm is required until belief promotion.

Exclusions are **file-level**, keyed by source and relative path, and apply to
all revisions and all routes. They survive rebuilds and purges. `exclude` takes
effect immediately; `unexclude` is an explicit local operation. Rows a file
already has stay in place under the exclusion, so `unexclude` needs no
re-sync and the exact-move check below has hashes to compare. `prune` or
`purge` removes them.

An excluded file that disappears in the same sync in which new files appear
may have been renamed, and publishing the new path would undo the exclusion.
The response is scoped to the files at risk, not the whole source, because
the `daily` dialect adds a new file nearly every day and disabling the source
would turn every such deletion into an outage:

- A new file whose bytes hash to any stored revision of the vanished excluded
  file is an exact move. The exclusion is copied to the new path, the file is
  never published, and `exclusion_carried` names both paths.
- Every other file that appeared in that sync is held with
  `availability=pending` and diagnostic `held_for_reconcile`, unsearchable,
  until `reconcile --source UUID` either excludes it or releases it. The rest
  of the source stays enabled and current.

Ordinary unexcluded renames are removal plus addition. The hold rule does not
pretend a content hash identifies every rename (a renamed and edited file
has a new hash); it only refuses to guess on the files that could be one.

`enabled=false`, `secret`, exclusions and quarantine filter before candidate text
leaves the DB layer. Revalidate immediately before returning an observation;
if versions changed, retry once, then return `source_changed`. Previously sent
prompts cannot be retracted. Record policy/catalog versions in observation data.

Freshness means “as of the last successful explicit sync.” Queries do not stat
or reread the filesystem. Editing/deleting an original is reflected after sync;
disable/exclude is the immediate revocation mechanism. Display the snapshot's
ingestion time so this limit is visible rather than implying live-file access.

## 7. Read API and assistant integration

Add one read capability, `diary_query`, in its own `diary` family. Leave
`memory_query` unchanged. A separate capability rather than a nested branch
of `memory_query` because the registry already provides everything the diary
needs. It has its own flat argument list, its own `output_cap_chars`, a
per-turn capability set to hide it where it cannot work, the operator's
`assistant.disabled_capabilities` kill switch, and the action name as the
key for fence rendering and audit handling. A nested branch would need each
of those special-cased by parsing `args['diary']`, and it would give a local
model a two-level schema to fill in instead of one.

Arguments are flat, validated strictly in `diary/action.py` with unknown keys
forbidden. The five shapes:

```json
{"mode":"search","query":"undo corruption"}
{"mode":"literal","query":"Edit::VSpace#move_left","source":"diary-pilot"}
{"mode":"timeline","date_from":"2027-07-26","date_to":"2027-07-27"}
{"mode":"read","citation":"diary:REVISION_UUID:120-980"}
{"mode":"continue","cursor":"CURSOR_UUID"}
```

These examples are for this document only. The capability description
states the shape with placeholders (`{"mode": "search", "query": "..."}`),
never example words, because models copy example values.

Contract:

- `mode` is required. `search`/`literal` require query length 1–512
  characters. Do not trim literal text. Date bounds are ISO dates and **both
  are inclusive**; `date_from == date_to` is one day. A model asked about
  “the 26th” writes the 26th twice, which is less error-prone than working out
  an exclusive end. Internally the bounds become `[date_from, date_to + 1 day)`.
  `search`/`literal` accept optional bounds; `timeline` requires both and
  spans at most 366 days. Optional `source` narrows the query to one source
  *name*, as the observation labels print it. Names are unique, and a model
  copies a short name more reliably than a UUID.
- `read` opens a citation as a **reader**, not a byte slicer. If the cited
  bytes are still at the same offsets in the file's current revision
  (checked by hashing that prefix of the current bytes), rendering starts at
  the current entry that contains the citation start, with that entry's
  header context. It continues in source order through the following entries
  of the same file until the budget is full, and `next_cursor` carries on
  from there. This answers the two things a model means by reading a hit:
  the whole entry, and what was written next. A citation into a superseded
  snapshot the current file no longer contains renders a raw byte window
  from the citation start. That window is labeled as a superseded snapshot
  and has no date metadata, because its parse was not retained. Syntax is
  checked before access (`invalid_request`). Everything that needs the
  snapshot, such as offsets past its end or inside a multi-byte character,
  is checked after access and fails as `not_found`, so a caller outside the
  room cannot probe a revision's length. A start inside a BOM moves to byte
  3. `read` never opens a filesystem path.
- `continue` accepts only a cursor; it reuses its stored request and context.
- Context room/agent UUIDs come solely from `AssistantActionContext`. Require a
  matching room and, when configured, matching agent; missing context denies
  access. A source name, citation or cursor is not permission. Unknown,
  denied and unavailable citations, and a cursor bound to another room or
  agent, return the same `not_found` response.
- `AssistantActionContext.models_local` is computed once per turn from
  `assistant_models_all_local()`. When it is false, sources without
  `allow_remote_models` are ineligible. Missing context counts as remote.
- Unknown modes/keys, conflicting fields, invalid dates and oversized input return
  `ok=false,error=invalid_request` before retrieval. Search with no matches is
  a successful empty result. Partial routes return `ok=true` plus
  degraded/coverage metadata. If every source becomes ineligible mid-turn
  (disabled, purged, excluded), the action returns
  `ok=false,error=unavailable`.
- No free-form natural-language date parser. `dateparser` is present in the
  venv for other features; `diary_query` does not use it. The assistant
  supplies date bounds for clear requests; ambiguous “before the rewrite”
  uses search first. Speaker/addressee filtering is unsupported and must not
  silently do nothing.

**Per-turn availability.** `handle()` already resolves the turn's capability
set once (`enabled_capabilities()`). After the room is known, it drops
`DIARY_QUERY` unless the room (and agent, when a source names one) has at
least one enabled, private source with a ready file that is eligible under
the turn's `models_local`. So the model is never offered a capability that
can only fail, and no tokens go to describing it. The run's trace records
which condition hid it: no source for the room, every source disabled, or a
remote member in a named slot. The operator can see why on /assistant
without the model having to explain it. (`capability_report()` is not
room-scoped, so it keeps reporting only the operator setting.)

`assistant_models_all_local()` resolves every `assistant.*` slot through
`resolve_assistant_model_uuids` and checks that each member's provider base
URL has a loopback host (`127.0.0.0/8`, `::1`, `localhost`). The check is on
the host, not the provider type, because `OLLAMA_BASE_URL` can point Ollama
at another machine. Its boundary is the assistant's own calls. Once a reply
quotes a passage, it is ordinary chat history, and any agent or bridge
connector that reads the room reads it.

Internal Python seams:

```python
parse_file(raw_bytes, relative_path, parser_config) -> ParsedFile
sync_source(source_uuid) -> SyncReport
eligible_sources(context, source_name) -> source_query
diary_available(room_uuid, agent_uuid, models_local) -> bool
retrieve_diary(request, context, *, embed_query=None) -> DiaryResult
render_diary(result, context) -> AssistantObservation
read_citation(locator, context) -> DiaryResult
```

`DiaryResult` includes items with source/file/entry/passage IDs, citation,
byte range, exact text, date/time precision, match ranges and retrieval reasons;
plus `next_cursor`, `degraded_routes`, `catalog_manifest`, and timings. Observation
`data` records returned and actually injected citations separately. Never count a
retrieved-but-omitted passage as injected.

Telemetry identifies what it counts by citation string, not passage UUID.
Passage UUIDs are deleted at the next publication, while a citation survives
appends and also names a read range that covers header bytes and has no
passage.

Coverage uses two fields: `enumeration = complete|partial|not_applicable` and
`metadata = complete|partial`. Timeline/read can enumerate completely; ranked
search always uses `not_applicable`. Quarantine, undated material and unresolved
parsing diagnostics make metadata partial within the requested sources. Report
counts only for sources the caller may see. “No outcome found” is not proof that
no outcome exists.

### Retrieval routes

All route SQL applies eligibility and explicit date/source constraints **before
LIMIT**. Do not fetch global top-K and then filter in Python.

- **Literal:** case-sensitive substring matching over complete eligible entry
  text, so chunk boundaries cannot hide an error. Map matches to original byte
  ranges. Explicit literal mode ranks earlier source-order occurrences first,
  returns exact match windows and skips FTS/vector calls. Multiple hits are not
  silently declared unique. Return a cursor to enumerate additional matches.
  The SQL predicate is `text LIKE :pattern ESCAPE '\'`, where the pattern is
  `%` + the query with `\`, `%` and `_` escaped + `%`. That is the same
  exact, case-sensitive substring test as `strpos`, and unlike `strpos` a
  `pg_trgm` GIN index can serve it. Without the index it is a sequential
  scan, well inside the 2-second statement timeout at this corpus's 15 MB.
  An index does not change a `LIKE` result, so unlike HNSW it needs no
  recall gate: `sync` creates it automatically once a source's entry text
  exceeds 25 MB, and `index --trgm` creates it on demand below that. (A
  query under three characters has no trigram and scans either way.) Match
  positions come from a Python `str.find` loop over each fetched entry, so
  every occurrence in an entry is found, not only the first.
- **Prompt spelling of a literal:** the prompt fence replaces `<` and `>` with
  `‹` and `›` (§8), so an assistant copying a string it was shown would
  search for a spelling the diary never contained. A literal query containing
  `‹` or `›` is therefore tried as written and with those two characters
  reverted, and the hits are merged. Neither spelling is preferred; both are
  exact matches of real bytes.
- **Headers are not entry text:** a literal on a date or author header (a
  ChangeLog `27-juli-2027 kreese` line) finds nothing, because headers are
  context ranges, not entry text. Dates are reached through `timeline`;
  author tokens are rare enough to leave to `read`.
- **Identifier route in search:** at most eight recognized query tokens; equality
  first, hash-prefix matches second (minimum seven hex characters). Keep subtype
  and original spelling. Paths/symbols are case-sensitive; ambiguous hash prefixes
  remain multiple hits. This is one route with a total 20-passage cap.
- **FTS:** `simple` configuration, OR distinct query lexemes in a parameterized
  tsquery, `@@` match required, rank by `ts_rank` descending. Cap at 20.
- **Vector:** cosine distance over the source's current-epoch vectors, joined
  to eligible passages on `text_hash`; top 20 distinct texts. A missing
  model/index/embedding never removes lexical results.

Search unions the three routes by passage UUID. Score with reciprocal-rank
fusion `sum(1 / (60 + rank))`, where ranks start at one. Tie-break by source UUID,
relative path, entry ordinal and part index. No mixing raw score scales.

A diary repeats itself. A routine line recurs on hundreds of days, and
without grouping one query would spend the whole budget showing the same
sentence four times. After fusion, candidates with the same `text_hash`
within a source form one **group**, scored by its best member. A group
renders its text once, with the citation of its most recent occurrence and
a compact occurrence list: up to eight dates, then a count, such as
`also on 2027-03-09, 2027-03-10, … (+212 more)`. That list is often itself
the answer to “when did I …”. Grouping applies to ranked search only; literal
and timeline enumerate every occurrence, because they promise completeness.

Select at most four groups, at most two per entry; the observation budget
usually admits all four, and the rest become citations. Expand by at most one adjacent
passage on either side *within that entry*, subject to the same count/access/
rendering caps. Two windows that touch or overlap are merged only when the
merged range fits the 600-character window cap; otherwise they stay
separate windows, each charged to the budget, each containing its own
whole match — a merge never truncates and never drops the later match.
Stored source slices are never concatenated across gaps into one quote.

Timeline bypasses relevance search and the embedding service. Fetch at most 30
entries per DB page in the specified order; render complete bounded parts until
the observation budget is full. Its cursor resumes at the first unrendered part,
including within a long entry. Do not advance past 30 fetched entries when only
three were actually displayed. Literal/read continuations follow the same rule.
Ranked search exposes omitted candidate counts and citations, not a claim of
exhaustive corpus traversal.

Cursors are random DB UUIDs, expire after 30 minutes, bind room/agent/request and
source catalog/policy versions, and contain only positions/IDs. A ranked
search cursor stores the fused candidate list (at most 60 passage IDs) from
its first page, so later pages continue that ranking instead of re-running
it. A vector backfill landing between pages therefore cannot reorder or
duplicate results. A changed version returns `cursor_stale`; restart the
request rather than mix snapshots. Expiry
returns `cursor_expired`. Repeated cursor reads are idempotent while valid; each
page generates a cursor for its next position. Each cursor insert deletes
expired rows in the same transaction (the expiry index makes that cheap), so
the table stays small without a scheduled job; the CLI cleanup remains for
an idle installation.

### Embeddings and HNSW

Use local `embeddinggemma:300m`, 768 dimensions, batches of 32 for backfill.
Configure a loopback endpoint and record its model digest before a job.
The embedding input is exactly `passage.text`; inherited dates and author labels
are retrieval metadata, not prose prepended to the vector. The epoch is the
model name/digest, dimension and input-format version; vectors are stored per
source, epoch and passage `text_hash` (§4).

`embedding_spec` stores the loopback base URL, model name/digest, dimension and
input-format version. `embed` captures or validates this spec; changing it
sets vector mode off until the new epoch is ready. Old-epoch rows never
participate in current-epoch queries, and `embed` deletes them once the new
epoch covers every current passage.

The query adapter checks the served digest against stored configuration and
keys its cache by epoch + query. A mismatch disables the vector route until an
explicit re-embed. Do not reuse the seed cache keyed only by mutable model name.
Use a 2-second query-embedding timeout with zero retries, a 30-second background
batch timeout and at most two background retries. Do not silently switch provider.
These are new adapter limits, not the existing seed client's 10-second timeout.

Input-format version 1 is the bare passage text, matching how the seed and
claim paths embed today. EmbeddingGemma's model card recommends task
prompts instead (`title: none | text: …` for documents, `task: search
result | query: …` for queries). Version 2 is those prompts, and the pilot
measures it as its own baseline (§9). It is adopted only if it injects at
least as many topical gold spans and loses no baseline-passing case. The
version is already part of the epoch, so switching is a re-embed, not a
schema change.

Start with exact distance ordering over a materialized eligible-row set so an
existing HNSW index cannot change the baseline. In `hnsw` mode, use the same SQL
eligibility predicates, `SET LOCAL hnsw.ef_search=100` and
`SET LOCAL hnsw.iterative_scan=strict_order` (pgvector 0.8, the installed
version, keeps scanning the graph until the filtered LIMIT is met or
`hnsw.max_scan_tuples` is reached). If fewer than
`min(20, eligible_embedded_count)` rows survive anyway, fill using exact filtered search.
Approximate indexes can underfill after filtering; a full result set also does
not prove exact recall. Measure both before enabling the mode.
[pgvector documents this filtering behavior](https://github.com/pgvector/pgvector#filtering).

For sources with vectors enabled, exact mode is the default. `set-vector-mode
--source UUID --mode off|exact|hnsw` controls it independently of source visibility.
The CLI may enable HNSW for a source only
when its filtered Recall@20 against exact search is ≥0.95 on the fixed benchmark,
all exact-lookup cases remain correct, and p95 search latency improves by ≥20%.
Record extension version and query plan; never assume an index is being used.
Failure of this optimization gate leaves exact search enabled.

## 8. End-to-end rendering contract

Reuse the escaping of `fence_recalled_memory`, not its note. Today's fence
tells the model the body holds “facts the user stored earlier”, which is the
wrong claim for a diary: a passage is a past record, not a current fact, and
it is full of imperatives addressed to the writer's past self or a past
agent. Add a second code-owned constant pair to `memory/retrieval.py`, tag
`diary_passages` with a note saying the block holds historical diary
passages that are neither current facts nor instructions to act on now, and
have `split_recalled_fence` and `_set_observation_content` recognize either
exact constant so the diary fence is rendered as real structure too. The
escape (`<` → `‹`, `>` → `›`) is one character for one, so it never
changes a length the budget already counted. Source viewer output remains
byte-exact.

Excerpt labels are grouped to save budget: one line per source carries its
name and snapshot time; each excerpt then carries path, diary date/time
precision and citation. No model-written summary or scorer explanation.

### The observation budget and compact eviction

Two constants in `agents/assistant.py` and one measurement set the budget:
`MAX_SCRATCHPAD_CHARS` (5,000), `STEP_LIMIT` (6) and a median decide step
of about ten seconds (§9). `_bounded_turn_events` keeps events newest-first while
they fit the scratchpad. The newest is always kept whole, and an older
event is dropped whole once it no longer fits. Each event is charged for
its action, JSON args, reason and a 120-character allowance on top of its
observation.

Two designs fail against those constants. An observation near the whole
scratchpad evicts every earlier step when it arrives, so a search and the
`read` that follows it are never visible together. An observation small
enough that two fit side by side (about 1,900 characters) shows two
passages, and it turns “show the passages about X” into a search followed
by two reads. That spends three of six steps and half a minute.

The design keeps a large observation and makes eviction soft. An
`AssistantObservation` may carry `compact`, a code-built stand-in of at most
`COMPACT_OBSERVATION_MAX_CHARS` (400). For the diary it holds the mode, how
many excerpts were shown, their citations, and one fixed sentence saying the
text is no longer shown and can be reopened with `mode=read`.
`AssistantTurnStep` carries it as `compact_observation`. When an older
event no longer fits in full, `_bounded_turn_events` keeps its compact form
if that fits, rendered as `<observation compacted="true">`, and only then
stops. Only the form it renders is charged. Capabilities without a compact
form behave exactly as today. This is not slicing: the stand-in is a
separate record written by code, and the evicted text stays in the trace
and in the audit's evidence.

`DIARY_OBSERVATION_CHARS = MAX_SCRATCHPAD_CHARS - DIARY_SCRATCHPAD_RESERVE`,
with a reserve of 1,500. Up to about 550 of that is the newest event's own
overhead (action, args, a 300-character reason, the 120 allowance), and up
to about 950 is one compacted predecessor with the same overhead. That gives
3,500 today, and the constant follows any change to the scratchpad. A search
compacted to its citations plus a whole `read` fit together. After a third
diary step, the newest is whole, the one before it is compacted, and the
oldest drops out as it does today. A reason longer than
the reserve assumes costs the predecessor its place, exactly as any
over-budget event does today. The reserve is checked by a test that builds
a search event and a read event with 300-character reasons and asserts both
render.

The passage cap follows from the budget, with the overheads measured on
the renderer, not estimated. The status line with its degradation notes is
about 140 characters, the fence 139, the source line 60, each excerpt label
128 (the 54-character citation dominates it) and the continuation footer
105. Four 600-character passages bring that to about 3,360 of 3,500. The
routine lines that carry occurrence lists are short, so their lists fit in
the space those passages leave unused. A render test packs four maximal
passages with realistic labels at `DIARY_OBSERVATION_CHARS` and asserts all
four fit, so shrinking the scratchpad or lengthening a label fails a test
instead of silently showing fewer passages. (Estimated overheads of 90 and
150 had made 700-character passages look like they fit; they did not.) Changing the cap is a new
parser fingerprint and re-embeds, which is why it is fixed here rather than
recomputed at runtime.

| Limit | Value and meaning |
|---|---|
| Stored passage | 600 characters, no token-equivalence promise |
| Search candidates | 20 per route; no generative scorer call |
| Selected groups | ≤4, ≤2 per entry; neighbor expansion counts toward both |
| Diary observation | **`DIARY_OBSERVATION_CHARS` total** (3,500 today), including labels, fence, citations and continuation notice |
| Compact stand-in | ≤400 characters: mode, count, citations, reopen hint |
| Timeline DB batch | 30 entries; output may contain fewer, with exact continuation |
| Literal scan | 2-second DB statement timeout; timeout returns partial/error metadata, never “no matches” |
| Query embedding | 2 seconds, zero retries; lexical fallback |

Pack in rank order (source order for literal/read, timeline order for timeline).
Prefer complete passages. If a literal range is too large, choose one
contiguous window containing the match and a continuation offset. Do not
truncate a citation, join disconnected fragments or remove the middle of a
quote. Literal windows are at most 600 characters, start up to 160
characters before the match, and must contain the entire match; a match
longer than 600 characters is shown from its start with a continuation. A
`read` fills the budget with whole entries in source order and continues
mid-entry only when one entry alone exceeds it. Date/header context is
labeled metadata and charged to the same cap.
Clamp displayed path labels to 120 characters without changing their locator.
Omitted item notices are reserved before packing. An item that cannot fit yields
a citation/continuation, not an over-budget exception for the first item.

### Wiring

Wire and test the complete assistant path, not only `render_diary`:

1. Register `AssistantActionName.DIARY_QUERY = "diary_query"`: family
   `diary`, read-only, `required_args=("mode",)`, optional
   `query`/`date_from`/`date_to`/`source`/`citation`/`cursor`, and
   `output_cap_chars=DIARY_OBSERVATION_CHARS`. The dispatcher's cap is a
   plain slice that would cut the fence's closing tag. The renderer's packing
   therefore never reaches it, and a test asserts that the slice never fires.
2. In `handle()`, after `enabled_capabilities()`, drop `DIARY_QUERY` unless
   `diary_available(...)` holds, and store `models_local` for the turn's
   `AssistantActionContext`.
3. Carry `compact` through the dispatcher, whose output-cap path rebuilds the
   observation, into `AssistantTurnStep.compact_observation`, and teach
   `_turn_event_size`/`_append_turn_event` the compacted form.
4. `_set_observation_content` chooses the fence by action name:
   `memory_query` splits the recalled-memory fence, `diary_query` the diary
   fence, and every other action stays escaped text.
5. `_build_reply_audit_prompt` passes `diary_query` observations without the
   `REPLY_AUDIT_MAX_OBSERVATION_CHARS` (2,000) shortening, because the auditor
   checks quoted passages and a cut would hide the one a reply quotes. The
   audit sees every step, evicted ones included, so its diary evidence is
   bounded by `(STEP_LIMIT - 1) × DIARY_OBSERVATION_CHARS`, 17,500 characters
   today. That is an accepted cost for the one read whose output is quoted
   verbatim. Other reads keep the existing limit.

Tests on the real prompt builders: a search then a `read` renders the search
compacted (its citations present, its text absent) and the read whole; a
third step keeps one compacted predecessor and drops the oldest; legacy
actions render unchanged; an
answer-bearing span in the middle and at the end of a full-budget result
reaches both the deciding and the audit prompt.

Room/source rules are enforced before producing the observation. Fencing and
addressee hints are not proof against instruction carryover: test that a diary
`/goal` does not cause a write, including when no event extraction exists.

Record telemetry with `target_type=diary_citation`, `target_id` the citation
string, stages `retrieved` and `injected`, and source `diary.<mode>`
(`diary.search`, `diary.literal`, `diary.timeline`, `diary.read`), so the
modes can be told apart in rollups. Put route ranks, generation/policy
versions, counts, degradation and timings in metadata; do not duplicate
passage text there. The actual observation remains in the existing
assistant trace. CLI reports omit queries, identifier values and diary text
by default; private report files may contain operator-authored evaluation
labels, never committed fixtures.

## 9. CLI, pilot and performance evidence

Provide one entry point, `python -m tools.diary`, run from `source/`. All commands
require `--database-url`; parse and validate it and set `DATABASE_URL` **before
importing `db`**, without importing `webapp`, starting agents or firing cron.
The pilot accepts only a database whose parsed name is `rainbox_claude` or starts
with `rainbox_diary_test_`; a forbidden URL fails before any DB connection.
Normal production import requires a separate explicit `--production` option;
there is no production reset command. `reset-pilot` additionally requires the
source's recorded CLI-created pilot marker, so it cannot purge another sandbox
source accidentally.

Subcommands:

| Command | Behavior |
|---|---|
| `register --manifest PATH` | Validate and create/update one disabled source; print its UUID/config hash |
| `parse --source UUID --dry-run` | Pure inspection report without DB mutation or model calls |
| `sync --source UUID` | Publish snapshots/parses/identifiers/FTS; no embedding calls |
| `embed --source UUID` | Resume missing current vectors; report batches and failures |
| `index --source UUID` | Build/inspect optional HNSW; does not enable it |
| `set-vector-mode --source UUID --mode off\|exact\|hnsw` | Set the route mode; exact requires a recorded epoch, HNSW also requires its passing report |
| `probe --source UUID --cases PATH` | Run fixed private queries; JSON report with ranges/ranks and correctness |
| `show --citation LOCATOR` | Local operator inspection; print original slice, date basis and snapshot status |
| `enable`, `disable`, `exclude`, `unexclude` | Explicit source/file policy operations; bump versions atomically |
| `reconcile --source UUID [--exclude PATH]… [--release PATH]…` | List files held after an excluded file vanished; exclude or release each one |
| `prune --source UUID --path PATH` | Drop one file's non-current revisions; citations into them become `not_found` |
| `purge --source UUID` | Disable and remove imported content for that source; retain exclusions/config |
| `reset-pilot --source UUID` | Sandbox-only purge of that pilot source, never DROP/TRUNCATE or other sources |

`register --pilot` records a pilot marker in source config; it is sandbox-only.
`probe` accepts `--vectors off|exact|hnsw` as an inspection-only override to compare
routes without enabling assistant access. Pilot probes use a trusted CLI context
that permits the selected disabled source and supplies its actual room/agent;
file exclusions/quarantine still apply. No model-supplied argument can select
this context. A pilot source must be private, not secret.

`show` is a local operator operation distinct from the assistant's context-limited
read API. The assistant cannot invoke the operator bypass through action args.
No purge/reset deletes the input scratch files or any original diary file.

### Pilot order and gate

The pilot runs **after parser/schema/retriever tests pass**, before production
activation. Copy one calendar month's selected files into a private scratch root;
use a sandbox source/room. Pin its file hashes and write 20 private queries with
expected byte ranges before tuning: six literal, eight topical (at least two
query/source language pairs), four timeline, and two negative queries. Do not
require addressee extraction in this pilot.

1. Dry-run, inspect every diagnostic, then `sync` and probe literal/FTS baseline A.
2. `embed`; probe baseline B with vectors (input format 1). Keep the same
   queries/gold ranges. Re-embed with input format 2 into its own epoch and
   probe baseline B2; §7 says when it replaces format 1.
3. Inspect the ten longest entries, ten fixed-seed randomly selected entries
   and every `clock_regression`/`date_regression` site. Record accepted
   boundaries/date labels and every unresolved ambiguity.
4. Replay the month's `timed` file as it grew: in a second pilot source,
   write the file truncated at the end of each day in turn, and `sync` and
   `embed` after each. Record the stored raw bytes, entry/passage rows and
   vector rows after every sync. Keep every citation the probes returned on
   each day.
5. Optionally `index` and compare HNSW to exact; this does not gate lexical recall.
6. Write a private report: manifest/config/code/model hashes, counts, coverage,
   gold-range results, p50/p95 latency, cold/warm timings, DB/snapshot/index size,
   failures and fallback mode. Console diagnostics contain offsets, not snippets.

**The release gate is executable:** all synthetic hard-invariant tests pass; every
pilot literal's gold is the set of all its occurrences in the month, and
pagination returns exactly that set in source order (literal is exact, so
anything less than equality is a bug, not a ranking weakness); at least seven of eight topical
queries inject a gold span; all four timelines enumerate expected entries through
pagination without skips/duplicates; both negatives produce no false literal/date
hits; manual inspection finds no unexplained boundary/date errors. The append
replay must end with exactly one file's worth of raw bytes, one generation's
rows and at most one vector per distinct passage text. Every citation issued
on an earlier day for text the later days did not touch must come back as
the same string and resolve to identical bytes. A replay that grows with the
number of syncs fails the gate, whatever the recall. Any correction
updates parser/config and reruns the unchanged query set. Vector mode must also
preserve all baseline-passing cases. Add a separate held-out query set for later
optimization rather than repeatedly optimizing only these twenty.

Performance is measured over three warm passes of the fixed queries after one
warm-up; retain the raw samples. Initial target: p95 literal/FTS/timeline ≤500 ms,
p95 search including local query embedding ≤3 s on the pilot machine. Record
cold-start separately. Missing either target blocks enabling the corresponding
route until fixed or an explicit new budget is recorded; it does not block
continued development of other routes. A local-model outage must produce a
bounded lexical result rather than a stalled or empty-success response.

Planning observations taken on the operator's machine: 44 ms for one warm
embedding, 17 ms/passage in batches of 32, about 700 input and 50 output
tokens/s for extraction, and a 9.8-second median assistant decide step. These
are **planning observations**, not release guarantees: no benchmark manifest
accompanies them. The pilot supplies reproducible measurements. Its query embedding is
a model call; “no model call” applies only to literal/FTS/timeline routes.

Do not gate the first release on event extraction or “a weekend of GPU time.” For a later event
pilot, record a seeded sample of up to 200 passages and measure actual prefix
cache misses, output lengths and validation failures. A 12–15-hour
full-corpus estimate assumes prefix caching and roughly 50 output tokens per
passage; at 50 output tokens/s, output alone is about 11 hours for 40,000
passages. Without caching, 40,000 repeated 600-token prefixes add 24 million
input tokens, about 9.5 more hours at 700 tokens/s. On gemma4 that caching
depends on Ollama running with `LLAMA_ARG_SWA_FULL=1`; without it only a
byte-identical prompt is reused, so a shared prefix followed by a varying
passage is reprocessed whole. The pilot confirms the flag from the server
log before timing anything. The 40,000 figure counts passages, but
extraction runs per entry, so recount entries from the pilot rather than
reusing a passage count. Extraction remains opt-in, initially one month and then at most
the latest two years if its own evaluation justifies expansion.

## 10. Acceptance tests and implementation sequence

Fixtures are synthetic Terminator-universe material in English, Danish and
German. Include all three formats, plain fallback and append/edit/delete versions.
Gold targets identify file hash and byte range, not unstable generated UUIDs.
`diary_recall` scoring records candidate recall and injected-range coverage
separately. Forbidden-source exposure, invalid citations, budget overflow and
unexpected writes are hard failures; an averaged relevance score cannot mask them.
Missing/extra case inventories invalidate a comparison.

| Work package | Required acceptance evidence before the next package |
|---|---|
| **P1 — Parser and manifest**: `diary/config.py`, `parsing.py`, fixtures | All bytes covered; exact UTF-8/CRLF/BOM ranges; invalid encoding/NUL; every dialect; invalid date/time; filename precedence; terminal ambiguity/override; stale and invalid overrides skip without quarantine; clock/date regression diagnostics; first command token vs path; chunk boundary continuity; deterministic repeated output |
| **P2 — Persistence and CLI sync**: models, DB ops, bootstrap, CLI | Fresh/upgrade/repeated bootstrap; interrupted transaction exposes no partial generation; unchanged sync is a no-op; append/edit/quarantine/delete; thirty appends store the bytes once, keep one generation and re-embed nothing unchanged; citations stable across appends and moved by an edit; revert to an earlier revision; `prune` rematerializes before deleting; source scan failure; exclusions survive rebuild/rename/purge; exact-move carry and `held_for_reconcile`; rejected production pilot before import/connect |
| **P3 — Reads and rendering**: literal/FTS/timeline/read, cursor and renderer | Room/agent/secret/disabled/excluded isolation on every route; literal crossing chunks; SQL wildcard input treated literally; date filter before cap; paging a huge entry without skips; cursor expiry/version change; a vector backfill between search pages changes nothing; citations to old bytes (current-chain reader and superseded byte window); identical routine entries render as one group with an occurrence list; inclusive one-day bounds; locator probes outside the room return `not_found`; `‹`/`›` literal fallback; exact `DIARY_OBSERVATION_CHARS` boundary and closed `diary_passages` fence |
| **P4 — Optional vectors and pilot**: adapter, exact route, HNSW command | Stubbed vectors, invalid vector rejection, digest change, stale-worker discard, outage fallback; exact search despite HNSW existence; restrictive-filter ANN underfill; fixed pilot report/gates above |
| **P5 — Assistant and eval integration**: capability, availability, compaction, audit, telemetry, case scorer | Legacy `memory_query` tests unchanged; `diary_query` absent from the catalog with no enabled source, with any remote member in any slot (unless a source opts in), and when listed in `assistant.disabled_capabilities`; `capability_report` names the reason; search then read renders the search compacted and the read whole; the dispatcher's slice never fires; actual deciding and audit prompt retain gold span; historical commands cause no writes; telemetry retrieved vs injected is accurate by citation; pilot gates pass before enablement |

Tests live with their modules (`diary/test_*.py`, `db/test_diary.py`,
`agents/test_diary_memory.py`, `evals/test_diary.py`). Cleanup source-scoped rows
in `finally`; restore settings changed by tests. Unit tests inject fake embedders;
no live model calls in pytest. Follow the existing [sandbox and test guard](../testing.md).

Planned targeted commands once those files exist, from `source/`:

```sh
venv/bin/python -m pytest diary/ db/test_diary.py agents/test_diary_memory.py evals/test_diary.py -q
venv/bin/python -m pytest memory/ agents/test_assistant_actions.py agents/test_reply_audit.py -q
```

Verify actual deciding/audit prompt construction, not just a renderer snapshot.
The CLI pilot is a separate live run with a report. No real diary is required
for CI. Do not claim the pilot was run merely because synthetic tests pass.

Production enablement is explicit and ordered: additive schema deployed; sources
registered disabled; parse/sync/embedding completed; the fixed report and gates
pass; source enabled, which is the moment `diary_query` appears in that room. Failure or rollback disables access
while retaining citations and operator decisions. No automatic source ingestion
occurs on the first user query.

## 11. Follow-on designs: retain the direction, do not implement yet

Each needs its own implementation contract and gate; none is implied by the
first release being ready.

- **Events:** zero-or-more supported spans per entry, distinct author/speaker/
  addressee, diary time vs occurred time, uncertain attribution retained. Marker
  hints never prove a recipient. Versioned extraction jobs distinguish empty
  success from failure, recheck generation before publishing and use a configured
  local model. Generated summaries stay in the index/inspector. A generated
  sensitivity restriction must be durable and survive disabling extraction;
  source access is already adequate before classification begins.
- **Belief proposals:** operator-selected assertions through `record_belief`,
  actor `model_inferred`, existing candidate/tombstone/conflict rules, versioned
  `source_type=file` evidence. Requests and hypotheticals are not world facts.
  Add a durable source-observation/promotion ledger before retryable writes;
  repeated `record_belief` currently increments support. Source restrictions
  must invalidate dependent claims across every read/profile path. A lone
  `valid_from` column does not solve temporal conflicts or supersession.
- **Threads:** topic navigation across years, bounded episodes within topics,
  multiple memberships, explicit evidence for causal/resolution links. No
  outcome found is not an open commitment. No generated thread-state sentence
  in answer context, Datalog engine, graph store or runtime clustering.
- **Assistant diary:** a separate room-scoped managed export of run records.
  Today's summarizer stores optional trigger/obstacles/outcome, not verified work
  history. Render available fields deterministically, link run/step evidence,
  and never use a copied model digest as independent corroboration. Export needs
  retry/crash-safe file publication, revision handling, no recursive self-export,
  and typed run-evidence retrieval before it is useful to answering prompts.

## Appendix — synthetic source examples

The input is plain text the operator has appended to for decades and
occasionally corrects. The format has changed over the years, and the
files keep whichever shape they were written in, so the design assumes a
small set of **dialects**, each with an explicit grammar and an ambiguity
policy. The current one: a line holding one date, `YYYYMMDD`, then entries
opened by a time line — a start time `HHhMM` or a range `HHhMM - HHhMM` — followed
by free text until the next time line or date line. A synthetic day in
that shape:

```text
20270312
Woke at 07h10. Slept badly; the dog next door barked until two.

09h30 - 10h00
Physiotherapy exercises.

10h15
reread the threat model
https://example.org/notes/threat-model

11h02
find the two papers on sparse memory tables and write a note about them.
no need to download the models, there is no disk for it.

11h40
IDEA: let the assistant keep its own diary in this format, so we can
query each other's.

12h05
A user of the export tool asked for JSONL output (issue #41). Follow up
next week.

12h30
/goal process the queue

13h15
Mein Rücken fühlt sich wieder normal an.
```

An older dialect is a ChangeLog: a header line of a day-month-year date
with the month spelled out, followed by an author token, then one `*`
bullet per entry with continuation lines indented, no times, and the
newest day first in the file. Synthetic:

```text
27-juli-2027 kreese
*	refactoring: true/false return values from commands have been
	replaced by an exception system; a thrown exception means no
	modification occurred. This has improved robustness a lot.
*	version 0.8 is released.
*	bugfix: Edit::VSpace#move_left did not report its change,
	trashing undo/redo completely.

26-juli-2027 kreese
*	re-enabled Buffer#test_exception_xxx.
```

A third dialect keeps one file per day, `YYYY_MM_DD.txt`, with entries
opened by a bare four-digit time, blank lines inside an entry, and pasted
terminal sessions in the middle of the prose. Synthetic:

```text
0830
call with reese

exclude the admin tool
exclude images

we agree on what to build first


0910
reese says look at
http://trac.example.org/skynet/ticket/6

so I start with "product_info.php"

first the ignore list needs fixing.

www:/srv/skynet/www# svn st
?      admin
M      index.php
?      images/products/t800.gif
D      index.php.bak
www:/srv/skynet/www#


argh. commit fails, the repository is locked.
```

Entries mix languages, sometimes inside one entry; carry exact technical
strings; and differ in **whom they are for**: a note to self, an instruction
to an agent (an imperative, a `/word` command), a personal idea (`IDEA:`),
or a report of what another person said or asked. Health and body notes
sit between project notes. Ingestion never edits these files. All examples
in this document are synthetic; the operator's diary appears nowhere in
the repository.
