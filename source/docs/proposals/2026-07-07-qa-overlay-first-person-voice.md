# Q&A overlay: first-person voice — "I" is the path's subject

A voice convention for the operator's private Q&A overlay, layered on the
person schema (see `2026-07-04-qa-overlay-person-schema.md`): inside
`human.<personid>.*` entries, answers are written in first person, and
"I"/"my" refer to the person named in the path — not to the bot, and not to
any fixed operator.

All names in this document are fictional placeholders (`ada`, `cleo`, …).

## The idea

Instead of every answer restating its subject's name —

```json
{"path": "human.ada.job.overview",
 "answer": "Ada is a retired nurse. Ada worked at the county hospital…"}
```

— the path carries the subject once, and the answer speaks as that person:

```json
{"path": "human.ada.job.overview",
 "answer": "I am a retired nurse. I worked at the county hospital…"}
```

## What it buys

- **Multiple people can author their own entries.** A household member writes
  about themselves the way anyone writes about themselves — "I", "my" — like
  a diary. Nobody has to compose text about themselves in the third person,
  which is awkward enough that it becomes a barrier to contributing at all.
- **The path becomes the single binding of subject.** Today the subject is
  stated twice — in the path and in every sentence of the answer — and the
  two can drift. With subject-relative voice there is exactly one binding.
- **Portability.** The convention (and even entry text) works unchanged for
  whoever the subject is; entries can be contributed by their subject and
  copied between instances without a rename pass through the prose.
- **It matches what the entries are.** Under the cards-and-stories schema, a
  story is testimony. First person is the natural voice of testimony; the
  path names the witness.

## Where a bare "I" breaks today — verified

All three agents obtain the answer text through `_resolve_match` in
`memory/seed_memory.py`, and **none of them passes the path along**, so
nothing downstream can bind the pronoun:

1. **`query_router` exact match posts the answer verbatim as the bot's chat
   reply, with no LLM call** (`agents/query_router.py`). A first-person
   answer would come out of the bot's mouth as a claim about itself: the bot
   would tell the room "I am a retired nurse."
2. **`query_filter_router`** hands the candidate answer text to the LLM as a
   hint, without the path. The LLM cannot tell whose "I" it is reading.
3. **The assistant's `query_memory`** renders each fact as
   `uuid, seed/<source>: <answer>` — uuid and source tag, no path.

Worse, an unbound "I" has a *default wrong reading*: the bot's own persona
entries (`identity.*`) legitimately use "I" to mean the bot. An LLM that sees
first-person text among recalled facts will tend to fold it into the bot's
identity.

**So the convention is unsafe to adopt as pure authoring practice.** It needs
one small render change first.

## The fix: bind the pronoun at the choke point

`_resolve_match` is the single point where a seed answer leaves the store.
For subject-voiced namespaces, prefix a byline derived from the path:

```
[ada] I am a retired nurse. I worked at the county hospital…
```

- **Byline = the person id**, the second path segment of `human.<pid>.*` — no
  lookup, no new field. The id → display-name binding lives where it already
  lives: the identity card.
- **`household.<hid>.*` gets the same treatment** with "we" as the pronoun:
  `[maplest] We host the summer gathering…`
- **The verbatim chat reply keeps the byline.** `[ada] I am a retired nurse`
  is honest UX: the bot is visibly relaying someone's own words rather than
  speaking as itself. (If the bracket-id feels raw in chat, the router can
  later resolve the display name from the identity card; the bracket form is
  the correct minimum.)
- **One line of prompt guidance** in the three consuming agents: text
  prefixed `[<id>]` speaks in the voice of that subject; "I" inside it is
  that person, never you.
- `SeedMemory.answer` (used by the assistant's fact lines) gets the same
  byline at construction, so every consumer is covered by two small edits.

Base-registry entries and non-person namespaces are untouched: `identity.*`
keeps its unprefixed "I" — that voice *is* the bot's.

## Voice rules

1. **Exactly two voices may say a bare "I":** the bot in `identity.*`, and
   the path's subject in `human.<pid>.*` (with "we" in `household.<hid>.*`).
   Everywhere else, name people.
2. **The identity card is the one third-person exception per person.** It is
   the anchor that binds the id to a display name — the entry a reader (or a
   future display-name resolver) consults to learn who `[ada]` is. It stays
   "Ada Quist (b. 1950), retired nurse…".
3. **Write under your own id when writing as yourself.** A story about a
   friendship is the author's testimony and lives in the author's subtree
   (`human.cleo.friend.ines` — "we met at Acme…"). Under another person's id
   goes only what that person would say themselves.
4. **Quoting is explicit.** Inside an entry, the bare "I" never shifts; if
   the subject relays someone else's words, they are quoted and named.
5. **Questions keep the subject's name.** Questions are the embedding
   surface, matched against *anyone's* ask. First-person questions ("What is
   my job?") are word-for-word identical across subjects — retrieval cannot
   tell Ada's from Cleo's apart without knowing who is asking. The name in
   the question list is the retrieval key; the first person lives in the
   answer. So:

```json
{"path": "human.ada.job.overview",
 "questions": ["What is Ada's job?", "Ada Quist professional background"],
 "answer": "I am a retired nurse. I worked at the county hospital…"}
```

## Later: speaker identity

The full payoff — a household member asks "what is *my* schedule?" and gets
*their* entry — needs the retrieval layer to know who is speaking. Once chat
carries a speaker identity, the ask (not the entry) is where "I" gets
resolved: rewrite "my" → the speaker's name before embedding, and/or boost
`human.<speaker>.*` candidates. A populate-time variant — embedding each
named question a second time with the name replaced by "I/my", scoped per
subject — only becomes meaningful with that same speaker signal, for the same
collision reason.

None of this blocks the voice convention: with named questions and the
byline, first-person answers work today, single-operator, unchanged.

## Adoption order

1. **Land the byline first** (`_resolve_match`, `SeedMemory` construction,
   one prompt line per consumer). Do not flip any answer to first person
   before it ships — the verbatim reply path misattributes from the first
   entry.
2. Rewrite answers to first person opportunistically, entry by entry;
   questions keep names; identity cards stay third person.
3. **Repopulate Q&A memory** after edits (answer text changed).

## The trade-off

The byline spends a few tokens per fact and shows a machine-ish id in
verbatim chat replies. In exchange, every consumer — deterministic or LLM —
gets an unambiguous speaker for free, from data already in the path. The
alternative of binding voice by convention alone (no byline) is exactly the
kind of invisible contract that fails silently: everything works until an
exact-match reply makes the bot claim to be a retired nurse.
