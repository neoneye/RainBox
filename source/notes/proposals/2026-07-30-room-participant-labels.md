# Room participant labels — telling speakers apart without names or uuids

**Status:** Proposed. Nothing built yet.
**Date:** 2026-07-30

## The problem

A room can hold more than one human. Today the prompt cannot express that.

Two separate renderers both collapse distinct speakers into one:

- **The IRC transcript** (`chat/transcript.py`, used by the unstructured
  chat, router, query, tool and MCP agents) renders `<sender_name> text`.
  The seed creates one human named `user`, and nothing defines what a
  second one would be called — the only name-free default available is
  the same string, so two humans collapse into two lines from `<user>`.
- **The assistant's XML** (`agents/assistant.py._append_prompt_message`)
  renders `<message role="user">` and nothing else. `_message_role`
  returns `user` for every human and `assistant` for every agent, so a
  room with two humans and three agents renders as two roles. The
  multi-user proposal (`2026-07-15-multi-user-oauth-and-shared-knowledge.md`)
  states that "the assistant's `conversation_history` already render
  per-sender". That is true of the IRC path's intent and false of the XML
  path: **the assistant currently cannot tell its participants apart at
  all**, including its fellow agents.

So the model cannot answer "who asked this?", cannot attribute a stated
preference to the person who stated it, and cannot address one participant
without guessing. In a one-human room none of that matters, which is why it
has not hurt yet.

Two constraints shape the fix:

1. **No personal names.** Names are not wanted in the transcript. This is
   not a privacy floor — the profile blocks and recalled memory carry the
   person's real identity deliberately, and should keep doing so. It is
   about the transcript specifically, where a name is a label and a
   generic label does the same job.
2. **No raw uuids as labels.** A uuid in an IRC line is unreadable, costs
   real tokens on every message, and gives the model nothing to reason
   with — it cannot tell `3f2a…` from `9c81…` at a glance any better than
   it can tell two identical `<user>` lines apart.

## The idea

Synthesize a stable per-room label for each human, and map the labels to
uuids once, in a side block.

```
Participants: user, user_2, benny

Chat history, oldest first:
[2026-07-30 09:14] <user> can you book the room for thursday?
[2026-07-30 09:15] <user_2> make it friday, thursday is the offsite
[2026-07-30 09:15] <benny> friday it is — which room?

Current message:
[2026-07-30 09:16] <user> friday works
```

The label is the readable handle; the uuid stays available for anything
that needs to be exact.

## Ordinal 1 is always `user`

Labels are assigned by order of first message in the room. Ordinal 1
renders as `user`; the rest as `user_2`, `user_3`, and so on.

Not `user_1`. The asymmetry is deliberate and is the property that makes
this safe to ship:

- A one-human room renders **byte-identical to today**, forever. No eval
  churn, no prompt-shape test rewrites, no risk to the common case.
- A second human joining does not relabel the first. A long room's history
  stays stable as people arrive.

### Why not label relative to who is asking

The tempting alternative is to make the current speaker always `user` and
number the others around them. It gives every turn the familiar shape.

It also makes the transcript lie. The same person would be `user` in this
turn's history and `user_2` in the next one, so any statement the model
attributes to a label is wrong as soon as the speaker changes — which is
exactly the situation the labels exist for. It would also break prompt
prefix caching on every speaker change.

Room-stable labels plus an explicit pointer to who is asking gets both
properties. The pointer is cheap; a rewritten history is not.

### Derived, not stored

Labels are computed from the room's message history, not persisted.

A `chatroom_member.label_ordinal` column would need a backfill, a
uniqueness constraint per room, and a decision about what happens when a
member is removed and re-added — all for a value that is a rendering
concern with one obvious source of truth already in the database.

The failure mode of deriving: hard-deleting a room's earliest messages can
shift ordinals. Messages are soft-handled everywhere that matters and the
result is a relabel, not corruption, so this is an acceptable trade. If it
ever bites, promoting to a stored ordinal is a contained change behind the
same helper.

## Agents keep their names

Agents render as `benny`, `assistant`, `router` — unchanged.

An agent's config name is already a stable, non-personal, readable label:
precisely what this proposal synthesizes for humans. Replacing it with a
uuid would spend tokens to destroy the one thing the scheme is trying to
create. The no-names constraint is about a *person's* name, and an agent
does not have one.

Their uuids still appear in the participants block, so anything needing an
exact reference has it.

## What gets built

### 1. The label map

```python
# chat/transcript.py

def participant_labels(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Map each human sender_uuid to its stable transcript label.

    Ordinal 1 is the room's first human to speak and renders as `user`, so
    a one-human room is unchanged and stays unchanged when someone else
    joins. Later humans are `user_2`, `user_3`, … by first message.

    Agents are absent by design: their config names are already the kind
    of label this synthesizes.
    """
    labels: dict[str, str] = {}
    for m in messages:
        if m.get("sender_type") != "human":
            continue
        uuid = str(m.get("sender_uuid") or "")
        if not uuid or uuid in labels:
            continue
        labels[uuid] = "user" if not labels else f"user_{len(labels) + 1}"
    return labels
```

Callers pass the room's history, which is what `db.list_room_messages`
returns; `format_history` trims to `context_limit` after labels are
assigned, so a label never shifts as the window slides.

A message dict without `sender_uuid`/`sender_type` yields no label and
falls through to `sender_name`, so the existing hand-built fixtures in
`chat/test_transcript.py` keep passing untouched.

### 2. The IRC transcript

`_format_irc_line` prefers the label and falls back to `sender_name`,
which keeps agents working with no special case:

```python
def _format_irc_line(m: dict[str, Any], labels: dict[str, str]) -> str:
    sender = labels.get(str(m.get("sender_uuid") or "")) or _one_line_text(
        m.get("sender_name") or "unknown")
    ...
```

`format_history` computes the map once and emits a `Participants:` header
**only when the room has more than one human**. One-human rooms get no
header and no behavioural change.

### 3. The assistant's XML

One `speaker` attribute covers both collapses:

```python
<conversation_history_xml>
  <message role="user" speaker="user" timestamp="2026-07-30 09:14">…</message>
  <message role="user" speaker="user_2" timestamp="2026-07-30 09:15">…</message>
  <message role="assistant" speaker="benny" timestamp="2026-07-30 09:15">…</message>
</conversation_history_xml>
```

`role` keeps its current meaning — the training-distribution signal for
whose turn it is. `speaker` carries identity within a role. They are
different questions and should stay different attributes.

**Emit `speaker` for a role only when that role has more than one distinct
speaker in the room.** A room with one human and one agent produces the
prompt it produces today, unchanged. The structure appears exactly when
there is ambiguity to resolve, and never as a tax on the common case.

### 4. Who is asking

```xml
<current_user_request speaker="user_2">make it friday</current_user_request>
```

Same condition: only when more than one human is present.

### 5. The mapping block

```xml
<room_participants authority="context">
  <participant label="user" kind="human" uuid="…"/>
  <participant label="user_2" kind="human" uuid="…"/>
  <participant label="benny" kind="agent" uuid="…"/>
</room_participants>
```

Emitted under the same condition as the attributes. `authority="context"`
because it is reference data, matching `user_profile` and
`knowledge_calibration`.

### 6. One system-prompt sentence

The identity blocks describe the requester, not the room. Without a rule,
a model holding one `user_settings_json` and two speakers will apply one
person's units and language to the other person's message.

> `user_settings_json`, `user_profile` and `knowledge_calibration`
> describe the participant named in `current_user_request`'s `speaker`
> attribute. They say nothing about the other participants; do not apply
> them to another participant's message.

## A hardening side effect

Labels are synthesized by code from message order. They are never taken
from a display name, so a participant cannot name themselves `user_2` and
have the model attribute someone else's statements to them. Today's
transcript would render such a name verbatim.

This is closed by construction rather than by validation, which is the
right way to close it.

## Non-goals

- **Not the identity model.** Accounts, OAuth, and per-person memory
  spaces are `2026-07-15-multi-user-oauth-and-shared-knowledge.md`. This
  is the rendering layer above whatever identity model lands: it needs
  only a stable `sender_uuid`, which exists today.
- **Not per-participant profiles.** Injecting a settings block per human
  multiplies the identity blocks by the participant count and has no
  demonstrated payoff. The requester's blocks plus the rule in §6 is the
  v1.
- **Not memory attribution.** `memory_claim.subject` is free text, not a
  uuid reference, so "user_2 prefers metric" is not expressible today.
  Scoped memory is the spaces work.
- **Not direct rooms.** `room_type='direct'` is 1:1 and untouched.

## Build order

1. `participant_labels` + tests in `chat/transcript.py`, unused.
2. Wire the IRC path; assert a one-human room's transcript is unchanged.
3. Wire the XML path: `speaker`, `<room_participants>`, the
   `current_user_request` attribute, all behind the >1-speaker condition.
   Assert the existing prompt-shape tests still pass untouched — that is
   the real gate.
4. The system-prompt sentence.
5. An eval with a two-human room: does the model attribute a preference
   to the participant who stated it, and reply to the one who asked?

Steps 1–3 are mechanical and safe. Step 5 is the one that decides whether
the labels are worth their tokens, and it is worth running before the
sentence in step 4 is tuned.

## Open questions

- **Ordinal 1: first speaker or room creator?** First speaker needs no
  extra data and matches "the room's primary human" in practice. Creator
  is more principled and needs `chatroom.created_by`. First speaker is
  proposed; the helper is the only thing that would change.
- **Does the model use the labels at all?** Small local models may ignore
  a `speaker` attribute the way they ignored formatting preferences until
  the guide became imperative. If so, the IRC path's inline `<user_2>`
  may outperform the XML attribute, and the XML may need the label inline
  in the text rather than in metadata. Step 5 answers this.
- **Label churn on account linkage.** When `chat_user.account_uuid`
  arrives and one person has two chat_user rows (web + bridge), they will
  take two labels. Collapsing by `account_uuid` when it exists is the
  obvious fix and belongs with that work, not here.
