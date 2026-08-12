# UI Kebab Menu (app-wide overflow-menu pattern)

The 3-dot overflow ("kebab") menu used on tree nodes, board items, and chat
messages across the UI. This document is the canonical spec — the same
behavior applies on every page, and a new kebab menu should follow it rather
than re-invent positioning.

## Where they live

| Page      | Menus                                   | Code                                             |
|-----------|-----------------------------------------|--------------------------------------------------|
| `/chat`   | message (…), room, folder              | `webapp/chat_template.py` (inline, `placeMenu`)  |
| `/prompt` | folder + prompt tree nodes              | `static/prompt.js` (`promptPlaceMenu`)           |
| `/git`    | folder + repo tree nodes                | `static/git.js` (`gitPlaceMenu`)                 |
| `/cron`   | folder + job tree nodes                 | `static/cron.js` (`cronPlaceMenu`)               |
| `/kanban` | board items                             | `static/kanban.js` (`kbPlaceMenu`)               |
| `/memory` | claim tree leaves                       | `static/memory.js` (inline clamp in `openClaimMenu`) |

Each page's JS is self-contained (no shared static JS module), so the helper
is duplicated per page under the page's naming prefix. Keep the copies
identical in behavior; this spec is the reference.

## The pattern

### Markup + stacking

- The kebab is a `<button>` (`aria-label`, `aria-haspopup="menu"`); the menu
  is a `div` with `role="menu"` holding `<button class="item">` rows
  (`role="menuitem"`; destructive items add `.danger`).
- The menu is **`position:fixed`** with a high `z-index` (1000), so it
  overlays neighbouring rows/columns instead of being clipped by an
  `overflow:auto` ancestor or losing a stacking fight to later siblings.
- Coordinates are set in JS from the kebab's `getBoundingClientRect()` at
  open time — never from layout offsets.
- If an ancestor creates a stacking context (e.g. `opacity` on /chat debug
  bubbles), reparent the menu to `<body>` before showing it so its z-index
  competes in the root stacking context (see `buildMessageMenu` in
  `chat_template.py`).
- Beware transformed ancestors: a `transform` on any ancestor becomes the
  containing block for `position:fixed` and breaks viewport anchoring (this
  is why /chat centers the kebab with flex, not translate).

### Positioning: always inside the viewport

Never place a menu at "anchor bottom + 4" unconditionally — a kebab near the
bottom of the viewport (the newest chat message, the last node of a long
tree) would push the menu off-screen. Every page uses this clamp:

```js
// Position a fixed kebab menu near its anchor, clamped inside the viewport:
// below the anchor when it fits, flipped above when it would overflow the
// bottom edge. Unhides the menu first so offsetWidth/Height are measurable.
function placeMenu(menu, anchorRect){
  menu.hidden = false;
  const margin = 6;
  const left = Math.max(margin,
    Math.min(anchorRect.left, window.innerWidth - menu.offsetWidth - margin));
  let top = anchorRect.bottom + 4;
  if (top + menu.offsetHeight > window.innerHeight - margin){
    top = anchorRect.top - menu.offsetHeight - 4;  // flip above the anchor
  }
  menu.style.left = left + 'px';
  menu.style.top = Math.max(margin, top) + 'px';
}
```

The rules, in order:

1. **Measure the real menu.** Unhide before positioning so `offsetWidth` /
   `offsetHeight` reflect the actual item count — menus vary (Retry only in
   direct rooms, status-dependent items on /memory).
2. **Prefer below the anchor** (`bottom + 4px`), left edges aligned. /chat's
   message menu aligns **right** edges instead (`anchorRect.right -
   menu.offsetWidth`) so it doesn't overflow the message column — that's the
   `alignRight` flag on its `placeMenu`; the clamps are the same.
3. **Flip above when below doesn't fit** within `window.innerHeight` minus a
   6px margin. Flip, don't slide: keeping the menu adjacent to its anchor
   preserves the connection between click and menu.
4. **Clamp horizontally** to `[margin, innerWidth - width - margin]`.
5. **Final safety clamp** `top >= margin`, so even a menu taller than both
   directions stays reachable from the top.

### Open/close behavior

- Opening a menu first hides **all** other open menus of that page's class
  (`document.querySelectorAll('.x-menu').forEach(m => m.hidden = true)`), so
  at most one is open.
- The kebab's click handler calls `e.stopPropagation()`; a document-level
  click handler and an Escape keydown handler dismiss any open menu.
- Item clicks `stopPropagation()`, hide the menu, then act. Items that copy
  (`Copy … id`) confirm via the page toast or a brief in-item "Copied" flash.

## Checking consistency

Grep for positioning done without the clamp:

```
grep -n "bottom + 4" static/*.js webapp/chat_template.py
```

Every hit should be inside a `*PlaceMenu` helper (or /memory's inline clamp),
never a bare `menu.style.top = (r.bottom + 4) + 'px'` at a call site. When
adding a kebab menu to a new page, copy the helper above under the page's
prefix and call it from the kebab's click handler.
