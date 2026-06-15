# UI Modals (app-wide overlay pattern)

Rainbox uses a small, dependency-free modal system: one shared backdrop, any
number of centered "card" overlays, and a handful of helper functions for
opening, closing, and guarding dismissal. The same UX is meant to apply on
**every** page that needs a dialog — `/chat`, `/cron`, `/kanban`, and anything
added later.

This document is the **canonical spec** for that pattern, written against a
neutral `ui-modal` naming. It is page-agnostic: no single page "owns" it.
`/chat` already uses it; `/cron` and `/kanban` still ship their own prefixes
(`cron-modal` / `cron-edit-modal`, `kb-modal`) — converging them onto `ui-modal`
is tracked in [Adopting the standard](#adopting-the-standard) at the bottom.

There are no third-party libraries and no `<dialog>` element — it's plain DOM,
which keeps the idle-tab guarantees in
[`chat-frontend-rules.md`](chat-frontend-rules.md) intact, and avoids the
native `prompt`/`confirm`/`alert` dialogs that `/kanban` and `/cron` already
ban.

> The reference implementation lives in `webapp/chat_template.py` — `/chat`
> already uses the `ui-modal` naming and the dirty-guarded dismissal described
> below, with its modal JS inline in that file. Use it as the working reference
> while reading the snippets here.

## The three pieces

### 1. One shared backdrop, many cards

There is a **single** backdrop element and **one card per dialog**. The cards
are *siblings* of the backdrop, never children — this is load-bearing (see
"Why cards are siblings" below).

```html
<div class="ui-modal-backdrop" id="ui-modal-backdrop" hidden></div>

<div class="ui-modal" id="folder-modal" hidden> … </div>
<div class="ui-modal" id="delete-modal" hidden> … </div>
<div class="ui-modal" id="room-modal"   hidden> … </div>
```

Each card follows the same skeleton: a title, the body (inputs), and a
right-aligned `.modal-actions` row whose last button is the primary/confirm
action.

```html
<div class="ui-modal" id="room-modal" hidden>
  <h3>New chatroom</h3>
  <input type="text" id="room-input" placeholder="Room name" autocomplete="off">
  <!-- …optional extra body, e.g. an agent checklist… -->
  <div class="modal-actions">
    <button type="button" class="btn-cancel"  id="room-cancel">Cancel</button>
    <button type="button" class="btn-primary" id="room-create" disabled>Create</button>
  </div>
</div>
```

Visibility is controlled entirely by the boolean `hidden` attribute — both the
backdrop CSS (`.ui-modal-backdrop[hidden]{display:none}`) and the card CSS
(`.ui-modal[hidden]{display:none}`) key off it. No `.hidden` class, no
`style.display` juggling.

### 2. The CSS

The backdrop is a fixed full-viewport scrim; each card is fixed and centered
via the `translate(-50%,-50%)` trick. The backdrop sits at `z-index:1500`, the
cards at `1600`, so a card always renders above the scrim.

```css
.ui-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.35);z-index:1500}
.ui-modal-backdrop[hidden]{display:none}
.ui-modal{position:fixed;z-index:1600;left:50%;top:50%;transform:translate(-50%,-50%);
          background:#fff;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,0.25);
          padding:1.2em 1.3em;width:min(420px,92vw)}
.ui-modal[hidden]{display:none}
.ui-modal h3{margin:0 0 0.6em;font-size:1.05rem}
.ui-modal input[type=text]{width:100%;box-sizing:border-box;padding:0.5em;border:1px solid #ccc;
                           border-radius:6px;font:inherit}
.ui-modal .modal-actions{display:flex;justify-content:flex-end;gap:0.5em;margin-top:1em}
.ui-modal button{border:none;border-radius:6px;padding:0.45em 1em;cursor:pointer;font:inherit}
.ui-modal .btn-cancel{background:#e5e7eb;color:#374151}
.ui-modal .btn-primary{background:#2563eb;color:#fff}
.ui-modal .btn-danger{background:#dc2626;color:#fff}   /* destructive confirm */
.ui-modal button:disabled{opacity:0.5;cursor:default}
```

Wider cards (e.g. a markdown editor) override just the width inline or with a
modifier class: `<div class="ui-modal" style="width:min(760px,94vw)">`.

### 3. The JS: one open/close trio per dialog

Each dialog gets three small functions:

- **`openXModal(opts)`** — populate fields from `opts`, reset the confirm
  button to `disabled`, show the backdrop *then* the card, and `focus()` the
  first input. Lazy-load any remote data here (e.g. the chat room modal fetches
  the agent list on first open).
- **`closeXModal()`** — hide the card, hide the backdrop, and clear any
  module-level state for that dialog (e.g. `folderModalState = null`).
- **`confirmXModal()`** — read + validate input, perform the action (usually a
  `fetch`/`postJSON`), then `closeXModal()` on success or surface the error on
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
function closeOpenModal(){
  if (!document.getElementById('folder-modal').hidden) closeFolderModal();
  if (!document.getElementById('delete-modal').hidden) closeDeleteModal();
  if (!document.getElementById('room-modal').hidden)   closeRoomModal();
}

// Has the user touched the currently open modal?  (Per-modal definition.)
function openModalDirty(){
  if (!document.getElementById('folder-modal').hidden){
    return document.getElementById('folder-input').value !== ((folderModalState && folderModalState.current) || '');
  }
  if (!document.getElementById('delete-modal').hidden){
    return document.getElementById('delete-input').value !== '';
  }
  if (!document.getElementById('room-modal').hidden){
    return document.getElementById('room-input').value !== ''
        || agentListEl.querySelectorAll('input:checked').length > 0;
  }
  return false;
}

// Backdrop-click / Esc: dismiss only when untouched.
function dismissOpenModalIfClean(){
  if (!openModalDirty()) closeOpenModal();
}
document.getElementById('ui-modal-backdrop').addEventListener('click', dismissOpenModalIfClean);
document.addEventListener('keydown', e => { if (e.key === 'Escape') dismissOpenModalIfClean(); });
```

"Dirty" is defined **per modal**, not generically — a few examples from `/chat`:

| Modal        | Considered dirty when…                                            |
|--------------|------------------------------------------------------------------|
| New chatroom | a name is typed **or** any agent checkbox is checked             |
| New folder   | the name differs from its initial value (covers create & rename) |
| Delete       | the type-to-confirm box is non-empty                            |

When adding a new modal, extend both `closeOpenModal()` and `openModalDirty()`
with a branch for it.

## Why cards are siblings of the backdrop

The backdrop's `click` handler fires for clicks anywhere on the scrim. Because
each card is a *sibling* of the backdrop (not nested inside it), a click landing
on a card does **not** bubble up to the backdrop, so it won't trigger a
dismiss. If you instead nest the card inside the backdrop, every in-card click
bubbles to the backdrop and you'd need a `stopPropagation()` / `e.target ===
backdrop` workaround. Keep them siblings and the dismissal logic stays trivial.

This is also why a **single** shared backdrop is preferred over one backdrop per
modal: with sibling cards, one scrim and one pair of dismiss handlers cover
every dialog on the page.

## Adopting the standard

All three pages now use the `ui-modal` naming and the dirty-guarded dismissal.
This section records where each started and what changed, so the convergence is
auditable.

A wrinkle worth knowing before adding the next modal: the pages keep their modal
JS in different places. `/chat`'s is inline in `chat_template.py`, while
`/kanban` and `/cron` load theirs from `static/kanban.js` and `static/cron.js`.
On those two pages a new modal is a two-file change — markup/CSS in the view,
behavior in the static JS — and `/cron` also has assertions in
`webapp/test_cron_views.py` that reference modal markup, so update them in step.

### Where each page stands today

| Page      | Source                      | Prefix(es)                                   | Backdrop                                            | Title | Dirty-guard? |
|-----------|-----------------------------|----------------------------------------------|-----------------------------------------------------|-------|--------------|
| `/chat`   | `webapp/chat_template.py`   | ✅ `ui-modal`, `ui-modal-backdrop`           | **single** shared backdrop                          | `h3`  | **yes**      |
| `/cron`   | `cron_views.py` + `static/cron.js` | ✅ `ui-modal`, `ui-modal-backdrop` (was 5 backdrops) | **single** shared backdrop (collapsed from 5) | — | **yes** |
| `/kanban` | `kanban_views.py` + `static/kanban.js` | ✅ `ui-modal`, `ui-modal-backdrop` (`kb-row` kept internal) | **single** shared backdrop          | `h3`  | **yes**      |

### Changes required to converge

**Shared / design-system level**

1. Pick one home for the canonical CSS block (a shared `<style>` partial or a
   small static stylesheet) so all three pages link the same `.ui-modal*` rules
   instead of each re-declaring them. Until that exists, copy the CSS block
   above into each page verbatim under the `ui-modal` prefix.
2. Standardize the card title tag (this doc uses `h3`) and the action row
   (`.modal-actions` with trailing primary button). `/kanban` currently uses
   `h2` + `kb-row`; `/cron` is mixed.

**`/chat` (`webapp/chat_template.py`)** — ✅ **Done.** Renamed `chat-modal` →
`ui-modal` and `chat-modal-backdrop` → `ui-modal-backdrop` across CSS, markup,
and JS, and renamed the dismissal helpers to the page-neutral names
(`closeOpenModal` / `openModalDirty` / `dismissOpenModalIfClean`). `/chat`'s
modal JS is inline in this file, so it was a single-file change.

**`/cron` (`webapp/cron_views.py` + `static/cron.js` + `webapp/test_cron_views.py`)** — ✅ **Done.**

5. Collapsed the **five** per-group backdrops (`cron-modal-backdrop`,
   `cron-edit-backdrop`, `cron-delete-backdrop`, `cron-desc-backdrop`,
   `cron-folder-backdrop`) into **one** shared `ui-modal-backdrop`, every card a
   sibling of it. All `getElementById('cron-*-backdrop')` calls in `cron.js`
   were repointed to the single backdrop.
6. Renamed `cron-as-modal` / `cron-edit-modal` cards to `ui-modal` (the New-job
   builder is `class="builder ui-modal"`, keeping a `.builder.ui-modal` rule for
   its wider `min(640px,92vw)` so it doesn't shrink to the 420px default). The
   `cron-as-modal` class toggle in `cron.js` was removed since the class is now
   static.
7. Added the dirty-guarded backdrop-click / Esc dismissal: New-job builder dirty
   when any field changed from its snapshot; Edit schedule/action/description
   dirty when changed from the loaded value; New folder dirty when the name box
   is non-empty; Delete dirty only when its type-to-confirm box is non-empty.
   The pre-existing kebab-menu Esc handler in `cron.js` was left intact.
8. Updated `test_cron_views.py` assertions from the old `cron-modal-backdrop` /
   `cron-as-modal` tokens to `ui-modal-backdrop` / `builder ui-modal`.

(`/cron`'s modals use their own title markup rather than `h2`/`h3`, so the
title-tag standardization didn't apply here.)

**`/kanban` (`webapp/kanban_views.py` + `static/kanban.js`)** — ✅ **Done.**
Renamed `kb-modal` → `ui-modal` and `kb-backdrop` → `ui-modal-backdrop`
(id + class), switched modal titles `h2` → `h3`, and added the dirty-guarded
backdrop-click / Esc dismissal (board + task modals guard on their text fields;
the read-only markdown view and the confirm modal are never dirty). The `kb-row`
form-row layout was kept as a page-internal class, now scoped under `.ui-modal`.
The markup/CSS lives in `kanban_views.py` but the modal JS is in
`static/kanban.js`, so this spanned two files.

### Remaining cosmetic follow-ups (optional)

The structural + behavioral convergence (shared `ui-modal` classes, single
backdrop, dirty-guarded dismissal) is complete on all three pages. Left as
optional polish, none of which changes behavior:

- A single shared home for the `.ui-modal*` CSS (a partial or static stylesheet)
  instead of each page re-declaring the block.
- Unifying the action-button row on `.modal-actions` + `.btn-*` everywhere
  (`/kanban` still uses its internal `kb-row`; `/cron` keeps its own builder
  layout).
- A consistent title tag across pages (`/chat` and `/kanban` use `h3`; `/cron`
  uses its own title markup).
