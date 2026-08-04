# Chat persona picker — design

## Problem

`/persona` stores who the assistant could be, and nothing reads it. The
assistant's prompt still describes only *how to work* — one step at a time,
pick an action, cite sources — so every room gets the same voice.

This feature connects the two: a room links to a persona, and the assistant's
per-turn prompt carries that persona's text. The link is made from the `/chat`
right-side panel.

## Scope

The persona applies to **the assistant, in agents rooms** — any room whose
`room_type` is not `'direct'`. Direct rooms are not touched: they already have
a complete system-prompt mechanism (free text or a linked `/prompt` version,
`docs/direct-chat.md`), and a third source there would only raise "which one
wins". The other chat responders (router, query,
tool_demo, …) carry their own prompts and are not what "who is the assistant"
means.

## Binding: which persona, and which version of it

Two nullable columns on `chatroom`, both plain UUID columns with no FK — the
same shape as the existing `prompt_uuid`:

```
chatroom
  persona_uuid           -- which persona; null = none, the assistant has no persona block
  persona_revision_uuid  -- null = follow newest (the default); set = pinned to that revision
```

The default is **follow newest**: picking a persona sets `persona_uuid` and
leaves `persona_revision_uuid` null, so editing the persona on `/persona`
reaches the room's next reply with no re-linking. That is what the stable
persona uuid was for.

**Pinning** is the opt-out: set `persona_revision_uuid` and the room speaks
with that exact text until released, no matter how the persona is edited
afterwards. A pinned revision must belong to the linked persona — a room can
never pin to another persona's history.

### Resolution, fresh every turn

`db.resolve_room_persona(room) -> PersonaResolution` returns the text and the
revision uuid that produced it:

| Room state | Text | Stamped revision |
|---|---|---|
| `persona_uuid` null | `""` — no block | none |
| Pinned, revision exists | that revision's `content` | the pinned uuid |
| Following | the persona's `content` (newest by the invariant) | the newest revision's uuid |
| Persona deleted | `""` — no block | none |
| Persona has no revisions yet (created, never saved) | `""` — no block | none |

A **deleted persona sends no block**, rather than stale text — the same
fail-obvious choice `resolve_room_system_prompt` already makes for a deleted
linked prompt. The room visibly behaves as though it has no persona instead of
quietly using text the operator thought they replaced.

Deleting a persona cascades its revisions, so a pinned revision cannot outlive
its persona; the pinned room simply falls to "no block".

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
  the exact version behind it. A following room and a pinned room are equally
  traceable; the difference is only whether the pointer moves.

## The picker

The `/chat` right sidebar has four modes — `members` / `stats` / `settings` /
`export`. `Settings` is currently direct-room-only: `effectiveSidebarMode()`
maps `settings → members` in an agents room, and `renderDirectSettings` renders
a "doesn't apply here" note. The mode is already in the dropdown and already
does nothing useful in exactly the rooms the assistant lives in.

So: stop mapping `settings → members` for agents rooms, and give the Settings
panel an agents-room branch. The direct-room panel is unchanged.

**Agents-room Settings panel:**

- **Persona** — the linked persona's name, linking to `/persona?id=<uuid>`, or
  *"(none — the assistant has no persona)"*. A deleted persona renders in red
  as *"(deleted)"*, matching the deleted-linked-prompt rendering, because the
  room genuinely has no persona at that point.
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
persona that was pinned.

## HTTP API

`PUT /chat/api/rooms/<uuid>/settings` today rejects any non-direct room with
400 "settings apply to direct rooms only". It gains two fields:

- **`persona_uuid`** — accepted for **agents rooms only**; validated to name a
  real persona. Setting it clears `persona_revision_uuid` (follow newest).
  `null` unlinks and clears both.
- **`persona_revision_uuid`** — accepted for agents rooms only; validated to
  name a revision **of the linked persona**. `null` releases the pin.

A direct room sending either field is a 400 — direct rooms use prompt linking.
An agents room may now call the endpoint at all, which is the change that makes
the panel possible; it still rejects the direct-only fields (`model_uuid`,
`system_prompt`, `prompt_uuid`, `request_timeout`).

## Testing

- **`db/test_chat_*`** — resolution: unset, following, pinned, deleted persona,
  persona with no revisions, and a pinned revision belonging to another persona
  (rejected at write time, so resolution never sees it). A settings write that
  doesn't mention the persona columns leaves them alone.
- **`webapp/test_chat_*`** — the settings PUT accepts a persona on an agents
  room; 400 on an unknown persona uuid; 400 on a revision that isn't the linked
  persona's; 400 on a direct room; picking a persona clears an existing pin.
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
- **Direct rooms untouched.** They have their own mechanism; a second source
  would create exactly the ambiguity `/prompt`'s design already warns about.
- **A deleted persona sends nothing.** Fail-obvious over fail-soft.

## Open questions

- **Usage back-references.** `/persona` still has no "what links this?" view,
  so deleting a persona silently leaves rooms personaless. This design keeps
  that behavior (the room visibly loses its voice) rather than building
  back-references now, but the gap stops being theoretical once rooms link
  personas — the delete modal warning is the natural next step.
- **Non-chat assistant work.** A cron-fired assistant run posts into a room,
  so it inherits that room's persona. Whether a background run *should* speak
  in character is unexamined; today it will.
