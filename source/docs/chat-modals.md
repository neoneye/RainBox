# Chat Modals (reusable overlay pattern)

The `/chat` page uses a small, dependency-free modal system: one shared
backdrop, any number of centered "card" overlays, and a handful of helper
functions for opening, closing, and guarding dismissal. It powers the **New
chatroom**, **New folder**, and **Delete** dialogs.

This document describes that pattern so it can be reused verbatim on `/cron`
and `/kanban`. All of it lives in `webapp/chat_template.py` (markup + CSS +
inline JS). There are no third-party libraries and no `<dialog>` element — it's
plain DOM, which keeps the idle-tab guarantees in
[`chat-frontend-rules.md`](chat-frontend-rules.md) intact.

> Note: `/cron` already ships a near-identical pattern under the `cron-modal`
> prefix (`webapp/cron_views.py`). This doc is the canonical description; when
> reconciling the two, prefer these conventions and rename the cron classes to
> match, or factor a shared partial.

## The three pieces

### 1. One shared backdrop, many cards

There is a **single** backdrop element and **one card per dialog**. The cards
are *siblings* of the backdrop, never children — this is load-bearing (see
"Why cards are siblings" below).

```html
<div class="chat-modal-backdrop" id="chat-modal-backdrop" hidden></div>

<div class="chat-modal" id="chat-folder-modal" hidden> … </div>
<div class="chat-modal" id="chat-delete-modal" hidden> … </div>
<div class="chat-modal" id="chat-room-modal" hidden> … </div>
```

Each card follows the same skeleton: a title, the body (inputs), and a
right-aligned `.modal-actions` row whose last button is the primary/confirm
action.

```html
<div class="chat-modal" id="chat-room-modal" hidden>
  <h3>New chatroom</h3>
  <input type="text" id="chat-room-input" placeholder="Room name" autocomplete="off">
  <!-- …optional extra body, e.g. an agent checklist… -->
  <div class="modal-actions">
    <button type="button" class="btn-cancel"  id="chat-room-cancel">Cancel</button>
    <button type="button" class="btn-primary" id="chat-room-create" disabled>Create</button>
  </div>
</div>
```

Visibility is controlled entirely by the boolean `hidden` attribute — both the
backdrop CSS (`.chat-modal-backdrop[hidden]{display:none}`) and the card CSS
(`.chat-modal[hidden]{display:none}`) key off it. No `.hidden` class, no
`style.display` juggling.

### 2. The CSS

The backdrop is a fixed full-viewport scrim; each card is fixed and centered
via the `translate(-50%,-50%)` trick. The backdrop sits at `z-index:1500`, the
cards at `1600`, so a card always renders above the scrim.

```css
.chat-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.35);z-index:1500}
.chat-modal-backdrop[hidden]{display:none}
.chat-modal{position:fixed;z-index:1600;left:50%;top:50%;transform:translate(-50%,-50%);
            background:#fff;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,0.25);
            padding:1.2em 1.3em;width:min(420px,92vw)}
.chat-modal[hidden]{display:none}
.chat-modal h3{margin:0 0 0.6em;font-size:1.05rem}
.chat-modal input[type=text]{width:100%;box-sizing:border-box;padding:0.5em;border:1px solid #ccc;
                             border-radius:6px;font:inherit}
.chat-modal .modal-actions{display:flex;justify-content:flex-end;gap:0.5em;margin-top:1em}
.chat-modal button{border:none;border-radius:6px;padding:0.45em 1em;cursor:pointer;font:inherit}
.chat-modal .btn-cancel{background:#e5e7eb;color:#374151}
.chat-modal .btn-primary{background:#2563eb;color:#fff}
.chat-modal .btn-danger{background:#dc2626;color:#fff}   /* destructive confirm */
.chat-modal button:disabled{opacity:0.5;cursor:default}
```

When porting to `/cron` or `/kanban`, copy this block and rename the prefix
(e.g. `kanban-modal`). Keep the `z-index` ordering and the
`[hidden]`-drives-`display:none` convention.

### 3. The JS: one open/close trio per dialog

Each dialog gets three small functions modeled on the folder modal:

- **`openXModal(opts)`** — populate fields from `opts`, reset the confirm
  button to `disabled`, show the backdrop *then* the card, and `focus()` the
  first input. Lazy-load any remote data here (the room modal fetches the agent
  list on first open).
- **`closeXModal()`** — hide the card, hide the backdrop, and clear any
  module-level state for that dialog (e.g. `folderModalState = null`).
- **`confirmXModal()`** — read + validate input, perform the action (usually a
  `fetch`/`postJSON`), then `closeXModal()` on success or `alert(...)` on
  failure.

Wiring per dialog, all `type="button"` (no native form submit):

```js
trigger.addEventListener('click', openXModal);                  // the "+ New …" button
document.getElementById('x-cancel').addEventListener('click', closeXModal);
document.getElementById('x-confirm').addEventListener('click', confirmXModal);
// confirm enabled only when input is non-empty:
xInput.addEventListener('input', e => { confirmBtn.disabled = !e.target.value.trim(); });
// Enter confirms from within the input:
xInput.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); confirmXModal(); } });
```

## Dismissal: three ways out, one of them guarded

A modal can be dismissed by:

1. **Cancel button** — explicit, *always* closes (calls `closeXModal()`
   directly).
2. **Clicking the backdrop** (outside the card).
3. **Pressing Esc.**

Paths (2) and (3) are the *accidental* ones, so they are **guarded**: they only
dismiss when the modal is "clean" (the user hasn't typed or checked anything).
This prevents an errant click/keystroke from discarding entered data. The
Cancel button is the deliberate escape hatch and is never guarded.

```js
// Close whichever modal is open; each close fn clears its own state.
function closeOpenChatModal(){
  if (!document.getElementById('chat-folder-modal').hidden) closeFolderModal();
  if (!document.getElementById('chat-delete-modal').hidden) closeDeleteModal();
  if (!document.getElementById('chat-room-modal').hidden)   closeRoomModal();
}

// Has the user touched the currently open modal?  (Per-modal definition.)
function openChatModalDirty(){
  if (!document.getElementById('chat-folder-modal').hidden){
    return document.getElementById('chat-folder-input').value !== ((folderModalState && folderModalState.current) || '');
  }
  if (!document.getElementById('chat-delete-modal').hidden){
    return document.getElementById('chat-delete-input').value !== '';
  }
  if (!document.getElementById('chat-room-modal').hidden){
    return document.getElementById('chat-room-input').value !== ''
        || agentListEl.querySelectorAll('input:checked').length > 0;
  }
  return false;
}

// Backdrop-click / Esc: dismiss only when untouched.
function dismissOpenChatModalIfClean(){
  if (!openChatModalDirty()) closeOpenChatModal();
}
document.getElementById('chat-modal-backdrop').addEventListener('click', dismissOpenChatModalIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') dismissOpenChatModalIfClean(); });
```

"Dirty" is defined **per modal**, not generically:

| Modal        | Considered dirty when…                                            |
|--------------|------------------------------------------------------------------|
| New chatroom | a name is typed **or** any agent checkbox is checked             |
| New folder   | the name differs from its initial value (covers create & rename) |
| Delete       | the type-to-confirm box is non-empty                            |

When adding a new modal, extend both `closeOpenChatModal()` and
`openChatModalDirty()` with a branch for it.

## Why cards are siblings of the backdrop

The backdrop's `click` handler fires for clicks anywhere on the scrim. Because
each card is a *sibling* of the backdrop (not nested inside it), a click landing
on a card does **not** bubble up to the backdrop, so it won't trigger a
dismiss. If you instead nest the card inside the backdrop, every in-card click
bubbles to the backdrop and you'd need a `stopPropagation()` / `e.target ===
backdrop` workaround. Keep them siblings and the dismissal logic stays trivial.

## Porting checklist for /cron and /kanban

1. Add **one** `*-modal-backdrop` element and one `*-modal` card per dialog,
   cards as siblings of the backdrop, controlled by the `hidden` attribute.
2. Copy the CSS block, renaming the prefix; preserve `z-index` 1500 (backdrop)
   < 1600 (card) and the `[hidden]{display:none}` rules.
3. Give each dialog an `open/close/confirm` trio; confirm button starts
   `disabled` and enables on non-empty input; Enter confirms.
4. Add the shared `closeOpen*Modal()` + `open*ModalDirty()` + backdrop-click /
   Esc handlers. Define "dirty" per modal so accidental dismiss can't lose data.
5. Cancel always closes; backdrop-click and Esc close only when clean.
