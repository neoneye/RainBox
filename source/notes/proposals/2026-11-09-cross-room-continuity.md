# Cross-room continuity: what the operator was doing elsewhere

**Status:** Proposal. Nothing here is built.
**Date:** 2026-11-09
**Related:**
- [The room digest](2026-11-09-room-digest.md): topics, statuses and the
  current topic, per room. This proposal lets them cross rooms.
- [Noticing a repeated request](2026-11-09-noticing-a-repeated-request.md):
  repeat detection is per room; this is where crossing rooms is decided.
- `notes/memory-architecture.md`: memory claims, which already cross rooms
  through `scope = 'global'`.

## The problem

This series started in a room where the assistant was asked about its own
amnesia. Its answer located the cause correctly: each room is a silo. A new
room starts with nothing. A topic worked on yesterday in another room, even
one left contested, is invisible.

Memory claims cross rooms when they are `global`, but only what was
explicitly remembered, and only as facts. The *shape* of the operator's work
does not cross at all: what they were in the middle of, what keeps failing,
what was settled.

The room digest gives each room its topics and statuses. This proposal
decides when those may be seen from another room, and how they are
connected.

## The boundary comes first

**A room is an audience.** A profile switch is not an audience boundary, but
a room is. Some rooms are the operator's alone. Others are family rooms,
bridged to Telegram or Discord, with real people's messages in them.
Membership counts cannot tell these apart. Today every assistant room has
exactly one human member in `chatroom_member`, yet a bridged room carries
other people's words through its connector.

So nothing crosses rooms by inference. Two per-room settings, both **off by
default**, decide it:

| setting | meaning |
|---|---|
| `continuity.share` | this room's topics and statuses may be shown in other rooms |
| `continuity.receive` | this room may show topics from rooms that share |

**Eligibility.** A room can enable either setting only when:
- its human members are exactly the operator;
- it has no `bridge_binding`;
- it is not a direct room with another person.

A room that gains a bridge binding or another human member loses
eligibility at once. The check runs at render time as well as at setting
time. What it shared before is not shown again, though nothing is deleted.

**What crosses** is the room digest's structure: topic slugs and titles,
statuses with their basis, last-seen times, and the room's name. **Summary
text never crosses**, and neither do messages, replies or excerpts. A topic's
title is model-written, so it is screened like a summary: no answer content,
no value, and no name from a source.

### 1. Topics are linked across rooms, not merged

Two rooms may each have their own `letter-count`. Whether they are the same
thing is a decision, recorded once:

```
topic_link  uuid, topic_a, topic_b, source ('detected'|'operator'), probability,
            model, created_at, state ('linked'|'not_linked')
```

- **When a link is considered.** When a sharing room creates a topic, the
  `repeat_confirm` slot asks one Noul against each candidate in other
  sharing rooms: "Do these two topic titles name the same subject?" The
  candidates are the 20 most recently active topics there, with at least one
  shared slug token or a title-overlap score.
- **What gets recorded.** At ≥ 0.8 a `linked` row is written; anything else
  writes nothing.
- **The operator decides in the end.** Links are confirmed or rejected with
  detector labels, and an operator row outranks a detected one.

A link never merges topics or moves exchanges. Each room's digest stays its
own record.

### 2. What a receiving room shows

A receiving room's `room_digest` gains an `elsewhere` section:

```xml
<elsewhere>
  <topic slug="letter-count" room="assistant sandbox" status="contested"
         last="yesterday" basis="operator said wrong" linked_to="letter-count"/>
  <topic slug="kanban-status" room="planning" status="quiet" last="3 days ago"/>
</elsewhere>
```

- **Up to 6 entries.** First the topics linked to the current topic, then
  topics that are `contested` or `open` in sharing rooms within 7 days, then
  the most recent others. The section is at most 600 characters.
- **One rule for the decide instructions:** a topic shown elsewhere was
  discussed in another room, and nothing from that room's content is here.
  To use it, ask the operator, or read memory. A contested topic elsewhere is
  a reason to verify, as it is here.
- **The payoff.** In a new room, "count the R's in Strawberry" is assigned
  `letter-count` at turn start. It is linked to the sandbox room's contested
  `letter-count`, so the loop learns that an answer to this was rejected
  yesterday, without seeing that answer.

### 3. Repeats across rooms: recorded, not shown

Exact repeats across sharing rooms are detected the way they are within a
room, and recorded only. Showing another room's attempt means showing its
request text, which is content. The topic link already carries the useful
part, "this was contested", without it. Whether a request's text may cross
is left to the operator's verdicts on what was recorded.

### 4. Memory suggestions, never memory writes

A topic that is linked across three or more sharing rooms, or one that is
`settled` by the operator, becomes a *suggestion* on /memory: "this keeps
coming up; remember something about it?". It links to the topics. The
operator writes the claim, or doesn't. Nothing is promoted to memory
automatically. A topic is a subject of conversation, not a fact.

## Measuring it

- **Link precision**, from labels. The 20-candidate shortlist is checked for
  misses with `missed` labels.
- **The payoff case.** A scripted pair of rooms: a contested topic in one,
  and the same request in the other a day later. The test is whether the
  reply verifies instead of answering from scratch.
- **Boundary tests.** Each must show nothing:
  - a bridged room;
  - a room with a second human member;
  - a room that gains a bridge after sharing;
  - a title that fails the content screen.

## Rollout

Rollout comes after the room digest's topics are recording:

1. The settings and the eligibility check.
2. Topic links, recording only.
3. The `elsewhere` section, behind `assistant.cross_room` (`off` / `record`
   / `on`).
4. Memory suggestions.

## What this does not do

- **Cross an audience boundary.** Bridged and shared rooms are never
  eligible.
- **Carry content.** Only topics, statuses and room names cross.
- **Replace memory.** Facts that should follow the operator everywhere
  belong in `global` memory claims, written deliberately.
