# Chat write-proposal UI + step provenance — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator confirm/reject a proposed confirm-tier write directly in chat, and surface a deep link to the exact `/assistant` step that created a reminder — both in chat and persisted on the cron job.

**Architecture:** Reuse the existing `AssistantWriteIntent` lifecycle and its confirm/reject HTTP endpoints. A new nullable `chat_message.meta` JSONB carries `{write_intent, capability, step_link}` on the turn's terminal reply; room-message serialization enriches it with the intent's live state; chat JS renders a card wired to the existing endpoints. Two nullable `cron_job.origin_*` columns persist the creating run/step, surfaced read-only on `/cron`.

**Tech Stack:** Python 3.14, Flask, SQLAlchemy 2.x (declarative `Mapped`), Postgres (JSONB), vanilla JS templates. Tests: pytest against local Postgres (`rainbox_claude`).

## Global Constraints

- Tests run with the project venv and the sandbox DB: `DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest ...` from `source/`. Never touch `rainbox_production`.
- Schema evolution pattern: declare the column on the model (covers fresh DBs via `create_all`) **and** add an idempotent `_add_column_if_missing(...)` call in `init_db` (covers existing DBs). Both are required.
- Deep-link format is fixed: `/assistant?id=<run_uuid>#step-<step_uuid>`.
- Reuse the existing endpoints `POST /chat/api/assistant/write-intents/<uuid>/{confirm,reject}`; do not add new ones or change the intent state machine.
- Branch: `feat/chat-write-proposal-ui` (already created; the spec is committed there).
- `db/assistant.py`, `db/chat.py`, `db/cron.py` are re-exported via `from db.X import *` in `db/__init__.py`; a new public function is automatically available as `db.<name>`.

---

## File structure

| File | Change |
|---|---|
| `db/assistant.py` | **New** pure helper `assistant_step_path(run_uuid, step_uuid) -> str`. |
| `webapp/core.py` | Refactor `_format_step_trace_link` to build its href from `assistant_step_path`. |
| `db/models.py` | `ChatMessage.meta` JSONB; `CronJob.origin_run_uuid` / `origin_step_uuid`. |
| `db/__init__.py` | `_add_column_if_missing` for the three new columns. |
| `db/chat.py` | `post_chat_message(meta=)`; `list_room_messages` emits `meta` + enriched `intent_state`. |
| `agents/assistant.py` | `_propose_write` returns a `proposal` dict; run loop harvests it into `pending_proposal` and attaches it as `meta` on the terminal reply. |
| `webapp/chat_template.py` | Render the proposal card + wire buttons (JS; manual verify). |
| `db/cron.py` | `cron_create_one_shot_message(origin_*)`; `cron_load_tree` adds `origin_step_link`. |
| `agents/assistant.py` (`_action_set_reminder`) | Derive + pass origin on real execution. |
| `webapp/cron_views.py` + `static/cron.js` | Render the Origin row on the job-details panel (JS; manual verify). |
| Tests | `db/test_assistant_step_link.py`, `db/test_chat_meta.py`, `agents/test_reminders.py`, `webapp/test_cron_api.py`. |

---

## Task 1: `assistant_step_path` helper + core.py refactor

**Files:**
- Modify: `source/db/assistant.py` (add function near the other public helpers, e.g. after `start_assistant_run`)
- Modify: `source/webapp/core.py:644-651` (`_format_step_trace_link`)
- Test: `source/db/test_assistant_step_link.py` (create)

**Interfaces:**
- Produces: `assistant_step_path(run_uuid: UUID, step_uuid: UUID) -> str` returning `f"/assistant?id={run_uuid}#step-{step_uuid}"`. Re-exported as `db.assistant_step_path`.

- [ ] **Step 1: Write the failing test**

Create `source/db/test_assistant_step_link.py`:

```python
"""The /assistant step deep-link builder shared by the chat proposal card and
the cron provenance row."""

from uuid import UUID

from db.assistant import assistant_step_path


def test_assistant_step_path_format():
    run = UUID("11111111-1111-1111-1111-111111111111")
    step = UUID("22222222-2222-2222-2222-222222222222")
    assert assistant_step_path(run, step) == (
        "/assistant?id=11111111-1111-1111-1111-111111111111"
        "#step-22222222-2222-2222-2222-222222222222"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest db/test_assistant_step_link.py -q`
Expected: FAIL — `ImportError: cannot import name 'assistant_step_path'`.

- [ ] **Step 3: Add the helper**

In `source/db/assistant.py`, add (after `start_assistant_run`, keep the `UUID` import that's already present):

```python
def assistant_step_path(run_uuid: UUID, step_uuid: UUID) -> str:
    """The /assistant deep link to one step of one run: the run page scrolled to
    (and :target-highlighting) the element with id="step-<step_uuid>"."""
    return f"/assistant?id={run_uuid}#step-{step_uuid}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest db/test_assistant_step_link.py -q`
Expected: PASS.

- [ ] **Step 5: Refactor `_format_step_trace_link` to use the helper**

In `source/webapp/core.py`, change the body of `_format_step_trace_link` (lines 644-651) so the href comes from the helper (keep the `↗` + 6-char display):

```python
def _format_step_trace_link(view, context, model, name):
    """Render a step's uuid cell as a link to its /assistant trace location —
    the run, scrolled to this step's anchor (id="step-<uuid>"). Shows only the
    first 6 chars (full value on hover), to match the other uuid columns."""
    full = str(model.uuid)
    href = db.assistant_step_path(model.run_uuid, model.uuid)
    return Markup(f'<a href="{escape(href)}" title="{escape(full)}">'
                  f'<code>{escape(full[:6])}</code> ↗</a>')
```

Confirm `import db` (or the existing `db` reference) is in scope in `core.py`; it is used elsewhere in the file.

- [ ] **Step 6: Run the admin step-link test to confirm no regression**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest webapp/test_assistant_step_admin_link.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/db/assistant.py source/db/test_assistant_step_link.py source/webapp/core.py
git commit -m "feat(assistant): assistant_step_path deep-link helper"
```

---

## Task 2: `chat_message.meta` column + `post_chat_message(meta=)`

**Files:**
- Modify: `source/db/models.py` (`ChatMessage`, after the `kind` column ~line 650)
- Modify: `source/db/__init__.py` (init_db migration block ~line 382, after the assistant_step adds)
- Modify: `source/db/chat.py:635-690` (`post_chat_message`)
- Test: `source/db/test_chat_meta.py` (create)

**Interfaces:**
- Produces: `ChatMessage.meta: Mapped[dict]` (JSONB, default `{}`). `post_chat_message(..., meta: dict | None = None) -> ChatMessage` persists it.

- [ ] **Step 1: Write the failing test**

Create `source/db/test_chat_meta.py`:

```python
"""chat_message.meta carries structured attachments (e.g. a write proposal)."""

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
        db.db.session.rollback()
        ctx.pop()


def _room():
    human = db.get_human_user()
    return db.create_chatroom(f"meta-{uuid4().hex[:8]}", human.uuid, [])


def test_post_chat_message_persists_meta(app_ctx):
    room = _room()
    sender = db.get_human_user()
    meta = {"write_intent": str(uuid4()), "capability": "set_reminder"}
    msg = db.post_chat_message(room.uuid, sender.uuid, "hi", meta=meta)
    fetched = db.db.session.get(db.ChatMessage, msg.id)
    assert fetched.meta == meta


def test_post_chat_message_meta_defaults_empty(app_ctx):
    room = _room()
    sender = db.get_human_user()
    msg = db.post_chat_message(room.uuid, sender.uuid, "hi")
    fetched = db.db.session.get(db.ChatMessage, msg.id)
    assert fetched.meta == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest db/test_chat_meta.py -q`
Expected: FAIL — `TypeError: post_chat_message() got an unexpected keyword argument 'meta'` (or an attribute/column error).

- [ ] **Step 3: Add the model column**

In `source/db/models.py`, inside `class ChatMessage`, after the `kind` column add:

```python
    # Structured attachment for interactive messages (default {}). A confirm-tier
    # write proposal stores {write_intent, capability, step_link} so chat can render
    # confirm/reject controls; list_room_messages splices in the intent's live state.
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
```

`JSONB` is already imported in `models.py` (used by `AssistantWriteIntent.payload`). Confirm the import is present.

- [ ] **Step 4: Add the migration for existing DBs**

In `source/db/__init__.py`, in the `init_db` migration block (after the `assistant_step` `_add_column_if_missing` calls, ~line 382), add:

```python
        # Structured attachment on a chat message (write-proposal card data).
        _add_column_if_missing("chat_message", "meta", "meta jsonb NOT NULL DEFAULT '{}'::jsonb")
```

- [ ] **Step 5: Thread `meta` through `post_chat_message`**

In `source/db/chat.py`, update the signature (line 635) and the `ChatMessage(...)` construction (line 660):

```python
def post_chat_message(
    room_uuid: UUID,
    sender_uuid: UUID,
    text: str,
    content_type: str = "markdown",
    kind: str = "message",
    streaming: bool = False,
    meta: dict | None = None,
) -> ChatMessage:
```

and in the constructor:

```python
    msg = ChatMessage(
        room_uuid=room_uuid,
        sender_uuid=sender_uuid,
        text=text,
        content_type=content_type,
        kind=kind,
        streaming=streaming,
        meta=meta or {},
    )
```

Add one line to the docstring noting `meta` carries an optional structured attachment.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest db/test_chat_meta.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Run the chat/db suites to confirm no regression**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest db/test_chat_streaming.py db/test_chat_progress.py webapp/test_chat_views.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/db/models.py source/db/__init__.py source/db/chat.py source/db/test_chat_meta.py
git commit -m "feat(chat): chat_message.meta JSONB + post_chat_message(meta=)"
```

---

## Task 3: `list_room_messages` emits `meta` + live `intent_state`

**Files:**
- Modify: `source/db/chat.py:531-580` (`list_room_messages`)
- Test: `source/db/test_chat_meta.py` (extend)

**Interfaces:**
- Consumes: `ChatMessage.meta`, `AssistantWriteIntent` (uuid, state) via `db.session`.
- Produces: each message dict gains `"meta": dict`. When `meta.write_intent` is set, `meta["intent_state"]` is the intent's current state (e.g. `"proposed"`, `"completed"`, `"rejected"`).

- [ ] **Step 1: Write the failing test**

Append to `source/db/test_chat_meta.py`:

```python
from agents.config import ASSISTANT_UUID


def _run_and_step(room_uuid):
    run = db.start_assistant_run(journal_id=uuid4(), room_uuid=room_uuid,
                                 agent_uuid=ASSISTANT_UUID)
    step = db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed", action="set_reminder")
    return run, step


def test_list_room_messages_enriches_intent_state(app_ctx):
    room = _room()
    run, step = _run_and_step(room.uuid)
    intent = db.create_write_intent(
        run_uuid=run.uuid, step_uuid=step.uuid, capability_name="set_reminder",
        payload={"text": "t", "when": "2026-06-29T09:00"}, preview_text="p",
        room_uuid=room.uuid, agent_uuid=ASSISTANT_UUID,
    )
    db.post_chat_message(
        room.uuid, ASSISTANT_UUID, "awaiting confirmation",
        meta={"write_intent": str(intent.uuid), "capability": "set_reminder",
              "step_link": db.assistant_step_path(run.uuid, step.uuid)},
    )
    msgs = db.list_room_messages(room.uuid)
    card = next(m for m in msgs if m["meta"].get("write_intent") == str(intent.uuid))
    assert card["meta"]["intent_state"] == "proposed"
    assert card["meta"]["step_link"] == db.assistant_step_path(run.uuid, step.uuid)

    # The state tracks a transition done elsewhere (e.g. on /assistant).
    db.set_write_intent_state(intent, "rejected")
    msgs2 = db.list_room_messages(room.uuid)
    card2 = next(m for m in msgs2 if m["meta"].get("write_intent") == str(intent.uuid))
    assert card2["meta"]["intent_state"] == "rejected"


def test_list_room_messages_meta_empty_for_plain_message(app_ctx):
    room = _room()
    db.post_chat_message(room.uuid, db.get_human_user().uuid, "plain")
    msg = db.list_room_messages(room.uuid)[-1]
    assert msg["meta"] == {} and "intent_state" not in msg["meta"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest db/test_chat_meta.py -k enriches -q`
Expected: FAIL — `KeyError: 'meta'` (the dict has no `meta` yet).

- [ ] **Step 3: Implement the enrichment**

In `source/db/chat.py`, ensure `AssistantWriteIntent` and `UUID` are importable. `UUID` is already imported. Add `AssistantWriteIntent` to the model imports at the top of the file (alongside `ChatMessage`, `ChatUser`, `FeedbackEvent`).

In `list_room_messages`, after the `latest_feedback` block and before the `out` loop, add the batched intent-state lookup:

```python
    # Live write-intent state for proposal messages (meta.write_intent set), so a
    # card reflects a confirm/reject performed on /assistant. One batched lookup.
    intent_state: dict[str, str] = {}
    wanted: list[UUID] = []
    for r in rows:
        wid = (r.meta or {}).get("write_intent")
        if wid:
            try:
                wanted.append(UUID(str(wid)))
            except ValueError:
                pass
    if wanted:
        for iu, st in (
            db.session.query(AssistantWriteIntent.uuid, AssistantWriteIntent.state)
            .filter(AssistantWriteIntent.uuid.in_(wanted))
            .all()
        ):
            intent_state[str(iu)] = st
```

Then in the `out.append({...})` dict, add a `meta` key built per row:

```python
    for r in rows:
        sender = users.get(r.sender_uuid)
        meta = dict(r.meta or {})
        wid = meta.get("write_intent")
        if wid and str(wid) in intent_state:
            meta["intent_state"] = intent_state[str(wid)]
        out.append(
            {
                "id": r.id,
                "uuid": str(r.uuid),
                "sender_uuid": str(r.sender_uuid),
                "sender_name": sender.name if sender else "(unknown)",
                "sender_type": sender.user_type if sender else "agent",
                "text": r.text,
                "content_type": r.content_type,
                "kind": r.kind,
                "streaming": r.streaming,
                "timestamp": r.created_at.strftime("%Y-%m-%d %H:%M"),
                "feedback": latest_feedback.get(r.uuid),
                "meta": meta,
            }
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest db/test_chat_meta.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/db/chat.py source/db/test_chat_meta.py
git commit -m "feat(chat): list_room_messages emits meta + live intent_state"
```

---

## Task 4: Proposal harvest — attach card `meta` to the terminal reply

**Files:**
- Modify: `source/agents/assistant.py:1709-1717` (`_propose_write` return), `:1319` (init `pending_proposal`), `:1418-1422` (harvest), `:1364-1366` (attach to reply)
- Test: `source/agents/test_reminders.py` (extend)

**Interfaces:**
- Consumes: `db.assistant_step_path`, `post_chat_message(meta=)`.
- Produces: the turn's terminal `REPLY` `ChatMessage` carries `meta = {"write_intent", "capability", "step_link"?}` when a confirm-tier write was proposed that turn.

- [ ] **Step 1: Write the failing test**

Append to `source/agents/test_reminders.py` (it already imports `AssistantAgent`, `scripted_decisions`, `_room`, `ASSISTANT_UUID`, `db`):

```python
def test_proposal_meta_attached_to_reply(app_ctx):
    tag = f"rem-{uuid4()}"
    chatroom = _room()
    db.post_chat_message(chatroom.uuid, db.get_human_user().uuid, "remind me")
    agent = AssistantAgent(agent_uuid=ASSISTANT_UUID, name="assistant", send=lambda _: None)
    agent._decide_next_step = scripted_decisions(
        AssistantStepDecision(reason="remind", action=AssistantActionName.SET_REMINDER,
                              args={"text": tag, "when": "2026-06-29T09:00"}),
        AssistantStepDecision(reason="reply", action=AssistantActionName.REPLY,
                              args={"message": "awaits your confirmation"}),
    )
    try:
        agent.handle(uuid4(), {"room_uuid": str(chatroom.uuid)})
        msgs = db.list_room_messages(chatroom.uuid)
        reply = next(m for m in msgs
                     if m["sender_type"] == "agent" and m["kind"] == "message"
                     and m["meta"].get("write_intent"))
        assert reply["meta"]["capability"] == "set_reminder"
        assert reply["meta"]["step_link"].startswith("/assistant?id=")
        assert "#step-" in reply["meta"]["step_link"]
        # The intent the card points at really exists and is proposed.
        assert reply["meta"]["intent_state"] == "proposed"
    finally:
        db.db.session.query(db.AssistantWriteIntent).filter(
            db.AssistantWriteIntent.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.AssistantRun).filter(
            db.AssistantRun.room_uuid == chatroom.uuid).delete()
        db.db.session.query(db.Chatroom).filter(db.Chatroom.uuid == chatroom.uuid).delete()
        db.db.session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest agents/test_reminders.py::test_proposal_meta_attached_to_reply -q`
Expected: FAIL — `StopIteration` (no reply carries `meta.write_intent`).

- [ ] **Step 3: Return a `proposal` dict from `_propose_write`**

In `source/agents/assistant.py`, change the `_propose_write` return (lines 1709-1717) to include a `proposal` payload (build `step_link` only when a step uuid exists):

```python
        proposal: dict[str, Any] = {
            "write_intent": str(intent.uuid),
            "capability": cap.name.value,
        }
        if ctx.step_uuid is not None:
            proposal["step_link"] = db.assistant_step_path(self._run.uuid, ctx.step_uuid)
        return AssistantObservation(
            ok=True,
            text=(f"Proposed for the operator's approval: {preview}. "
                  f"This is the end of your job for this request — there is no "
                  f"action you can take to apply it yourself; the operator "
                  f"confirms it. Reply to the operator that it awaits their "
                  f"confirmation, and do not take any further action."),
            data={"write_intent_uuid": str(intent.uuid), "state": "proposed",
                  "proposal": proposal},
        )
```

- [ ] **Step 4: Initialize `pending_proposal` and harvest it**

In `source/agents/assistant.py`, next to `result_links: list[str] = []` (line 1319) add:

```python
            # The card payload for a confirm-tier write proposed this turn, attached
            # as `meta` on the terminal reply so chat can render confirm/reject.
            pending_proposal: dict[str, Any] | None = None
```

In the observation-handling block (after line 1419, near the `result_links` harvest), add:

```python
                proposal = observation.data.get("proposal")
                if proposal:
                    pending_proposal = proposal
```

- [ ] **Step 5: Attach `meta` on the terminal reply**

In `source/agents/assistant.py`, change the terminal post (line 1366):

```python
                    db.post_chat_message(room_uuid, self.agent_uuid, text,
                                         kind="message", meta=pending_proposal)
```

(`post_chat_message` treats `None` as `{}`.)

- [ ] **Step 6: Run test to verify it passes**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest agents/test_reminders.py -q`
Expected: PASS (all reminder tests, including the new one).

- [ ] **Step 7: Run the assistant suite to confirm no regression**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest agents/test_assistant.py agents/test_assistant_writes.py agents/test_assistant_actions.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/agents/assistant.py source/agents/test_reminders.py
git commit -m "feat(assistant): attach write-proposal card meta to the reply"
```

---

## Task 5: Render the proposal card in chat (JS — manual verify)

**Files:**
- Modify: `source/webapp/chat_template.py` (the `makeMessage` JS + a small CSS block)

**Interfaces:**
- Consumes: `m.meta.{write_intent, capability, step_link, intent_state}`; endpoints `POST /chat/api/assistant/write-intents/<uuid>/{confirm,reject}`.
- Produces: a `.write-proposal` card appended to the message body; no new server contract.

- [ ] **Step 1: Locate the message-body assembly**

In `source/webapp/chat_template.py`, find `makeMessage(m)` (~line 632) and the point after the text/markdown body is set but before the feedback buttons are appended (~lines 686-717). The card is appended to the message element there.

- [ ] **Step 2: Add the card renderer and call it**

Add a function (near `makeMessage`) and invoke it after the body is built, guarded on `m.meta`:

```javascript
function renderProposalCard(m) {
  const meta = m.meta || {};
  if (!meta.write_intent) return null;
  const wrap = document.createElement('div');
  wrap.className = 'write-proposal';
  const cap = meta.capability || 'write';
  const state = meta.intent_state || 'proposed';
  const stepLink = meta.step_link
    ? '<a class="wp-step" href="' + escapeAttr(meta.step_link) + '">View step ↗</a>' : '';
  if (state === 'proposed') {
    wrap.innerHTML =
      '<span class="wp-cap">' + escapeHtml(cap) + '</span>' +
      '<button class="wp-confirm">Confirm</button>' +
      '<button class="wp-reject">Reject</button>' + stepLink;
    const base = '/chat/api/assistant/write-intents/' + encodeURIComponent(meta.write_intent) + '/';
    wrap.querySelector('.wp-confirm').addEventListener('click',
      () => proposalAct(wrap, base + 'confirm', cap, meta.step_link));
    wrap.querySelector('.wp-reject').addEventListener('click',
      () => proposalAct(wrap, base + 'reject', cap, meta.step_link));
  } else {
    wrap.innerHTML = proposalStatusHtml(cap, state, meta.step_link);
  }
  return wrap;
}

function proposalStatusHtml(cap, state, stepLink) {
  const link = stepLink
    ? ' <a class="wp-step" href="' + escapeAttr(stepLink) + '">View step ↗</a>' : '';
  const label = {completed: '✓ Confirmed', rejected: '✕ Rejected',
                 failed: '⚠ Failed'}[state] || '… working';
  return '<span class="wp-cap">' + escapeHtml(cap) + '</span>' +
         '<span class="wp-state wp-' + escapeAttr(state) + '">' + label + '</span>' + link;
}

async function proposalAct(wrap, url, cap, stepLink) {
  wrap.querySelectorAll('button').forEach(b => b.disabled = true);
  let j = {};
  try { const r = await fetch(url, {method: 'POST'}); j = await r.json(); }
  catch (e) { j = {ok: false, text: 'network error'}; }
  // Confirm endpoint returns the execution observation; reject returns ok=rejected.
  const isConfirm = url.endsWith('/confirm');
  let state;
  if (isConfirm) state = j.ok ? 'completed' : 'failed';
  else state = j.ok ? 'rejected' : 'proposed';
  wrap.innerHTML = proposalStatusHtml(cap, state, stepLink);
  if (j.text) { const t = document.createElement('div');
    t.className = 'wp-result muted'; t.textContent = j.text; wrap.appendChild(t); }
}
```

Where the message DOM node is assembled, after the body and before returning, add:

```javascript
  const card = renderProposalCard(m);
  if (card) el.appendChild(card);
```

(Use the existing element variable name in `makeMessage`; if there is no `escapeAttr` helper, reuse the page's existing attribute-escaping helper or `escapeHtml` for the href value.)

- [ ] **Step 3: Add minimal CSS**

In the page's `<style>` block in `chat_template.py`, add:

```css
.write-proposal { margin-top:.4rem; padding:.4rem .6rem; border:1px solid #d1d5db;
  border-radius:6px; display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }
.write-proposal .wp-cap { font-weight:600; }
.write-proposal button { cursor:pointer; }
.write-proposal .wp-confirm { background:#2563eb; color:#fff; border:none;
  border-radius:4px; padding:.2rem .6rem; }
.write-proposal .wp-reject { background:#fff; color:#b91c1c; border:1px solid #b91c1c;
  border-radius:4px; padding:.2rem .6rem; }
.write-proposal .wp-completed { color:#15803d; } .write-proposal .wp-rejected { color:#b91c1c; }
.write-proposal .wp-failed { color:#b45309; }
.write-proposal .wp-step { margin-left:auto; font-size:.85em; }
.write-proposal .wp-result { flex-basis:100%; font-size:.85em; }
```

- [ ] **Step 4: Manual verification**

Start the app and exercise the real flow (use the `run` skill or the project's normal launch). In a chat room: ask the assistant "remind me to brush my teeth at 23:45 localtime". Confirm:
1. The assistant's reply shows a card: capability `set_reminder`, **Confirm**/**Reject** buttons, and a **View step ↗** link to `/assistant?id=…#step-…`.
2. Clicking **View step** opens the run scrolled to that step.
3. Clicking **Confirm** flips the card to `✓ Confirmed` and shows the result text; reloading the page keeps it confirmed (intent_state from the server).
4. In a second reminder, clicking **Reject** flips to `✕ Rejected`; reload keeps it.
5. Confirming a reminder from `/assistant` instead, then reloading chat, shows the card already `✓ Confirmed`.

- [ ] **Step 5: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/webapp/chat_template.py
git commit -m "feat(chat): render confirm/reject card for write proposals"
```

---

## Task 6: `cron_job.origin_*` columns + `cron_create_one_shot_message(origin_*)`

**Files:**
- Modify: `source/db/models.py` (`CronJob`, near other nullable columns ~line 277)
- Modify: `source/db/__init__.py` (init_db migration block)
- Modify: `source/db/cron.py:866-881` (`cron_create_one_shot_message`)
- Test: `source/webapp/test_cron_api.py` (extend)

**Interfaces:**
- Produces: `CronJob.origin_run_uuid: Mapped[UUID | None]`, `CronJob.origin_step_uuid: Mapped[UUID | None]`. `cron_create_one_shot_message(..., origin_run_uuid: UUID | None = None, origin_step_uuid: UUID | None = None)` stores them.

- [ ] **Step 1: Write the failing test**

Append to `source/webapp/test_cron_api.py` (`fire_at` stays required — pass it explicitly):

```python
def test_one_shot_message_stores_origin(app_ctx, cron_tree_snapshot):
    from datetime import UTC, datetime, timedelta
    run, step = uuid4(), uuid4()
    job = db.cron_create_one_shot_message(
        message="⏰ Reminder: x", fire_at=datetime.now(UTC) + timedelta(hours=1),
        origin_run_uuid=run, origin_step_uuid=step)
    fetched = db.db.session.get(db.CronJob, job.id)
    assert fetched.origin_run_uuid == run
    assert fetched.origin_step_uuid == step


def test_one_shot_message_origin_defaults_null(app_ctx, cron_tree_snapshot):
    from datetime import UTC, datetime, timedelta
    job = db.cron_create_one_shot_message(
        message="⏰ Reminder: y", fire_at=datetime.now(UTC) + timedelta(hours=1))
    fetched = db.db.session.get(db.CronJob, job.id)
    assert fetched.origin_run_uuid is None and fetched.origin_step_uuid is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest webapp/test_cron_api.py -k origin -q`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'origin_run_uuid'` (or attribute error).

- [ ] **Step 3: Add the model columns**

In `source/db/models.py`, inside `class CronJob`, add (near the other nullable columns):

```python
    # Provenance: the assistant run+step that created this job (e.g. a reminder via
    # set_reminder). Null for manually-created jobs. Surfaced read-only on /cron.
    origin_run_uuid: Mapped[UUID | None] = mapped_column(default=None)
    origin_step_uuid: Mapped[UUID | None] = mapped_column(default=None)
```

- [ ] **Step 4: Add the migrations for existing DBs**

In `source/db/__init__.py`, in the init_db migration block, add:

```python
        # Assistant provenance for jobs created by the assistant (reminders).
        _add_column_if_missing("cron_job", "origin_run_uuid", "origin_run_uuid uuid")
        _add_column_if_missing("cron_job", "origin_step_uuid", "origin_step_uuid uuid")
```

- [ ] **Step 5: Thread origin through `cron_create_one_shot_message`**

In `source/db/cron.py`, update the signature and the `CronJob(...)` construction (lines 866-881):

```python
def cron_create_one_shot_message(
    *, message: str, fire_at: datetime, target: str = "", name: str = "",
    folder_uuid: UUID | None = None,
    origin_run_uuid: UUID | None = None, origin_step_uuid: UUID | None = None,
) -> CronJob:
```

```python
    job = CronJob(
        name=name or "Reminder", enabled=True, folder_uuid=folder_uuid,
        cron_expr="", timezone="localtime", action_type="message",
        target=target, message=message, next_run_at=fire_at,
        origin_run_uuid=origin_run_uuid, origin_step_uuid=origin_step_uuid,
    )
```

Keep `fire_at` required (the Step 1 tests already pass it). Do not add a `fire_at` default — firing semantics are unchanged.

- [ ] **Step 6: Run test to verify it passes**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest webapp/test_cron_api.py -k origin -q`
Expected: PASS.

- [ ] **Step 7: Run the cron + reminder suites**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest webapp/test_cron_api.py db/test_cron_firing.py agents/test_reminders.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/db/models.py source/db/__init__.py source/db/cron.py source/webapp/test_cron_api.py
git commit -m "feat(cron): store assistant origin (run+step) on one-shot jobs"
```

---

## Task 7: `_action_set_reminder` records origin on execution

**Files:**
- Modify: `source/agents/assistant.py` (`_action_set_reminder`, lines 770-800)
- Test: `source/agents/test_reminders.py` (extend)

**Interfaces:**
- Consumes: `ctx.step_uuid`, `db.session.get(AssistantStep, ...)`, `db.cron_create_one_shot_message(origin_*)`.
- Produces: on real execution, the created cron job has `origin_run_uuid`/`origin_step_uuid` set from the proposing step.

- [ ] **Step 1: Write the failing test**

Append to `source/agents/test_reminders.py`:

```python
def test_set_reminder_records_origin_from_step(app_ctx):
    from agents.config import ASSISTANT_UUID as _A
    room = uuid4()
    run = db.start_assistant_run(journal_id=uuid4(), room_uuid=room, agent_uuid=_A)
    step = db.append_assistant_step(
        run_uuid=run.uuid, step_index=0, phase="observed", action="set_reminder")
    tag = f"rem-{uuid4()}"
    ctx = AssistantActionContext(
        journal_id=None, room_uuid=room, agent_uuid=_A, step_index=0,
        step_uuid=step.uuid,
    )
    obs = _action_set_reminder(ctx, {"text": tag, "when": "2026-06-29T09:00"})
    try:
        assert obs.ok is True
        job = _jobs_with(tag)[0]
        assert job.origin_run_uuid == run.uuid
        assert job.origin_step_uuid == step.uuid
    finally:
        _cleanup_cron(tag)
        db.db.session.query(db.AssistantRun).filter(db.AssistantRun.uuid == run.uuid).delete()
        db.db.session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest agents/test_reminders.py::test_set_reminder_records_origin_from_step -q`
Expected: FAIL — `assert None == UUID(...)` (origin not stored yet).

- [ ] **Step 3: Implement origin derivation**

In `source/agents/assistant.py`, in `_action_set_reminder`, replace the real-execution `cron_create_one_shot_message` call (lines 793-796) with origin derivation:

```python
    origin_run_uuid = None
    if ctx.step_uuid is not None:
        step = db.db.session.get(db.AssistantStep, ctx.step_uuid)
        origin_run_uuid = step.run_uuid if step is not None else None
    job = db.cron_create_one_shot_message(
        message=f"⏰ Reminder: {text}", fire_at=fire_at, target=str(ctx.room_uuid),
        name=f"Reminder: {text[:40]}",
        origin_run_uuid=origin_run_uuid, origin_step_uuid=ctx.step_uuid,
    )
```

Confirm `AssistantStep` is reachable as `db.AssistantStep` (it is re-exported from `db.models`). If `db.db.session` is not the in-module idiom, use the same session accessor the file already uses for reads.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest agents/test_reminders.py -q`
Expected: PASS (all reminder tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/agents/assistant.py source/agents/test_reminders.py
git commit -m "feat(assistant): record creating run+step on a reminder's cron job"
```

---

## Task 8: Surface `origin_step_link` on `/cron`

**Files:**
- Modify: `source/db/cron.py` (`cron_load_tree` job dict, ~lines 91-110)
- Modify: `source/webapp/cron_views.py:211-238` (job-detail panel markup)
- Modify: `source/static/cron.js:516-532` (`cronRenderJobDetail`)
- Test: `source/webapp/test_cron_api.py` (extend)

**Interfaces:**
- Consumes: `db.assistant_step_path`, `CronJob.origin_run_uuid`/`origin_step_uuid`.
- Produces: each `cron_load_tree` job dict gains `"origin_step_link": str | None`.

- [ ] **Step 1: Write the failing test**

Append to `source/webapp/test_cron_api.py`:

```python
def test_cron_load_tree_exposes_origin_step_link(app_ctx, cron_tree_snapshot):
    from datetime import UTC, datetime, timedelta
    run, step = uuid4(), uuid4()
    job = db.cron_create_one_shot_message(
        message="⏰ Reminder: z", fire_at=datetime.now(UTC) + timedelta(hours=1),
        origin_run_uuid=run, origin_step_uuid=step)
    out = db.cron_load_tree()
    row = next(j for j in out["jobs"] if j["uuid"] == str(job.uuid))
    assert row["origin_step_link"] == db.assistant_step_path(run, step)

    plain = db.cron_create_one_shot_message(
        message="⏰ Reminder: nolink", fire_at=datetime.now(UTC) + timedelta(hours=1))
    out2 = db.cron_load_tree()
    prow = next(j for j in out2["jobs"] if j["uuid"] == str(plain.uuid))
    assert prow["origin_step_link"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest webapp/test_cron_api.py -k origin_step_link -q`
Expected: FAIL — `KeyError: 'origin_step_link'`.

- [ ] **Step 3: Add `origin_step_link` to the job dict**

In `source/db/cron.py`, in the `cron_load_tree` jobs comprehension (after the `next_run_at` line ~109), add:

```python
                # Provenance deep-link to the /assistant step that created the job
                # (reminders); null for manual jobs. Read-only — ignored on save.
                "origin_step_link": (
                    assistant_step_path(j.origin_run_uuid, j.origin_step_uuid)
                    if j.origin_run_uuid and j.origin_step_uuid else None
                ),
```

Ensure `assistant_step_path` is imported in `cron.py` (add `from db.assistant import assistant_step_path` near the top, or reference via the `db` package as the file already does for other helpers).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest webapp/test_cron_api.py -k origin_step_link -q`
Expected: PASS.

- [ ] **Step 5: Add the Origin row to the job-detail panel markup**

In `source/webapp/cron_views.py`, inside `<div id="cron-job-detail" ...>` (after the Description section, before Health, ~line 233), add:

```html
  <div class="cjd-section" id="cjd-origin-section" hidden>
    <div class="cjd-label">Origin</div>
    <div class="cjd-value">created by assistant — <a id="cjd-origin-link" href="#">View step ↗</a></div>
  </div>
```

- [ ] **Step 6: Populate it in `cronRenderJobDetail`**

In `source/static/cron.js`, in `cronRenderJobDetail` (after the description line ~529, before `cronLoadHealth`), add:

```javascript
  const originSec = document.getElementById('cjd-origin-section');
  if (r.origin_step_link) {
    document.getElementById('cjd-origin-link').href = r.origin_step_link;
    originSec.hidden = false;
  } else {
    originSec.hidden = true;
  }
```

- [ ] **Step 7: Manual verification**

Start the app. Create a reminder via chat and **Confirm** it. Open `/cron`, select the reminder job, open **Details**. Confirm an **Origin** row appears with a **View step ↗** link that opens `/assistant?id=…#step-…` scrolled to the creating step. Confirm a manually-created cron job shows **no** Origin row.

- [ ] **Step 8: Run the cron view/api suites**

Run: `cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest webapp/test_cron_api.py webapp/test_cron_views.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd /Users/neoneye/git/rainbox
git add source/db/cron.py source/webapp/cron_views.py source/static/cron.js source/webapp/test_cron_api.py
git commit -m "feat(cron): show assistant origin step link on job details"
```

---

## Task 9: Full-suite regression + finish

- [ ] **Step 1: Run the affected suites together**

Run:
```bash
cd source && DATABASE_URL=postgresql+psycopg://localhost/rainbox_claude ./venv/bin/python -m pytest \
  db/test_assistant_step_link.py db/test_chat_meta.py agents/test_reminders.py \
  agents/test_assistant.py agents/test_assistant_writes.py agents/test_assistant_actions.py \
  webapp/test_cron_api.py webapp/test_cron_views.py webapp/test_chat_views.py \
  webapp/test_assistant_write_intent_api.py webapp/test_assistant_step_admin_link.py -q
```
Expected: all PASS.

- [ ] **Step 2: Manual end-to-end smoke**

Confirm the full story once: ask for a reminder → card with Confirm/Reject + View step in chat → Confirm → reminder fires at the right local time (or appears scheduled on `/cron`) → `/cron` job details show the Origin step link.

- [ ] **Step 3: Push the branch and open a PR (only if the user asks)**

Do not push or open a PR unless the user requests it (per repo workflow). When asked:
```bash
cd /Users/neoneye/git/rainbox
git push -u origin feat/chat-write-proposal-ui
```

---

## Self-review notes (for the implementer)

- The `meta` column default is `{}` at both the model layer (`default=dict`) and the DB layer (`DEFAULT '{}'::jsonb NOT NULL`); never store `None`.
- `intent_state` is computed at read time only — it is never persisted into `meta`, so the stored proposal payload stays `{write_intent, capability, step_link}`.
- `origin_step_link` is read-only output of `cron_load_tree`; the save path (`validate_cron_tree`/`cron_save_tree`) does not read it, so no save-path change is needed.
- At most one confirm-tier write is proposed per turn (the loop steers to `reply` right after a proposal); `pending_proposal` is a single slot by design.
