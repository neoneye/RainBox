# Q&A overlay: person / household / relation schema

A proposed authoring convention for the operator's private Q&A overlay
(`<customize.dir>/question_answer.jsonl`, which overlays the base registry).
It applies to entries about **people** — individuals, their relationships, and
the households they belong to.

All names in this document are fictional placeholders (`ada`, `ben`, `cleo`, …).
Substitute your own; never commit real personal data.

## Why

When people are modelled as *household records* — one entry per family unit that
inlines each member with their attributes and a role — the same individual is
described in several places at once (as a child in one record, a parent in
another, plus their own standalone entries). Three problems follow:

- **Duplication.** A person's birth date / occupation is restated in every
  record that mentions them. One real-world change means editing several
  entries, which drift out of sync.
- **Roles have no anchor.** The same person is "youngest child" in one record
  and "parent" in another; there is no single statement of who they are relative
  to a fixed reference.
- **Structure is trapped in the path.** Family is a graph (parent, child,
  sibling, partner — many-to-many), but a path is a tree. Nesting one partner
  under another, or a person under a couple, is an arbitrary choice, and
  generations (grandparent → parent → child) cannot be expressed by nesting at
  all — they survive only in prose.

Retrieval here is question-embedding plus exact-path lookup, so the path does not
*need* to carry structure. It is a label and a clustering key. The family graph
belongs in the answers, where it is readable and traversable.

## Model: three entity types

| Entity | Namespace | Holds | Rule |
|---|---|---|---|
| **Person** | `human.<personid>.*` | one individual's own attributes | one record per person; no attribute restated elsewhere |
| **Household / place** | `household.<id>.*` | who lives together, and shared life | references people by id; never re-describes them |
| **Relation edges** | `human.<personid>.relations` | that person's links | short, anchored on the subject |

`human.` earns its place as an entity-type prefix once non-humans use `bot.<id>`.

## Person atoms

One `identity` leaf and one `relations` leaf per person. Everyone referenced
anywhere gets their own record — including people who would otherwise exist only
inside a household blob.

```json
{"path": "human.ada.identity",
 "questions": ["Who is Ada?", "Ada's age and occupation"],
 "answer": "Ada Q. (b. 1950-02-14), retired nurse."}

{"path": "human.ada.relations",
 "questions": ["Ada's family", "Who are Ada's children and partner?"],
 "answer": "Partner: Ben. Children: Cleo (1979), Dov (1990). Mother: Edith (deceased). Household: maplest."}

{"path": "human.cleo.identity",
 "questions": ["Who is Cleo?"],
 "answer": "Cleo Q. (b. 1979-05-26), UX designer."}

{"path": "human.cleo.relations",
 "questions": ["Cleo's family", "Cleo's parents, siblings, children"],
 "answer": "Oldest child of Ada & Ben. Sibling: Dov. Partner: Faye. Children: Gil (2018), Hana (2022). Household: oakave."}
```

## Household / place entity

Membership by reference plus the facts that belong to the unit rather than to any
one member (gatherings, trips, shared communities). It never restates a person's
own attributes.

```json
{"path": "household.maplest.roster",
 "questions": ["Who lives at the Maple St household?"],
 "answer": "Members: Ada (mother), Ben (father), Cleo (eldest child). Address: Maple St 24."}

{"path": "household.maplest.events",
 "questions": ["Gatherings at the Maple St household"],
 "answer": "Hosts the summer gathering; grandchildren visit often."}

{"path": "household.maplest.travel",
 "questions": ["Trips the Maple St household takes"],
 "answer": "Regular domestic trips; occasional stays at a countryside rental."}
```

This is where couple-level and home-level facts live, instead of being nested
under a compound `human.<a>.<b>.*` path that forces one partner beneath the other.

## Conventions

1. **One fact, one home.** A birth date lives on `human.<person>.identity`, never
   inside a household or another person's entry.
2. **Roles are relative statements anchored per person** ("oldest child of Ada &
   Ben"), so a person's role never contradicts itself across records.
3. **Lifespan on identity**: `(b. 1914 d. 1991)` for deceased people.
4. **`as of <date>` for anything that ages.** Stable facts (birth dates) need no
   date; changing ones (current living situation, a plan, a CV, a prediction)
   carry an explicit date in the answer, or a `date` field, so staleness is
   visible.
5. **Person id = shield id.** Use the same token for `human.<personid>.*` and any
   `shield` on that person's entries (see below), so the two cluster together.

## Shields and privacy

The `shield` field (see the Q&A shields design) hides an entry from the LLM until
the operator unlocks that exact shield name on the Settings page. It composes
with this schema:

- **Align the shield to the subject**: `shield: "cleo"` or `shield: "cleo.health"`
  on Cleo's entries. Because the Settings checklist clusters shields by dotted
  prefix, aligning shield ids with person ids yields one unlock cluster per
  person.
- **Shield records about others by default.** Every `human.<other-person>.*` and
  `household.*` entry describes people who have not opted in; a per-subject shield
  (`shield: "ben"`, `shield: "maplest"`) keeps each person's set locked unless
  deliberately unlocked — the mechanism for letting several people share one
  instance while keeping separate private sets.
- **Apply one sensitivity policy uniformly.** Health, finances, past
  relationships, and any data about minors are the obvious shield-by-default
  categories. Decide the policy once and apply it to every matching entry rather
  than ad hoc.
- **A shield is a retrieval filter, not encryption.** A shielded entry still
  exists in Postgres and in backups; it is only kept out of the LLM's prompt.
  "How much to store about other people" is a separate decision that a shield
  does not resolve.

## Moving existing data to this shape

1. Enumerate every distinct person across all current entries; create one
   `human.<person>.identity` for each, deduplicating restated attributes.
2. Move each relationship into the subject's `human.<person>.relations` as a short
   edge list.
3. Hoist household- and couple-level facts into `household.<id>.*`, referencing
   members by id.
4. Remove the compound-couple paths and the duplicated household blobs.
5. Add shields per the sensitivity policy, then press **Repopulate Q&A memory**
   (editing shields requires a repopulate; toggling a lock in Settings does not).

## One trade-off

Per-person `relations` states each edge from both ends (Ada lists Cleo; Cleo
lists Ada). That is mild duplication, but each edge is small, and it makes
retrieval robust: a question about *either* person returns the link. The
alternative — a single `household.<id>` tree as the one source of truth — removes
the duplication but surfaces a relationship only when that one document is
retrieved. For a question-answering system, the small per-person duplication is
the better trade: recall matters more than strict normalization here.
