# Editable Chatroom Membership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator add/remove agents from a chatroom directly in the `/chat` right panel via inline checkbox toggles, instead of only at room-creation time.

**Architecture:** Two new DB helpers (`add_room_member`, `remove_room_member`) back two new HTTP endpoints (POST add, DELETE remove) on the existing `/chat/api/rooms/<uuid>/members` resource. The right-panel `renderMembers()` is rewritten to list every agent as a checkbox (checked = member); toggling fires the add/remove call live. The human creator is fixed (read-only in UI, `409`-guarded in the API). Past messages are never touched.

**Tech Stack:** Flask + SQLAlchemy (Postgres) backend; vanilla-JS inline template (`webapp/chat_template.py`); pytest against the live `rainbox_claude` DB (forced by `conftest.py`).

**Spec:** `docs/superpowers/specs/2026-06-15-editable-chatroom-membership-design.md`

---

## File Structure

- `db/chat.py` — add `add_room_member` / `remove_room_member` next to the existing membership helpers. Re-exported automatically via `from db.chat import *` in `db/__init__.py` (no `__all__`, so public names export as `db.add_room_member` etc.).
- `db/test_chat_membership.py` — **new** db-layer test module (mirrors `db/test_chat_progress.py` fixture style).
- `webapp/chat_api.py` — add POST handling to the existing `chat_room_members` view; add a new DELETE view for a single member.
- `webapp/test_chat_membership_api.py` — **new** HTTP test module (mirrors `webapp/test_chat_feedback_api.py` fixture style).
- `webapp/chat_template.py` — rewrite `renderMembers()`; add `toggleMember()`; add one CSS rule for the toggle row.

---

## Task 1: DB helpers `add_room_member` / `remove_room_member`

**Files:**
- Modify: `db/chat.py` (insert after `get_room_member_uuids`, which ends at line 112)
- Test: `db/test_chat_membership.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `db/test_chat_membership.py`:

```python
"""Tests for add_room_member / remove_room_member in db/chat.py.

Uses the live local Postgres database. Every test cleans up rows it
created so artifacts don't accumulate.
"""

from uuid import uuid4

import pytest

import db
from db import ChatUser, Chatroom


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
def room_with_one_agent(app_ctx):
    """Fresh room: human + agent_a as members, agent_b a non-member spare.
    Returns (room_uuid, human_uuid, agent_a_uuid, agent_b_uuid)."""
    human = db.get_human_user()
    assert human is not None, "seed_chat_defaults should have run"
    agent_a = ChatUser(uuid=uuid4(), name=f"mem-a-{uuid4().hex[:6]}", user_type="agent")
    agent_b = ChatUser(uuid=uuid4(), name=f"mem-b-{uuid4().hex[:6]}", user_type="agent")
    db.db.session.add_all([agent_a, agent_b])
    db.db.session.flush()
    room = db.create_chatroom(
        f"mem-test-{uuid4().hex[:6]}", human.uuid, [agent_a.uuid]
    )
    try:
        yield room.uuid, human.uuid, agent_a.uuid, agent_b.uuid
    finally:
        db.db.session.query(Chatroom).filter(Chatroom.uuid == room.uuid).delete()
        db.db.session.query(ChatUser).filter(
            ChatUser.uuid.in_([agent_a.uuid, agent_b.uuid])
        ).delete()
        db.db.session.commit()


def _member_uuids(room_uuid):
    return set(db.get_room_member_uuids(room_uuid))


def test_add_room_member_adds_new_member(room_with_one_agent):
    room_uuid, _human, _agent_a, agent_b = room_with_one_agent
    assert agent_b not in _member_uuids(room_uuid)
    added = db.add_room_member(room_uuid, agent_b)
    assert added is True
    assert agent_b in _member_uuids(room_uuid)


def test_add_room_member_is_idempotent(room_with_one_agent):
    room_uuid, _human, agent_a, _agent_b = room_with_one_agent
    # agent_a is already a member.
    added = db.add_room_member(room_uuid, agent_a)
    assert added is False
    # Still exactly one membership row for agent_a (no duplicate).
    members = db.get_room_member_uuids(room_uuid)
    assert members.count(agent_a) == 1


def test_remove_room_member_removes_existing(room_with_one_agent):
    room_uuid, _human, agent_a, _agent_b = room_with_one_agent
    removed = db.remove_room_member(room_uuid, agent_a)
    assert removed is True
    assert agent_a not in _member_uuids(room_uuid)


def test_remove_room_member_absent_returns_false(room_with_one_agent):
    room_uuid, _human, _agent_a, agent_b = room_with_one_agent
    # agent_b was never added.
    removed = db.remove_room_member(room_uuid, agent_b)
    assert removed is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest db/test_chat_membership.py -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'add_room_member'` (and `remove_room_member`).

- [ ] **Step 3: Implement the helpers**

In `db/chat.py`, insert directly after the `get_room_member_uuids` function (after line 112, before `create_chatroom`):

```python
def add_room_member(room_uuid: UUID, user_uuid: UUID) -> bool:
    """Add a user to a room. Idempotent: returns True if a new membership row
    was created, False if the user was already a member (no duplicate inserted).
    The (room_uuid, user_uuid) unique index also guards against duplicates."""
    existing = (
        db.session.query(ChatroomMember)
        .filter(
            ChatroomMember.room_uuid == room_uuid,
            ChatroomMember.user_uuid == user_uuid,
        )
        .first()
    )
    if existing is not None:
        return False
    db.session.add(ChatroomMember(room_uuid=room_uuid, user_uuid=user_uuid))
    db.session.commit()
    return True


def remove_room_member(room_uuid: UUID, user_uuid: UUID) -> bool:
    """Remove a user from a room. Returns True if a membership row was deleted,
    False if the user wasn't a member. Messages are untouched (chat_message has
    no FK on sender_uuid), so a removed agent's history stays in the room."""
    deleted = (
        db.session.query(ChatroomMember)
        .filter(
            ChatroomMember.room_uuid == room_uuid,
            ChatroomMember.user_uuid == user_uuid,
        )
        .delete()
    )
    db.session.commit()
    return deleted > 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest db/test_chat_membership.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add db/chat.py db/test_chat_membership.py
git commit -m "feat(db): add/remove chatroom member helpers"
```

---

## Task 2: API endpoints — POST add member, DELETE remove member

**Files:**
- Modify: `webapp/chat_api.py:118-123` (the existing `chat_room_members` view) and add a new DELETE view after it
- Test: `webapp/test_chat_membership_api.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `webapp/test_chat_membership_api.py`:

```python
"""HTTP API tests for chatroom membership add/remove endpoints."""

import json
from uuid import uuid4

import pytest

import db


@pytest.fixture
def client():
    app = db.make_app()
    db.init_db(app)
    import webapp.core as webapp_core
    return webapp_core.app.test_client(), webapp_core.app


@pytest.fixture
def room(client):
    """Room with human + agent_a; agent_b is a non-member spare."""
    _client, app = client
    with app.app_context():
        human = db.get_human_user()
        assert human is not None
        agent_a = db.ChatUser(
            uuid=uuid4(), name=f"mem-api-a-{uuid4().hex[:6]}", user_type="agent"
        )
        agent_b = db.ChatUser(
            uuid=uuid4(), name=f"mem-api-b-{uuid4().hex[:6]}", user_type="agent"
        )
        db.db.session.add_all([agent_a, agent_b])
        db.db.session.flush()
        room = db.create_chatroom(
            f"mem-api-{uuid4().hex[:6]}", human.uuid, [agent_a.uuid]
        )
        agent_a_uuid, agent_b_uuid = agent_a.uuid, agent_b.uuid
        try:
            yield room.uuid, human.uuid, agent_a_uuid, agent_b_uuid
        finally:
            db.db.session.query(db.Chatroom).filter(
                db.Chatroom.uuid == room.uuid
            ).delete()
            db.db.session.query(db.ChatUser).filter(
                db.ChatUser.uuid.in_([agent_a_uuid, agent_b_uuid])
            ).delete()
            db.db.session.commit()


def _add(client, room_uuid, user_uuid):
    return client.post(
        f"/chat/api/rooms/{room_uuid}/members",
        data=json.dumps({"user_uuid": str(user_uuid)}),
        content_type="application/json",
    )


def _remove(client, room_uuid, user_uuid):
    return client.delete(f"/chat/api/rooms/{room_uuid}/members/{user_uuid}")


def _member_uuids(app, room_uuid):
    with app.app_context():
        return {m["uuid"] for m in db.list_room_members(room_uuid)}


def test_add_member(client, room):
    flask_client, app = client
    room_uuid, _human, _agent_a, agent_b = room
    resp = _add(flask_client, room_uuid, agent_b)
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["added"] is True
    assert str(agent_b) in _member_uuids(app, room_uuid)


def test_add_member_idempotent(client, room):
    flask_client, app = client
    room_uuid, _human, agent_a, _agent_b = room
    resp = _add(flask_client, room_uuid, agent_a)  # already a member
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["added"] is False


def test_remove_member(client, room):
    flask_client, app = client
    room_uuid, _human, agent_a, _agent_b = room
    resp = _remove(flask_client, room_uuid, agent_a)
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["removed"] is True
    assert str(agent_a) not in _member_uuids(app, room_uuid)


def test_remove_human_rejected(client, room):
    flask_client, app = client
    room_uuid, human_uuid, _agent_a, _agent_b = room
    resp = _remove(flask_client, room_uuid, human_uuid)
    assert resp.status_code == 409, resp.data
    assert str(human_uuid) in _member_uuids(app, room_uuid)


def test_add_to_unknown_room_404(client, room):
    flask_client, _app = client
    _room_uuid, _human, _agent_a, agent_b = room
    resp = _add(flask_client, uuid4(), agent_b)
    assert resp.status_code == 404


def test_remove_from_unknown_room_404(client, room):
    flask_client, _app = client
    _room_uuid, _human, agent_a, _agent_b = room
    resp = _remove(flask_client, uuid4(), agent_a)
    assert resp.status_code == 404


def test_add_missing_user_uuid_400(client, room):
    flask_client, _app = client
    room_uuid, _human, _agent_a, _agent_b = room
    resp = flask_client.post(
        f"/chat/api/rooms/{room_uuid}/members",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest webapp/test_chat_membership_api.py -v`
Expected: FAIL — the POST returns `405 Method Not Allowed` (route is GET-only) and the DELETE route doesn't exist (`404` for the wrong reason / `405`).

- [ ] **Step 3: Implement the endpoints**

In `webapp/chat_api.py`, replace the existing `chat_room_members` view (lines 118-123):

```python
@app.route("/chat/api/rooms/<room_uuid>/members")
def chat_room_members(room_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")
    return jsonify(db.list_room_members(ruuid))
```

with this (adds POST handling) plus a new DELETE view immediately after:

```python
@app.route("/chat/api/rooms/<room_uuid>/members", methods=["GET", "POST"])
def chat_room_members(room_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        raw = data.get("user_uuid")
        if not raw:
            abort(400, "user_uuid required")
        uuser = _parse_uuid(raw)
        if db.get_chat_user(uuser) is None:
            abort(404, "user not found")
        added = db.add_room_member(ruuid, uuser)
        return jsonify(
            {"room_uuid": str(ruuid), "user_uuid": str(uuser), "added": added}
        )
    return jsonify(db.list_room_members(ruuid))


@app.route(
    "/chat/api/rooms/<room_uuid>/members/<user_uuid>", methods=["DELETE"]
)
def remove_chat_room_member(room_uuid: str, user_uuid: str) -> Response:
    ruuid = _parse_uuid(room_uuid)
    if db.get_chatroom(ruuid) is None:
        abort(404, "room not found")
    uuser = _parse_uuid(user_uuid)
    target = db.get_chat_user(uuser)
    # Defense-in-depth: the UI never offers to remove the human, but reject it
    # here too so a room can't be orphaned by a hand-crafted request.
    if target is not None and target.user_type == "human":
        abort(409, "cannot remove the human from a room")
    removed = db.remove_room_member(ruuid, uuser)
    return jsonify(
        {"room_uuid": str(ruuid), "user_uuid": str(uuser), "removed": removed}
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest webapp/test_chat_membership_api.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add webapp/chat_api.py webapp/test_chat_membership_api.py
git commit -m "feat(chat): API endpoints to add/remove room members"
```

---

## Task 3: Right-panel inline membership toggles

No JS test harness exists in this repo, so this task is verified manually in the running app. Keep the diff minimal and follow the existing vanilla-DOM style.

**Files:**
- Modify: `webapp/chat_template.py` — `renderMembers()` (lines 747-773), add `toggleMember()` after it, and add one CSS rule near line 86.

- [ ] **Step 1: Add the CSS rule for toggle rows**

In `webapp/chat_template.py`, immediately after the `.member-name` rule (line 87), add:

```css
  .room-sidebar .member-list li label.member-toggle{display:flex;align-items:center;gap:0.5em;flex:1 1 auto;cursor:pointer;margin:0}
```

- [ ] **Step 2: Rewrite `renderMembers()` and add `toggleMember()`**

Replace the entire `renderMembers()` function (lines 747-773) with:

```javascript
async function renderMembers(){
  const room = currentRoom;
  let members, agents;
  try {
    [members, agents] = await Promise.all([
      getJSON('/chat/api/rooms/' + room + '/members'),
      getJSON('/chat/api/agents'),
    ]);
  } catch (_) { return; }
  if (room !== currentRoom || sidebarMode !== 'members') return;  // changed while loading
  const memberUuids = new Set(members.map(m => m.uuid));
  const humans = members.filter(m => m.user_type === 'human');
  sidebarEl.innerHTML = '';
  const h = document.createElement('h3');
  h.className = 'sidebar-title';
  h.textContent = 'Members (' + members.length + ')';
  sidebarEl.appendChild(h);
  const ul = document.createElement('ul');
  ul.className = 'member-list';
  // Humans: always members, rendered read-only (no toggle).
  humans.forEach(m => {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'member-name';
    name.textContent = m.name;
    const badge = document.createElement('span');
    badge.className = 'msg-type msg-type-' + m.user_type;
    badge.textContent = m.user_type;
    li.appendChild(name);
    li.appendChild(badge);
    ul.appendChild(li);
  });
  // Agents: every agent is a checkbox; checked = member. Toggling adds/removes live.
  agents.forEach(a => {
    const li = document.createElement('li');
    const label = document.createElement('label');
    label.className = 'member-toggle';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = a.uuid;
    cb.checked = memberUuids.has(a.uuid);
    cb.addEventListener('change', () => toggleMember(room, a.uuid, cb));
    const name = document.createElement('span');
    name.className = 'member-name';
    name.textContent = a.name;
    label.appendChild(cb);
    label.appendChild(name);
    li.appendChild(label);
    ul.appendChild(li);
  });
  sidebarEl.appendChild(ul);
}

// Add (checkbox now checked) or remove (now unchecked) an agent from a room.
// Optimistic: the checkbox is already flipped; on failure we revert it.
async function toggleMember(room, agentUuid, cb){
  const wantMember = cb.checked;
  cb.disabled = true;
  try {
    let resp;
    if (wantMember){
      resp = await fetch('/chat/api/rooms/' + room + '/members', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user_uuid: agentUuid}),
      });
    } else {
      resp = await fetch('/chat/api/rooms/' + room + '/members/' + agentUuid, {
        method: 'DELETE',
      });
    }
    if (!resp.ok) throw new Error('member toggle -> ' + resp.status);
    // Reflect the new count in the left room list locally (no full reload).
    const r = rooms.find(x => x.uuid === room);
    if (r){ r.member_count += wantMember ? 1 : -1; renderRooms(); }
    // Rebuild the panel so the heading count stays accurate (also re-enables).
    if (room === currentRoom && sidebarMode === 'members') renderMembers();
  } catch (e) {
    cb.checked = !wantMember;  // revert on failure
    cb.disabled = false;
  }
}
```

- [ ] **Step 3: Manually verify in the running app**

Start the app (per the project's run instructions) and open `/chat`:
1. Select a room, set the right-panel mode dropdown to "members".
2. Confirm the human shows with a `human` badge and **no** checkbox.
3. Confirm every agent shows with a checkbox; current members are checked.
4. Check an unchecked agent → it stays checked, the heading count increments, and the left-panel "N members" updates. Reload the page → the agent is still a member.
5. Uncheck a member agent → it stays unchecked, counts decrement, and on reload it's gone from the room. Past messages from that agent are still visible in the transcript.
6. (Optional) In devtools, `DELETE /chat/api/rooms/<uuid>/members/<human_uuid>` returns `409` and the human remains.

- [ ] **Step 4: Commit**

```bash
git add webapp/chat_template.py
git commit -m "feat(chat): edit room membership via right-panel toggles"
```

---

## Final verification

- [ ] Run the new test modules together:

Run: `python -m pytest db/test_chat_membership.py webapp/test_chat_membership_api.py -v`
Expected: PASS (11 passed total).

- [ ] Run the existing chat tests to confirm no regression in the touched files:

Run: `python -m pytest db/test_chat_progress.py db/test_chat_streaming.py webapp/test_chat_feedback_api.py -q`
Expected: PASS (no failures introduced).

---

## Notes / known limitations (from spec)

- No SSE push for membership changes: a second open browser tab sees the change only when it next reloads the sidebar. Accepted for a single-operator app.
- Out of scope: per-agent roles/permissions, membership audit log, bulk select-all/clear, member reordering.
