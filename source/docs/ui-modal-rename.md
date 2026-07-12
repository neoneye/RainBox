# UI Rename (modal-confirmed, app-wide preference)

How renaming a thing (a prompt, a folder, a room, a job, …) should work in
rainbox, on every page. The reference implementation is the /prompt page
(`webapp/prompt_views.py` markup + `static/prompt.js`); the modal mechanics it
builds on are specified in [ui-modals.md](ui-modals.md).

## The rule

**Never rename through an inline text field with a separate Rename/Save
button.** Instead:

1. The pane shows the node's **name as a click-to-rename control** — a button
   that reads as the name itself (it can double as the pane heading). A hover
   border and a `title="Click to rename"` tooltip reveal the affordance. No
   extra icon; the tooltip carries the hint.
2. Clicking it opens a **rename modal** (the [ui-modals.md](ui-modals.md)
   pattern) with the current name pre-filled and the cursor at the end.
3. The modal offers exactly two ways out: **Rename** (primary; applies the
   change, saves, and confirms with a toast) and **Cancel**. The Rename button
   is disabled while the typed name is empty or unchanged, so a no-op can't
   masquerade as a rename.
4. Esc / backdrop-click follow the standard dirty guard: they dismiss only
   while the typed name still equals the stored one. Once it differs, only the
   explicit buttons close the modal.

The name displayed on the page is therefore **always the stored name** — there
is no in-between state where the screen shows one name and the database holds
another.

## Why

The inline-field-plus-button version has a silent failure mode: type the new
name, get distracted, never click Rename. Nothing complains — the field shows
the new name until the next re-render, then the edit evaporates. Hours later
the operator looks for a prompt under the name they *typed*, can't find it,
and has to reconstruct what happened ("did I rename it? delete it? which one
was it?") — detective work that costs far more than the rename itself.

The modal closes that gap structurally rather than by discipline:

- **An edit can't dangle.** The moment editing starts, the UI is in a state
  that must be resolved — Rename or Cancel. There is no third outcome where
  typed text quietly sits in a field that something else will later reset.
- **The outcome is explicit.** Confirming shows a "Renamed to …" toast;
  cancelling visibly restores the old name. Either way the operator *knows*
  what state the name is in when they walk away.
- **Accidental dismissal can't lose the edit.** The dirty guard means a stray
  Esc or backdrop click never discards a changed name.

## Notes for implementers

- Kebab/context-menu "Rename" items open the same modal directly (select the
  node first) — one rename path, not two.
- The click-to-rename control is a real `<button>` (keyboard focus + Enter
  work for free), styled as text with a transparent border that colors on
  hover.
- Don't add a redundant pane title ("Prompt", "Folder", …) above the name —
  the selection is already visible in the left tree, and the label is noise
  the operator has to skip over on every visit. The name display is the
  heading.
- Enter inside the modal's input confirms (when the Rename button is enabled);
  the input listener wiring is the standard per-dialog trio from
  [ui-modals.md](ui-modals.md).

## Where it applies

Any editable name/title in the UI. As of now the /prompt page (prompts +
folders) implements it; the /chat room title still uses an inline field with a
Rename button and should be migrated to this pattern when touched.
