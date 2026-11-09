# Roadmap: conversation awareness

**Status:** Roadmap over proposals. Nothing on it is built except the probe.
**Date:** 2026-11-09

## Where this started

In room "assistant demo 8" the operator asked how to show the ReAct loop
more than its 30-message window: the current topic, recent topics, what was
solved, and what is still open. They imagined three lenses: a tag table, a
compact timeline, and a Fibonacci-like summary that fades with age.

A chain of proposals followed. This page orders them for building, says what
each one buys and costs, and lists what is still the operator's to decide.

## The proposals

| proposal | one line |
|---|---|
| [Noticing a repeated request](2026-11-09-noticing-a-repeated-request.md) | exact repeats, redos and repeated commands, with what each earlier attempt tried; the request profile; reply checklists as diagnostics |
| [Later verdicts on earlier messages](2026-11-09-later-verdicts-on-earlier-messages.md) | "that was wrong" / "my guess was right" as append-only records, shown on the earlier message |
| [The room digest](2026-11-09-room-digest.md) | topics, derived status, a compact timeline, and chunks of every 10 messages, counted from message 0, built from records and never invalidated; a generated phrase per chunk only if it earns its place |
| [Decision-model foundation](2026-11-09-decision-model-foundation.md) | one System One client, two slots, verified local execution, one record per call, the probe as regression suite |
| [Detector labels](2026-11-09-detector-labels.md) | one place and one UI for "that detection was wrong" or "it missed this", which every promotion gate reads |
| [Post-turn housekeeping](2026-11-09-post-turn-housekeeping.md) | one ordered queue after each turn, never during one, with a heat budget |
| [Cross-room continuity](2026-11-09-cross-room-continuity.md) | topics and statuses shown in other rooms, only between rooms the operator opts in that have no other audience |
| [The conversation working set](2026-10-17-conversation-working-set.md) (earlier) | evidence fetched by earlier turns, carried exactly, under today's access rules |
| [Acceptance-criteria cost](2026-09-16-acceptance-criteria-cost-and-removal.md) (earlier) | where the criteria call's seconds go |

The measured record behind the decision-model parts is
`benchmarks/decision_probe.py`, with its fixture (version 4) and recorded
runs in `benchmarks/fixtures/decision_probe_runs/`.

## The order

Each step is buildable and useful on its own, and each unblocks the next.
Sizes are rough: S is a day or two of focused work, M is several, and L is
more.

### Step 0. Two small foundations that need no model (S)

1. **Record `metadata_["request_message"]` on every run.** This is the
   exact link from a run to the human message it answers. Today it is only
   recoverable through the journal payload.
2. **Move the run summarizer into the `housekeeping` role**, with its
   turn-activity gate and the After the turn view on /assistant. Behaviour
   does not change otherwise.

- **Pros:**
  - Every later step needs the request link.
  - The gate fixes a contention that exists today: the summarizer's model
    call can overlap the next turn's decide call.
  - Both are low risk.
- **Cons:**
  - Nothing visible to the operator, except a cleaner /assistant.

### Step 1. The decision-model foundation, and the label table (M)

The `llm/systemone.py` client, the `repeat_check` and `repeat_confirm`
slots, the locality check, the `decision_call` table, and its row on
/assistant. Also the `detector_label` table and the right/wrong/not-sure
controls, so the first detector can be labelled from day one.

- **Pros:**
  - Built once instead of three times.
  - Labels exist before the first detector, so no recording week is wasted
    unlabelled.
- **Cons:**
  - Infrastructure with no behaviour of its own.
  - The locality check depends on a field in Ollama's response that has to
    be confirmed against a real `:cloud` model.

### Step 2. Repeat detection: the original Strawberry ask (M)

Exact repeats, the short-message Choice (redo, expand, command,
acknowledge, new request), chains, and the `repeated_request` block, behind
`record` first.

**The request profile rides in the same decision call from the start,
recording only.** Its data will be needed in step 5.

- **Pros:**
  - It is the behaviour the operator asked for first: notice the fifth
    attempt, say so, and verify instead of repeating the same route.
  - Exact matching needs no model, so it works even with the decision slots
    unbound.
  - The short-message Choice gave no wrong answer above its band in the probe
    (37 of 51 accepted).
- **Cons:**
  - The prompt and audit changes touch five consumers. That is the largest
    change to the decide prompt in this series.
  - Rephrased repeats stay recording-only until the identity experiment is
    evaluated.
  - "igen" and similar one-word redos in smaller languages are missed. They
    fail safe, with no block.

### Step 3. Later verdicts, operator first (M)

The `message_verdict` table, operator marks on messages, and the
`later_verdict` attributes in history. Then the detector for the operator's
own words ("that's wrong", "my guess was right"), recording only until
labelled. Then hedge flags.

- **Pros:**
  - It addresses the operator's own experience: realising later that they or
    the assistant were wrong.
  - Operator marks need no model and are certain.
  - Every `wrong` on a reply becomes an eval candidate.
  - It fills the `later_verdict=` field the repeat block already reserves.
- **Cons:**
  - Verdict attributes change bytes inside history, costing one partial
    prefill per verdict.
  - The detector's 14/14 on d3 is on 14 invented cases.

### Step 4. Room digest from records: current topic, topics, timeline, chunks (M)

The topic Choice joins the turn-start call, follow-ups inherit, new topics
are named after the turn, status is derived from step 3's verdicts, and the
collapsed timeline renders. Chunk nodes are written in housekeeping with no
model call, and each chunk renders its topics, exchange count and outcomes
from records. The history window starts at a stored chunk edge.

- **Pros:**
  - It delivers "where were we" (the current topic and its status) and the
    operator's tag table and linear lens.
  - It delivers the fading summary's shape as well, with no generative load.
  - Topic assignment was 14/14 on both models.
  - It costs one more question in a call that already happens.
- **Cons:**
  - Status needs step 3 to say anything stronger than `quiet`.
  - Topic naming adds a short generative call when a new topic appears.
  - Topics only begin when recording begins. Older chunks show their run
    outcomes and exact repeats from existing records, under `?`, with no
    topic names.
  - The aligned window changes which messages the history holds, so it needs
    the same `record` phase as the rest.

### Step 5. The acceptance-criteria gate (S to build, M to evaluate)

Using the request profiles recorded since step 2, replay stored turns where
`work` was `conversation` or `knowledge` and no constraint was stated,
with and without the criteria call. If replies don't get worse by the
operator's labels, skip the call on those turns.

- **Pros:**
  - The largest latency win in this series. The criteria call ran on every
    run last week, at a median of 8.7 s (p90 11.3 s), against a median turn
    of 43.6 s.
- **Cons:**
  - It changes behaviour on a share of turns, so it needs its own evaluation.
  - The 2026-09-16 cost proposal may reduce the call's cost by other means
    first. Read it before choosing.

### Step 6. Room digest, generated text: the `asked` phrase, only if it earns its place (M)

One phrase per level 1 chunk, of at most 120 characters, saying what was
asked in kind, written once by a local generative model in housekeeping.
Level 2 is never generated. The answer screen (a Noul that rejects text
stating answer content) is added to the probe and measured first. Then the
orientation replay runs three ways: no digest, step 4's record-derived
chunks, and chunks with the phrase. The phrase ships only if the third beats
the second. The housekeeping heat budget comes before this step.

- **Pros:**
  - It says what kind of thing was asked in a thread, which a topic title
    does not.
  - One generative call per 10 messages, off the request path.
  - The same job, run over a backlog, gives rooms that predate topic
    recording their only chunk content (operator decision 3, below).
- **Cons:**
  - It is the only step that adds sustained generative load, on a laptop with
    a heat history.
  - The model-written text needs the strictest access rule: verified-local
    models only.
  - The answer screen is not yet measured.
  - It may measure as no better than step 4, and then it is not built.

### Step 7. Cross-room continuity (M)

Opt-in sharing between the operator's own unbridged rooms. Topic links
recording first, then the `elsewhere` section. It carries topics and
statuses only, never generated chunk text.

- **Pros:**
  - It answers the amnesia the first room complained about: a new room
    learns that a topic was contested yesterday elsewhere.
  - With no generated text crossing rooms, nothing model-written reaches a
    room that did not produce it.
- **Cons:**
  - It depends on step 4 being trustworthy.
  - Its audience rule must be right the first time.
  - Value is limited until several rooms opt in.

### A parallel track: the conversation working set (L)

It carries exact evidence (diary citations, memory claims) from earlier
turns into the current one. It is independent of everything above, except
step 0's request link. It addresses the other half of amnesia: the facts a
turn looked up five messages ago.

- **Pros:**
  - Its value is concrete and measurable with its own omission cases.
- **Cons:**
  - It is large, and touches the same prompt and audit surfaces as step 2.

**Recommendation:** start it after step 3, so the two prompt changes are
not evaluated at the same time.

### Optional experiments, at any time after step 1, all diagnostic

- **The reply checklist** on drafts and delivered replies.
- **The identity experiment**, the screen and confirm for rephrased repeats.

These cost little, collect data, and change nothing.

## Decisions waiting for the operator

1. **Should reply-check scores reach the loop?** Astra's revision made
   `answered=` and `constraints=` diagnostic only. That reverses the earlier
   version, where the loop could see that an earlier reply missed the point.
   The current documents follow Astra. Reinstating it is a step-5 change in
   the repeat proposal, after labels exist.
2. **The digest's gradient.** It uses fixed chunks of ten, counted from
   message 0, instead of a smooth Fibonacci fade. It is coarser, and no chunk
   is ever invalidated. The operator chose the chunks of ten; this records
   that the Fibonacci shape was given up for it. Level 2 is derived from
   records, never generated from level 1 text, so no text in the digest is a
   summary of a summary.
3. **Backfill for old rooms.** Topics start when recording starts. Older
   chunks still render their run outcomes and exact repeats from existing
   records, but no topic names. The step 6 job can write `asked` text and
   display-only topic names for them (about 0.1 calls per message, level 1
   only). Topic names are the one thing with no record-derived alternative. Running it is a one-off budget decision. The
   machine takes 10 to 15 calls in a row before it needs a 60-second
   cool-down, so a long room's backlog trickles through idle time over days.
   Assigning topics to old exchanges would go against the "records are
   written when things happen" rule.
4. **Cross-room eligibility.** The proposal excludes bridged rooms and rooms
   with another human member. Should direct rooms with agents only count as
   the operator's own?
5. **Step 5 versus the earlier criteria proposal.** Gate the call per turn,
   or reduce it for every turn first?
6. **Where standing instructions live.** A rule the operator states in a room
   ("answer in Danish", "don't use tool X") needs its exact wording to stay
   enforceable. Once it leaves the 30-to-39-message window, nothing in this
   series keeps it: the digest keeps no wording by design, and the working
   set carries evidence, not instructions. The options are memory, the
   working set extended to instructions, or an operator-pinned room-rules
   block kept verbatim and never compacted. Summarizing such a rule is not
   an option: a rule rewritten in fewer words is a different rule.

## What the probes taught, for whoever builds this

- **Questions in one System One request are answered in isolation.** A
  question that must see a list needs the list in the state.
- **For intent, a Choice beats a yes/no question.** "Is *elaborate* a
  redo?" scored yes at 0.72; given an `expand` option, 0.92–0.95.
- **Never ask a classifier whether an answer is correct.** Both models
  accepted "2 R's in Strawberry" as correct at 0.84 and 0.82. Correctness
  comes from computing or reading.
- **Follow-ups have no topic of their own.** "again", "thanks" and
  "elaborate" read as new topics when judged by their words. Classify what
  they *do* first, then inherit.
- **A topic that comes back fools a bisect.** Pairwise "same topic?"
  comparison is both slow and wrong on follow-ups. Topic records give
  boundaries for free.
- **d3 is more careful, d1 is much faster.** d3 also scales badly with many
  questions in one request: 14 took 4.6 s, and 18 took 10.3 s. Use d1 on the
  request path and d3 after the turn.
- **A window aligned to chunk edges makes prefix reuse across turns
  possible.** Today's 30-message window changes its first message every
  turn. Aligned, the history only appends for about five turns. The reuse
  also needs the history ahead of the per-turn sections, which is a layout
  change to measure, not assume. Step 3's verdict attributes also change
  bytes inside that history, once per verdict, so the two are measured
  together.
- **Chunks and the window must count the same messages.** One function gives
  both their edges. `kind="progress"` rows are reaped when the reply posts,
  so they never count.
- **One process per role means post-turn jobs can overlap the next turn
  today.** That is the reason for the housekeeping gate.
- **Record before inject, everywhere.** Every detector ships recording-only,
  and is promoted on the operator's labels, never on the probe's invented
  cases.
