# Q&A overlay: person data as cards and stories

An authoring convention for the operator's private Q&A overlay
(`<customize.dir>/question_answer.jsonl`, which overlays the base registry).
It applies to entries about **people** — individuals, their relationships, and
the households they belong to.

All names in this document are fictional placeholders (`ada`, `ben`, `cleo`, …).
Substitute your own; never commit real personal data.

## What this data is — and is not

The overlay is not a database; it is a bag of **retrieval units**. An entry
reaches the LLM when one of its `questions` embeds close to the operator's
question, or when its `path` is looked up exactly. Four consequences drive
everything below:

- **The question list is the real schema.** The path is a label for the human
  maintainer; the questions decide what the entry can ever answer. An entry
  with no questions is unreachable by similarity, however good its answer.
- **An entry is a chunk.** Its size is a retrieval decision: retrieved whole,
  it should answer the matched question without burying the answer in
  unrelated topics, and without being so atomic that the LLM must assemble
  five entries to say one sentence.
- **Recall beats normalization.** Stating a fact twice is cheap; failing to
  retrieve it is expensive. Duplication is only a problem for facts that
  *change* — those need a single maintained home.
- **The maintenance budget is the constraint.** The operator edits this file
  by hand, occasionally. Only facts that must stay current deserve a
  maintained home. Everything else should be allowed to be what it naturally
  is: a dated account, written once.

Full normalization — one fact, one home, everywhere — optimizes for a database
client that follows joins. Nothing here follows joins. It would also force
long-form narratives to be decomposed into atoms, which costs hours of
authoring, loses the writer's voice, and turns every old account into a
standing maintenance liability. The schema below spends normalization only
where it pays.

## Two kinds of entries

| | **Card** | **Story** |
|---|---|---|
| holds | current truth about one subject | a dated account: an anecdote, a friendship, a CV, a year's trips |
| size | a few lines | as long as it wants to be |
| tense | present | past, anchored to its date |
| updated | edited in place when reality changes | append-only; never rewritten for consistency |
| duplication | one fact, one card | free to restate anything — it is testimony, not truth |

Cards are the maintained index of the world; stories are the archive. A fact
inside a story is implicitly *as of the story's date*; when a story and a card
disagree, the card wins — the story hasn't become wrong, it has become
historical. This is what makes the corpus sustainable: when reality changes,
the operator touches one card, and no story ever needs a rewrite.

Long narrative entries — a friendship's history, gratitude for one's parents,
a reflection written during a strange year — are not a smell to be normalized
away. They are the most valuable entries in the file: they carry context,
causality, and voice that no fact atom can. The schema's job is to surround
them with a thin layer of cards, not to replace them.

## Entry schema

Both kinds use the same JSON line format as the base registry:

```json
{"id": "<uuid>", "path": "human.ada.identity", "kind": "static",
 "questions": ["Who is Ada?"], "answer": "…", "shield": "ada"}
```

No new fields are needed. A story's date lives in its path (year suffix) and
in its answer text; a card carries no date because it is always current.

## Person

### Cards — the maintained set

Every person named *anywhere* in the file gets an **identity card**, even if
they only appear inside someone else's story. Two lines is enough:

```json
{"path": "human.ada.identity",
 "questions": ["Who is Ada?", "Ada", "Ada Quist"],
 "answer": "Ada Quist (b. 1950), retired nurse. Partner of Ben, mother of Cleo and Dov. Household: maplest."}
```

People central to the operator's life additionally get a **relations card**,
one edge list anchored on the subject:

```json
{"path": "human.cleo.relations",
 "questions": ["Cleo's family", "Who are Cleo's parents and siblings?"],
 "answer": "Oldest child of Ada & Ben. Sibling: Dov. Partner: Faye. Children: Gil (2018), Hana (2022). Household: oakave."}
```

Edges are stated from both ends (Ada lists Cleo; Cleo lists Ada). That is
deliberate: a question about either person retrieves the link. Roles are
relative statements anchored per person ("oldest child of Ada & Ben"), so a
role never contradicts itself across records. Deceased people get a lifespan
on the identity card: `(b. 1914 d. 1991)`.

**Write card facts so they don't age.** Birth year, not current age; "since
2019", not "for six years"; "as of 2026-07" on anything that will drift (a
living situation, a plan, being single). A card that can silently go stale is
a card written wrong.

### Stories — everything else

Stories hang under the person as topical leaves, topic first, year last when
the account is anchored to a time:

```
human.cleo.friend.ines          — the history of one friendship
human.cleo.job.cv.2019          — a CV as written that year
human.cleo.health.knee.2021     — one incident
human.cleo.reflection.2020      — a mood, a plan, a snapshot of a mind
human.cleo.food.pizza           — a standing preference, told as prose
```

A relationship with its own history gets its own story leaf named
`<reltype>.<otherid>` (`friend.ines`, `expartner.mira`): the narrative is
anchored on the subject, while the other person's own facts live on *their*
identity card. Never year-first paths (`2019.jobchange`) — that scatters one
person's timeline instead of clustering it under its topic.

The **split test** for an oversized story: if a question you want answered
targets only one paragraph of it, that paragraph wants to be its own entry.
Otherwise, leave the story whole.

## Household

A household is a social unit; `location.<placeid>.*` remains the physical
place and its structures. When the unit is defined by an address the two may
share an id; they still hold different facts.

One **roster card**, membership by reference:

```json
{"path": "household.maplest.roster",
 "questions": ["Who lives at the Maple St household?", "the maplest family"],
 "answer": "Members: Ada, Ben, and their eldest child Cleo. Place: location.maplest."}
```

Everything else about the unit's shared life — trips, gatherings, clubs,
routines — is stories under `household.<id>.*`:

```
household.maplest.travel.2026.farmstay
household.maplest.events
household.maplest.community.tennisclub
```

This is where couple-level facts live, instead of a compound
`human.family.<a>.<b>.*` path that forces one partner's id before the other
and belongs to neither person's subtree. The couple's shared life is the
household's; each partner's own attributes stay on their own cards.

## Writing questions — the highest-leverage skill

Since questions are the schema, most retrieval quality is decided here:

1. **Write the questions you would actually ask, verbatim**, in the language
   you would ask them in. Answers may be in any language; the questions are
   what gets embedded.
2. **Include the subject's name variants** — full name, first name, nickname,
   handle — as bare questions on the identity card. A name dropped mid-
   conversation should resolve to its person.
3. **One question per distinct angle** the answer covers. A story about a
   farm trip that also contains a funny mishap gets a question for the trip
   *and* a question for the mishap.
4. **Cards get who/what questions; stories get event questions with the year
   in them** ("Cleo's CV from 2019"), which makes the dated nature visible at
   retrieval time.
5. `questions: []` means *never retrieved by similarity* — reserved for
   meta/header entries addressed only by exact path.

## Shields and privacy

The `shield` field (see the Q&A shields design) hides an entry from the LLM
until that exact shield name is unlocked on the Settings page:

- **One short alias per subject, chosen once.** Shield names are dotted
  `alias.topic` strings (`ada.health`, `cleo.expartner`, `maplest.cellar`).
  The alias may be shorter than the path's person id; what matters is that
  each subject has exactly one alias applied consistently, so the Settings
  checklist shows one cluster per subject. Renaming a shield later means
  editing entries *and* re-unlocking, so pick it once.
- **Shield records about others by default.** Every `human.<other-person>.*`
  and `household.*` entry describes someone who has not opted in; a
  per-subject shield keeps each person's set locked unless deliberately
  unlocked — the mechanism for letting several people share one instance with
  separate private sets.
- **One sensitivity policy, applied uniformly**: health, religion, finances,
  past relationships, intimate topics, and anything about minors are
  shield-by-default, whoever the subject is.
- **Unlocking is exact-match** — unlocking `ada` does not unlock
  `ada.health`; the dots only cluster the checklist. A more sensitive topic
  gets a deeper shield name, unlockable on its own.
- **A shield is a retrieval filter, not encryption.** A shielded entry still
  exists in Postgres and in backups; it is only kept out of the LLM's prompt.

## Adopting the shape

Deliberately cheap — the existing narratives are already valid stories:

1. **Add identity cards** for every person mentioned anywhere in the file,
   including those who today exist only inside someone else's story. This is
   the single highest-value step and is one short line per person.
2. **Add relations cards** for the core family.
3. **Leave the narratives alone.** Rename paths opportunistically toward
   topic-first-year and `household.<id>.*` — renames are cosmetic for
   similarity retrieval, so no big-bang migration is needed; do them when
   touching an entry anyway. Retire `human.family.<a>.<b>.*` compound paths
   as their stories move under the household.
4. **When a story's facts go stale, don't edit the story** — the card carries
   the current truth.
5. **Apply the shield policy**, then press **Repopulate Q&A memory** (editing
   `shield` fields or any entry text requires a repopulate; toggling a lock in
   Settings does not).

## The trade-off

Two layers mean the LLM can retrieve a story whose facts have since changed.
Three things keep that safe: the card's name-variant questions make it likely
the card is retrieved alongside the story; stories carry their date in path
and text, so the LLM can see which account is older; and the convention is
explicit that cards state current truth. The alternative — keeping every
entry perpetually current — is a maintenance contract no hand-edited file
survives; stale-but-dated beats silently-wrong.
