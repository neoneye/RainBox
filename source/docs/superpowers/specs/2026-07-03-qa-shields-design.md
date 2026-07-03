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

- **Entry field.** An entry gains an optional single `"shield": "human"` string.
  An entry with no `shield` (absent or empty) is always visible — so all existing
  base entries are unchanged.
- **Reveal rule.** An entry with a shield reaches the LLM only when that shield
  is currently unlocked. Otherwise it is hidden.
- **Default.** No shields unlocked ⇒ every shielded entry is hidden. This is the
  safe default the operator asked for.

Names are the operator's to choose, so they can distinguish kinds of subject
(`human` vs `bot` vs a persona name) or sensitivity, or a future family member's
name — whatever grouping they want. A single entry sits behind exactly one
shield; an entry that is sensitive on two axes is placed behind the more
restrictive of them (or a shield named for that combination).

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

## Filter placement — one chokepoint, all three agents

All three agents reach Q&A content through the same functions in
`memory/seed_memory.py`. Verified call sites:

- `query_router` → `_exact_match`, `_semantic_ranked`, `_resolve_match`
- `query_filter_router` → `_exact_match`, `_semantic_ranked` (candidates via
  `[:TOP_K_FILTER]`), `get_entry`, `_resolve_match`
- `assistant` → `_exact_match`, `_semantic_match`, `retrieve_seed_memories`,
  `_resolve_match`

`_semantic_match` is a gate on top of `_semantic_ranked`. So filtering at three
functions covers everything, matching the codebase's existing "filter before
rank" contract:

1. `_exact_match` — after resolving the alias to a qa_id, if that entry is
   shielded (locked), return `None` (a locked exact hit is treated as no match).
2. `_semantic_ranked` — drop locked qa_ids from the candidate list before
   ranking/returning. `_semantic_match` inherits this automatically.
3. `retrieve_seed_memories` — skip locked entries.

`_resolve_match` and `get_entry` need no change: they only ever receive qa_ids
that already passed the filter above. Keeping the filter at the retrieval
boundary (not the resolve boundary) means one source of truth.

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
```

The three filtered functions gain an optional injected parameter
(`unlocked_shields: set[str] | None = None`) that defaults to
`_unlocked_shields()` when omitted. Production callers pass nothing (they run
inside an app context); tests inject an explicit set. Existing callers need no
change.

Reading `get_setting` once per retrieval call (not per candidate) is fine — it's
one indexed lookup and retrieval already touches the DB.

## Discovery + Settings UI

- New helper `available_qa_shields() -> list[str]`: load the KB, return the
  sorted distinct shield names across all loaded entries. Drives the checklist.
- The Settings page keeps `qa.unlocked_shields` as an ordinary registry setting
  (so its source badge and JSON persistence work), but renders a **checklist**
  for this one key — a small special-case mirroring the existing `customize.dir`
  "Repopulate Q&A memory" button. One checkbox per discovered shield, labelled so
  checked = *unlocked* (revealed to the LLM), unchecked = *locked* (hidden, the
  default). Saving posts the JSON array of unlocked names through the existing
  `settings_set_api`.
- The available-shields list is injected into the page payload (server side,
  after loading the KB) alongside the settings JSON, so no new endpoint is
  strictly required; a tiny read-only endpoint is acceptable if it keeps the
  template cleaner.

## Non-goals / honest limitations

- **Not encryption-at-rest.** A locked shield stops an entry from reaching the
  LLM's prompt. The entry's text still lives (embedded) in the `data_seed_memory`
  pgvector table and in database backups. This is a prompt-level filter only.
- **No re-embed on toggle.** Shields filter at retrieval time; embeddings are
  unchanged. Adding/removing entries in the overlay still uses the existing
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
- `_exact_match`: a locked exact hit returns `None`; unlocking it returns the
  match; unshielded entries unaffected.
- `_semantic_ranked`: locked qa_ids dropped from candidates; ordering of the
  survivors preserved; `_semantic_match` inherits the exclusion.
- `retrieve_seed_memories`: locked entries skipped, `limit` still honoured over
  the survivors.
- `available_qa_shields`: distinct + sorted across base and overlay entries;
  empty when nothing is shielded.
- Settings roundtrip: `qa.unlocked_shields` set/get through the registry;
  default `[]`.

## Constraint

Per the operator's request, no sensitive category names appear anywhere in the
implementation, tests, commit messages, or this spec — only neutral placeholders
and the mechanism itself.
