"""`rainbox doctor`: an operator health check. Run: `python -m tools.doctor`.

Each probe returns a Check; the process exits 1 if any check failed. The most
useful probe is the embedder reachability check (Ollama nomic-embed-text) — a
down embedder silently degrades memory retrieval to lexical-only.
"""

import logging
import sys
from collections.abc import Callable
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


def check_embedder(*, embed_fn: Callable[[str], list[float]] | None = None) -> Check:
    from agents.query_kb_helpers import EMBED_MODEL_NAME, OLLAMA_BASE
    fn = embed_fn
    if fn is None:
        from memory.embeddings import _default_embed
        fn = _default_embed
    try:
        vec = fn("rainbox doctor probe")
    except Exception as e:  # noqa: BLE001 — any failure means "unreachable"
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


def run_doctor(*, embed_fn: Callable[[str], list[float]] | None = None) -> list[Check]:
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
