# Cleanups

Known-but-deferred work: things that are wrong, stale, or untidy, and were
left that way deliberately rather than missed. Each entry says where it is and
why it was deferred, so picking one up does not start with an investigation.

This is a backlog, not a design doc. Delete an entry when it is done.

## Repo-wide

### `db.db.session` → `db.session`

**1332 occurrences across 154 files** (mostly tests). `db/__init__.py`
re-exports `session`, so `db.session` already works everywhere and new code
uses it; the old spelling is the extension object routed through the facade
for no reason, and reads like a typo.

Deferred because the sweep touches 154 files and would bury whatever change it
rode in on. It is mechanical — the string is unambiguous — so it wants its own
branch and one full-suite run, not a slow migration.

### `testing.md` claims a green suite

`notes/testing.md` says "There are no known failures: the suite is green (2384
passed, 10 skipped)". Both halves are stale: the count is now ~2637, and
`db/test_cron_backup.py::test_fire_backup_job_without_recipient_posts_error`
fails. That failure predates the response-language gate branch — confirmed by
running it at that branch's base commit — and nobody has diagnosed it.

Two jobs, and the first is the real one: find out why the cron backup test
fails, then make `testing.md` describe the suite as it actually is. A doc that
says "no known failures" is worse than one that names a failure, because it
teaches the reader to distrust a red suite as environmental.

## Response-language gate

### The design spec has drifted from two signatures

`docs/superpowers/specs/2026-08-27-response-language-shift-gate-design.md:315`
gives `window_dominant(messages) -> str | None`. It returns
`tuple[str | None, int]` — the dominant language and how many messages
qualified — and takes `texts`. `decide` is keyword-only over `window_texts`,
`request_text`, `has_previous` and `profile_languages_changed`.

The prose in that spec is accurate and carries the measurements the thresholds
come from; only the component signatures lagged.

### The detection cache is bounded in count, not in size

`agents/response_language_gate.py` memoises `detect` with
`lru_cache(maxsize=512)` keyed by the raw message text. The window re-reads the
same history every turn and a message's language cannot change once written,
so the cache is worth having — but 512 entries of arbitrary length means a run
of long pastes holds them all for the life of the process.

Keying on a hash, or capping what is cached by length, would bound it. Nobody
has seen this matter; it is written down so the next person reading that
`lru_cache` does not have to work out whether it was considered.

### `window_dominant` breaks ties by dict insertion order

`agents/response_language_gate.py:183` — `max(totals, key=...)` returns the
first key inserted when two languages tie on weighted confidence, which is
deterministic (window order, then the detector's own ordering) but incidental.
No test depends on it. If one ever needs to, the rule should be made explicit
rather than inherited from dict ordering.

### A first turn that names a language is labelled `named_language`

The name check runs before the no-previous check, so a room's very first
message naming a language records `trigger: "named_language"` where
`no_previous` is equally true. Both ask, so behaviour is right and only the
trace label is arguable. The ordering is deliberate — the name check is
cheapest and independent of everything else — so this is a labelling question,
not a logic one.

### One dependency pin lacks the comment its siblings have

`source/requirements.txt` — `lingua-language-detector==2.2.0` sits without the
trailing explanation that `language_data` beside it carries.

## Not cleanups: decisions waiting on the operator

The gate ships behind `assistant.response_language_gate`, default off. Two
things should happen before it is judged, both described at the end of
`docs/superpowers/specs/2026-08-27-response-language-shift-gate-design.md`:
bind `assistant.response_language_classifier` to a small model on
`/agentmodel` (it resolves to `assistant.default`, which is why an 81-token
answer costs 11.4s), then turn the switch on and read some runs.
