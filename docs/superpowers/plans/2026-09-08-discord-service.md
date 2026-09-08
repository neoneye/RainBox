# Discord Bridge Service + Direct-Room History Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give direct rooms an adjustable history window and a sidebar "Bridge troubleshooting" section, and add a standalone `discord_service/` that bridges one Discord channel to one rainbox room, forwarding replies, notices, and live-edited progress bubbles — per `docs/superpowers/specs/2026-09-08-discord-service-design.md`.

**Architecture:** Core (Tasks 1–5): one nullable `Chatroom.history_window` column threaded through the db setter, the direct-chat agent, the settings API, and the /chat sidebar; plus one endpoint that posts rows into a direct room as its responder without enqueueing a turn. Service (Tasks 6–10): a Telegram-shaped bridge — `requests`-only REST polling inbound, SSE-driven outbound, injected clients, JSON state file, two threads.

**Tech Stack:** Python 3 (stdlib + `requests`), Flask + SQLAlchemy in the core, pytest. No discord.py, no gateway, no core dependency changes.

## Global Constraints

- Work from `/Users/neoneye/git/rainbox/source`. Core tests: `venv/bin/python -m pytest -q <path>`. They run against the `rainbox_claude` Postgres DB automatically (conftest); never target `rainbox_production`.
- Service tests: `venv/bin/python -m pytest -q discord_service/` from `source/` (root venv has `requests`). Never collect `discord_service/` and `telegram_service/` in one run — both define `bridge` and `rainbox_api` modules.
- Test basenames must be unique repo-wide: `test_discord_bridge.py`, `test_discord_api.py`, `test_discord_rainbox_api.py`.
- `webapp/chat_template.py` is a NON-raw Python string rendered with Jinja `render_template_string`: no backslash escapes in added JS, and never `{{` / `{%`.
- Discord snowflake ids are strings end to end (they exceed 2^53).
- Every outbound Discord message sets `allowed_mentions: {"parse": []}`.
- Discord content limit: 2000 chars. Replies are chunked; progress text is truncated with `…`.
- The bridge is not a server: no inbound HTTP, no Flask, no /health.
- Commit after each task. Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Do not edit `source/notes/**` (operator-owned).

---

### Task 1: `Chatroom.history_window` column + db setter

**Files:**
- Modify: `source/db/models.py` (Chatroom, after `request_timeout` ~line 607)
- Modify: `source/db/__init__.py` (~line 415, next to the `request_timeout` column add)
- Modify: `source/db/chat.py` (`set_chatroom_settings`, ~line 187)
- Test: `source/db/test_chat_direct.py`

**Interfaces:**
- Produces: `Chatroom.history_window: int | None`; `db.set_chatroom_settings(room_uuid, *, ..., history_window: int | None = _UNSET)`.

- [ ] **Step 1: Write the failing test** — append to `source/db/test_chat_direct.py`:

```python
def test_set_chatroom_settings_history_window(direct_room):
    """The rolling window: how many kind='message' rows the model sees.
    Null (the default) = the whole room; partial updates leave it alone."""
    room_uuid, _human = direct_room
    assert db.get_chatroom(room_uuid).history_window is None
    db.set_chatroom_settings(room_uuid, history_window=12)
    assert db.get_chatroom(room_uuid).history_window == 12
    db.set_chatroom_settings(room_uuid, system_prompt="unrelated")
    assert db.get_chatroom(room_uuid).history_window == 12
    db.set_chatroom_settings(room_uuid, history_window=None)
    assert db.get_chatroom(room_uuid).history_window is None
```

- [ ] **Step 2: Run it** — `venv/bin/python -m pytest -q db/test_chat_direct.py -k history_window` → FAIL (`AttributeError: history_window` or `TypeError: unexpected keyword`).

- [ ] **Step 3: Implement.** In `db/models.py`, after the `request_timeout` column:

```python
    # How much history the model sees, in kind="message" rows (the operator's
    # and the model's alike), counted from the newest. Null = the whole room.
    history_window: Mapped[int | None] = mapped_column(default=None)
```

Also reword the `room_type` comment's `"direct"` line to: `the model sees the room history (all of it, or the newest history_window messages) as system/user/assistant messages, replies with one plain-text completion, and the room's own settings (below) pick the model + prompt.`

In `db/__init__.py`, right after the `request_timeout` `_add_column_if_missing` call:

```python
        _add_column_if_missing("chatroom", "history_window",
                               "history_window INTEGER")
```

In `db/chat.py` `set_chatroom_settings`: add parameter `history_window: int | None = _UNSET,` after `request_timeout`, extend the docstring with `history_window=None means the model sees the whole room again`, and add:

```python
    if history_window is not _UNSET:
        room.history_window = history_window
```

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q db/test_chat_direct.py` → all PASS.

- [ ] **Step 5: Commit** — `git add db/models.py db/__init__.py db/chat.py db/test_chat_direct.py && git commit -m "feat(chat): direct rooms get a history_window setting"`.

---

### Task 2: DirectChatAgent applies the window

**Files:**
- Modify: `source/agents/direct_chat.py` (`build_messages`, `handle`, module docstring)
- Test: `source/agents/test_direct_chat.py`

**Interfaces:**
- Consumes: `Chatroom.history_window` (Task 1).
- Produces: `DirectChatAgent.build_messages(system_prompt: str, history: list[dict], window: int | None = None) -> list[ChatMessage]`.

- [ ] **Step 1: Failing tests** — append to `source/agents/test_direct_chat.py`:

```python
def test_build_messages_window_keeps_newest_rows_and_system_message():
    history = [
        {"kind": "message", "sender_type": "human", "text": "one"},
        {"kind": "message", "sender_type": "agent", "text": "two"},
        {"kind": "thinking", "sender_type": "agent", "text": "hmm"},
        {"kind": "message", "sender_type": "human", "text": "three"},
        {"kind": "message", "sender_type": "agent", "text": "four"},
        {"kind": "message", "sender_type": "human", "text": "five"},
    ]
    messages = DirectChatAgent.build_messages("Sys.", history, window=2)
    assert [(m.role, m.content) for m in messages] == [
        (MessageRole.SYSTEM, "Sys."),
        (MessageRole.ASSISTANT, "four"),
        (MessageRole.USER, "five"),
    ]


def test_build_messages_window_none_keeps_everything():
    history = [
        {"kind": "message", "sender_type": "human", "text": str(i)}
        for i in range(5)
    ]
    assert len(DirectChatAgent.build_messages("", history, window=None)) == 5
    assert len(DirectChatAgent.build_messages("", history, window=50)) == 5


def test_handle_applies_room_history_window(direct_room, monkeypatch):
    room_uuid, human_uuid = direct_room
    db.post_chat_message(room_uuid, human_uuid, "first")
    db.post_chat_message(room_uuid, DIRECT_CHAT_UUID, "reply one")
    db.post_chat_message(room_uuid, human_uuid, "second")
    db.set_chatroom_settings(room_uuid, model_uuid=uuid4(), history_window=1)
    agent = _agent()
    seen = {}

    def fake_stream(room, model, messages, request_timeout=None):
        seen["messages"] = messages
        return "ok"

    monkeypatch.setattr(agent, "_stream_reply", fake_stream)
    agent.handle(uuid4(), {"room_uuid": str(room_uuid)})
    assert [(m.role, m.content) for m in seen["messages"]] == [
        (MessageRole.USER, "second"),
    ]
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest -q agents/test_direct_chat.py -k window` → FAIL (`unexpected keyword argument 'window'`).

- [ ] **Step 3: Implement.** In `agents/direct_chat.py`:

Module docstring: replace `the model sees the ENTIRE room history` with `the model sees the room history (all of it, or the newest history_window kind="message" rows)`.

`build_messages`:

```python
    @staticmethod
    def build_messages(
        system_prompt: str, history: list[dict[str, Any]],
        window: int | None = None,
    ) -> list[ChatMessage]:
        """The LLM message list: optional system message (blank prompt = none),
        then kind='message' rows oldest-first — human rows as `user`,
        everything else as `assistant`. `window` keeps only the newest that
        many message rows (the room's history_window); None = every row. The
        system message is never counted. The triggering message is simply
        the last user row."""
        messages: list[ChatMessage] = []
        if system_prompt.strip():
            messages.append(
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt)
            )
        rows = [m for m in history if m.get("kind") == "message"]
        if window is not None and window > 0:
            rows = rows[-window:]
        for m in rows:
            role = (
                MessageRole.USER
                if m.get("sender_type") == "human"
                else MessageRole.ASSISTANT
            )
            messages.append(ChatMessage(role=role, content=m.get("text", "")))
        return messages
```

`handle`: `messages = self.build_messages(db.resolve_room_system_prompt(room), history, window=room.history_window)`.

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q agents/test_direct_chat.py` → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(agents): direct chat sends only the room's history window"`.

---

### Task 3: settings API carries `history_window`

**Files:**
- Modify: `source/webapp/chat_api.py` (`chat_room_settings`, ~line 577–643)
- Test: `source/webapp/test_chat_direct_api.py`

**Interfaces:**
- Produces: `GET/PUT /chat/api/rooms/<uuid>/settings` key `history_window` (positive int | null).

- [ ] **Step 1: Failing tests.** In `test_settings_get_and_put`, add `"history_window": None,` to the expected dict. Append:

```python
def test_settings_history_window(client, direct_room):
    """The rolling window: positive int or null; anything else is a 400."""
    test_client, _app = client
    room_uuid, _human = direct_room
    url = f"/chat/api/rooms/{room_uuid}/settings"

    resp = test_client.put(url, json={"history_window": 5})
    assert resp.status_code == 200
    assert resp.get_json()["history_window"] == 5
    assert test_client.get(url).get_json()["history_window"] == 5

    for bad in (0, -1, "5", 2.5, True):
        resp = test_client.put(url, json={"history_window": bad})
        assert resp.status_code == 400, bad

    resp = test_client.put(url, json={"history_window": None})
    assert resp.status_code == 200
    assert resp.get_json()["history_window"] is None
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest -q webapp/test_chat_direct_api.py -k settings` → FAIL.

- [ ] **Step 3: Implement.** In the PUT branch after the `request_timeout` block:

```python
        if "history_window" in data:
            raw = data.get("history_window")
            if raw is None:
                kwargs["history_window"] = None
            else:
                if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
                    abort(400, "history_window must be a positive integer "
                               "(messages) or null")
                kwargs["history_window"] = raw
```

In the response dict after `"request_timeout": room.request_timeout,` add `"history_window": room.history_window,`. Extend the route docstring: `..., which model it talks to, its reply timeout, and how many recent messages the model sees (history_window).`

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q webapp/test_chat_direct_api.py` → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(chat-api): history_window on the direct-room settings endpoint"`.

---

### Task 4: troubleshooting-post endpoint

**Files:**
- Modify: `source/webapp/chat_api.py` (new route after `retry_chat_room_message`)
- Test: `source/webapp/test_chat_direct_api.py`

**Interfaces:**
- Produces: `POST /chat/api/rooms/<uuid>/troubleshooting-post` body `{"text": str, "kind": "message"|"progress"|"notice"}` → 201 `{"id", "uuid"}`; sender is `DIRECT_CHAT_UUID`; nothing enqueued.

- [ ] **Step 1: Failing tests** — append:

```python
def test_troubleshooting_post_progress_then_reply(client, direct_room):
    """The sidebar's bridge check: progress rows are rewritten in place
    (one bubble), a following reply reaps them, and no turn is enqueued."""
    test_client, app = client
    room_uuid, _human = direct_room
    url = f"/chat/api/rooms/{room_uuid}/troubleshooting-post"
    r1 = test_client.post(url, json={"kind": "progress", "text": "step 1"})
    assert r1.status_code == 201
    r2 = test_client.post(url, json={"kind": "progress", "text": "step 2"})
    assert r2.status_code == 201
    assert r2.get_json()["id"] == r1.get_json()["id"]
    with app.app_context():
        rows = db.list_room_messages(room_uuid)
        assert [(r["kind"], r["sender_uuid"], r["text"]) for r in rows] == [
            ("progress", str(DIRECT_CHAT_UUID), "step 2"),
        ]
    r3 = test_client.post(url, json={"kind": "message", "text": "done"})
    assert r3.status_code == 201
    with app.app_context():
        rows = db.list_room_messages(room_uuid)
        assert [(r["kind"], r["sender_type"], r["text"]) for r in rows] == [
            ("message", "agent", "done"),
        ]
        assert _drain_direct_inbox() == []


def test_troubleshooting_post_notice_and_empty_progress(client, direct_room):
    test_client, app = client
    room_uuid, _human = direct_room
    url = f"/chat/api/rooms/{room_uuid}/troubleshooting-post"
    # An empty progress text mirrors the room's own empty "working" bubble.
    assert test_client.post(url, json={"kind": "progress", "text": ""}).status_code == 201
    assert test_client.post(url, json={"kind": "notice", "text": "oops"}).status_code == 201
    with app.app_context():
        rows = db.list_room_messages(room_uuid)
        assert [(r["kind"], r["text"]) for r in rows] == [("notice", "oops")]


def test_troubleshooting_post_rejects_bad_input(client, direct_room, agents_room):
    test_client, _app = client
    room_uuid, _human = direct_room
    url = f"/chat/api/rooms/{room_uuid}/troubleshooting-post"
    assert test_client.post(
        f"/chat/api/rooms/{uuid4()}/troubleshooting-post",
        json={"kind": "message", "text": "x"},
    ).status_code == 404
    assert test_client.post(
        f"/chat/api/rooms/{agents_room[0]}/troubleshooting-post",
        json={"kind": "message", "text": "x"},
    ).status_code == 400
    assert test_client.post(url, json={"kind": "thinking", "text": "x"}).status_code == 400
    assert test_client.post(url, json={"kind": "message", "text": "  "}).status_code == 400
    assert test_client.post(url, json={"kind": "notice", "text": ""}).status_code == 400
    assert test_client.post(url, json={"kind": "message", "text": 5}).status_code == 400
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest -q webapp/test_chat_direct_api.py -k troubleshooting` → FAIL (404/405).

- [ ] **Step 3: Implement** — after `retry_chat_room_message`:

```python
TROUBLESHOOTING_KINDS: tuple[str, ...] = ("message", "progress", "notice")


@app.route("/chat/api/rooms/<room_uuid>/troubleshooting-post", methods=["POST"])
def troubleshooting_post(room_uuid: str) -> tuple[Response, int]:
    """Post into a direct room AS its responder, with no model turn — the
    sidebar's "Bridge troubleshooting" section. The rows are ordinary
    progress/message/notice rows, so the web UI shows them and the bridges
    (Discord, Telegram) forward them exactly as they would a real turn: that
    is how an operator watches the far-side bot post several messages, and
    edit one progress bubble in place, without any message from that side.
    Nothing is enqueued: the sender is an agent and the db layer is called
    directly, never the human-post trigger."""
    ruuid = _parse_uuid(room_uuid)
    room = db.get_chatroom(ruuid)
    if room is None:
        abort(404, "room not found")
    if room.room_type != "direct":
        abort(400, "troubleshooting posts apply to direct rooms only")
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in TROUBLESHOOTING_KINDS:
        abort(400, "kind must be one of: " + ", ".join(TROUBLESHOOTING_KINDS))
    text = data.get("text")
    if not isinstance(text, str):
        abort(400, "text must be a string")
    if kind != "progress" and not text.strip():
        abort(400, "text required")
    if kind == "progress":
        # Rewrites the responder's one live bubble (or creates it) — the same
        # path a working turn uses, so repeated presses edit in place.
        msg = db.upsert_progress(ruuid, DIRECT_CHAT_UUID, text)
    else:
        # A terminal kind: reaps the responder's progress rows in the same
        # transaction, like a real reply or failure notice.
        msg = db.post_chat_message(
            ruuid, DIRECT_CHAT_UUID, text, db.detect_content_type(text), kind=kind
        )
    return jsonify({"id": msg.id, "uuid": str(msg.uuid)}), 201
```

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q webapp/test_chat_direct_api.py` → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(chat-api): troubleshooting-post posts as the direct-room responder"`.

---

### Task 5: sidebar — history window input + Bridge troubleshooting section

**Files:**
- Modify: `source/webapp/chat_template.py` (CSS ~line 130, `renderDirectSettings` ~lines 2699–2812)
- Test: `source/webapp/test_chat_views.py`

**Interfaces:**
- Consumes: settings key `history_window` (Task 3); `POST .../troubleshooting-post` (Task 4); existing JS helpers `postJSON`, `putJSON`, `chatToast`.

- [ ] **Step 1: Failing marker tests** — append to `test_chat_views.py`:

```python
def test_direct_room_history_window_setting():
    body = _body()
    assert "History window (messages)" in body
    assert "ds-window" in body
    assert "history_window:" in body


def test_direct_room_bridge_troubleshooting():
    body = _body()
    assert "Bridge troubleshooting" in body
    assert "/troubleshooting-post" in body
    for marker in ("ds-trouble-text", "ds-trouble-progress",
                   "ds-trouble-reply", "ds-trouble-notice"):
        assert marker in body
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest -q webapp/test_chat_views.py -k "history_window or troubleshooting"` → FAIL.

- [ ] **Step 3: Implement.**

CSS: change the `input.ds-timeout` rule's selector to `.room-sidebar input.ds-timeout,.room-sidebar input.ds-window{...}` and add after the `.ds-save:disabled` rule:

```css
  /* Bridge troubleshooting: post as the responder, no model turn. */
  .room-sidebar .ds-trouble-help{margin:0.25em 0 0.4em;font-size:0.8rem;color:#6b7280;line-height:1.35}
  .room-sidebar textarea.ds-trouble-text{width:100%;box-sizing:border-box;font:inherit;font-size:0.85rem;line-height:1.4;padding:0.4em;border:1px solid #ccc;border-radius:6px;resize:vertical;min-height:4em}
  .room-sidebar .ds-trouble-row{display:flex;gap:0.4em;flex-wrap:wrap;margin-top:0.4em}
  .room-sidebar .ds-trouble-row button{border:1px solid #cbd5e1;background:#fff;color:#374151;border-radius:6px;padding:0.3em 0.7em;font:inherit;font-size:0.8rem;cursor:pointer}
  .room-sidebar .ds-trouble-row button:hover{background:#f1f5f9}
  .room-sidebar .ds-trouble-row button:disabled{background:#f8fafc;color:#9ca3af;cursor:default}
```

JS, directly after `sidebarEl.appendChild(timeoutInput);`:

```js
  // Per-room history window: how many kind="message" rows (yours and the
  // model's, newest first) the model sees. Empty = the whole room.
  const windowLabel = document.createElement('span');
  windowLabel.className = 'ds-label';
  windowLabel.textContent = 'History window (messages)';
  sidebarEl.appendChild(windowLabel);
  const windowInput = document.createElement('input');
  windowInput.type = 'number';
  windowInput.className = 'ds-window';
  windowInput.min = '1';
  windowInput.step = '1';
  windowInput.placeholder = 'all';
  if (settings.history_window) windowInput.value = settings.history_window;
  sidebarEl.appendChild(windowInput);
```

In the Save handler, after `const t = parseInt(timeoutInput.value, 10);` add `const w = parseInt(windowInput.value, 10);` and after the `request_timeout:` line add `history_window: Number.isFinite(w) && w > 0 ? w : null,`.

Directly after `sidebarEl.appendChild(save);` (end of `renderDirectSettings`):

```js
  // Bridge troubleshooting: post into this room AS the responder, with no
  // model turn. The rows are ordinary progress/message/notice rows, so the
  // bridges (Discord, Telegram) forward them like a real turn — press
  // progress a few times, then reply, and watch the far side edit one bubble
  // in place and then replace it. That is the bot posting on its own, not
  // answering a message.
  const troubleHead = document.createElement('span');
  troubleHead.className = 'ds-label';
  troubleHead.textContent = 'Bridge troubleshooting';
  sidebarEl.appendChild(troubleHead);
  const troubleHelp = document.createElement('div');
  troubleHelp.className = 'ds-trouble-help';
  troubleHelp.textContent = 'Posts into this room as the responder, without a model turn. Bridges (Discord, Telegram) forward these like real replies.';
  sidebarEl.appendChild(troubleHelp);
  const troubleText = document.createElement('textarea');
  troubleText.className = 'ds-trouble-text';
  troubleText.placeholder = 'Message text';
  sidebarEl.appendChild(troubleText);
  const troubleRow = document.createElement('div');
  troubleRow.className = 'ds-trouble-row';
  sidebarEl.appendChild(troubleRow);
  [['progress', 'Post as progress', 'ds-trouble-progress'],
   ['message', 'Post as reply', 'ds-trouble-reply'],
   ['notice', 'Post as notice', 'ds-trouble-notice']].forEach(([kind, label, cls]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.textContent = label;
    b.addEventListener('click', async () => {
      const text = troubleText.value;
      if (kind !== 'progress' && !text.trim()){ chatToast('Reply/notice needs text'); return; }
      b.disabled = true;
      try {
        await postJSON('/chat/api/rooms/' + room + '/troubleshooting-post', {kind: kind, text: text});
        chatToast('Posted as ' + (kind === 'message' ? 'reply' : kind) + '.');
      } catch (e) {
        alert('Post failed: ' + e.message);
      } finally {
        b.disabled = false;
      }
    });
    troubleRow.appendChild(b);
  });
```

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q webapp/test_chat_views.py webapp/test_chat_direct_api.py` → PASS. Then start the app (`venv/bin/python main.py` or however the operator runs it) if convenient and open a direct room's Settings sidebar: the window input sits under the timeout, the troubleshooting section under Save, and pressing "Post as progress" twice then "Post as reply" shows one bubble edited then replaced by the reply.

- [ ] **Step 5: Commit** — `git commit -m "feat(chat-ui): history window input and bridge troubleshooting in the direct-room sidebar"`.

---

### Task 6: `discord_service/` scaffold + `discord_api.py`

**Files:**
- Create: `source/discord_service/.gitignore`, `requirements.txt`, `discord_api.py`
- Test: `source/discord_service/test_discord_api.py`

**Interfaces:**
- Produces:
  - `DISCORD_MAX_LEN = 2000`, `chunk_text(text, limit=DISCORD_MAX_LEN) -> list[str]`
  - `DiscordClient(token, session=None, sleep=time.sleep)` with `get_me() -> dict`, `get_messages(channel_id: str, after: str | None, limit: int = 100) -> list[dict]` (oldest first), `send_message(channel_id: str, text: str) -> list[str]` (ids, chunked), `edit_message(channel_id, message_id, text) -> None`, `delete_message(channel_id, message_id) -> None` (404 swallowed).
  - The session must expose `request(method, url, **kwargs)`.

- [ ] **Step 1: Scaffold.**

`.gitignore`:
```
venv/
state.json
state.tmp
```

`requirements.txt`:
```
# Discord bridge service — isolated from the main project's deps.
# Fully pinned (direct + transitive) for supply-chain safety, following the
# voice_tts_kokoro convention. Regenerate with `pip freeze` after changes.

# Direct dependency
requests==2.34.2

# Transitive (pins copied from the telegram_service lock, same platform)
certifi==2026.5.20
charset-normalizer==3.4.7
idna==3.18
urllib3==2.7.0
```

- [ ] **Step 2: Failing tests** — `test_discord_api.py`:

```python
"""DiscordClient against a fake requests session — no network."""
import pytest

from discord_api import DISCORD_MAX_LEN, DiscordClient, chunk_text


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _client(responses, slept=None):
    session = FakeSession(responses)
    return DiscordClient("tok", session=session,
                         sleep=(slept.append if slept is not None else lambda s: None)), session


def test_chunk_text():
    assert chunk_text("") == []
    assert chunk_text("hi") == ["hi"]
    chunks = chunk_text("x" * (DISCORD_MAX_LEN + 1))
    assert [len(c) for c in chunks] == [DISCORD_MAX_LEN, 1]


def test_headers_carry_bot_token_and_user_agent():
    client, session = _client([FakeResponse({"id": "42", "username": "bot"})])
    assert client.get_me()["id"] == "42"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", "https://discord.com/api/v10/users/@me")
    assert kwargs["headers"]["Authorization"] == "Bot tok"
    assert kwargs["headers"]["User-Agent"].startswith("DiscordBot")


def test_get_messages_sorted_oldest_first_with_after():
    client, session = _client([FakeResponse([
        {"id": "300", "content": "c"}, {"id": "100", "content": "a"},
        {"id": "200", "content": "b"},
    ])])
    rows = client.get_messages("777", after="50")
    assert [r["id"] for r in rows] == ["100", "200", "300"]
    _m, url, kwargs = session.calls[0]
    assert url == "https://discord.com/api/v10/channels/777/messages"
    assert kwargs["params"] == {"limit": 100, "after": "50"}


def test_get_messages_without_after_omits_param():
    client, session = _client([FakeResponse([])])
    assert client.get_messages("777", after=None, limit=1) == []
    assert session.calls[0][2]["params"] == {"limit": 1}


def test_send_message_chunks_and_suppresses_mentions():
    client, session = _client([FakeResponse({"id": "1"}), FakeResponse({"id": "2"})])
    ids = client.send_message("777", "x" * (DISCORD_MAX_LEN + 5))
    assert ids == ["1", "2"]
    for _m, url, kwargs in session.calls:
        assert url == "https://discord.com/api/v10/channels/777/messages"
        assert kwargs["json"]["allowed_mentions"] == {"parse": []}
    assert len(session.calls[0][2]["json"]["content"]) == DISCORD_MAX_LEN
    assert session.calls[1][2]["json"]["content"] == "xxxxx"


def test_send_message_empty_posts_nothing():
    client, session = _client([])
    assert client.send_message("777", "") == []
    assert session.calls == []


def test_edit_and_delete():
    client, session = _client([FakeResponse({"id": "9"}), FakeResponse(None, status=204)])
    client.edit_message("777", "9", "new text")
    client.delete_message("777", "9")
    assert session.calls[0][:2] == ("PATCH", "https://discord.com/api/v10/channels/777/messages/9")
    assert session.calls[0][2]["json"] == {"content": "new text", "allowed_mentions": {"parse": []}}
    assert session.calls[1][:2] == ("DELETE", "https://discord.com/api/v10/channels/777/messages/9")


def test_delete_swallows_404():
    client, _session = _client([FakeResponse({"message": "Unknown Message"}, status=404)])
    client.delete_message("777", "9")  # no raise


def test_429_sleeps_retry_after_and_retries_once():
    slept = []
    client, session = _client(
        [FakeResponse({"retry_after": 1.5}, status=429), FakeResponse({"id": "1"})],
        slept=slept,
    )
    assert client.send_message("777", "hi") == ["1"]
    assert slept == [1.5]
    assert len(session.calls) == 2


def test_second_429_raises():
    client, _session = _client([
        FakeResponse({"retry_after": 1}, status=429),
        FakeResponse({"retry_after": 1}, status=429),
    ])
    with pytest.raises(RuntimeError):
        client.send_message("777", "hi")
```

- [ ] **Step 3: Run** `venv/bin/python -m pytest -q discord_service/` → FAIL (import error).

- [ ] **Step 4: Implement** `discord_api.py`:

```python
"""Thin Discord REST client — only the calls the bridge needs.

Raw HTTP via `requests` (no discord.py, no gateway WebSocket): read a
channel's messages, post/edit/delete messages, identify the bot. See
https://discord.com/developers/docs/resources/message.
"""
import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_LEN = 2000  # message content limit
RETRY_AFTER_CAP_SECONDS = 30.0

# Model output can contain "@everyone" or a role mention; never let it ping.
_NO_MENTIONS: dict[str, Any] = {"parse": []}


def chunk_text(text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
    """Split text into <=limit chunks; empty text yields no chunks."""
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class DiscordClient:
    def __init__(
        self, token: str, session: Any | None = None, sleep: Any = time.sleep
    ) -> None:
        self._headers = {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (rainbox discord_service, 1.0)",
        }
        self._session = session or requests.Session()
        self._sleep = sleep

    def _request(self, method: str, path: str, *, ok_404: bool = False, **kwargs: Any) -> Any:
        """One call. A 429 is honored once (sleep the body's retry_after,
        capped, then retry); a second 429 or any other error status raises."""
        url = f"{API_BASE}{path}"
        for attempt in (1, 2):
            resp = self._session.request(
                method, url, headers=self._headers, timeout=30, **kwargs
            )
            if resp.status_code == 429 and attempt == 1:
                try:
                    retry_after = float((resp.json() or {}).get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                wait = min(RETRY_AFTER_CAP_SECONDS, max(0.0, retry_after))
                logger.warning("discord rate limited; sleeping %.1fs", wait)
                self._sleep(wait)
                continue
            if ok_404 and resp.status_code == 404:
                return resp
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/users/@me").json()

    def get_messages(
        self, channel_id: str, after: str | None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Messages in the channel, OLDEST first (Discord returns newest
        first). `after` is a snowflake string; None = the newest page."""
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        rows = self._request("GET", f"/channels/{channel_id}/messages", params=params).json()
        return sorted(rows, key=lambda m: int(m["id"]))

    def send_message(self, channel_id: str, text: str) -> list[str]:
        """Post text as one or more messages (chunked at the limit); returns
        the Discord message ids in order. Empty text posts nothing."""
        ids: list[str] = []
        for chunk in chunk_text(text):
            resp = self._request(
                "POST", f"/channels/{channel_id}/messages",
                json={"content": chunk, "allowed_mentions": _NO_MENTIONS},
            )
            ids.append(str(resp.json()["id"]))
        return ids

    def edit_message(self, channel_id: str, message_id: str, text: str) -> None:
        """Replace one message's content (caller keeps it within the limit)."""
        self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}",
            json={"content": text, "allowed_mentions": _NO_MENTIONS},
        )

    def delete_message(self, channel_id: str, message_id: str) -> None:
        """Delete one message; already gone (404) is fine."""
        self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}", ok_404=True)
```

- [ ] **Step 5: Run** `venv/bin/python -m pytest -q discord_service/` → PASS.

- [ ] **Step 6: Commit** — `git add discord_service && git commit -m "feat(discord_service): scaffold and thin Discord REST client"`.

---

### Task 7: `rainbox_api.py` (+ `get_message`)

**Files:**
- Create: `source/discord_service/rainbox_api.py`
- Test: `source/discord_service/test_discord_rainbox_api.py`

**Interfaces:**
- Produces: `RainboxClient(base_url, session=None)` with `find_room_by_name(name) -> dict | None`, `post_message(room_uuid, text) -> dict`, `get_messages_after(room_uuid, after_id: int) -> list[dict]`, `get_message(room_uuid, message_id: int) -> dict | None`, `iter_sse_events() -> Iterator[dict]`.

- [ ] **Step 1: Failing tests** — `test_discord_rainbox_api.py`:

```python
"""RainboxClient against a fake requests session — no network."""
from rainbox_api import RainboxClient


class FakeResponse:
    def __init__(self, payload=None, lines=None, status=200):
        self._payload = payload
        self._lines = lines or []
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


def _client(responses):
    return RainboxClient("http://127.0.0.1:5000/", session=FakeSession(responses))


def test_find_room_by_name():
    c = _client([FakeResponse([{"uuid": "u1", "name": "discord"}, {"uuid": "u2", "name": "x"}])])
    assert c.find_room_by_name("discord") == {"uuid": "u1", "name": "discord"}
    c = _client([FakeResponse([])])
    assert c.find_room_by_name("discord") is None


def test_post_message_posts_as_human():
    c = _client([FakeResponse({"id": 7, "uuid": "m7"})])
    assert c.post_message("u1", "hi") == {"id": 7, "uuid": "m7"}
    _m, url, kwargs = c._session.calls[0]
    assert url == "http://127.0.0.1:5000/chat/api/rooms/u1/messages"
    assert kwargs["json"] == {"text": "hi"}


def test_get_messages_after_passes_cursor():
    c = _client([FakeResponse([{"id": 8}])])
    assert c.get_messages_after("u1", 7) == [{"id": 8}]
    assert c._session.calls[0][2]["params"] == {"after": 7}


def test_get_message_200_and_404():
    c = _client([FakeResponse({"id": 8, "text": "t"}), FakeResponse({"error": "gone"}, status=404)])
    assert c.get_message("u1", 8) == {"id": 8, "text": "t"}
    assert c.get_message("u1", 9) is None
    assert c._session.calls[0][1] == "http://127.0.0.1:5000/chat/api/rooms/u1/messages/8"


def test_iter_sse_events_parses_data_lines_only():
    c = _client([FakeResponse(lines=[
        ": connected", "", 'data: {"room_uuid": "u1", "message_id": 3}', "",
        ": keepalive", "data: not json",
    ])])
    assert list(c.iter_sse_events()) == [{"room_uuid": "u1", "message_id": 3}]
```

- [ ] **Step 2: Run** → FAIL (import error).

- [ ] **Step 3: Implement** `rainbox_api.py` — copy `telegram_service/rainbox_api.py` verbatim (docstring: "The bridge is a pure consumer of the core's existing HTTP surface (webapp/chat_api.py)."), then add after `get_messages_after`:

```python
    def get_message(self, room_uuid: str, message_id: int) -> dict[str, Any] | None:
        """One row by id, or None once it is gone (a reaped progress row)."""
        resp = self._session.get(
            f"{self._base}/chat/api/rooms/{room_uuid}/messages/{message_id}",
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q discord_service/` → PASS.

- [ ] **Step 5: Commit** — `git commit -m "feat(discord_service): rainbox chat-API client"`.

---

### Task 8: `bridge.py` — config, state, inbound

**Files:**
- Create: `source/discord_service/bridge.py`
- Test: `source/discord_service/test_discord_bridge.py`

**Interfaces:**
- Produces: `Config(bot_token, channel_id: str, allowed_user_ids: frozenset[str], rainbox_url, room_name, state_file: Path, poll_seconds: float)`, `load_config(env)`, `load_state(path)`, `save_state(path, state)`, `RateLimitedLogger`, `process_messages(messages, cfg, state, rainbox, room_uuid, limiter)`, `init_discord_cursor(cfg, state, discord)`, `init_room_cursor(cfg, state, rainbox, room_uuid)`, `redact(text, token)`.
- State keys: `discord_after: str`, `room_cursor: int`, `progress_messages: dict[str, str]`.

- [ ] **Step 1: Failing tests** — `test_discord_bridge.py` (fakes at top are reused by Task 9; write them all now):

```python
"""Bridge logic with in-memory fakes — no network, no threads."""
import json
import threading
from typing import Any

import pytest

from bridge import (
    PROGRESS_PLACEHOLDER,
    Config,
    RateLimitedLogger,
    handle_event,
    inbound_loop,
    init_discord_cursor,
    init_room_cursor,
    load_config,
    load_state,
    outbound_catchup,
    outbound_loop,
    process_messages,
    reconcile_progress,
    redact,
    save_state,
    truncate_text,
)


# --- fakes --------------------------------------------------------------


class FakeDiscord:
    def __init__(self, channel_messages=None):
        self.channel_messages = channel_messages or []
        self.sent: list[str] = []      # texts posted
        self.edited: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._next = 1000

    def get_messages(self, channel_id, after, limit=100):
        rows = self.channel_messages
        if after is not None:
            rows = [m for m in rows if int(m["id"]) > int(after)]
        return sorted(rows, key=lambda m: int(m["id"]))[:limit]

    def send_message(self, channel_id, text):
        ids = []
        for i in range(0, len(text), 2000):
            self._next += 1
            self.sent.append(text[i:i + 2000])
            ids.append(str(self._next))
        return ids

    def edit_message(self, channel_id, message_id, text):
        self.edited.append((message_id, text))

    def delete_message(self, channel_id, message_id):
        self.deleted.append(message_id)


class FakeRainbox:
    def __init__(self, messages=None, post_fails=False):
        self.posted = []
        self.messages = messages or []
        self.post_fails = post_fails
        self._next_id = 100

    def post_message(self, room_uuid, text):
        if self.post_fails:
            raise RuntimeError("rainbox down")
        self.posted.append((room_uuid, text))
        self._next_id += 1
        return {"id": self._next_id, "uuid": f"m{self._next_id}"}

    def get_messages_after(self, room_uuid, after_id):
        return [m for m in self.messages if m["id"] > after_id]

    def get_message(self, room_uuid, message_id):
        return next((m for m in self.messages if m["id"] == message_id), None)


def _cfg(tmp_path, **overrides: Any) -> Config:
    base: dict[str, Any] = dict(
        bot_token="tok",
        channel_id="777",
        allowed_user_ids=frozenset({"111"}),
        rainbox_url="http://127.0.0.1:5000",
        room_name="discord",
        state_file=tmp_path / "state.json",
        poll_seconds=0.0,
    )
    base.update(overrides)
    return Config(**base)


def _dmsg(mid: str, author_id: str = "111", content: str | None = "hello", bot: bool = False) -> dict[str, Any]:
    author: dict[str, Any] = {"id": author_id}
    if bot:
        author["bot"] = True
    return {"id": mid, "author": author, "content": content or ""}


def _row(mid: int, kind: str = "message", sender_type: str = "agent", text: str = "t", streaming: bool = False) -> dict[str, Any]:
    return {"id": mid, "kind": kind, "sender_type": sender_type, "text": text, "streaming": streaming}


# --- config -------------------------------------------------------------


def test_load_config_reads_env(tmp_path):
    cfg = load_config({
        "DISCORD_BOT_TOKEN": "tok",
        "DISCORD_CHANNEL_ID": "777",
        "DISCORD_ALLOWED_USER_IDS": "111, 222",
        "DISCORD_STATE_FILE": str(tmp_path / "s.json"),
        "DISCORD_POLL_SECONDS": "5",
    })
    assert cfg.bot_token == "tok"
    assert cfg.channel_id == "777"
    assert cfg.allowed_user_ids == frozenset({"111", "222"})
    assert cfg.rainbox_url == "http://127.0.0.1:5000"
    assert cfg.room_name == "discord"
    assert cfg.poll_seconds == 5.0


def test_load_config_defaults_poll_seconds():
    cfg = load_config({"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "1", "DISCORD_ALLOWED_USER_IDS": "2"})
    assert cfg.poll_seconds == 2.0


@pytest.mark.parametrize("env", [
    {"DISCORD_CHANNEL_ID": "1", "DISCORD_ALLOWED_USER_IDS": "2"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_ALLOWED_USER_IDS": "2"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "1"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "abc", "DISCORD_ALLOWED_USER_IDS": "2"},
    {"DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": "1", "DISCORD_ALLOWED_USER_IDS": "not-a-number"},
])
def test_load_config_rejects_missing_or_non_numeric(env):
    with pytest.raises(SystemExit):
        load_config(env)


def test_redact_hides_token():
    assert redact("Bot tok failed", "tok") == "Bot <redacted> failed"
    assert redact("plain", "") == "plain"


# --- state --------------------------------------------------------------


def test_state_round_trip_and_missing_file(tmp_path):
    path = tmp_path / "state.json"
    assert load_state(path) == {}
    save_state(path, {"discord_after": "5"})
    assert load_state(path) == {"discord_after": "5"}
    assert json.loads(path.read_text())["discord_after"] == "5"


def test_init_discord_cursor_uses_newest_message(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    init_discord_cursor(cfg, state, FakeDiscord([_dmsg("10"), _dmsg("30"), _dmsg("20")]))
    assert state["discord_after"] == "30"
    init_discord_cursor(cfg, state, FakeDiscord([_dmsg("99")]))
    assert state["discord_after"] == "30"  # kept


def test_init_discord_cursor_empty_channel(tmp_path):
    state: dict[str, Any] = {}
    init_discord_cursor(_cfg(tmp_path), state, FakeDiscord([]))
    assert state["discord_after"] == "0"


def test_init_room_cursor_uses_latest_message(tmp_path):
    state: dict[str, Any] = {}
    init_room_cursor(_cfg(tmp_path), state, FakeRainbox([_row(3), _row(9)]), "room")
    assert state["room_cursor"] == 9
    init_room_cursor(_cfg(tmp_path), state, FakeRainbox([_row(50)]), "room")
    assert state["room_cursor"] == 9


# --- inbound ------------------------------------------------------------


def test_inbound_posts_text_and_advances_after(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    rb = FakeRainbox()
    process_messages([_dmsg("10", content="hi"), _dmsg("11", content="there")], cfg, state, rb, "room", RateLimitedLogger(60))
    assert rb.posted == [("room", "hi"), ("room", "there")]
    assert state["discord_after"] == "11"
    assert load_state(cfg.state_file)["discord_after"] == "11"


def test_inbound_drops_unauthorized_and_bots_but_advances(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    rb = FakeRainbox()
    process_messages([_dmsg("10", author_id="999"), _dmsg("11", bot=True)], cfg, state, rb, "room", RateLimitedLogger(60))
    assert rb.posted == []
    assert state["discord_after"] == "11"


def test_inbound_skips_empty_content_but_advances(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {}
    rb = FakeRainbox()
    process_messages([_dmsg("10", content="   ")], cfg, state, rb, "room", RateLimitedLogger(60))
    assert rb.posted == []
    assert state["discord_after"] == "10"


def test_inbound_post_failure_does_not_advance(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"discord_after": "9"}
    with pytest.raises(RuntimeError):
        process_messages([_dmsg("10")], cfg, state, FakeRainbox(post_fails=True), "room", RateLimitedLogger(60))
    assert state["discord_after"] == "9"
```

- [ ] **Step 2: Run** `venv/bin/python -m pytest -q discord_service/test_discord_bridge.py` → FAIL (import error; the Task 9 names are missing too — that is expected until Task 9. To keep Task 8 green on its own, implement the Task 9 names as they appear in Task 9 or temporarily comment them out of the import; the plan's executor runs Tasks 8 and 9 back to back).

- [ ] **Step 3: Implement** `bridge.py` (the whole file; Task 9 fills in the outbound section and Task 10 the loops/entrypoint, shown here in full so the file is written once):

```python
"""Discord <-> rainbox chatroom bridge — entrypoint and loop logic.

Run `python bridge.py` from inside discord_service/ with its venv active and
the core webapp running. See README.md for setup. Two worker threads:
inbound (poll the channel -> POST chat message) and outbound (SSE -> Discord
messages: replies and notices as new messages, progress bubbles edited in
place and deleted when the core reaps them). All loop logic takes injected
client objects so tests use fakes.
"""
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Both worker threads persist the shared state dict; the lock makes each
# snapshot+write atomic so one thread's update can't be lost to the other's
# stale json.dumps or a clobbered temp file.
_state_lock = threading.Lock()

DISCORD_MAX_LEN = 2000
# What an empty progress row (the room's own "working" bubble) looks like on
# Discord, where an empty message is not allowed.
PROGRESS_PLACEHOLDER = "⏳ working…"
FORWARDED_KINDS = frozenset({"message", "notice"})
BACKOFF_CAP_SECONDS = 60.0


def redact(text: str, token: str) -> str:
    """The bot token travels in a header, not the URL, so requests' error
    text normally never carries it — but never trust that; scrub it anyway."""
    return text.replace(token, "<redacted>") if token else text


def truncate_text(text: str, limit: int = DISCORD_MAX_LEN) -> str:
    """A row edited in place (progress) stays ONE message: cut with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# --- config -------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: str
    allowed_user_ids: frozenset[str]
    rainbox_url: str
    room_name: str
    state_file: Path
    poll_seconds: float


def _snowflake(value: str, what: str, hint: str) -> str:
    value = value.strip()
    if not value.isdigit():
        raise SystemExit(f"{what} must be a numeric Discord id (got {value!r}). {hint}")
    return value


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    token = (env.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is required (Developer Portal -> your app -> Bot -> Reset Token)"
        )
    hint = "Enable Developer Mode in Discord (Settings -> Advanced), then right-click -> Copy ID."
    raw_channel = (env.get("DISCORD_CHANNEL_ID") or "").strip()
    if not raw_channel:
        raise SystemExit(f"DISCORD_CHANNEL_ID is required (the one text channel to bridge). {hint}")
    channel_id = _snowflake(raw_channel, "DISCORD_CHANNEL_ID", hint)
    raw_ids = (env.get("DISCORD_ALLOWED_USER_IDS") or "").strip()
    ids = frozenset(
        _snowflake(part, "DISCORD_ALLOWED_USER_IDS", hint)
        for part in raw_ids.split(",") if part.strip()
    )
    if not ids:
        raise SystemExit(
            f"DISCORD_ALLOWED_USER_IDS is required (comma-separated numeric user ids). {hint}"
        )
    try:
        poll = float((env.get("DISCORD_POLL_SECONDS") or "2").strip())
    except ValueError:
        raise SystemExit("DISCORD_POLL_SECONDS must be a number of seconds") from None
    return Config(
        bot_token=token,
        channel_id=channel_id,
        allowed_user_ids=ids,
        rainbox_url=(env.get("RAINBOX_URL") or "http://127.0.0.1:5000").strip(),
        room_name=(env.get("DISCORD_ROOM_NAME") or "discord").strip(),
        state_file=Path(env.get("DISCORD_STATE_FILE") or "state.json"),
        poll_seconds=poll,
    )


# --- state --------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write (temp + rename) so a crash never truncates the state."""
    with _state_lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)


def _progress_map(state: dict[str, Any]) -> dict[str, str]:
    """room row id (str) -> Discord message id, for live progress bubbles."""
    return state.setdefault("progress_messages", {})


def init_discord_cursor(cfg: Config, state: dict[str, Any], discord: Any) -> None:
    """First run only: start after the channel's newest message so Discord
    history is never replayed into the room."""
    if "discord_after" in state:
        return
    rows = discord.get_messages(cfg.channel_id, after=None, limit=1)
    state["discord_after"] = str(max((int(m["id"]) for m in rows), default=0))
    save_state(cfg.state_file, state)


def init_room_cursor(cfg: Config, state: dict[str, Any], rainbox: Any, room_uuid: str) -> None:
    """First run only: start the cursor at the room's latest message so the
    bridge never replays room history to Discord."""
    if "room_cursor" in state:
        return
    rows = rainbox.get_messages_after(room_uuid, 0)
    state["room_cursor"] = max((r["id"] for r in rows), default=0)
    save_state(cfg.state_file, state)


# --- logging helper -----------------------------------------------------


class RateLimitedLogger:
    """At most one warning per key per interval — so an unauthorized spammer
    can't flood the log."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last: dict[str, float] = {}

    def warn(self, key: str, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last.get(key, float("-inf")) >= self._interval:
            self._last[key] = now
            logger.warning(msg, *args)


# --- inbound: Discord -> chatroom ---------------------------------------


def process_messages(
    messages: list[dict[str, Any]],
    cfg: Config,
    state: dict[str, Any],
    rainbox: Any,
    room_uuid: str,
    limiter: RateLimitedLogger,
) -> None:
    """Handle one poll's messages (oldest first). `discord_after` advances
    per message only after that message is fully handled — a failed post
    raises BEFORE the advance, so the next poll fetches it again
    (at-least-once; see README)."""
    for msg in messages:
        author = msg.get("author") or {}
        author_id = str(author.get("id"))
        content = msg.get("content") or ""
        if author.get("bot"):
            pass  # our own posts, and any other bot's
        elif author_id not in cfg.allowed_user_ids:
            limiter.warn(
                f"unauthorized:{author_id}",
                "dropping discord message from unauthorized user %s", author_id,
            )
        elif not content.strip():
            logger.info(
                "skipping discord message %s without text (attachments/embeds are not bridged)",
                msg.get("id"),
            )
        else:
            rainbox.post_message(room_uuid, content)  # raises -> cursor not advanced
            logger.info("discord -> room: %d chars", len(content))
        state["discord_after"] = str(msg["id"])
        save_state(cfg.state_file, state)
```

(Task 9 appends the outbound section, Task 10 the loops and entrypoint.)

- [ ] **Step 4: Run** the Task 8 tests: `venv/bin/python -m pytest -q discord_service/test_discord_bridge.py -k "config or redact or state or cursor or inbound"` → PASS once Task 9's names exist (or with the import trimmed).

- [ ] **Step 5: Commit** — `git commit -m "feat(discord_service): config, state, inbound polling logic"`.

---

### Task 9: `bridge.py` — outbound (catch-up, progress events, reconcile)

**Files:**
- Modify: `source/discord_service/bridge.py` (append)
- Test: `source/discord_service/test_discord_bridge.py` (append)

**Interfaces:**
- Produces: `outbound_catchup(cfg, state, rainbox, discord, room_uuid)`, `forward_progress(cfg, state, discord, row_id: int, text: str)`, `drop_progress(cfg, state, discord, row_ids: list[int])`, `reconcile_progress(cfg, state, rainbox, discord, room_uuid)`, `handle_event(cfg, state, rainbox, discord, room_uuid, event: dict)`.

- [ ] **Step 1: Failing tests** — append:

```python
# --- outbound -----------------------------------------------------------


def test_truncate_text():
    assert truncate_text("abc", 5) == "abc"
    assert truncate_text("abcdefgh", 5) == "abcd…"


def test_catchup_forwards_agent_message_and_notice_only(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    rb = FakeRainbox([
        _row(1, sender_type="human", text="mine"),
        _row(2, kind="thinking", text="hmm"),
        _row(3, kind="progress", text="step"),
        _row(4, kind="message", text="reply"),
        _row(5, kind="notice", text="failed"),
    ])
    dc = FakeDiscord()
    outbound_catchup(cfg, state, rb, dc, "room")
    assert dc.sent == ["reply", "failed"]
    assert state["room_cursor"] == 5


def test_catchup_stops_at_streaming_row(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    rb = FakeRainbox([_row(1, text="done"), _row(2, text="partial", streaming=True), _row(3, text="later")])
    dc = FakeDiscord()
    outbound_catchup(cfg, state, rb, dc, "room")
    assert dc.sent == ["done"]
    assert state["room_cursor"] == 1
    rb.messages[1]["streaming"] = False
    outbound_catchup(cfg, state, rb, dc, "room")
    assert dc.sent == ["done", "partial", "later"]
    assert state["room_cursor"] == 3


def test_catchup_chunks_long_reply(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    dc = FakeDiscord()
    outbound_catchup(cfg, state, FakeRainbox([_row(1, text="x" * 2001)]), dc, "room")
    assert [len(t) for t in dc.sent] == [2000, 1]


def test_progress_event_sends_then_edits_then_deletes(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    rb = FakeRainbox([_row(7, kind="progress", text="step 1")])
    dc = FakeDiscord()
    handle_event(cfg, state, rb, dc, "room", {"room_uuid": "room", "message_id": 7, "event": "insert", "kind": "progress"})
    assert dc.sent == ["step 1"]           # text fetched by id (insert carries none)
    assert state["progress_messages"] == {"7": "1001"}
    handle_event(cfg, state, rb, dc, "room", {"room_uuid": "room", "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": "step 2"})
    assert dc.edited == [("1001", "step 2")]
    rb.messages = [_row(8, text="reply")]
    handle_event(cfg, state, rb, dc, "room", {"room_uuid": "room", "message_id": 8, "event": "insert", "kind": "message", "deleted_progress_ids": [7]})
    assert dc.deleted == ["1001"]
    assert state["progress_messages"] == {}
    assert dc.sent == ["step 1", "reply"]
    assert load_state(cfg.state_file)["progress_messages"] == {}


def test_progress_empty_text_uses_placeholder_and_long_text_truncates(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0}
    dc = FakeDiscord()
    handle_event(cfg, state, FakeRainbox(), dc, "room", {"room_uuid": "room", "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": ""})
    assert dc.sent == [PROGRESS_PLACEHOLDER]
    handle_event(cfg, state, FakeRainbox(), dc, "room", {"room_uuid": "room", "message_id": 7, "event": "update", "kind": "progress", "streaming": False, "text": "y" * 2500})
    assert len(dc.edited[0][1]) == 2000


def test_progress_event_for_vanished_row_is_dropped(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0, "progress_messages": {"7": "1001"}}
    dc = FakeDiscord()
    handle_event(cfg, state, FakeRainbox([]), dc, "room", {"room_uuid": "room", "message_id": 7, "event": "insert", "kind": "progress"})
    assert dc.deleted == ["1001"]
    assert state["progress_messages"] == {}


def test_reconcile_deletes_orphaned_bubbles(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"progress_messages": {"7": "1001", "8": "1002"}}
    dc = FakeDiscord()
    reconcile_progress(cfg, state, FakeRainbox([_row(8, kind="progress")]), dc, "room")
    assert dc.deleted == ["1001"]
    assert state["progress_messages"] == {"8": "1002"}


def test_delete_event_triggers_reconcile(tmp_path):
    cfg = _cfg(tmp_path)
    state: dict[str, Any] = {"room_cursor": 0, "progress_messages": {"7": "1001"}}
    dc = FakeDiscord()
    handle_event(cfg, state, FakeRainbox([]), dc, "room", {"room_uuid": "room", "message_id": 0, "event": "delete"})
    assert dc.deleted == ["1001"]
```

- [ ] **Step 2: Run** → FAIL (names missing).

- [ ] **Step 3: Implement** — append to `bridge.py` after the inbound section:

```python
# --- outbound: chatroom -> Discord ----------------------------------------


def outbound_catchup(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any, room_uuid: str,
) -> None:
    """Forward unseen finished agent replies/notices as new Discord messages,
    advancing the cursor row by row. Stops at the first still-streaming row
    WITHOUT advancing past it: streamed rows are updated in place (same id)
    and the finalizing update fires another SSE event that re-runs this
    catch-up. Progress rows are skipped here — they are edited in place, so
    the cursor is the wrong tool; handle_event mirrors them from events."""
    rows = rainbox.get_messages_after(room_uuid, state.get("room_cursor", 0))
    for row in rows:
        if row.get("streaming"):
            break
        if row.get("kind") in FORWARDED_KINDS and row.get("sender_type") == "agent":
            discord.send_message(cfg.channel_id, row.get("text") or "")
            logger.info("room -> discord: %s row id=%s", row.get("kind"), row["id"])
        state["room_cursor"] = row["id"]
        save_state(cfg.state_file, state)


def forward_progress(
    cfg: Config, state: dict[str, Any], discord: Any, row_id: int, text: str,
) -> None:
    """Mirror one progress row: post its Discord message the first time, edit
    it in place after. Empty text (the room's own "working" bubble) shows the
    placeholder; long text is truncated so the bubble stays one message."""
    shown = truncate_text(text) if text.strip() else PROGRESS_PLACEHOLDER
    mapping = _progress_map(state)
    key = str(row_id)
    existing = mapping.get(key)
    if existing:
        discord.edit_message(cfg.channel_id, existing, shown)
    else:
        ids = discord.send_message(cfg.channel_id, shown)
        if ids:
            mapping[key] = ids[0]
    save_state(cfg.state_file, state)


def drop_progress(
    cfg: Config, state: dict[str, Any], discord: Any, row_ids: list[int],
) -> None:
    """The core reaped these progress rows: delete their Discord messages."""
    mapping = _progress_map(state)
    for rid in row_ids:
        did = mapping.pop(str(rid), None)
        if did:
            discord.delete_message(cfg.channel_id, did)
            logger.info("room -> discord: progress row id=%s reaped", rid)
    save_state(cfg.state_file, state)


def reconcile_progress(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any, room_uuid: str,
) -> None:
    """After a (re)connect: any mirrored progress row that no longer exists
    was reaped while we weren't listening — delete its Discord message."""
    gone = [
        int(key) for key in list(_progress_map(state))
        if rainbox.get_message(room_uuid, int(key)) is None
    ]
    if gone:
        drop_progress(cfg, state, discord, gone)


def handle_event(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any,
    room_uuid: str, event: dict[str, Any],
) -> None:
    """One SSE event for the bridge's room (payload shape: db.chat
    _chat_event_payload). Order matters: reaps first, then the progress
    mirror, then the cursor catch-up for finished replies/notices."""
    deleted = event.get("deleted_progress_ids") or []
    if deleted:
        drop_progress(cfg, state, discord, [int(i) for i in deleted])
    if event.get("event") == "delete":
        reconcile_progress(cfg, state, rainbox, discord, room_uuid)
    if event.get("kind") == "progress" and event.get("event") in ("insert", "update"):
        row_id = int(event["message_id"])
        text = event.get("text")
        if text is None:
            row = rainbox.get_message(room_uuid, row_id)
            if row is None:
                drop_progress(cfg, state, discord, [row_id])
                return
            text = row.get("text") or ""
        forward_progress(cfg, state, discord, row_id, text)
    outbound_catchup(cfg, state, rainbox, discord, room_uuid)
```

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q discord_service/` → PASS (except the loop tests, which Task 10 adds).

- [ ] **Step 5: Commit** — `git commit -m "feat(discord_service): outbound replies, notices, and mirrored progress bubbles"`.

---

### Task 10: loops, entrypoint, README, repo integration

**Files:**
- Modify: `source/discord_service/bridge.py` (append loops + `main`)
- Create: `source/discord_service/README.md`
- Modify: `source/README.md` (layout table row ~line 372)
- Test: `source/discord_service/test_discord_bridge.py` (append)

- [ ] **Step 1: Failing tests** — append:

```python
# --- loops --------------------------------------------------------------


class _OneShotDiscord(FakeDiscord):
    """get_messages returns the batch once, then sets stop."""

    def __init__(self, batch, stop):
        super().__init__()
        self._batch = batch
        self._stop = stop

    def get_messages(self, channel_id, after, limit=100):
        self._stop.set()
        return self._batch


def test_inbound_loop_processes_then_stops(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    state: dict[str, Any] = {"discord_after": "0"}
    rb = FakeRainbox()
    inbound_loop(cfg, state, rb, _OneShotDiscord([_dmsg("5", content="yo")], stop), "room", stop)
    assert rb.posted == [("room", "yo")]
    assert state["discord_after"] == "5"


class _SSERainbox(FakeRainbox):
    def __init__(self, events, stop, **kw):
        super().__init__(**kw)
        self._events = events
        self._stop = stop

    def iter_sse_events(self):
        for e in self._events:
            yield e
        self._stop.set()


def test_outbound_loop_reconciles_then_handles_room_events(tmp_path):
    cfg = _cfg(tmp_path)
    stop = threading.Event()
    state: dict[str, Any] = {"room_cursor": 0, "progress_messages": {"3": "900"}}
    rb = _SSERainbox(
        [{"room_uuid": "other", "message_id": 1, "event": "insert", "kind": "message"},
         {"room_uuid": "room", "message_id": 4, "event": "insert", "kind": "message"}],
        stop, messages=[_row(4, text="reply")],
    )
    dc = FakeDiscord()
    outbound_loop(cfg, state, rb, dc, "room", stop)
    assert dc.deleted == ["900"]   # orphan from before the connect
    assert dc.sent == ["reply"]
    assert state["room_cursor"] == 4


def test_loop_errors_never_log_the_bot_token(tmp_path, caplog):
    cfg = _cfg(tmp_path, bot_token="SECRET-TOKEN")
    stop = threading.Event()

    class Boom(FakeDiscord):
        def get_messages(self, channel_id, after, limit=100):
            stop.set()
            raise RuntimeError("401 for Bot SECRET-TOKEN")

    with caplog.at_level("ERROR"):
        inbound_loop(cfg, {"discord_after": "0"}, FakeRainbox(), Boom(), "room", stop)
    assert "SECRET-TOKEN" not in caplog.text
    assert "<redacted>" in caplog.text
```

- [ ] **Step 2: Run** → FAIL (loop names missing).

- [ ] **Step 3: Implement** — append to `bridge.py`:

```python
# --- loops ----------------------------------------------------------------


def _backoff_wait(attempt: int, stop: "threading.Event") -> None:
    stop.wait(min(BACKOFF_CAP_SECONDS, 2.0 ** attempt))


def inbound_loop(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any,
    room_uuid: str, stop: "threading.Event",
) -> None:
    limiter = RateLimitedLogger(60.0)
    attempt = 0
    while not stop.is_set():
        try:
            messages = discord.get_messages(cfg.channel_id, after=state.get("discord_after", "0"))
            process_messages(messages, cfg, state, rainbox, room_uuid, limiter)
            attempt = 0
            stop.wait(cfg.poll_seconds)
        except Exception as exc:
            attempt += 1
            logger.error(
                "inbound loop error (attempt %d): %s: %s",
                attempt, type(exc).__name__, redact(str(exc), cfg.bot_token),
            )
            _backoff_wait(attempt, stop)


def outbound_loop(
    cfg: Config, state: dict[str, Any], rainbox: Any, discord: Any,
    room_uuid: str, stop: "threading.Event",
) -> None:
    attempt = 0
    while not stop.is_set():
        try:
            # (Re)connect: bubbles reaped while we weren't listening, then
            # replies that landed meanwhile.
            reconcile_progress(cfg, state, rainbox, discord, room_uuid)
            outbound_catchup(cfg, state, rainbox, discord, room_uuid)
            for event in rainbox.iter_sse_events():
                attempt = 0
                if str(event.get("room_uuid")) == str(room_uuid):
                    handle_event(cfg, state, rainbox, discord, room_uuid, event)
                if stop.is_set():
                    break
        except Exception as exc:
            attempt += 1
            logger.error(
                "outbound loop error (attempt %d): %s: %s",
                attempt, type(exc).__name__, redact(str(exc), cfg.bot_token),
            )
            _backoff_wait(attempt, stop)
        else:
            if not stop.is_set():
                # SSE generator ended without error (server restart): reconnect
                attempt += 1
                _backoff_wait(attempt, stop)


# --- entrypoint -------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Deferred so `import bridge` stays stdlib-only for tests.
    from discord_api import DiscordClient
    from rainbox_api import RainboxClient

    cfg = load_config()
    rainbox = RainboxClient(cfg.rainbox_url)
    discord = DiscordClient(cfg.bot_token)

    try:
        me = discord.get_me()
    except Exception as exc:
        raise SystemExit(
            f"discord rejected the bot token: {type(exc).__name__}: "
            f"{redact(str(exc), cfg.bot_token)}"
        ) from None
    logger.info("discord bot %s (%s)", me.get("username"), me.get("id"))

    room = rainbox.find_room_by_name(cfg.room_name)
    if room is None:
        raise SystemExit(
            f"chatroom {cfg.room_name!r} not found at {cfg.rainbox_url}. Create it "
            f"on /chat in the webapp (as a direct room, pick its model), then rerun."
        )
    room_uuid = str(room["uuid"])
    logger.info("bridging discord channel %s <-> room %r (%s)", cfg.channel_id, cfg.room_name, room_uuid)

    state = load_state(cfg.state_file)
    init_discord_cursor(cfg, state, discord)
    init_room_cursor(cfg, state, rainbox, room_uuid)

    stop = threading.Event()

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("signal %d: shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    threads = [
        threading.Thread(
            target=inbound_loop, name="inbound",
            args=(cfg, state, rainbox, discord, room_uuid, stop), daemon=True,
        ),
        threading.Thread(
            target=outbound_loop, name="outbound",
            args=(cfg, state, rainbox, discord, room_uuid, stop), daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    while not stop.is_set():
        stop.wait(1.0)
    logger.info("bye")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run** `venv/bin/python -m pytest -q discord_service/` → all PASS. Also `venv/bin/python -m pytest -q telegram_service/` still passes (untouched).

- [ ] **Step 5: README** — `discord_service/README.md`:

```markdown
# Discord bridge service

A standalone process that bridges one Discord text channel and one rainbox
chatroom, two-way. Kept separate from the main project (own venv) so no
Discord-related dependency enters the main venv. Talks to the core over HTTP
only (the chat JSON API + SSE stream); the core never imports this code.

It is deliberately **not** a request/response bot: nothing here waits for a
Discord message in order to answer it. The room drives Discord — whenever a
row lands in the room the bridge mirrors it, so one turn can produce a
working bubble that is edited as the agent progresses, a failure notice, and
the reply, and the Settings sidebar can make the bot post with no Discord
input at all (see *Troubleshooting* below).

- **Inbound:** messages allowed users post in the channel are posted into the
  room as you (the human operator); the room's responder replies exactly as
  if you had typed in the web UI. Bots (including this one) are ignored;
  attachment-only messages are skipped.
- **Outbound:** agent `message` and `notice` rows become new Discord
  messages (chunked at 2000 chars, mentions suppressed). Agent `progress`
  rows become one Discord message each, edited in place as they change and
  deleted when the reply lands. `thinking`/debug rows stay in the webapp.

## Setup

1. https://discord.com/developers/applications → New Application → Bot →
   Reset Token → copy it. Under *Privileged Gateway Intents* enable
   **Message Content Intent**.
2. OAuth2 → URL Generator: scope `bot`; permissions *View Channels*, *Send
   Messages*, *Read Message History*, *Manage Messages* (deleting its own
   progress bubbles). Open the URL and add the bot to your server.
3. Discord → Settings → Advanced → **Developer Mode**. Right-click the
   channel → Copy Channel ID; right-click yourself → Copy User ID.
4. In rainbox `/chat`, create a **direct** room named `discord`, pick its
   model in Settings, optionally set its *History window*.
5. Create the venv:

   ```bash
   cd discord_service
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

## Run

With `main.py` (the core) already running:

```bash
cd discord_service
DISCORD_BOT_TOKEN=... \
DISCORD_CHANNEL_ID=123456789012345678 \
DISCORD_ALLOWED_USER_IDS=234567890123456789 \
venv/bin/python bridge.py
```

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | — | bot token from the developer portal |
| `DISCORD_CHANNEL_ID` | yes | — | the one text channel the bridge binds |
| `DISCORD_ALLOWED_USER_IDS` | yes | — | comma-separated numeric user ids; everyone else is dropped |
| `RAINBOX_URL` | no | `http://127.0.0.1:5000` | core webapp base URL |
| `DISCORD_ROOM_NAME` | no | `discord` | chatroom the bridge binds to |
| `DISCORD_STATE_FILE` | no | `./state.json` | cursor + progress-map persistence |
| `DISCORD_POLL_SECONDS` | no | `2` | inbound poll interval |

Stop with Ctrl-C.

## Troubleshooting

Open the room on `/chat`, choose **Settings** in the right panel, scroll to
**Bridge troubleshooting**. Type something, press *Post as progress* three
times with different text, then *Post as reply*. On Discord one bubble
appears and is edited twice, then it disappears and the reply is posted —
no message was sent from Discord, so the bot is demonstrably not answering
anything. *Post as notice* posts a notice row the same way.

If nothing arrives: the bridge logs every forwarded row (`room -> discord`);
a 403 on send means the bot lacks *Send Messages* in that channel; a 401 at
startup means the token is wrong.

## Behavior notes

- **At-least-once inbound:** the Discord cursor advances only after a
  message is successfully posted into the room; a crash at the wrong instant
  can duplicate one message after restart.
- **No history replay:** on first run both cursors start at "newest": old
  Discord messages are never posted into the room, old room rows never sent
  to Discord.
- **Reconnects reconcile:** after an SSE reconnect the bridge deletes Discord
  bubbles whose progress rows were reaped meanwhile, then catches up on
  replies from its cursor.
- **Rate limits:** a 429 is honored once (sleep `retry_after`), then treated
  as an error with backoff.
- **Token hygiene:** the token is scrubbed from logged errors.

## Tests

From the repo's source root: `venv/bin/python -m pytest -q discord_service/`
(in-memory fakes; no network, no token). The root-wide run must ignore this
directory (`--ignore=discord_service`, like `telegram_service`): both define
a `bridge` module.
```

Then in `source/README.md` change the services row to:
`| \`voice_tts_kokoro/\`, \`voice_stt_whisper/\`, \`reranker/\`, \`telegram_service/\`, \`discord_service/\` | standalone processes with their own venvs (TTS, STT, cross-encoder reranking, Telegram and Discord bridges) — the core talks to/with them over HTTP only |`

- [ ] **Step 6: Commit** — `git add discord_service README.md && git commit -m "feat(discord_service): loops, entrypoint, README, repo integration"`.

- [ ] **Step 7: Full check** — run the core suites touched: `venv/bin/python -m pytest -q db/test_chat_direct.py agents/test_direct_chat.py webapp/test_chat_direct_api.py webapp/test_chat_views.py discord_service/ telegram_service/` (two invocations: services separately). Push when clean.
