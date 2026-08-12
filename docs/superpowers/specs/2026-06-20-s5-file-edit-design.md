# S5 — file edit / patch proposals (confirm-tier + dry-run diff) — design (2026-06-20)

**Status:** approved-direction, complete spec (decisions all made; implement
directly). Implements card **S5** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
the assistant proposes edits to workspace files as **previewable patches**, never
silent writes. Confirm-tier, reusing the **dry-run preview protocol** built in S4.

## Decisions (made, with rationale)

- **One capability, `edit_file`, confirm-tier with a dry-run unified-diff
  preview.** File writes are high blast radius → the assistant *proposes*, the
  operator sees the **diff** and confirms, then it applies. This is "proposed
  patches, not silent writes" (v2 Phase 5 #4). It is the second `Capability.dry_run`
  user (after reminders), so it exercises the S4 protocol on a real diff.
- **Args are `{path, content}` (both strings).** The model supplies the **full new
  file content**; the system computes the unified diff (`difflib`) old→new for the
  preview and writes `content` on confirm. Chosen over line-range patches because:
  (a) the loop's validator only accepts non-empty *string* args, so a `patches`
  list can't be a required arg; (b) full-content is robust (no line-number
  fragility) and the operator sees an exact diff. Token cost is acceptable for the
  config/doc-sized files this targets; a size cap bounds it. (`apply_patches` line
  patches stay the edit-document agents' tool; not reused here.)
- **Path safety = reuse `resolve_workspace_path`.** It already confines to
  `SHELL_ROOT`, and rejects `~`, NUL, traversal, symlink-escape (via `.resolve()`),
  and sensitive names (`.env`, `.ssh`, …). The assistant can only edit files
  inside the workspace (`/tmp/pp_workspace_shell`) — the same boundary
  `workspace_read_command` already uses. **No boundary expansion.**
- **Size cap.** Reject when the existing file or the new `content` exceeds
  `MAX_EDIT_BYTES = 100_000`, and reject a directory target. Keeps a huge blob out
  of the prompt/diff and the DB.
- **`output_cap_chars` raised to 12000** for this capability so a real diff preview
  isn't truncated by `_dispatch_action`'s cap (the S4 heads-up).
- **New-file creation allowed** (old content = ""), creating parent dirs *inside*
  the workspace. The diff then shows all-additions.
- **Rollback is deferred (manual / git).** The operator approves the exact diff
  before it applies (that is the safety); the execute result records `path` +
  old/new byte counts for audit. An automated one-click revert needs an undo path
  for confirm-tier writes — a follow-up, not S5.

## `agents/assistant.py`

**Enum** (after `set_reminder`):

```python
    EDIT_FILE = "edit_file"            # confirm-tier (dry-run diff): edit a workspace file
```

**Action:**

```python
MAX_EDIT_BYTES: int = 100_000

def _action_edit_file(
    ctx: AssistantActionContext, args: dict[str, Any]
) -> AssistantObservation:
    """Confirm-tier write: replace a workspace file's content. Dry-run (propose)
    shows the unified diff and writes nothing; real execution applies it. Confined
    to the workspace by resolve_workspace_path."""
    import difflib

    from tools.workspace_policy import (
        SHELL_CWD, DisallowedCommand, resolve_workspace_path,
    )

    path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    if len(content.encode("utf-8", "ignore")) > MAX_EDIT_BYTES:
        return AssistantObservation(ok=False, text="new content too large (>100KB)")
    try:
        resolved = resolve_workspace_path(path, SHELL_CWD)
    except DisallowedCommand as e:
        return AssistantObservation(ok=False, text=f"blocked: {e}")
    if resolved.is_dir():
        return AssistantObservation(ok=False, text=f"path is a directory: {path}")
    old = ""
    if resolved.exists():
        if resolved.stat().st_size > MAX_EDIT_BYTES:
            return AssistantObservation(ok=False, text="existing file too large to edit (>100KB)")
        old = resolved.read_text(encoding="utf-8", errors="replace")
    if old == content:
        return AssistantObservation(ok=False, text="no change: new content matches the file")
    diff = "\n".join(difflib.unified_diff(
        old.splitlines(), content.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    ))
    verb = "create" if not resolved.exists() else "edit"
    if ctx.dry_run:
        return AssistantObservation(
            ok=True, text=f"Would {verb} {path}:\n{diff}", data={"path": path},
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return AssistantObservation(
        ok=True, text=f"Applied edit to {path} ({len(old)} → {len(content)} chars).",
        data={"path": path, "old_chars": len(old), "new_chars": len(content)},
    )
```

(`MAX_EDIT_BYTES` is a module constant near the other caps.)

**Registry entry:**

```python
    AssistantActionName.EDIT_FILE: Capability(
        name=AssistantActionName.EDIT_FILE, family="workspace",
        description=('propose an edit to a workspace file (you supply the full new '
                     'content); shows a diff and needs your confirmation. args: '
                     '{"path": "...", "content": "..."}'),
        required_args=("path", "content"), action=_action_edit_file,
        read=False, write=True, tier="confirm", dry_run=True,
        output_cap_chars=12000,
    ),
```

No `_propose_write` change — the S4 dry-run protocol already builds the preview
from this action and rejects bad input (e.g. a blocked path) at propose time.

## `agents/test_assistant_fakes.py`

Add `"edit_file"` to the locked action surface.

## Tests (TDD, model-free) — `agents/test_edit_file.py` (new)

Isolate the workspace by monkeypatching `tools.workspace_policy.SHELL_CWD` and
`SHELL_ROOT` to a `tmp_path` (the action imports `SHELL_CWD` lazily and
`resolve_workspace_path` reads the module `SHELL_ROOT`, so both pick up the patch).

1. **dry-run shows a diff, writes nothing:** edit an existing file with
   `ctx.dry_run=True` → `ok`, text contains the unified diff; file on disk
   unchanged.
2. **real execution writes the file:** `dry_run=False` → file content updated;
   data has `old_chars`/`new_chars`.
3. **create a new file:** path doesn't exist → diff is all-additions; real run
   creates it (and parent dirs).
4. **no-op rejected:** content == current file → `ok=False`.
5. **path traversal rejected:** `../escape.txt` → `ok=False` "blocked"; nothing
   written outside the workspace.
6. **absolute path outside workspace rejected:** `/etc/hosts` → `ok=False`.
7. **sensitive name rejected:** `.env` → `ok=False`.
8. **size cap:** content > `MAX_EDIT_BYTES` → `ok=False`; existing oversize file →
   `ok=False`.
9. **propose path uses the diff preview + confirm executes:** drive the loop with
   a scripted `edit_file`; the `assistant_write_intent.preview_text` contains the
   diff and **no** file write happened yet; `execute_write_intent` then writes the
   file and the intent → `completed`.
10. **capability flags:** `edit_file` is `tier="confirm"`, `dry_run=True`,
    `write=True`, `output_cap_chars=12000`; surface lock updated.

## Done when

- The assistant proposes a file edit with a unified-diff preview; the operator
  confirms to apply; an unconfirmed edit writes nothing.
- Out-of-workspace / traversal / sensitive / oversize paths are rejected (tested)
  with no write.
- New files can be created inside the workspace; no-op edits are refused.
- Model-free tests cover all of the above; full affected suite green.

## Out of scope (follow-ups)

- Line-range/patch-fragment edits (token-cheaper than full content) — would need a
  non-string arg path or a JSON-encoded patch arg + the `apply_patches` validator.
- Automated one-click revert of an applied edit (needs an undo path for
  confirm-tier writes).
- Editing files outside the workspace root (a deliberate boundary; not expanded).
