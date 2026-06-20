# S6 — `rainbox doctor` CLI — design (2026-06-20)

**Status:** approved-direction, complete spec (decisions all made; implement
directly). Implements card **S6** of
[`../../proposals/2026-06-20-improvements-v3.md`](../../proposals/2026-06-20-improvements-v3.md):
promote `capability_report()` into an operator-facing health check that flags
missing prerequisites — most usefully the embedder, the recurring gap.

## Decisions (made, with rationale)

- **CLI-only: `python -m tools.doctor`.** `agents/__main__` is a socket-based
  agent subprocess entry, not a general CLI, so doctor is a standalone module.
  An admin/web page is deferred (the card marks it optional).
- **Checks, each a structured `Check(name, status, detail)`** with status
  `ok`/`warn`/`fail`. `fail` = something is broken (exit code 1); `warn` =
  degraded-but-usable (e.g. embedder down → lexical-only); `ok` = fine.
- **Probes:** capabilities (enabled/prompt-exposed counts), model groups (none →
  `fail`: agents can't run), embedder reachability (down → `warn`), skills
  (active/candidate counts + unparseable-file lint → `warn`), MCP (configured
  server count; absent → `ok`, it's optional).
- **Injectable embedder** so the check is testable without a live Ollama.

## `skills/loader.py` — a lint helper

```python
def lint_skills(base_dir: Path | None = None, overlay_dir=_UNSET) -> list[str]:
    """Paths of *.md skill files that fail to parse (invalid metadata) — for
    `rainbox doctor`. Reuses the loader's own parser."""
    if base_dir is None:
        base_dir = SKILLS_DIR
    if overlay_dir is _UNSET:
        overlay_dir = _overlay_dir()
    bad: list[str] = []
    for d in (base_dir, overlay_dir):
        if d is None or not Path(d).is_dir():
            continue
        for p in sorted(Path(d).glob("*.md")):
            if _parse_skill(p, "lint") is None:
                bad.append(str(p))
    return bad
```

Export `lint_skills` from `skills/__init__.py`.

## `tools/doctor.py` (new)

```python
"""`rainbox doctor`: operator health check. Run: `python -m tools.doctor`.

Each probe returns a Check; the process exits 1 if any check failed."""

import logging
import sys
from dataclasses import dataclass

import db
import skills

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    name: str
    status: str   # "ok" | "warn" | "fail"
    detail: str


def check_capabilities() -> Check:
    from agents.assistant import capability_report
    report = capability_report()
    enabled = [c for c in report if c.get("enabled")]
    exposed = [c for c in enabled if c.get("prompt_exposed")]
    return Check("capabilities", "ok",
                 f"{len(enabled)}/{len(report)} enabled, {len(exposed)} prompt-exposed")


def check_model_groups() -> Check:
    groups = db.list_model_groups()
    if not groups:
        return Check("model_groups", "fail",
                     "no model groups configured — agents cannot run")
    return Check("model_groups", "ok",
                 f"{len(groups)} group(s): {', '.join(g.name for g in groups)}")


def check_embedder(*, embed_fn=None) -> Check:
    from agents.query_kb_helpers import EMBED_MODEL_NAME, OLLAMA_BASE
    fn = embed_fn
    if fn is None:
        from memory.embeddings import _default_embed
        fn = _default_embed
    try:
        vec = fn("rainbox doctor probe")
    except Exception as e:
        return Check("embedder", "warn",
                     f"{EMBED_MODEL_NAME} unreachable at {OLLAMA_BASE} "
                     f"({type(e).__name__}) — memory degrades to lexical-only")
    if not vec:
        return Check("embedder", "warn", f"{EMBED_MODEL_NAME} returned an empty vector")
    return Check("embedder", "ok", f"{EMBED_MODEL_NAME} reachable ({len(vec)}-dim)")


def check_skills() -> Check:
    loaded = skills.load_skills()
    active = sum(1 for s in loaded if s.status == "active")
    candidate = sum(1 for s in loaded if s.status == "candidate")
    bad = skills.lint_skills()
    if bad:
        return Check("skills", "warn",
                     f"{active} active, {candidate} candidate; "
                     f"{len(bad)} unparseable file(s): {', '.join(bad)}")
    return Check("skills", "ok", f"{active} active, {candidate} candidate")


def check_mcp() -> Check:
    from agents.mcp_config import load_mcp_servers
    servers = load_mcp_servers()
    if not servers:
        return Check("mcp", "ok", "no MCP servers configured (optional)")
    return Check("mcp", "ok",
                 f"{len(servers)} server(s): {', '.join(s.name for s in servers)}")


def run_doctor(*, embed_fn=None) -> list[Check]:
    return [check_capabilities(), check_model_groups(),
            check_embedder(embed_fn=embed_fn), check_skills(), check_mcp()]


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == "fail" for c in checks) else 0


def format_checks(checks: list[Check]) -> str:
    icon = {"ok": "✓", "warn": "!", "fail": "✗"}
    return "\n".join(f"  {icon.get(c.status, '?')} {c.name}: {c.detail}" for c in checks)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    app = db.make_app()
    with app.app_context():
        checks = run_doctor()
    print("rainbox doctor")
    print(format_checks(checks))
    sys.exit(exit_code(checks))


if __name__ == "__main__":
    main()
```

## Tests (TDD, model-free) — `tools/test_doctor.py` (new)

1. **lint detects an invalid skill file:** write a valid skill and an invalid one
   (no id) to a tmp dir; `skills.lint_skills(base_dir=tmp, overlay_dir=None)`
   returns only the invalid path.
2. **embedder ok / warn:** `check_embedder(embed_fn=lambda _t: [0.1]*768)` →
   `ok`; `check_embedder(embed_fn=raises)` → `warn` (never raises out).
3. **capabilities ok:** `check_capabilities()` → `ok`, detail has the enabled
   count; > 0 enabled.
4. **model groups:** `check_model_groups()` returns a `Check` named `model_groups`
   (status reflects the DB; assert it's one of ok/fail).
5. **run_doctor + exit_code:** `run_doctor(embed_fn=lambda _t: [0.1]*768)` returns
   five checks covering all probe names; `exit_code` is 1 iff any `fail`.
6. **format_checks** renders an icon line per check.

## Done when

- `python -m tools.doctor` prints a per-check report and exits non-zero when a
  prerequisite is broken (no model groups).
- The embedder check flags an unreachable Ollama as `warn` (not a crash).
- Skills with invalid metadata are flagged; capabilities/MCP are reported.
- Model-free tests cover the checks (embedder injected); full affected suite green.

## Out of scope (follow-ups)

- An admin/web doctor page.
- A `rainbox` wrapper script/alias (run via `python -m tools.doctor` for now).
- Deeper probes (DB migration drift, pgvector extension presence, per-agent
  model-group assignment validity).
