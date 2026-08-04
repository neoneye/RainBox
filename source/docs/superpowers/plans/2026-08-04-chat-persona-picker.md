# Chat persona picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agents room link to a persona so the assistant's per-turn prompt carries that persona's text, chosen from the `/chat` right sidebar.

**Architecture:** Two nullable columns on `chatroom` (which persona, and optionally which revision to pin to). A resolver reads the text fresh every turn and reports which revision produced it. The assistant renders it as one more declared block — a `<persona>` section in the per-turn XML prompt, ranked with `formatting_guide`. The picker reuses the sidebar's existing `Settings` mode, which today is a dead end in agents rooms.

**Tech Stack:** Python 3 + Flask + Flask-SQLAlchemy (Postgres), vanilla JS inline in `webapp/chat_template.py`, `pytest`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-04-chat-persona-picker-design.md`. Read it before Task 1.
- The persona applies to **the assistant in agents rooms** — any room whose `room_type` is not `'direct'`. Direct rooms are untouched.
- **Default is follow-newest.** `persona_revision_uuid` null = follow; set = pinned. Picking a persona always clears an existing pin.
- **A deleted persona sends no block** — never stale text. Same for a persona that has never been saved (no revisions).
- A pinned revision must belong to the linked persona.
- The persona ranks with `formatting_guide` in `<source_priority>`: it changes voice, never which actions exist, never the working rules, never whether to answer.
- **`webapp/chat_template.py` is a non-raw Python string.** A `\n` written inside its inline JS is interpreted by Python and silently breaks the script; marker tests do not catch it. Write `\\n` (see `docs/chat-frontend-rules.md`).
- `chatroom` is an existing table: `db.create_all()` does **not** add columns to it. New columns need `_add_column_if_missing` in `db/__init__.py`.
- Tests run against `rainbox_claude` automatically (`source/conftest.py`). Never point anything at `rainbox_production`.
- Working directory for every command is `/Users/neoneye/git/rainbox/source`. Run tests with `./venv/bin/python -m pytest <path> -v`.
- Docs describe current state — no change history, no migration notes.

## Reference implementations (keep open)

- `db/chat.py` → `resolve_room_system_prompt` (line ~214) and `set_chatroom_settings` (line ~183) — the two functions this feature extends.
- `webapp/chat_api.py` → `chat_room_settings` (line ~530) — the endpoint, including how `prompt_uuid` is validated.
- `agents/assistant.py` → the `_skill_block` lifecycle: declared at ~2829, assigned at ~2964, reset at ~3684, rendered at ~3866. The persona block copies it exactly.
- `webapp/chat_template.py` → `effectiveSidebarMode` (~2064), `renderSidebar` (~2093), `renderDirectSettings` (~2110) and its "Choose stored prompt…" modal.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `db/models.py` | Two columns on `Chatroom` | Modify |
| `db/__init__.py` | `_add_column_if_missing` for both columns | Modify |
| `db/chat.py` | `PersonaResolution`, `resolve_room_persona`, persona fields in `set_chatroom_settings` | Modify |
| `db/persona.py` | `persona_revision_get(persona_uuid, revision_uuid)` for ownership validation | Modify |
| `db/test_chat_persona.py` | Resolution + settings-write tests | Create |
| `webapp/chat_api.py` | Persona fields on the settings endpoint; persona info in its response | Modify |
| `webapp/test_chat_persona_api.py` | Endpoint tests | Create |
| `agents/assistant.py` | `_persona_block`, the `<persona>` section, source-priority ranks, turn-log entry | Modify |
| `agents/test_assistant_persona.py` | Prompt-shape + turn-log tests | Create |
| `webapp/chat_template.py` | Agents-room Settings panel, persona + version pickers | Modify |
| `webapp/test_chat_views.py` | Marker tests for the panel | Modify |
| `docs/persona-design.md`, `docs/direct-chat.md` | Current-state docs | Modify |

---

## Task 1: Columns + resolution

**Files:**
- Modify: `db/models.py` (the `Chatroom` class), `db/__init__.py` (`init_db`), `db/chat.py`
- Test: `db/test_chat_persona.py` (create)

**Interfaces:**
- Produces:
  - `Chatroom.persona_uuid: UUID | None`, `Chatroom.persona_revision_uuid: UUID | None`
  - `db.PersonaResolution` — frozen dataclass `(text: str, revision_uuid: UUID | None, persona_uuid: UUID | None, name: str)`
  - `db.resolve_room_persona(room: Chatroom) -> PersonaResolution`

- [ ] **Step 1: Write the failing tests**

Create `db/test_chat_persona.py`:

```python
"""Tests for the room→persona binding: which persona a room speaks with, and
which revision of it produced the text (db.resolve_room_persona)."""
from uuid import uuid4

import pytest

import db


@pytest.fixture
def app_ctx():
    app = db.make_app()
    db.init_db(app)
    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def room(app_ctx):
    # create_chatroom(name, created_by, member_uuids, room_type="agents")
    human = db.get_human_user()
    r = db.create_chatroom(f"persona-test-{uuid4().hex[:8]}", human.uuid, [])
    try:
        yield r
    finally:
        db.delete_chatroom(r.uuid)


@pytest.fixture
def persona(app_ctx):
    p = db.persona_create(f"P-{uuid4().hex[:8]}", None)
    try:
        yield p
    finally:
        db.persona_delete(__import__("uuid").UUID(p["uuid"]))


def test_no_persona_linked_resolves_to_nothing(room):
    out = db.resolve_room_persona(room)
    assert out.text == "" and out.revision_uuid is None and out.persona_uuid is None


def test_following_resolves_to_newest_and_stamps_it(room, persona):
    from uuid import UUID
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "first")
    db.persona_update_content(pu, "second")
    db.set_chatroom_settings(room.uuid, persona_uuid=pu)
    out = db.resolve_room_persona(db.get_chatroom(room.uuid))
    newest = db.persona_revisions(pu)[0]
    assert out.text == "second"
    assert str(out.revision_uuid) == newest["uuid"]
    assert out.name == persona["name"]


def test_pinned_resolves_to_that_revision_not_the_newest(room, persona):
    from uuid import UUID
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "old text")
    oldest = UUID(db.persona_revisions(pu)[0]["uuid"])
    db.persona_update_content(pu, "new text")
    db.set_chatroom_settings(room.uuid, persona_uuid=pu, persona_revision_uuid=oldest)
    out = db.resolve_room_persona(db.get_chatroom(room.uuid))
    assert out.text == "old text"
    assert out.revision_uuid == oldest


def test_persona_never_saved_resolves_to_nothing(room, persona):
    from uuid import UUID
    db.set_chatroom_settings(room.uuid, persona_uuid=UUID(persona["uuid"]))
    out = db.resolve_room_persona(db.get_chatroom(room.uuid))
    assert out.text == "" and out.revision_uuid is None


def test_deleted_persona_resolves_to_nothing_not_stale_text(room):
    from uuid import UUID
    p = db.persona_create("Doomed", None)
    pu = UUID(p["uuid"])
    db.persona_update_content(pu, "text that must not survive deletion")
    db.set_chatroom_settings(room.uuid, persona_uuid=pu)
    db.persona_delete(pu)
    out = db.resolve_room_persona(db.get_chatroom(room.uuid))
    assert out.text == "" and out.revision_uuid is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest db/test_chat_persona.py -v`
Expected: FAIL — `TypeError: set_chatroom_settings() got an unexpected keyword argument 'persona_uuid'`.

- [ ] **Step 3: Add the columns**

In `db/models.py`, inside `class Chatroom`, immediately after the `prompt_uuid` column:

```python
    # Which persona the assistant speaks with in this room (persona.uuid, the
    # /persona page); null = none, and the turn carries no persona block.
    # Plain col, no FK — a deleted persona resolves to no block, not stale text.
    persona_uuid: Mapped[UUID | None] = mapped_column(default=None)
    # Null = follow the persona's newest revision (the default). Set = pinned
    # to that exact revision, so editing the persona no longer reaches this
    # room until the pin is released.
    persona_revision_uuid: Mapped[UUID | None] = mapped_column(default=None)
```

In `db/__init__.py`, beside the other `chatroom` column guards (near
`_add_column_if_missing("chatroom", "prompt_uuid", "prompt_uuid UUID")`):

```python
        _add_column_if_missing("chatroom", "persona_uuid", "persona_uuid UUID")
        _add_column_if_missing("chatroom", "persona_revision_uuid",
                               "persona_revision_uuid UUID")
```

- [ ] **Step 4: Add the resolver**

In `db/chat.py`, add `from dataclasses import dataclass` to the imports and
`Persona, PersonaRevision` to the `db.models` import, then add immediately
after `resolve_room_system_prompt`:

```python
@dataclass(frozen=True)
class PersonaResolution:
    """What a room's persona resolves to for one turn: the text to inject and
    the revision that produced it. `text` empty means the turn carries no
    persona block at all — no persona linked, the persona was deleted, or it
    has never been saved."""

    text: str
    revision_uuid: UUID | None
    persona_uuid: UUID | None
    name: str


_NO_PERSONA = PersonaResolution(text="", revision_uuid=None,
                                persona_uuid=None, name="")


def resolve_room_persona(room: Chatroom) -> PersonaResolution:
    """The persona text a turn in `room` actually sends, read fresh so an edit
    on /persona reaches the next reply with no re-linking.

    Pinned rooms (persona_revision_uuid set) get that exact revision and stop
    following edits. Following rooms get the persona's current content, which
    is the newest revision by the table's invariant; the newest revision's
    uuid is stamped so the turn records what it used either way.

    A deleted persona, a pin whose revision is gone, and a persona that was
    never saved all resolve to no block — fail-obvious, matching
    resolve_room_system_prompt: the room visibly has no voice rather than
    quietly using text the operator thought they had replaced."""
    if room.persona_uuid is None:
        return _NO_PERSONA
    persona = db.session.execute(
        sa.select(Persona).where(Persona.uuid == room.persona_uuid)
    ).scalar_one_or_none()
    if persona is None:
        return _NO_PERSONA
    if room.persona_revision_uuid is not None:
        pinned = db.session.execute(
            sa.select(PersonaRevision).where(
                PersonaRevision.uuid == room.persona_revision_uuid,
                PersonaRevision.persona_uuid == persona.uuid)
        ).scalar_one_or_none()
        if pinned is None:
            return _NO_PERSONA
        return PersonaResolution(text=pinned.content, revision_uuid=pinned.uuid,
                                 persona_uuid=persona.uuid, name=persona.name)
    newest = db.session.execute(
        sa.select(PersonaRevision)
        .where(PersonaRevision.persona_uuid == persona.uuid)
        .order_by(PersonaRevision.id.desc()).limit(1)
    ).scalar_one_or_none()
    if newest is None or not persona.content:
        return _NO_PERSONA
    return PersonaResolution(text=persona.content, revision_uuid=newest.uuid,
                             persona_uuid=persona.uuid, name=persona.name)
```

- [ ] **Step 5: Accept the persona fields in `set_chatroom_settings`**

Replace `set_chatroom_settings` in `db/chat.py` with:

```python
def set_chatroom_settings(
    room_uuid: UUID,
    *,
    system_prompt: str = _UNSET,
    model_uuid: UUID | None = _UNSET,
    prompt_uuid: UUID | None = _UNSET,
    request_timeout: int | None = _UNSET,
    persona_uuid: UUID | None = _UNSET,
    persona_revision_uuid: UUID | None = _UNSET,
) -> Chatroom:
    """Update a room's settings; only the fields passed are changed.

    Direct rooms own the prompt/model fields (model_uuid=None clears the model;
    prompt_uuid=None unlinks the stored prompt so the free-text system_prompt
    applies again; request_timeout=None falls back to the model config's).
    Agents rooms own the persona fields (persona_uuid=None unlinks and releases
    any pin; persona_revision_uuid=None releases the pin so the room follows
    the newest revision again). Passing a field to the wrong room type raises
    ValueError — the two mechanisms are deliberately separate.

    Setting persona_uuid clears persona_revision_uuid unless a pin is passed in
    the same call: picking a persona starts in follow-newest, including when it
    replaces a persona that was pinned.

    Applied mid-conversation: the next turn reads the room row fresh. Raises
    LookupError if the room is gone."""
    room = get_chatroom(room_uuid)
    if room is None:
        raise LookupError(f"chatroom {room_uuid} not found")
    direct_fields = (("system_prompt", system_prompt), ("model_uuid", model_uuid),
                     ("prompt_uuid", prompt_uuid),
                     ("request_timeout", request_timeout))
    persona_fields = (("persona_uuid", persona_uuid),
                      ("persona_revision_uuid", persona_revision_uuid))
    if room.room_type == "direct":
        for name, value in persona_fields:
            if value is not _UNSET:
                raise ValueError(f"{name} applies to agents rooms only")
    else:
        for name, value in direct_fields:
            if value is not _UNSET:
                raise ValueError(f"{name} applies to direct rooms only")
    if system_prompt is not _UNSET:
        room.system_prompt = system_prompt
    if model_uuid is not _UNSET:
        room.model_uuid = model_uuid
    if prompt_uuid is not _UNSET:
        room.prompt_uuid = prompt_uuid
    if request_timeout is not _UNSET:
        room.request_timeout = request_timeout
    if persona_uuid is not _UNSET:
        room.persona_uuid = persona_uuid
        if persona_revision_uuid is _UNSET:
            room.persona_revision_uuid = None
    if persona_revision_uuid is not _UNSET:
        room.persona_revision_uuid = persona_revision_uuid
    db.session.commit()
    return room
```

- [ ] **Step 6: Run the tests**

Run: `./venv/bin/python -m pytest db/test_chat_persona.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Confirm nothing else regressed**

Run: `./venv/bin/python -m pytest db/ -q`
Expected: all pass — in particular `db/test_chat_direct.py`, which exercises the direct-room half of `set_chatroom_settings`.

- [ ] **Step 8: Commit**

```bash
git add source/db/models.py source/db/__init__.py source/db/chat.py source/db/test_chat_persona.py
git commit -m "feat: room-to-persona binding with optional revision pin"
```

---

## Task 2: Ownership validation + pin-clearing tests

**Files:**
- Modify: `db/persona.py` (append), `db/test_chat_persona.py` (append)

**Interfaces:**
- Consumes: Task 1's columns and `set_chatroom_settings`.
- Produces: `db.persona_revision_get(persona_uuid: UUID, revision_uuid: UUID) -> dict | None` — the `_revision_dict` shape (`{uuid, created_at, bytes, lines, preview, current}`), or `None` when the revision does not exist **or belongs to a different persona**.

- [ ] **Step 1: Write the failing tests**

Append to `db/test_chat_persona.py`:

```python
def test_revision_get_rejects_a_foreign_revision(app_ctx):
    from uuid import UUID
    a = db.persona_create("Owner A", None)
    b = db.persona_create("Owner B", None)
    au, bu = UUID(a["uuid"]), UUID(b["uuid"])
    try:
        db.persona_update_content(au, "a text")
        rev = UUID(db.persona_revisions(au)[0]["uuid"])
        assert db.persona_revision_get(au, rev) is not None
        assert db.persona_revision_get(bu, rev) is None
    finally:
        db.persona_delete(au)
        db.persona_delete(bu)


def test_picking_a_persona_clears_an_existing_pin(room, persona):
    from uuid import UUID
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "one")
    pinned = UUID(db.persona_revisions(pu)[0]["uuid"])
    db.set_chatroom_settings(room.uuid, persona_uuid=pu, persona_revision_uuid=pinned)
    assert db.get_chatroom(room.uuid).persona_revision_uuid == pinned
    # Picking a persona again (even the same one) starts in follow-newest.
    db.set_chatroom_settings(room.uuid, persona_uuid=pu)
    assert db.get_chatroom(room.uuid).persona_revision_uuid is None


def test_unlinking_clears_both_columns(room, persona):
    from uuid import UUID
    pu = UUID(persona["uuid"])
    db.persona_update_content(pu, "one")
    db.set_chatroom_settings(room.uuid, persona_uuid=pu)
    db.set_chatroom_settings(room.uuid, persona_uuid=None)
    r = db.get_chatroom(room.uuid)
    assert r.persona_uuid is None and r.persona_revision_uuid is None


def test_persona_fields_are_rejected_on_a_direct_room(app_ctx, persona):
    from uuid import UUID
    r = db.create_chatroom(f"direct-{uuid4().hex[:8]}", db.get_human_user().uuid,
                           [], room_type="direct")
    try:
        with pytest.raises(ValueError, match="agents rooms only"):
            db.set_chatroom_settings(r.uuid, persona_uuid=UUID(persona["uuid"]))
    finally:
        db.delete_chatroom(r.uuid)


def test_direct_fields_are_rejected_on_an_agents_room(room):
    with pytest.raises(ValueError, match="direct rooms only"):
        db.set_chatroom_settings(room.uuid, system_prompt="nope")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest db/test_chat_persona.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'persona_revision_get'`.

- [ ] **Step 3: Add the accessor**

Append to `db/persona.py`, next to `persona_revisions`:

```python
def persona_revision_get(persona_uuid: UUID,
                         revision_uuid: UUID) -> dict[str, Any] | None:
    """One revision of one persona, for validating a pin before it is stored.
    None when the revision does not exist OR belongs to a different persona —
    a room can never pin to another persona's history."""
    rev = _revision_row(persona_uuid, revision_uuid)
    if rev is None:
        return None
    rows = _revision_rows(persona_uuid)
    return _revision_dict(rev, current=bool(rows) and rows[0].uuid == rev.uuid)
```

- [ ] **Step 4: Run the tests**

Run: `./venv/bin/python -m pytest db/test_chat_persona.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add source/db/persona.py source/db/test_chat_persona.py
git commit -m "feat: validate a pinned revision belongs to its persona"
```

---

## Task 3: The settings endpoint

**Files:**
- Modify: `webapp/chat_api.py` (`chat_room_settings`)
- Test: `webapp/test_chat_persona_api.py` (create)

**Interfaces:**
- Consumes: `db.set_chatroom_settings`, `db.persona_get`, `db.persona_revision_get`, `db.resolve_room_persona`.
- Produces: `PUT /chat/api/rooms/<uuid>/settings` accepting `persona_uuid` and `persona_revision_uuid` on agents rooms. The GET/PUT response gains: `persona_uuid`, `persona_name`, `persona_exists`, `persona_revision_uuid`, `persona_revision_saved_at`, `persona_following` (bool).

- [ ] **Step 1: Write the failing tests**

Create `webapp/test_chat_persona_api.py`:

```python
"""Tests for the persona half of /chat/api/rooms/<uuid>/settings."""
from uuid import uuid4

import pytest

import db
from webapp.core import app


@pytest.fixture
def ctx():
    a = db.make_app()
    db.init_db(a)
    c = a.app_context()
    c.push()
    try:
        yield
    finally:
        c.pop()


@pytest.fixture
def room(ctx):
    human = db.get_human_user()
    r = db.create_chatroom(f"api-persona-{uuid4().hex[:8]}", human.uuid, [])
    try:
        yield r
    finally:
        db.delete_chatroom(r.uuid)


@pytest.fixture
def persona(ctx):
    from uuid import UUID
    p = db.persona_create(f"ApiP-{uuid4().hex[:8]}", None)
    db.persona_update_content(UUID(p["uuid"]), "voice one")
    try:
        yield p
    finally:
        db.persona_delete(UUID(p["uuid"]))


def test_put_links_a_persona_and_reports_following(room, persona):
    c = app.test_client()
    resp = c.put(f"/chat/api/rooms/{room.uuid}/settings",
                 json={"persona_uuid": persona["uuid"]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["persona_uuid"] == persona["uuid"]
    assert body["persona_name"] == persona["name"]
    assert body["persona_exists"] is True
    assert body["persona_following"] is True
    assert body["persona_revision_uuid"] is None


def test_put_pins_a_revision(room, persona):
    from uuid import UUID
    c = app.test_client()
    rev = db.persona_revisions(UUID(persona["uuid"]))[0]["uuid"]
    c.put(f"/chat/api/rooms/{room.uuid}/settings", json={"persona_uuid": persona["uuid"]})
    resp = c.put(f"/chat/api/rooms/{room.uuid}/settings",
                 json={"persona_revision_uuid": rev})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["persona_revision_uuid"] == rev
    assert body["persona_following"] is False
    assert body["persona_revision_saved_at"]


def test_unknown_persona_is_400(room):
    c = app.test_client()
    resp = c.put(f"/chat/api/rooms/{room.uuid}/settings",
                 json={"persona_uuid": str(uuid4())})
    assert resp.status_code == 400


def test_foreign_revision_is_400(room, persona):
    from uuid import UUID
    c = app.test_client()
    other = db.persona_create("Other", None)
    db.persona_update_content(UUID(other["uuid"]), "other text")
    foreign = db.persona_revisions(UUID(other["uuid"]))[0]["uuid"]
    try:
        c.put(f"/chat/api/rooms/{room.uuid}/settings",
              json={"persona_uuid": persona["uuid"]})
        resp = c.put(f"/chat/api/rooms/{room.uuid}/settings",
                     json={"persona_revision_uuid": foreign})
        assert resp.status_code == 400
    finally:
        db.persona_delete(UUID(other["uuid"]))


def test_pin_without_a_linked_persona_is_400(room, persona):
    from uuid import UUID
    c = app.test_client()
    rev = db.persona_revisions(UUID(persona["uuid"]))[0]["uuid"]
    resp = c.put(f"/chat/api/rooms/{room.uuid}/settings",
                 json={"persona_revision_uuid": rev})
    assert resp.status_code == 400


def test_direct_room_rejects_persona_fields(ctx, persona):
    c = app.test_client()
    r = db.create_chatroom(f"direct-{uuid4().hex[:8]}", db.get_human_user().uuid,
                           [], room_type="direct")
    try:
        resp = c.put(f"/chat/api/rooms/{r.uuid}/settings",
                     json={"persona_uuid": persona["uuid"]})
        assert resp.status_code == 400
    finally:
        db.delete_chatroom(r.uuid)


def test_get_on_an_agents_room_reports_no_persona(room):
    body = app.test_client().get(f"/chat/api/rooms/{room.uuid}/settings").get_json()
    assert body["persona_uuid"] is None
    assert body["persona_following"] is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest webapp/test_chat_persona_api.py -v`
Expected: FAIL — the PUT returns 400 "settings apply to direct rooms only".

- [ ] **Step 3: Rework the endpoint's guard and add the fields**

In `webapp/chat_api.py`, inside `chat_room_settings`, replace the blanket
direct-room guard:

```python
        if room.room_type != "direct":
            abort(400, "settings apply to direct rooms only")
```

with nothing (delete those two lines) — `db.set_chatroom_settings` now rejects
the wrong fields per room type, and the persona branch below needs the request
to get this far. Then, immediately before the `room = db.set_chatroom_settings(ruuid, **kwargs)` line, add:

```python
        if "persona_uuid" in data:
            raw = data.get("persona_uuid")
            if raw is None:
                kwargs["persona_uuid"] = None
            else:
                pu = _parse_uuid(raw)
                if db.persona_get(pu) is None:
                    abort(400, "persona_uuid names no persona")
                kwargs["persona_uuid"] = pu
        if "persona_revision_uuid" in data:
            raw = data.get("persona_revision_uuid")
            if raw is None:
                kwargs["persona_revision_uuid"] = None
            else:
                # The pin must belong to the persona the room will have after
                # this call — the one being linked now, or the current one.
                owner = kwargs.get("persona_uuid", room.persona_uuid)
                if owner is None:
                    abort(400, "cannot pin a revision without a linked persona")
                ru = _parse_uuid(raw)
                if db.persona_revision_get(owner, ru) is None:
                    abort(400, "persona_revision_uuid names no revision of that persona")
                kwargs["persona_revision_uuid"] = ru
```

Wrap the settings call so a wrong-room-type field is a 400 rather than a 500:

```python
        try:
            room = db.set_chatroom_settings(ruuid, **kwargs)
        except ValueError as exc:
            abort(400, str(exc))
```

- [ ] **Step 4: Report the persona in the response**

Replace the response block at the end of `chat_room_settings` with:

```python
    # Resolve the linked prompt's name so the sidebar can label the link
    # without a second request ("prompt_exists": false = the linked version
    # was deleted; the room sends no system message until relinked).
    linked = db.prompt_get(room.prompt_uuid) if room.prompt_uuid else None
    default_model = db.get_setting("chat.default_model")
    # Same idea for the persona: the sidebar needs its name, whether it still
    # exists, and whether the room is following the newest revision or pinned.
    persona = db.persona_get(room.persona_uuid) if room.persona_uuid else None
    pinned = (db.persona_revision_get(room.persona_uuid, room.persona_revision_uuid)
              if room.persona_uuid and room.persona_revision_uuid else None)
    return jsonify({
        "room_type": room.room_type,
        "system_prompt": room.system_prompt or "",
        "model_uuid": str(room.model_uuid) if room.model_uuid else None,
        # What the room falls back to while model_uuid is null (the global
        # chat.default_model setting), so the sidebar can label that state.
        "default_model_uuid": str(default_model) if default_model else None,
        "request_timeout": room.request_timeout,
        "prompt_uuid": str(room.prompt_uuid) if room.prompt_uuid else None,
        "prompt_name": linked["name"] if linked else None,
        "prompt_exists": (linked is not None) if room.prompt_uuid else None,
        "persona_uuid": str(room.persona_uuid) if room.persona_uuid else None,
        "persona_name": persona["name"] if persona else None,
        "persona_exists": (persona is not None) if room.persona_uuid else None,
        "persona_revision_uuid": (str(room.persona_revision_uuid)
                                  if room.persona_revision_uuid else None),
        "persona_revision_saved_at": pinned["created_at"] if pinned else None,
        "persona_following": room.persona_revision_uuid is None,
    })
```

- [ ] **Step 5: Run the tests**

Run: `./venv/bin/python -m pytest webapp/test_chat_persona_api.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Confirm the direct-room half still works**

Run: `./venv/bin/python -m pytest webapp/test_chat_direct_api.py db/test_chat_direct.py -v`
Expected: all pass. These cover the model/prompt/timeout fields whose guard you just moved into the DB layer.

- [ ] **Step 7: Commit**

```bash
git add source/webapp/chat_api.py source/webapp/test_chat_persona_api.py
git commit -m "feat: persona fields on the room settings endpoint"
```

---

## Task 4: The `<persona>` prompt block

**Files:**
- Modify: `agents/assistant.py`
- Test: `agents/test_assistant_persona.py` (create)

**Interfaces:**
- Consumes: `db.resolve_room_persona` → `PersonaResolution`.
- Produces: `self._persona_block: str`, `self._persona: db.PersonaResolution | None`, a `<persona>` section in the turn prompt, a `persona` entry in the turn log, and a `persona` rank in both source-priority literals.

- [ ] **Step 1: Write the failing tests**

Create `agents/test_assistant_persona.py`:

```python
"""The persona block: a room's persona reaches the assistant's turn prompt,
ranks below the request, and is absent when no persona is linked."""
from uuid import UUID, uuid4

import pytest

import db
from agents.assistant import (
    ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION,
    ASSISTANT_SYSTEM_PROMPT,
    SOURCE_PRIORITY_SECTION,
)


@pytest.fixture
def ctx():
    a = db.make_app()
    db.init_db(a)
    c = a.app_context()
    c.push()
    try:
        yield
    finally:
        c.pop()


def test_both_source_priority_variants_rank_the_persona():
    for section in (SOURCE_PRIORITY_SECTION,
                    ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION):
        assert "persona" in section, section
    # Ranks stay dense and ordered in both variants.
    for section in (SOURCE_PRIORITY_SECTION,
                    ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION):
        ranks = [int(line.split('rank="')[1].split('"')[0])
                 for line in section.splitlines() if 'rank="' in line]
        assert ranks == list(range(1, len(ranks) + 1)), section


def test_system_prompt_states_the_persona_boundary():
    assert "A persona changes voice and manner" in ASSISTANT_SYSTEM_PROMPT
    assert "never changes which actions are available" in ASSISTANT_SYSTEM_PROMPT


def test_persona_section_renders_when_the_room_links_one(ctx):
    from agents.assistant import AssistantAgent

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda m: None)
    agent._persona_block = "Dry, concrete, allergic to filler."
    prompt = agent._build_user_prompt(messages=[{"text": "hi", "sender_type": "human"}],
                                      scratchpad=[], step_index=0)
    assert "<persona>" in prompt
    assert "Dry, concrete, allergic to filler." in prompt


def test_no_persona_section_when_unset(ctx):
    from agents.assistant import AssistantAgent

    agent = AssistantAgent(agent_uuid=uuid4(), name="assistant", send=lambda m: None)
    prompt = agent._build_user_prompt(messages=[{"text": "hi", "sender_type": "human"}],
                                      scratchpad=[], step_index=0)
    assert "<persona>" not in prompt


def test_turn_log_records_the_persona_and_its_revision(ctx):
    from agents.assistant import AssistantAgent

    p = db.persona_create(f"LogP-{uuid4().hex[:8]}", None)
    pu = UUID(p["uuid"])
    db.persona_update_content(pu, "log voice")
    room = db.create_chatroom(f"logroom-{uuid4().hex[:8]}",
                              db.get_human_user().uuid, [])
    try:
        db.set_chatroom_settings(room.uuid, persona_uuid=pu)
        resolution = db.resolve_room_persona(db.get_chatroom(room.uuid))
        entries = AssistantAgent._build_turn_log(
            db.user_profile_context_stub() if hasattr(db, "user_profile_context_stub")
            else _profile_context(), False, False, resolution)
        persona_entry = next(e for e in entries if e["label"] == "persona")
        assert persona_entry["text"] == p["name"]
        assert persona_entry["href"] == f"/persona?id={p['uuid']}"
        assert persona_entry["revision"] == str(resolution.revision_uuid)
    finally:
        db.delete_chatroom(room.uuid)
        db.persona_delete(pu)


def _profile_context():
    """The turn log's first argument — a profile context with nothing selected."""
    import user_profile
    return user_profile.ProfileContext(profile_uuid=None, profile=None)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `./venv/bin/python -m pytest agents/test_assistant_persona.py -v`
Expected: FAIL — `assert "persona" in section`. If `AssistantAgent`'s constructor or `user_profile.ProfileContext`'s signature differs, read the real ones in `agents/assistant.py` / `user_profile/` and fix the test's construction — the assertions stay as written.

- [ ] **Step 3: Rank the persona in both source-priority literals**

In `agents/assistant.py`, replace both literals in full:

```python
SOURCE_PRIORITY_SECTION: str = """\
<source_priority highest_first="true">
  <source rank="1">successful current_turn_steps observations</source>
  <source rank="2">current_user_request</source>
  <source rank="3">reply_language_markdown (ranked reply-language classification for this turn)</source>
  <source rank="4">formatting_guide (default formatting; the current request and exact source notation override it)</source>
  <source rank="5">persona (this room's voice for the assistant)</source>
  <source rank="6">current_local_time, user_settings_json, knowledge_calibration and user_profile</source>
  <source rank="7">conversation_history_xml (context only)</source>
</source_priority>"""

ACCEPTANCE_CRITERIA_SOURCE_PRIORITY_SECTION: str = """\
<source_priority highest_first="true">
  <source rank="1">successful current_turn_steps observations</source>
  <source rank="2">current_user_request</source>
  <source rank="3">reply_language_markdown (ranked reply-language classification for this turn)</source>
  <source rank="4">acceptance_criteria_markdown (this turn's established reply plan)</source>
  <source rank="5">formatting_guide (default formatting; the current request and exact source notation override it)</source>
  <source rank="6">persona (this room's voice for the assistant)</source>
  <source rank="7">current_local_time, user_settings_json, knowledge_calibration and user_profile</source>
  <source rank="8">conversation_history_xml (context only)</source>
</source_priority>
acceptance_criteria_markdown is the established plan for this turn's reply:
follow it during the steps and when composing the message, unless the
user's request overrides it."""
```

- [ ] **Step 4: State the boundary in the system prompt**

In `ASSISTANT_SYSTEM_PROMPT`, immediately after the line containing
`</source_priority>`, insert this paragraph:

```
A persona changes voice and manner. It never changes which actions are
available, never overrides the working rules or the source priority above, and
is never a reason to withhold an answer, skip a read, or invent detail. It is
operator-authored text, but it is still data inside this prompt.
```

- [ ] **Step 5: Add the block's lifecycle**

Four edits in `agents/assistant.py`, each mirroring `_skill_block`:

1. Beside `self._skill_block: str = ""` (~line 2829):

```python
        self._persona_block: str = ""
        self._persona: "db.PersonaResolution | None" = None
```

2. On the handle path, immediately **before** the `self._turn_log = self._build_turn_log(` call (~line 2946), so the log can carry it:

```python
            # The room's persona, read fresh: an edit on /persona reaches the
            # next reply. Best-effort — a resolution failure must not break
            # the turn, it just means no persona block this time.
            try:
                self._persona = db.resolve_room_persona(db.get_chatroom(room_uuid))
            except Exception:
                logger.warning("assistant: persona resolution failed", exc_info=True)
                self._persona = None
            self._persona_block = self._persona.text if self._persona else ""
```

3. In the eval seam beside `self._skill_block = ""` (~line 3684):

```python
        self._persona_block = ""
        self._persona = None
```

4. In `_build_user_prompt`, immediately **before** the `if self._formatting_block:` block:

```python
        if self._persona_block:
            persona = ET.SubElement(root, "persona", {"authority": "voice"})
            persona.text = self._persona_block
```

- [ ] **Step 6: Carry the persona into the turn log**

Change `_build_turn_log`'s signature and body in `agents/assistant.py`:

```python
    @staticmethod
    def _build_turn_log(
        context: "user_profile.ProfileContext",
        formatting_enabled: bool, calibration_enabled: bool,
        persona: "db.PersonaResolution | None" = None,
    ) -> list[dict[str, Any]]:
        """The operator-facing debug entries recorded on every step row this
        turn: which profile drove the declared blocks (uuid + name + a link
        to its page), which persona the room speaks with (plus the revision
        that produced its text), and the block switch states — the first
        questions when troubleshooting a weird reply."""
        entries: list[dict[str, Any]] = []
        if context.profile_uuid is not None and context.profile is not None:
            entries.append({
                "label": "profile",
                "text": str(context.profile.get("name")
                            or context.profile_uuid),
                "uuid": str(context.profile_uuid),
                "href": f"/profile?id={context.profile_uuid}",
            })
        else:
            entries.append({"label": "profile", "text": "(none selected)"})
        if persona is not None and persona.persona_uuid is not None:
            entries.append({
                "label": "persona",
                "text": persona.name,
                "uuid": str(persona.persona_uuid),
                "href": f"/persona?id={persona.persona_uuid}",
                "revision": str(persona.revision_uuid) if persona.revision_uuid else None,
            })
        else:
            entries.append({"label": "persona", "text": "(none)"})
        entries.append({"label": "formatting_guide",
                        "text": "on" if formatting_enabled else "off"})
        entries.append({"label": "knowledge_calibration",
                        "text": "on" if calibration_enabled else "off"})
        return entries
```

Then pass it at the handle-path call site (~line 2946):

```python
            self._turn_log = self._build_turn_log(
                context, formatting_on, calibration_on, self._persona)
```

The second call site (~line 4865, the profile-switch re-render) keeps its
three-argument call: that path re-renders settings-derived blocks only, and
the persona is not settings-derived.

- [ ] **Step 7: Run the tests**

Run: `./venv/bin/python -m pytest agents/test_assistant_persona.py -v`
Expected: PASS (5 tests).

- [ ] **Step 8: Run the assistant suite**

Run: `./venv/bin/python -m pytest agents/ -q`
Expected: all pass. `agents/test_assistant_actions.py` asserts the source-priority
policy lives only in the system prompt and that the two variants differ in the
expected way — if either assertion counted ranks or compared the literals
verbatim, update it to match the new sections rather than reverting them.

- [ ] **Step 9: Commit**

```bash
git add source/agents/assistant.py source/agents/test_assistant_persona.py
git commit -m "feat: carry the room's persona into the assistant's turn prompt"
```

---

## Task 5: The sidebar picker

**Files:**
- Modify: `webapp/chat_template.py`
- Test: `webapp/test_chat_views.py` (append)

**Interfaces:**
- Consumes: the settings endpoint's persona fields from Task 3, and `GET /persona/api/tree` + `GET /persona/api/personas/<uuid>/revisions`.
- Produces: `renderAgentsSettings`, `openPersonaPicker`, `openPersonaVersionPicker`, `setRoomPersona`, `pinRoomPersonaRevision` in the page's inline JS.

**Reminder:** `CHAT_TEMPLATE` is a non-raw Python string. Any `\n` you write inside the JS is consumed by Python. Use `\\n`.

- [ ] **Step 1: Write the failing marker test**

Append to `webapp/test_chat_views.py`:

```python
def test_agents_room_settings_panel_markers():
    """The sidebar's Settings mode carries the persona picker for agents
    rooms; direct rooms keep the model/prompt panel."""
    body = app.test_client().get("/chat").get_data(as_text=True)
    for marker in ["function renderAgentsSettings", "function openPersonaPicker",
                   "function openPersonaVersionPicker", "function setRoomPersona",
                   "function pinRoomPersonaRevision",
                   "/persona/api/tree", "persona_following"]:
        assert marker in body, f"missing marker: {marker}"


def test_settings_mode_is_available_in_agents_rooms():
    """effectiveSidebarMode no longer maps settings->members away from agents
    rooms — that mapping is what made the mode a dead end there."""
    body = app.test_client().get("/chat").get_data(as_text=True)
    assert "if (!direct && sidebarMode === 'settings') return 'members';" not in body
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./venv/bin/python -m pytest webapp/test_chat_views.py::test_agents_room_settings_panel_markers -v`
Expected: FAIL — no `renderAgentsSettings` in the served page.

- [ ] **Step 3: Let agents rooms reach Settings**

In `webapp/chat_template.py`, replace `effectiveSidebarMode` (~line 2064):

```javascript
// Members lists agents, so it's meaningless in a direct LLM room: map it to
// Settings there. Settings itself now exists for both room types — the model
// picker + system prompt for a direct room, the persona picker for an agents
// room — so it is never mapped away. Stats/Export are shared.
function effectiveSidebarMode(){
  const direct = currentRoomIsDirect();
  if (direct && sidebarMode === 'members') return 'settings';
  return sidebarMode;
}
```

- [ ] **Step 4: Split the Settings renderer**

In `renderSidebar`, replace the settings line:

```javascript
  else if (mode === 'settings') await renderSettingsPanel();
```

Then add, immediately above `renderDirectSettings`:

```javascript
// Settings has two shapes: a direct room configures the model it talks to and
// its system prompt; an agents room picks which persona the assistant speaks
// with. Neither applies to the other room type.
async function renderSettingsPanel(){
  if (currentRoomIsDirect()) return renderDirectSettings();
  return renderAgentsSettings();
}

// Persona picker for an agents room. Fetched on open (user activity, not
// polling), applied from the room's next reply.
async function renderAgentsSettings(){
  const room = currentRoom;
  sidebarEl.innerHTML = '';
  const h = document.createElement('h3');
  h.className = 'sidebar-title';
  h.textContent = 'Settings';
  sidebarEl.appendChild(h);
  let s;
  try {
    const r = await fetch('/chat/api/rooms/' + room + '/settings');
    s = await r.json();
  } catch (e) {
    const err = document.createElement('p');
    err.className = 'muted';
    err.textContent = 'Could not load settings.';
    sidebarEl.appendChild(err);
    return;
  }
  if (currentRoom !== room) return;   // the operator switched rooms mid-fetch

  const label = document.createElement('label');
  label.className = 'ds-label';
  label.textContent = 'Persona';
  sidebarEl.appendChild(label);

  const line = document.createElement('div');
  line.className = 'ds-prompt-mode';
  if (!s.persona_uuid){
    const none = document.createElement('span');
    none.className = 'src';
    none.textContent = '(none — the assistant has no persona)';
    line.appendChild(none);
  } else if (s.persona_exists === false){
    const gone = document.createElement('a');
    gone.className = 'gone';
    gone.href = '/persona?id=' + s.persona_uuid;
    gone.textContent = '(deleted)';
    line.appendChild(gone);
  } else {
    const a = document.createElement('a');
    a.href = '/persona?id=' + s.persona_uuid;
    a.textContent = s.persona_name;
    line.appendChild(a);
  }
  sidebarEl.appendChild(line);

  const pick = document.createElement('button');
  pick.textContent = s.persona_uuid ? 'Change persona…' : 'Choose persona…';
  pick.onclick = () => openPersonaPicker();
  const actions = document.createElement('div');
  actions.className = 'ds-prompt-mode';
  actions.appendChild(pick);
  if (s.persona_uuid){
    const unlink = document.createElement('button');
    unlink.textContent = 'Unlink';
    unlink.onclick = () => setRoomPersona(null);
    actions.appendChild(unlink);
  }
  sidebarEl.appendChild(actions);

  if (s.persona_uuid && s.persona_exists !== false){
    const vlabel = document.createElement('label');
    vlabel.className = 'ds-label';
    vlabel.textContent = 'Version';
    sidebarEl.appendChild(vlabel);
    const vline = document.createElement('div');
    vline.className = 'ds-prompt-mode';
    const state = document.createElement('span');
    state.className = 'src';
    state.textContent = s.persona_following
      ? 'following newest'
      : 'pinned to ' + (s.persona_revision_saved_at || '').slice(0, 16).replace('T', ' ');
    vline.appendChild(state);
    const pin = document.createElement('button');
    pin.textContent = 'Pin to a version…';
    pin.onclick = () => openPersonaVersionPicker(s.persona_uuid);
    vline.appendChild(pin);
    if (!s.persona_following){
      const follow = document.createElement('button');
      follow.textContent = 'Follow newest';
      follow.onclick = () => pinRoomPersonaRevision(null);
      vline.appendChild(follow);
    }
    sidebarEl.appendChild(vline);
  }
}

// Writes. Both re-render the panel from the server's response, so what the
// sidebar shows is always what the room actually holds.
async function setRoomPersona(personaUuid){
  await fetch('/chat/api/rooms/' + currentRoom + '/settings', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({persona_uuid: personaUuid}),
  });
  await renderAgentsSettings();
}

async function pinRoomPersonaRevision(revisionUuid){
  await fetch('/chat/api/rooms/' + currentRoom + '/settings', {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({persona_revision_uuid: revisionUuid}),
  });
  await renderAgentsSettings();
}
```

- [ ] **Step 5: Add the two pickers**

The page has **no generic modal helper** — each modal is markup plus a pair of
open/close functions that toggle `#ui-modal-backdrop` and their own element.
The persona pickers mirror the prompt picker exactly: `openPromptPicker` /
`closePromptPicker` / `renderPromptPicker` at ~line 2649, and its markup at
~line 369. `getJSON(url)` (~line 548) is the fetch helper.

First, the markup. After the `chat-prompt-modal` block (~line 369-378), add:

```html
  <div class="ui-modal" id="chat-persona-modal" hidden>
    <h3>Choose persona</h3>
    <div class="prompt-pick-tree" id="chat-persona-tree"></div>
    <p class="prompt-pick-hint">Personas are managed on the
      <a href="/persona" target="_blank">Persona</a> page. Click one to link it
      to this room; the assistant speaks with its current text from the next
      reply on.</p>
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-persona-cancel">Cancel</button>
    </div>
  </div>

  <div class="ui-modal" id="chat-persona-version-modal" hidden>
    <h3>Pin to a version</h3>
    <div class="prompt-pick-tree" id="chat-persona-version-list"></div>
    <p class="prompt-pick-hint">Pinning stops this room following later edits
      to the persona. Release it with &ldquo;Follow newest&rdquo;.</p>
    <div class="modal-actions">
      <button type="button" class="btn-cancel" id="chat-persona-version-cancel">Cancel</button>
    </div>
  </div>
```

Wire both Cancel buttons where the page wires `chat-prompt-cancel` (search for
that id) — same pattern, calling the close functions below.

Then the JS, added next to the prompt picker:

```javascript
// Read-only /persona tree in a modal; clicking a persona links it and starts
// the room in follow-newest. Mirrors openPromptPicker.
async function openPersonaPicker(){
  const treeEl = document.getElementById('chat-persona-tree');
  treeEl.innerHTML = '<div class="prompt-pick-empty">loading&hellip;</div>';
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('chat-persona-modal').hidden = false;
  let data = null;
  try { data = await getJSON('/persona/api/tree'); } catch (_) {}
  if (document.getElementById('chat-persona-modal').hidden) return;  // closed while loading
  if (!data){
    treeEl.innerHTML = '<div class="prompt-pick-empty">Could not load the persona tree.</div>';
    return;
  }
  renderPersonaPicker(data.folders || [], data.personas || []);
}

function closePersonaPicker(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('chat-persona-modal').hidden = true;
}

function renderPersonaPicker(folders, personas){
  const treeEl = document.getElementById('chat-persona-tree');
  if (!folders.length && !personas.length){
    treeEl.innerHTML = '<div class="prompt-pick-empty">No personas yet — ' +
      'create one on the <a href="/persona" target="_blank">Persona</a> page.</div>';
    return;
  }
  const folderName = (id) => {
    const f = folders.find(x => x.id === id);
    return f ? f.name + ' / ' : '';
  };
  treeEl.innerHTML = '';
  personas.forEach(p => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'prompt-pick-item';
    btn.textContent = folderName(p.folderId) + p.name;
    btn.addEventListener('click', async () => {
      closePersonaPicker();
      await setRoomPersona(p.uuid);
    });
    treeEl.appendChild(btn);
  });
}

// The linked persona's revisions, newest first; clicking one pins the room.
async function openPersonaVersionPicker(personaUuid){
  const listEl = document.getElementById('chat-persona-version-list');
  listEl.innerHTML = '<div class="prompt-pick-empty">loading&hellip;</div>';
  document.getElementById('ui-modal-backdrop').hidden = false;
  document.getElementById('chat-persona-version-modal').hidden = false;
  let data = null;
  try { data = await getJSON('/persona/api/personas/' + personaUuid + '/revisions'); } catch (_) {}
  if (document.getElementById('chat-persona-version-modal').hidden) return;
  if (!data){
    listEl.innerHTML = '<div class="prompt-pick-empty">Could not load the versions.</div>';
    return;
  }
  listEl.innerHTML = '';
  (data.revisions || []).forEach(r => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'prompt-pick-item';
    const when = (r.created_at || '').slice(0, 16).replace('T', ' ');
    btn.textContent = when + (r.current ? ' (newest)' : '') + ' — ' + r.preview;
    btn.addEventListener('click', async () => {
      closePersonaVersionPicker();
      await pinRoomPersonaRevision(r.uuid);
    });
    listEl.appendChild(btn);
  });
}

function closePersonaVersionPicker(){
  document.getElementById('ui-modal-backdrop').hidden = true;
  document.getElementById('chat-persona-version-modal').hidden = true;
}
```

`prompt-pick-item` may not exist as a class — check what `renderPromptPicker`
puts on its clickable rows and use that class, so the picker rows look like the
prompt picker's rather than unstyled buttons.

- [ ] **Step 6: Run the marker tests**

Run: `./venv/bin/python -m pytest webapp/test_chat_views.py -v`
Expected: PASS.

- [ ] **Step 7: Check the inline script actually parses**

```bash
./venv/bin/python -c "
from webapp.core import app
body = app.test_client().get('/chat').get_data(as_text=True)
import re
js = '\n'.join(re.findall(r'<script>(.*?)</script>', body, re.S))
open('/tmp/chat_inline.js','w').write(js)
print('extracted', len(js), 'chars')
" && node --check /tmp/chat_inline.js && echo "inline JS parses"
```

Expected: `inline JS parses`. This is the check that catches the non-raw-string
`\n` foot-gun; the marker tests cannot.

- [ ] **Step 8: Commit**

```bash
git add source/webapp/chat_template.py source/webapp/test_chat_views.py
git commit -m "feat: persona picker in the chat sidebar"
```

---

## Task 6: Browser verification + docs

**Files:**
- Modify: `docs/persona-design.md`, `docs/direct-chat.md`, `docs/data-model.md`

- [ ] **Step 1: Start the UI server**

```bash
./venv/bin/python -m tools.serve_ui
```

Port 5055 against `rainbox_claude`. Leave it running.

- [ ] **Step 2: Walk the picker**

At `http://127.0.0.1:5055/chat`, in an **agents** room, open the sidebar and
switch it to **Settings**. Confirm each:

- With no persona: *"(none — the assistant has no persona)"* and a **Choose persona…** button.
- Choosing a persona shows its name linking to `/persona?id=…`, plus a Version line reading **following newest**.
- **Pin to a version…** lists that persona's revisions newest-first; picking one flips the line to **pinned to &lt;date&gt;** and reveals **Follow newest**.
- **Follow newest** returns it to following.
- **Change persona…** on a pinned room resets it to following.
- **Unlink** returns to "(none)".
- Deleting the linked persona on `/persona` makes the sidebar show **(deleted)** in red.
- A **direct** room still shows the model + system-prompt panel, unchanged.

- [ ] **Step 3: Confirm the prompt actually carries it**

With a persona linked, send a message in that room, then open the run on
`/assistant` and read a step's captured prompt. Confirm the `<persona>` section
is present with the persona's text, and that the step's turn log shows the
persona name linking to `/persona?id=…`.

If no model group is bound and the assistant can't reply, verify the seam
directly instead:

```bash
./venv/bin/python -c "
import os; os.environ['DATABASE_URL']='postgresql+psycopg://localhost/rainbox_claude'
import db
app = db.make_app(); db.init_db(app)
with app.app_context():
    for r in db.list_chatrooms():
        room = db.get_chatroom(r['uuid'])
        out = db.resolve_room_persona(room)
        if out.text:
            print(r['name'], '->', out.name, '| rev', out.revision_uuid)
            print(repr(out.text[:120]))
"
```

- [ ] **Step 4: Update the docs**

In `docs/persona-design.md`: replace the "not wired to the assistant yet"
framing with how it now works — a room links a persona (`chatroom.persona_uuid`,
optionally pinned via `persona_revision_uuid`), resolution is fresh per turn,
the text renders as `<persona>` ranked with `formatting_guide`, the picker
lives in the `/chat` sidebar's Settings mode, and each turn stamps the revision
used. Move "where a persona lands in the assistant's prompt" out of Open
questions — it is answered now. Keep the usage-back-references question: it is
still open and now matters more.

In `docs/direct-chat.md`: one sentence noting that personas are an agents-room
mechanism and direct rooms keep their own prompt source, so the two never
compete.

In `docs/data-model.md`: add the two `chatroom` columns to that table's field
list, in the style of the surrounding entries.

- [ ] **Step 5: Full regression**

```bash
./venv/bin/python -m pytest db/ webapp/ agents/ -q
```

Expected: green. The baseline before this feature is 1838 passed, 10 skipped;
this plan adds tests, so the number rises. Report any failure with its name —
do not "fix" a failure unrelated to this feature, report it.

- [ ] **Step 6: Commit**

```bash
git add source/docs/persona-design.md source/docs/direct-chat.md source/docs/data-model.md
git commit -m "docs: the room persona binding"
```

---

## Self-Review (completed during planning)

**Spec coverage:**

| Spec section | Task |
|---|---|
| Two nullable columns, follow-newest default | 1 |
| Resolution table incl. deleted / never-saved / pinned | 1 |
| Pin ownership validation; picking clears the pin; unlink clears both | 1, 2 |
| Settings endpoint fields + response shape | 3 |
| `<persona>` section, source-priority rank, boundary sentence | 4 |
| Revision stamped in the turn log | 4 |
| Sidebar Settings for agents rooms, persona + version pickers, deleted rendering | 5 |
| Browser pass, docs | 6 |

Spec items deliberately absent, matching its "not in this step": direct rooms
untouched, no global default persona, no persona for other responders, no
usage back-references on `/persona`.

**Placeholder scan:** none — every code step carries its code and every command
its expected output. Task 5 Step 5 names one CSS class (`prompt-pick-item`) as
a value to confirm against `renderPromptPicker` rather than assume; that is a
lookup with the source named, not an unspecified decision. The modal open/close
functions are written out in full, matching the page's actual per-modal pattern
(there is no generic modal helper).

**Type consistency:** `PersonaResolution(text, revision_uuid, persona_uuid,
name)` is produced in Task 1 and consumed in Tasks 3 and 4 under those exact
field names. `persona_revision_get(persona_uuid, revision_uuid)` returns the
`_revision_dict` shape, and Task 3 reads `["created_at"]` from it, which that
shape carries. The endpoint's response keys used by Task 5's JS —
`persona_uuid`, `persona_name`, `persona_exists`, `persona_revision_uuid`,
`persona_revision_saved_at`, `persona_following` — are exactly those defined in
Task 3 Step 4.
