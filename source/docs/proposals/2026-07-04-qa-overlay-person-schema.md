# Q&A overlay: person / household / relation schema

An authoring convention for the operator's private Q&A overlay
(`<customize.dir>/question_answer.jsonl`, which overlays the base registry).
It applies to entries about **people** — individuals, their relationships, and
the households they belong to.

All names in this document are fictional placeholders (`ada`, `ben`, `cleo`, …).
Substitute your own; never commit real personal data.

## Entry schema

Every entry is one JSON line with the same fields as the base registry:

```json
{"id": "<uuid>", "path": "human.ada.identity", "kind": "static",
 "questions": ["Who is Ada?"], "answer": "…", "shield": "ada"}
```

- `id` — stable uuid; an overlay entry overrides a base entry with the same id.
- `path` — dotted label and clustering key; conventions below.
- `kind` — `"static"` for all person entries.
- `questions` — the retrieval surface (question-embedding match). Every
  retrievable entry needs at least one; `questions: []` is reserved for
  meta/header entries that must never match.
- `answer` — free prose, any length, any language. Questions are typically
  English (what gets embedded and asked); answers can be in the operator's
  native language.
- `shield` — optional; see below.

Retrieval is question-embedding plus exact-path lookup, so the path does not
*need* to carry structure. The family graph belongs in the answers, where it is
readable, and in per-person paths, where it clusters.

## Why

The overlay grows organically, and person data accumulates in shapes that all
describe the same individual in several places at once:

- **Compound-couple namespaces** — `human.family.<a>.<b>.*` holding a couple's
  shared life (travel, events, clubs, anniversaries). Family is a graph
  (parent, child, sibling, partner — many-to-many), but a path is a tree:
  nesting one partner's id before the other is arbitrary, and generations
  (grandparent → parent → child) cannot be expressed by nesting at all.
- **Single-person blobs** — `human.family.<person>` entries describing one
  relative inside a "family" namespace, sometimes *alongside* that person's own
  `human.<person>.*` records, restating the same attributes in both places.
- **People trapped in prose** — partners, children, and grandchildren who exist
  only inside another person's answer text, with no record of their own to
  anchor a question about them.
- **Roles with no anchor** — the same person is "youngest child" in one blob
  and "parent" in another; no single statement of who they are.

One real-world change then means editing several entries, which drift out of
sync. The fix is one home per fact, anchored on the person it belongs to.

## Model: three entity types

| Entity | Namespace | Holds | Rule |
|---|---|---|---|
| **Person** | `human.<personid>.*` | one individual's own attributes, as a topical subtree | one subtree per person; no attribute restated elsewhere |
| **Household** | `household.<id>.*` | who lives together, and shared life | references people by id; never re-describes them |
| **Relation edges** | `human.<personid>.relations` and `human.<personid>.<reltype>.<otherid>` | that person's links | anchored on the subject |

`human.` sits alongside the overlay's other top-level namespaces: `identity.*`
(the bot's own persona), `location.*` (physical places), `project.*`,
`social.*`, `_meta.*`.

**Person id** = lowercase concatenated full name (`adaquist`), so ids are
unambiguous when two people share a first name. Placeholder examples below use
short forms for readability.

## Person subtree

Two anchor leaves per person, plus any number of topical leaves. Everyone
referenced anywhere gets at least a stub `identity` — including people who
would otherwise exist only inside someone else's answer.

```json
{"path": "human.ada.identity",
 "questions": ["Who is Ada?", "Ada's age and occupation"],
 "answer": "Ada Q. (b. 1950-02-14), retired nurse."}

{"path": "human.ada.relations",
 "questions": ["Ada's family", "Who are Ada's children and partner?"],
 "answer": "Partner: Ben. Children: Cleo (1979), Dov (1990). Mother: Edith (deceased). Household: maplest."}
```

Topical leaves hold everything else, one topic per leaf, nested as deep as the
topic warrants:

```
human.cleo.health.allergy.grass
human.cleo.food.pizza
human.cleo.job.overview
human.cleo.job.cv.2019
human.cleo.project.birdhouse
human.cleo.unit.datetime
human.cleo.values
human.cleo.education.elementaryschool
```

This is the shape person data naturally takes — dozens of leaves per
well-documented person is normal and good: each leaf is a separate retrieval
target with its own questions.

### Relationship leaves

Beyond the compact `relations` edge list, a relationship with its own story
gets its own leaf, named by relation type then the *other* person's id:

```json
{"path": "human.cleo.friend.ines",
 "questions": ["Cleo's friend Ines", "Cleo and Ines as coworkers"],
 "answer": "Met at Acme in 2005; co-authored the birdhouse project. Ines lives in Aarhus, two kids."}
```

`friend.<otherid>`, `exfriend.<otherid>`, `expartner.<otherid>` — the narrative
is anchored on the subject, and the other person's own attributes still live in
*their* subtree. Past relationships are shield-by-default (see below).

## Household entity

A household is the social unit; `location.<placeid>.*` remains the physical
place (the building, garden structures, rooms). When the unit is defined by an
address, the household may reuse the place id; the two namespaces still hold
different facts.

Membership by reference plus the facts that belong to the unit rather than to
any one member — gatherings, trips, clubs, shared routines:

```json
{"path": "household.maplest.roster",
 "questions": ["Who lives at the Maple St household?"],
 "answer": "Members: Ada (mother), Ben (father), Cleo (eldest child). Place: location.maplest."}

{"path": "household.maplest.events",
 "questions": ["Gatherings at the Maple St household"],
 "answer": "Hosts the summer gathering; grandchildren visit weekly."}

{"path": "household.maplest.travel.abroad",
 "questions": ["Trips Ada and Ben take abroad"],
 "answer": "Frequent travellers: several countries a year, plus folk-high-school stays."}
```

This is where couple-level and home-level facts live, instead of a compound
`human.family.<a>.<b>.*` path that forces one partner's id before the other and
detaches the facts from both people's subtrees.

## Conventions

1. **One fact, one home.** A birth date lives on `human.<person>.identity`,
   never inside a household or another person's entry. If a fact keeps getting
   restated (an address, a shared hobby), that is the signal it belongs on a
   household leaf, referenced from elsewhere by id.
2. **Roles are relative statements anchored per person** ("oldest child of Ada
   & Ben"), so a person's role never contradicts itself across records.
3. **Lifespan on identity**: `(b. 1914 d. 1991)` for deceased people.
4. **Dated snapshots get a year suffix, topic first.** A CV, a reflection, a
   one-off event: `job.cv.2019`, `future.reflection.2020`,
   `health.toe.2019` — never year-first (`2019.jobchange`), which scatters the
   person's timeline instead of clustering it under the topic. Facts that are
   currently true but age (living situation, being single, a plan) carry an
   explicit `as of <date>` in the answer, so staleness is visible.
5. **Every entry needs a question or it is unreachable.** Similarity retrieval
   only sees `questions`; an entry with an empty list exists solely for humans
   reading the file.
6. **One shield alias per subject, used consistently.** See below.

## Shields and privacy

The `shield` field (see the Q&A shields design) hides an entry from the LLM
until the operator unlocks that exact shield name on the Settings page. It
composes with this schema:

- **One short alias per subject, forever.** Shield names are dotted
  `alias.topic` strings (`ada.health`, `cleo.expartner`, `maplest.cellar`).
  The alias may be shorter than the path's person id — the Settings checklist
  clusters shields by dotted prefix, so what matters is that each subject has
  exactly one alias, applied to every shielded entry about them, never varied.
  Renaming a shield later means editing entries *and* re-unlocking, so pick the
  alias once.
- **Shield records about others by default.** Every `human.<other-person>.*`
  and `household.*` entry describes people who have not opted in; a per-subject
  shield keeps each person's set locked unless deliberately unlocked — the
  mechanism for letting several people share one instance while keeping
  separate private sets.
- **Apply one sensitivity policy uniformly.** The shield-by-default categories:
  health, religion, finances, past relationships, intimate topics, private
  spots at a place, and any data about minors. Decide the policy once and apply
  it to every matching entry rather than ad hoc.
- **Unlocking is exact-match.** Unlocking `ada` does not unlock `ada.health`;
  the dots only cluster the checklist. A more sensitive topic therefore gets a
  deeper shield name, unlockable on its own.
- **A shield is a retrieval filter, not encryption.** A shielded entry still
  exists in Postgres and in backups; it is only kept out of the LLM's prompt.
  "How much to store about other people" is a separate decision that a shield
  does not resolve.

## Moving existing data to this shape

1. **Inventory every person**, including those who exist only in prose (a
   sibling's partner, the grandchildren). Create one `human.<person>.identity`
   per person — a stub with name, birth year, and one questions line is enough.
2. **Normalize the identity leaf name.** Some subtrees use `name`, others
   `identity`; converge on `identity`.
3. **Merge duplicates.** Where a person has both `human.<person>.*` leaves and
   a `human.family.<person>` blob, fold the blob's facts into the person's own
   subtree, deduplicating restated attributes.
4. **Add `relations` leaves**, moving each relationship statement onto the
   subject's side; long relationship narratives become `friend.<otherid>`-style
   leaves.
5. **Hoist couple- and home-level facts** from `human.family.<a>.<b>.*` into
   `household.<id>.*`, referencing members by id. Year-stamped shared events
   keep their year suffix (`household.<id>.travel.2026.farmstay`).
6. **Retire the compound paths** once their facts have homes.
7. **Add shields per the sensitivity policy**, then press **Repopulate Q&A
   memory** — editing `shield` fields in the file requires a repopulate;
   toggling a lock in Settings does not.

## One trade-off

Per-person `relations` states each edge from both ends (Ada lists Cleo; Cleo
lists Ada). That is mild duplication, but each edge is small, and it makes
retrieval robust: a question about *either* person returns the link. The
alternative — a single `household.<id>` tree as the one source of truth —
removes the duplication but surfaces a relationship only when that one document
is retrieved. For a question-answering system, the small per-person duplication
is the better trade: recall matters more than strict normalization here.
