# Q&A shields — hide selected Q&A groups from the LLM

## Problem

The operator's private Q&A overlay (the `question_answer.jsonl` under
`RAINBOX_CUSTOMIZE_DIR`, merged over the base registry — see `customize.dir`)
contains entries the operator rarely wants the LLM to reach: facts about
specific real people, specific bots/personas, or sensitive topics. Today every
loaded entry is retrievable by every agent. We want per-group visibility that is
**hidden by default** and unlocked from the Settings page, applying uniformly to
the three agents that consume the Q&A system: `query_filter_router`,
`query_router`, and `assistant`.

## Model: shields

An entry can sit behind a named **shield**. A shield is *locked* (blocking) by
default; the operator *unlocks* the ones they want to let through on the Settings
page. Shield names are free-form strings the operator chooses — the code
hardcodes none of them and discovers whatever names appear in the loaded
entries.

- **Entry field.** An entry gains an optional single `"shield"` string. Like the
  existing `path` field it is conventionally a **dotted path** — e.g.
  `"alice.travel"`, `"alice.projects"`, `"bob.notes"` — so related shields
  cluster together in the Settings UI. An entry with no `shield` (absent or
  empty) is always visible, so all existing base entries are unchanged.
- **Reveal rule.** An entry with a shield reaches the LLM only when that exact
  shield string is currently unlocked. Otherwise it is hidden.
- **Exact match, not prefix inheritance.** The dots are purely a naming
  convention for clustering the UI; the filter compares the *whole* string.
  Unlocking `alice` does **not** unlock `alice.travel` — no accidental broad
  reveal. (The UI may offer a per-cluster "unlock all" convenience, but it
  expands to the explicit leaf names it stores; see the UI section.)
- **Independent switches.** Each shield is its own on/off switch, and the
  operator can unlock any number of them at once — the setting holds a *list* of
  unlocked names. Unlocking one shield has no effect on the others; unlocking
  several simultaneously reveals every entry behind any of them.
- **Default.** No shields unlocked ⇒ every shielded entry is hidden. This is the
  safe default the operator asked for.

Names are the operator's to choose. The dotted path lets them organise shields by
owner then topic (`owner.topic`, or deeper like `owner.topic.year.label`) —
distinguishing kinds of subject, sensitivity, or a future family member — however
they want. A single entry sits behind exactly one shield; an entry that is
sensitive on two axes is placed behind the more restrictive one (or a shield path
named for that combination).

### Stored setting

New registry entry in `db/settings.py`:

```python
"qa.unlocked_shields": Setting(
    "qa.unlocked_shields", None, "json", [],
    description="Names of Q&A shields the operator has unlocked. A Q&A entry "
                "carrying a shield reaches the LLM only when that shield is in "
                "this list; an entry with no shield is always visible. Empty "
                "(the default) keeps every shielded entry hidden.",
),
```

`json` type, default `[]`. Reuses the existing get/set/persist/badge machinery.
Unlocking/locking a shield is instant — it changes retrieval, not the
embeddings, so no repopulate is needed (see Non-goals).

## Filter placement — exclude at the vector query, plus an in-memory backstop

All three agents reach Q&A content through the same functions in
`memory/seed_memory.py`. Verified call sites:

- `query_router` → `_exact_match`, `_semantic_ranked`, `_resolve_match`
- `query_filter_router` → `_exact_match`, `_semantic_ranked` (candidates via
  `[:TOP_K_FILTER]`), `get_entry`, `_resolve_match`
- `assistant` → `_exact_match`, `_semantic_match`, `retrieve_seed_memories`,
  `_resolve_match`

`_semantic_match` is a gate on top of `_semantic_ranked`. Two layers cooperate,
so a locked entry is excluded *before* it can consume a top-K slot:

**Layer 1 — vector-query metadata filter (the important one).** The shield is
stored in each node's pgvector metadata (see `_build_documents` below), so the
retriever excludes locked entries in SQL. Top-K then never *considers* a locked
entry — it can't crowd out legitimate results. In `_semantic_ranked`:

```python
from llama_index.core.vector_stores import (
    MetadataFilter, MetadataFilters, FilterCondition, FilterOperator,
)

def _shield_filters(unlocked: set[str]) -> MetadataFilters:
    # Unshielded nodes have no "shield" metadata key -> IS_EMPTY keeps them.
    keep = [MetadataFilter(key="shield", operator=FilterOperator.IS_EMPTY)]
    if unlocked:
        keep.append(MetadataFilter(
            key="shield", value=sorted(unlocked), operator=FilterOperator.IN))
    return MetadataFilters(filters=keep, condition=FilterCondition.OR)

nodes = index.as_retriever(
    similarity_top_k=TOP_K, filters=_shield_filters(unlocked),
).retrieve(query)
```

`IS_EMPTY` matches nodes with no `shield` key, so unshielded entries — and any
node embedded before this feature shipped — always pass. Deploying therefore
needs **no** immediate re-embed; a repopulate is only required once the operator
actually shields something (so the new metadata lands in the table).

**Layer 2 — in-memory backstop.** After retrieval, `_semantic_ranked` still
drops any candidate whose *current* in-memory entry is locked, via
`_entry_locked(_entries_by_id[qa_id], unlocked)`. This enforces a lock correctly
even in the window where the operator has just edited the overlay to add a shield
but has not yet pressed "Repopulate Q&A memory" (stale table metadata would
otherwise let it through). Same `unlocked` set drives both layers.

**Exact + seed paths.**

1. `_exact_match` — resolves via the alias table (not the vector store), so it
   post-filters with `_entry_locked`: a locked exact hit returns `None`.
2. `retrieve_seed_memories` — runs through `_semantic_ranked`, so it inherits
   both layers.

`_resolve_match` and `get_entry` need no change: they only ever receive qa_ids
that already passed the filters above.

### Storing the shield in node metadata (`_build_documents`)

Write the shield into each node's metadata **only when the entry has one**, so
unshielded entries keep no `shield` key (the `IS_EMPTY` filter depends on this):

```python
shield = e.get("shield")
if shield:
    md["shield"] = shield
```

It is added before `keys = list(md.keys())`, so it is already covered by
`excluded_embed_metadata_keys` / `excluded_llm_metadata_keys` — the shield never
pollutes the question-only embedding. Changing/adding/removing a shield in the
overlay requires the existing "Repopulate Q&A memory" flow to refresh the table;
toggling a shield's lock in Settings does not (it only rebuilds the per-query
filter).

### New helpers in `memory/seed_memory.py`

```python
def _unlocked_shields() -> set[str]:
    """Shields the operator has unlocked. Empty when unset or when called
    outside a Flask app context (the safe default: every shielded entry stays
    hidden)."""
    try:
        return set(get_setting("qa.unlocked_shields") or [])
    except Exception:
        return set()

def _entry_locked(entry: dict[str, Any], unlocked: set[str]) -> bool:
    """True if the entry is hidden: it carries a shield that is not unlocked."""
    shield = entry.get("shield")
    return bool(shield) and shield not in unlocked

def _drop_locked(matches: list[Match], unlocked: set[str]) -> list[Match]:
    """Layer-2 backstop: drop matches whose *current* in-memory entry is locked
    (order preserved). Pure over `_entries_by_id`, so unit-testable with no DB."""
    return [
        m for m in matches
        if not _entry_locked(_entries_by_id.get(m.qa_id) or {}, unlocked)
    ]
```

`_semantic_ranked` applies `_drop_locked` to its retrieved candidates;
`_exact_match` uses `_entry_locked` on the single resolved entry.

`_exact_match`, `_semantic_ranked`, and `retrieve_seed_memories` gain an optional
injected parameter (`unlocked_shields: set[str] | None = None`) that defaults to
`_unlocked_shields()` when omitted. Production callers pass nothing (they run
inside an app context); tests inject an explicit set. Existing callers need no
change.

Reading `get_setting` once per retrieval call (not per candidate) is fine — it's
one indexed lookup and retrieval already touches the DB.

## Discovery + Settings UI

- New helper `available_qa_shields() -> list[str]`: load the KB, return the
  sorted distinct shield strings across all loaded entries. Drives the checklist.
- The Settings page keeps `qa.unlocked_shields` as an ordinary registry setting
  (so its source badge and JSON persistence work), but renders a **clustered
  checklist** for this one key — a small special-case mirroring the existing
  `customize.dir` "Repopulate Q&A memory" button. The discovered shield strings
  are grouped by their dotted-path prefix (split on `.`), so all of one owner's
  shields sit under one heading. One checkbox per full shield string, labelled so
  checked = *unlocked* (revealed to the LLM), unchecked = *locked* (hidden, the
  default). Saving posts the JSON array of the checked full shield strings through
  the existing `settings_set_api`.
- Clustering is presentation only: a group heading may carry an "unlock all in
  this group" convenience checkbox, but it simply toggles the descendant leaf
  boxes — what gets stored is always the explicit list of full shield strings, so
  there is no implicit prefix unlock.
- The available-shields list is injected into the page payload (server side,
  after loading the KB) alongside the settings JSON, so no new endpoint is
  strictly required; a tiny read-only endpoint is acceptable if it keeps the
  template cleaner.

## Non-goals / honest limitations

- **Not encryption-at-rest.** A locked shield keeps an entry out of the top-K
  retrieval and out of the LLM's prompt, but its row (text + embedding) still
  physically exists in the `data_seed_memory` pgvector table and in database
  backups. This is a retrieval-level filter, not encryption.
- **No re-embed on toggle.** The lock is applied as a per-query metadata filter
  rebuilt from the setting each call; embeddings are unchanged. Editing the
  overlay (adding/removing/renaming a shield or entry) still uses the existing
  "Repopulate Q&A memory" flow; unlocking a shield does not.
- **Global, not per-room.** One operator-wide setting (the chosen scope). A
  per-room override is out of scope for this spec.
- **One shield per entry.** No AND-of-multiple-shields in this version; a single
  optional string keeps the model and the JSONL simple.

## Testing

Unit tests only — no live DB required for the filter logic (inject the unlocked
set). All shield names in tests are neutral placeholders (e.g. `alpha`, `beta`,
`colleagues`, `bots`) — never sensitive category names.

- `_entry_locked` truth table: no shield always visible; shield locked ⇒ hidden;
  shield unlocked ⇒ visible.
- `_shield_filters`: builds `IS_EMPTY`-only filter when nothing is unlocked;
  `IS_EMPTY OR IN[...]` when some are — the mechanism, without a live store.
- `_build_documents`: shield present ⇒ `shield` key on the node metadata and in
  the excluded-metadata keys; shield absent ⇒ no key.
- `_exact_match`: a locked exact hit returns `None`; unlocking it returns the
  match; unshielded entries unaffected (injected `unlocked` set).
- `_drop_locked` (layer-2 backstop): given `Match`es over a registry where one
  qa_id is locked and one is unlocked/unshielded, the locked one is removed and
  order is preserved — no DB needed. Covers the stale-table window and, through
  `_semantic_ranked`/`_semantic_match`, all agent paths.
- `retrieve_seed_memories`: locked entries skipped, `limit` still honoured over
  the survivors.
- `available_qa_shields`: distinct + sorted across base and overlay entries;
  empty when nothing is shielded.
- Settings roundtrip: `qa.unlocked_shields` set/get through the registry;
  default `[]`.

Layer 1 (the SQL metadata filter) is exercised end-to-end against
`rainbox_claude` if a repopulate/embedding run is available in the environment;
otherwise its unit coverage is `_shield_filters` and the implementer verifies the
`IS_EMPTY` + `IN` translation against the real pgvector table before merging
(confirm an unshielded and a pre-existing keyless node both survive, and a locked
node is absent from top-K).

## Future work (out of scope here)

- **Incremental repopulate.** Today `rebuild_kb` fully truncates and re-embeds
  the seed store; fine at the current scale, but it will get slow as the overlay
  grows. Follow the pattern already proven for memory-claim embeddings in
  `memory/embeddings.py` (`_text_hash` + `ensure_memory_embedding` skips when the
  stored hash matches; `sync_memory_embeddings` embeds only the dirty and prunes
  stale): store a **sha256 of the entire JSONL row** on the node metadata and, on
  repopulate, re-embed only rows whose hash changed, add new ones, and delete
  removed ones.
  - Hashing the whole row means every field that matters — `questions`,
    `answer`/`handler`, `kind`, `path`, and `shield` — is covered by one dirty
    bit, so a shield-only edit correctly counts as dirty (no risk of layer 1
    running on stale metadata). Any change to a row simply re-embeds that row.
  - Over-invalidation is safe: a cosmetic change (reordered keys, whitespace)
    flips the hash and re-embeds one row unnecessarily — cheap, and never wrong.
    Hash the raw line, or a canonical serialization if that churn ever matters.

## Constraint

Per the operator's request, no sensitive category names appear anywhere in the
implementation, tests, commit messages, or this spec — only neutral placeholders
and the mechanism itself.
