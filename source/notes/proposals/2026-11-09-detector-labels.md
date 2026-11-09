# Detector labels: one place for the operator to say "that detection was wrong"

**Status:** Proposal. Nothing here is built.
**Date:** 2026-11-09
**Related:**
- [The decision-model foundation](2026-11-09-decision-model-foundation.md):
  calibration and the regression suite, which both consume these labels.
- [Noticing a repeated request](2026-11-09-noticing-a-repeated-request.md),
  [Later verdicts](2026-11-09-later-verdicts-on-earlier-messages.md) and
  [The room digest](2026-11-09-room-digest.md): each records a detection the
  operator must be able to confirm or reject.

## The problem

Every proposal in this series ships its detectors in recording mode first,
and promotes them only on the operator's verdicts. They are the ground
truth: Rainbox runs on the operator's own conversations precisely so they
can judge. But each proposal invents its own way to collect that judgement:

| proposal | what the operator labels | its own storage |
|---|---|---|
| repeat | "this match is correct / incorrect / uncertain"; "this was a missed repeat" | "store feedback separately", unspecified |
| verdicts | a rejected detection becomes a `cleared` verdict row | inside `message_verdict` |
| digest | reassign a topic; mark a topic open or settled | `exchange_topic` rows with source `operator`, `topic_mark` |
| request profile, reply checks | nothing yet | none |
| existing | thumbs up / down on a reply | `feedback_event` |

Built separately, this means five tables, five UI patterns, and no single
query for "how often is detector X wrong". The calibration report and the
regression suite both need that query.

## The distinction that keeps it simple

There are two different kinds of operator statement, and they belong in
different places.

1. **A statement about the conversation.** "That answer was wrong." "This
   topic is settled." "This exchange belongs to letter-count." These are
   *domain records*. They change what later turns see, so they stay in their
   features' own tables (`message_verdict`, `topic_mark`, `exchange_topic`).
   Each feature's precedence rules already rank operator rows first.
2. **A statement about a detector.** "The repeat detector was wrong to match
   these two." "The verdict detector missed this correction." "The request
   profile's `work` label was wrong here." These are *labels*. They change no
   prompt. They measure the machinery.

Most operator actions are both. Rejecting a detected verdict writes a
`cleared` verdict (domain) *and* a label saying the detection was wrong
(meta). The UI does both in one click. The two records answer different
questions and are kept apart.

## The design

### 1. One table

```
detector_label
  uuid, created_at, created_by_uuid
  detector          'repeat.exact' | 'repeat.short_message' | 'repeat.identity'
                    | 'profile.work' | 'profile.has_constraints' | 'profile.topic'
                    | 'reply_check.answers' | 'reply_check.constraints'
                    | 'verdict.choice' | 'verdict.hedge' | 'verdict.same_answer'
                    | 'digest.answer_screen' | ...
  subject_kind      'decision_call' | 'repeat_record' | 'message' | 'digest_node'
  subject_id        the uuid of what was detected (or, for a miss, where)
  label             'correct' | 'incorrect' | 'uncertain' | 'missed'
  expected          the answer the operator says was right, when they give one
                    (an option name, yes/no, a message uuid)
  note              free text, optional
  detector_version  question-set and bands version at the time of the detection
```

It is append-only. A changed mind is a new row, and the newest row by
`created_by_uuid` for a `(detector, subject_id)` wins.

**`missed`** is the label with no detection behind it: "this was a repeat,
and nothing fired". It is what makes recall measurable at all. Positive rows
alone only measure precision.

### 2. One UI pattern, on the row where the detection happened

Every detector already gets a code-driven row on /assistant, at the point in
the turn where it ran. Each such row gets the same three controls: **right**,
**wrong**, **not sure**. A **wrong** opens a small modal (`notes/ui-modals.md`).
The modal asks for the correct answer, offering the detector's options, for
example the five short-message options or the room's topics. It also takes
an optional note.

**Missed detections** are flagged from the place they should have shown up:
- **On any run's header:** "a repeat was missed here", "a correction was
  missed here".
- **On any message in /chat** (kebab menu): "this corrects an earlier
  message", which also opens the verdict flow.

**One click, both records.** Where the label implies a domain change, the
same action writes both rows and says so in the modal. "Wrong topic" plus a
chosen topic writes the label and the `exchange_topic` operator row.
"Wrong verdict" writes the label and a `cleared` verdict. Nothing is ever
changed silently.

### 3. What reads the labels

- **The calibration report** (foundation, decision 6). It takes each
  question's recorded probabilities against its labels: per detector, per
  model digest, per question version.
- **The promotion gates** in each proposal. "Precision inside the band of at
  least N on at least M labels" becomes a query, not an impression.
- **The regression suite.** A label of `incorrect` or `missed` is offered
  as a probe case. The operator rewrites it with invented text before it
  enters the fixture, and the original never leaves the database.
- **The eval set.** `incorrect` labels on reply checks or verdicts become
  eval candidates through the existing `promote_feedback_to_eval_case`
  path.

### 4. The existing thumbs

`feedback_event` stays as it is. Its up/down votes are a reply-quality
signal, not a detector label. The verdicts proposal reads them as
`operator_vote`. They are not migrated, and nothing here changes their UI.

## Measuring it

- **Coverage:** the labels per detector per week. A detector with no labels
  cannot be promoted, and the report shows it as such rather than as
  "precise".
- **Effort:** the clicks per label. The modal must stay one click for
  right/not-sure and two for wrong. If labelling is a chore, it stops
  happening, and every promotion gate stalls.

## Rollout

1. The table and the three controls on the detector rows of whichever
   detector ships first. In the roadmap's order that is repeat detection.
2. `missed` flags on run headers and in the /chat kebab menu.
3. The calibration report.
4. Probe-case and eval-case offers from labels.

## What this does not do

- **Replace domain records.** A topic mark or a verdict stays in its
  feature's table, where its feature's rules read it.
- **Collect labels automatically.** An operator's later correction in chat
  is a *verdict* on a message, detected by the verdicts feature. It becomes
  a label only if the operator confirms the detection.
