# Chat persona picker — design

## Problem

`/persona` stores who the assistant could be, and nothing reads it. The
assistant's prompt still describes only *how to work* — one step at a time,
pick an action, cite sources — so every room gets the same voice.

This feature connects the two: the assistant's membership in a room links to a
persona, and its per-turn prompt carries that persona's text. The link is made
from the `/chat` right-side panel.

## Scope

The persona applies to **the assistant, in agents rooms** — any room whose
`room_type` is not `'direct'`. Direct rooms are not touched: they already have
a complete system-prompt mechanism (free text or a linked `/prompt` version,
`docs/direct-chat.md`), and a third source there would only raise "which one
wins". The other chat responders (router, query,
tool_demo, …) carry their own prompts and are not what "who is the assistant"
means.

## Binding: which participant, which persona, which version of it

The binding lives on **`chatroom_member`**, not on the room. That table is
already exactly `(room_uuid, user_uuid)` unique — "this participant, in this
room" — so a persona there reads as *this participant's voice in this room*:

```
chatroom_member
  persona_uuid           -- which persona this member speaks with; null = none
  persona_revision_uuid  -- null = follow newest (the default); set = pinned to that revision
```

**Why the membership row rather than the room.** With one assistant the two are
indistinguishable. With more than one — a math assistant and a physics
assistant in the same room — a room-level column is simply wrong: two
participants, two voices, one column. The membership row is also where the
eventual "answers unprompted" vs "answers when named" distinction belongs, so
both properties end up on the same row instead of a room column and a member
flag that have to agree.

Only **persona-capable** members can carry one. Today that is the assistant
(`PERSONA_CAPABLE_UUIDS`, a tuple in `webapp/chat_api.py` mirroring
`CHAT_RESPONDER_UUIDS`); the other responders — router, query, tool_demo — carry
their own prompts and are not what "who is the assistant" means. A second
assistant identity later is one entry added to that tuple, not a schema change.

The default is **follow newest**: picking a persona sets `persona_uuid` and
leaves `persona_revision_uuid` null, so editing the persona on `/persona`
reaches that member's next reply with no re-linking. That is what the stable
persona uuid was for.

**Pinning** is the opt-out: set `persona_revision_uuid` and the member speaks
with that exact text until released, no matter how the persona is edited
afterwards. A pinned revision must belong to the linked persona — a member can
never pin to another persona's history.

### Resolution, fresh every turn

`db.resolve_member_persona(room_uuid, user_uuid) -> PersonaResolution` returns
the text and the revision uuid that produced it. The assistant calls it with
its own `agent_uuid`, so a room with several assistants resolves each one
independently with no further plumbing.

| Member state | Text | Stamped revision |
|---|---|---|
| No membership row, or `persona_uuid` null | `""` — no block | none |
| Pinned, revision exists | that revision's `content` | the pinned uuid |
| Following | the persona's `content` (newest by the invariant) | the newest revision's uuid |
| Persona deleted | `""` — no block | none |
| Persona has no revisions yet (created, never saved) | `""` — no block | none |

A **deleted persona sends no block**, rather than stale text — the same
fail-obvious choice `resolve_room_system_prompt` already makes for a deleted
linked prompt. The member visibly behaves as though it has no persona instead
of quietly using text the operator thought they had replaced.

Deleting a persona cascades its revisions, so a pinned revision cannot outlive
its persona; the pinned member simply falls to "no block".


## Prompt insertion

The assistant's per-turn prompt is an XML `<assistant_turn>` document built in
`_build_user_prompt` (`agents/assistant.py`). Its sections come from declared
blocks held as instance attributes — `_identity_block`, `_formatting_block`,
`_calibration_block`, `_profile_block`, `_skill_block`. The persona becomes one
more of exactly that shape: `self._persona_block`, rendered as a `<persona>`
section.

**Why the user prompt and not `_system_prompt()`:** the system prompt is static
for a run, while the persona is per-room and known per turn; the trace already
captures the user prompt on every step row, so what the model was told is
visible without new plumbing; and it keeps the persona in the same tier as the
other operator-owned context blocks rather than above the working rules.

**Precedence.** `<persona>` ranks in `<source_priority>` next to
`formatting_guide`: below `current_user_request`, the acceptance criteria and
this turn's fresh observations; above `conversation_history_xml`. One
code-owned sentence in the system prompt states the boundary:

> A persona changes voice and manner. It never changes which actions are
> available, never overrides the working rules or the source priority, and is
> never a reason to withhold an answer, skip a read, or invent detail.

The persona text is operator-authored, but it is still data inside the prompt,
and the code-owned rules outrank it.

## Traceability

Every step already records `system_prompt` and `user_prompt`, so the persona
text that produced a reply is captured verbatim. This feature adds the
*pointer*:

- **The step's turn log** gains a `persona` entry — the persona's name, its
  uuid, and `href: /persona?id=<uuid>` — beside the existing `profile`,
  `formatting_guide` and `knowledge_calibration` entries.
- The stamped **revision uuid** rides along, so a months-old reply resolves to
  the exact version behind it. A following member and a pinned one are equally
  traceable; the difference is only whether the pointer moves.

## The picker

The `/chat` right sidebar has four modes — `members` / `stats` / `settings` /
`export`. `Settings` is currently direct-room-only: `effectiveSidebarMode()`
maps `settings → members` in an agents room, and `renderDirectSettings` renders
a "doesn't apply here" note. The mode is already in the dropdown and already
does nothing useful in exactly the rooms the assistant lives in.

So: stop mapping `settings → members` for agents rooms, and give the Settings
panel an agents-room branch. The direct-room panel is unchanged.

**Agents-room Settings panel.** One section per persona-capable member, from
`GET /chat/api/rooms/<uuid>/personas` — today that is a single section for the
assistant, and a room with several assistants renders one each with no further
UI work. Each section carries:

- **Persona** — the linked persona's name, linking to `/persona?id=<uuid>`, or
  *"(none — this assistant has no persona)"*. A deleted persona renders in red
  as *"(deleted)"*, matching the deleted-linked-prompt rendering, because the
  member genuinely has no persona at that point.
- **Choose persona…** — a modal rendering the persona folder tree read-only
  from `GET /persona/api/tree`; clicking one links it. Mirrors the existing
  "Choose stored prompt…" flow: same modal shape, same read-only tree, a
  different endpoint.
- **Version** — either *"following newest"* or *"pinned to &lt;saved-at&gt;"*,
  with **Pin to a version…** (lists that persona's revisions, newest first,
  from `GET /persona/api/personas/<uuid>/revisions`) and **Follow newest** to
  release the pin.
- **Unlink** — clears both columns back to none.

Picking a persona always starts in follow-newest, including when replacing a
persona that was pinned. A room with no persona-capable member shows a short
note instead of a picker.

## HTTP API

Two new endpoints, member-addressed. `PUT /chat/api/rooms/<uuid>/settings` is
**not** touched: it stays direct-room-only, and the persona mechanism does not
overload it.

**`GET /chat/api/rooms/<room_uuid>/personas`** → one row per persona-capable
member of the room, so the sidebar renders without a second request:

```json
{"members": [{"user_uuid": "…", "name": "assistant",
              "persona_uuid": "…", "persona_name": "Alice",
              "persona_exists": true,
              "persona_revision_uuid": null,
              "persona_revision_saved_at": null,
              "persona_following": true}]}
```

A room with no persona-capable member returns an empty list — which is what the
sidebar shows for a room the assistant isn't in.

**`PUT /chat/api/rooms/<room_uuid>/members/<user_uuid>/persona`**
`{persona_uuid, persona_revision_uuid}` → the same row shape for that member.

- `persona_uuid` — validated to name a real persona. Setting it clears
  `persona_revision_uuid` (follow newest). `null` unlinks and clears both.
- `persona_revision_uuid` — validated to name a revision **of the persona that
  member will have after this call**. `null` releases the pin. Pinning with no
  linked persona is a 400.
- A member that is not persona-capable, or not in the room, is a 404.

Addressing the member from the start is what keeps multi-assistant additive: a
second assistant needs no endpoint change, just another row in the response.


## Testing

- **`db/test_chat_*`** — resolution: unset, following, pinned, deleted persona,
  persona with no revisions, and a non-member. Two members of one room resolve
  independently — the property the per-member binding exists for.

  Ownership of a pin is validated at the **API layer** (`persona_revision_get`,
  below), matching how `prompt_uuid` is already validated: the endpoint checks,
  the DB writer does not. `resolve_member_persona` is fail-safe regardless — its
  pin lookup is scoped to the linked persona, so a mismatched pin written by
  some other path resolves to no block rather than leaking another persona's
  text.
- **`webapp/test_chat_*`** — the member persona PUT accepts a persona on an
  agents-room member; 400 on an unknown persona uuid; 400 on a revision that
  isn't the linked persona's; 400 on a direct room; 404 on a member that
  cannot carry a persona or isn't in the room; picking a persona clears an
  existing pin. The personas GET reports each persona-capable member's state,
  empty for a room with none.
- **`agents/test_assistant_*`** — `<persona>` appears in the built prompt when
  the room links one and is absent when it doesn't; the pinned room gets the
  pinned text, not the newest; the turn log carries the persona entry and the
  stamped revision. Driven through the existing fake seams — no live model.
- **Marker tests** for the sidebar panel and its two modals.
- **A browser pass**: link a persona, confirm the sidebar and a live reply
  reflect it, pin to an older version, release, unlink, and delete the linked
  persona to see the room fall back to no block.

## Deliberate tradeoffs

- **Follow-newest by default, pinning as an opt-out.** Tuning a persona should
  show up in the next reply; that is what the stable uuid bought. Pinning
  exists for when you want a room frozen, and it is visible in the sidebar so a
  room that stopped following is never a mystery.
- **The persona sits in the user prompt, not the system prompt.** Same tier as
  the other operator-owned context, visible in the existing per-step trace.
- **Style tier, not instruction tier.** A persona that could override the
  working rules would be a prompt-injection surface with the operator's own
  text as the vector. It ranks with `formatting_guide` and the system prompt
  says so.
- **The binding is per membership, not per room.** One assistant makes the two
  identical; more than one makes the room-level column wrong. Choosing the
  membership row now costs nothing and makes a multi-assistant room an additive
  change — another row in the same response — rather than a migration.
- **Direct rooms untouched.** They have their own mechanism; a second source
  would create exactly the ambiguity `/prompt`'s design already warns about.
- **A deleted persona sends nothing.** Fail-obvious over fail-soft.

## Open questions

- **Usage back-references.** `/persona` still has no "what links this?" view,
  so deleting a persona silently leaves rooms personaless. This design keeps
  that behavior (the room visibly loses its voice) rather than building
  back-references now, but the gap stops being theoretical once rooms link
  personas — the delete modal warning is the natural next step.
- **Non-chat assistant work.** A cron-fired assistant run posts into a room, so
  it inherits that member's persona. Whether a background run *should* speak in
  character is unexamined; today it will.
- **Several assistants in one room.** The schema and the endpoints are shaped
  for it — a math assistant and a physics assistant would be two persona-capable
  members with their own personas. What is *not* designed here: the second
  assistant identity itself, and the rule that a primary answers unprompted
  while extras answer only when named. That rule belongs on the same membership
  row when it is built.
