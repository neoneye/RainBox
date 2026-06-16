"""Dynamic handlers for the QueryAgent.

Each handler takes a `QueryContext` and returns a short string. Keep them small,
fast, and free of side effects. Where a handler shells out (git, ps, uptime,
pip), give the subprocess a tight timeout and surface a clear error blurb on
failure instead of raising — the agent isn't a place to make Postgres errors
look like Python tracebacks to a chat user.
"""

import json
import logging
import os
import platform
import random
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

import db

logger = logging.getLogger(__name__)

_REPO_DIR = Path(__file__).resolve().parent.parent


@dataclass
class QueryContext:
    """What a QueryAgent dynamic handler is told about the request it's
    answering. Lives here (not in query_agent) so handlers can import the type
    without circular imports."""

    room_uuid: UUID
    query: str
    payload: dict[str, Any]
    agent_uuid: UUID

    # Model-group context, populated by agents that run on a model group (e.g.
    # QueryFilterRouterAgent) so a handler can report which model is answering.
    # `candidate_model_uuids` is the group's priority-ordered fallback list;
    # `active_model_uuid` is the member the agent's LLM call already settled on
    # this turn (None until an LLM call has succeeded, e.g. on the exact-alias
    # path that never calls an LLM). Default empty/None so handlers and agents
    # that don't use a model group keep working unchanged.
    model_group_uuid: UUID | None = None
    candidate_model_uuids: list[UUID] = field(default_factory=list)
    active_model_uuid: UUID | None = None


# --- subprocess helpers -------------------------------------------------------


def _run(*args: str, timeout: float = 5.0) -> str:
    """Run a command in the repo root with stderr merged into stdout. Returns
    output stripped, or a short "(cmd: …)" blurb on failure."""
    try:
        out = subprocess.check_output(
            list(args), cwd=_REPO_DIR, text=True, stderr=subprocess.STDOUT, timeout=timeout
        )
        return out.strip()
    except subprocess.TimeoutExpired:
        return f"({args[0]}: timed out after {timeout:g}s)"
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return f"({args[0]}: {e})"


def _git(*args: str, timeout: float = 5.0) -> str:
    return _run("git", *args, timeout=timeout)


# --- identity -----------------------------------------------------------------


def get_capabilities(ctx: QueryContext) -> str:
    return (
        "I can answer questions from a small knowledge base in "
        "data/question_answer.jsonl — identity, system status / health / "
        "uptime / IP, git repo / branch / status / log / remote, project "
        "context, my own memory stats, and a bit of casual chat. Static "
        "answers come straight from the JSONL; dynamic ones run a Python "
        "handler (see query_handlers.py)."
    )


def get_version(ctx: QueryContext) -> str:
    sha = _git("rev-parse", "--short", "HEAD")
    when = _git("log", "-1", "--format=%ci")
    return f"git {sha}  ({when})"


_PROVIDER_LABELS = {"lm_studio": "LM Studio", "jan": "Jan", "ollama": "Ollama"}


def _describe_member(member_uuid: UUID) -> str:
    """One-line description of a model group member (a ModelConfig or
    ModelConfigOverride uuid): provider, the underlying model id, and the
    config/override's effective label."""
    try:
        provider_id, model_name, _args = db.resolved_model_kwargs(member_uuid)
    except Exception as e:
        return f"(unresolvable model {member_uuid}: {type(e).__name__}: {e})"
    provider = _PROVIDER_LABELS.get(provider_id, provider_id)
    override = db.get_model_config_override(member_uuid)
    if override is not None:
        return f"{provider} — {model_name} ({override.effective_display_name})"
    config = db.get_model_config(member_uuid)
    label = config.effective_display_name if config is not None else model_name
    return f"{provider} — {model_name} ({label})"


def get_model_info(ctx: QueryContext) -> str:
    """Report which LLM is answering: the agent's model group plus the specific
    model config / override currently in use.

    Prefers the model the agent's own LLM call already settled on this turn
    (`active_model_uuid` — proven working). When that's unset (the exact-alias
    path answers without ever calling an LLM), probe the group in priority order
    and report the first member that loads — the same fallback the agent would
    do, so the answer matches whatever WOULD serve a real generation even when
    the first model in the group is down."""
    from llm import prepare_llm

    if not ctx.candidate_model_uuids:
        return (
            "I don't have a model group bound, so I can't say which model is "
            "answering. Bind one on /agent_models."
        )

    group_name: str | None = None
    if ctx.model_group_uuid is not None:
        try:
            g = db.get_model_group(ctx.model_group_uuid)
            group_name = g.name if g is not None else None
        except Exception:
            group_name = None
    group_part = f"model group {group_name!r}" if group_name else "my model group"

    chosen = ctx.active_model_uuid
    skipped: list[str] = []
    if chosen is None:
        # No LLM call has run yet this turn — walk the group ourselves, loading
        # each in priority order, and take the first that comes up.
        for member_uuid in ctx.candidate_model_uuids:
            try:
                provider_id, model_name, args = db.resolved_model_kwargs(member_uuid)
                prepare_llm(provider_id, model_name, args)
                chosen = member_uuid
                break
            except Exception as e:
                skipped.append(f"{_describe_member(member_uuid)} [failed: {type(e).__name__}]")

    if chosen is None:
        lines = [
            f"All {len(ctx.candidate_model_uuids)} model(s) in {group_part} are "
            "currently unavailable."
        ]
        lines += [f"- {s}" for s in skipped]
        return "\n".join(lines)

    out = f"I'm running on {_describe_member(chosen)}, from {group_part}."
    if skipped:
        out += "\nEarlier models in the group were unavailable and skipped:\n" + "\n".join(
            f"- {s}" for s in skipped
        )
    return out


# --- system -------------------------------------------------------------------


def get_system_health(ctx: QueryContext) -> str:
    """Actually probe the dependencies rather than asserting they're up."""
    checks: list[str] = []
    try:
        with psycopg.connect(db.psycopg_dsn(), connect_timeout=2) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks.append("Postgres: ok")
    except Exception as e:
        checks.append(f"Postgres: failed ({type(e).__name__}: {e})")
    return "System health:\n" + "\n".join(f"- {c}" for c in checks)


def get_current_datetime(ctx: QueryContext) -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()


def get_process_uptime(ctx: QueryContext) -> str:
    """Report the supervisor's uptime.

    `main.py` records its start time in the `PP3_SUPERVISOR_STARTED` env var
    before spawning agents (and `os.posix_spawn(..., os.environ)` propagates it
    to each child), so the handler just reads it back. Avoids the PPID `ps`
    dance which is fragile (zombie/orphan parent, permissions, etc.)."""
    raw = os.environ.get("PP3_SUPERVISOR_STARTED")
    if not raw:
        return (
            "(supervisor start time not recorded — restart main.py so "
            "PP3_SUPERVISOR_STARTED gets set)"
        )
    try:
        started = float(raw)
    except ValueError:
        return f"(PP3_SUPERVISOR_STARTED malformed: {raw!r})"
    secs = int(time.time() - started)
    started_human = datetime.fromtimestamp(started).astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    return f"{_humanize_seconds(secs)} (since {started_human})"


def get_host_uptime(ctx: QueryContext) -> str:
    return _run("uptime")


def get_system_resources(ctx: QueryContext) -> str:
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = float("nan")
    return (
        f"load avg: {load1:.2f} (1m) / {load5:.2f} (5m) / {load15:.2f} (15m); "
        f"{os.cpu_count()} CPUs"
    )


def get_host_info(ctx: QueryContext) -> str:
    return f"{platform.node()}  —  {platform.platform()}  ({platform.machine()})"


def get_gpu_info(ctx: QueryContext) -> str:
    """Best-effort GPU description. macOS: parse `system_profiler` (-detailLevel
    mini is faster than the default). Linux: try `nvidia-smi -L`, then fall back
    to `lspci`. Returns a short error blurb on platforms we don't probe."""
    system = platform.system()
    if system == "Darwin":
        out = _run("system_profiler", "SPDisplaysDataType", "-detailLevel", "mini", timeout=10.0)
        if out.startswith("("):
            return out
        chipsets: list[str] = []
        cores: str | None = None
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Chipset Model:"):
                chipsets.append(stripped.split(":", 1)[1].strip())
            elif stripped.startswith("Total Number of Cores:"):
                cores = stripped.split(":", 1)[1].strip()
        if not chipsets:
            return "(could not parse GPU info from system_profiler)"
        head = ", ".join(chipsets)
        return f"{head} ({cores} GPU cores)" if cores else head
    if system == "Linux":
        nv = _run("nvidia-smi", "-L", timeout=5.0)
        if not nv.startswith("("):
            return nv
        return _run("lspci", "-nn", timeout=5.0)
    return f"(GPU detection not implemented for {system})"


def get_connectivity(ctx: QueryContext) -> str:
    """Brief TCP probe to a well-known DNS port; doesn't actually do DNS."""
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=2):
            return "Online (TCP to 1.1.1.1:53 ok)."
    except OSError as e:
        return f"Offline (TCP to 1.1.1.1:53 failed: {e})."


def get_local_ip(ctx: QueryContext) -> str:
    """The "outbound" local IP — the address the kernel would use to reach a
    public host. UDP doesn't send anything; we just ask getsockname."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError as e:
        return f"(local-ip lookup failed: {e})"
    finally:
        s.close()


# --- dev / git ----------------------------------------------------------------


def get_git_repo_path(ctx: QueryContext) -> str:
    return _git("rev-parse", "--show-toplevel")


def get_git_branch(ctx: QueryContext) -> str:
    return _git("branch", "--show-current")


def get_git_status(ctx: QueryContext) -> str:
    out = _git("status", "--porcelain")
    if not out:
        return "Working tree clean."
    lines = out.splitlines()
    return f"{len(lines)} uncommitted change(s):\n" + "\n".join(f"- {l}" for l in lines[:10]) + (
        f"\n…and {len(lines) - 10} more" if len(lines) > 10 else ""
    )


def get_last_git_commit(ctx: QueryContext) -> str:
    return _git("log", "-1", "--pretty=%h %ad %s", "--date=short")


def get_git_remote(ctx: QueryContext) -> str:
    url = _git("config", "--get", "remote.origin.url")
    if url.startswith("("):  # _git error blurb
        return "(no origin remote configured)"
    return url


def get_git_overview(ctx: QueryContext) -> str:
    """Markdown overview of the git repositories curated on the /git page,
    grouped by their folder. Reads the persisted tree (db.git_load_tree); does
    not shell out to git, so it reflects what the operator tracks rather than
    just this checkout."""
    try:
        tree = db.git_load_tree()
    except Exception as e:
        return f"(could not load git tree: {type(e).__name__}: {e})"
    folders = tree.get("folders", [])
    repos = tree.get("repos", [])
    if not repos:
        return "No git repositories are tracked yet. Add some at http://127.0.0.1:5000/git"

    # Resolve a repo's folder to a breadcrumb label ("Parent / Child").
    by_id = {f["id"]: f for f in folders}

    def folder_label(fid: str | None) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            node = by_id[fid]
            parts.append(node["name"])
            fid = node.get("parentId")
        return " / ".join(reversed(parts))

    # Group repos by folder, preserving the page's saved order.
    groups: dict[str, list] = {}
    for r in repos:
        label = folder_label(r.get("folderId")) or "(ungrouped)"
        groups.setdefault(label, []).append(r)

    noun = "repository" if len(repos) == 1 else "repositories"
    lines = [f"**{len(repos)} git {noun}** — http://127.0.0.1:5000/git", ""]
    for label, items in groups.items():
        lines.append(f"### {label}")
        for r in items:
            bits = [f"- **{r['name']}**"]
            if r.get("path"):
                bits.append(f" — `{r['path']}`")
            if r.get("description"):
                bits.append(f": {r['description']}")
            lines.append("".join(bits))
        lines.append("")
    return "\n".join(lines).strip()


def get_cron_overview(ctx: QueryContext) -> str:
    """Markdown overview of the cron jobs curated on the /cron page, grouped by
    folder. For each job: active/inactive, schedule (cron expr + timezone), uuid
    and next run. Reads the persisted tree (db.cron_load_tree); the scheduler
    owns next_run_at, so this just reports what's stored."""
    try:
        tree = db.cron_load_tree()
    except Exception as e:
        return f"(could not load cron tree: {type(e).__name__}: {e})"
    folders = tree.get("folders", [])
    jobs = tree.get("jobs", [])
    if not jobs:
        return "No cron jobs are configured yet. Add some at http://127.0.0.1:5000/cron"

    # Resolve a job's folder to a breadcrumb label ("Parent / Child").
    by_id = {f["id"]: f for f in folders}

    def folder_label(fid: str | None) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            node = by_id[fid]
            parts.append(node["name"])
            fid = node.get("parentId")
        return " / ".join(reversed(parts))

    # Group jobs by folder, preserving the page's saved order.
    groups: dict[str, list] = {}
    for j in jobs:
        label = folder_label(j.get("folderId")) or "(ungrouped)"
        groups.setdefault(label, []).append(j)

    noun = "cron job" if len(jobs) == 1 else "cron jobs"
    header = f"**{len(jobs)} {noun}** — http://127.0.0.1:5000/cron"
    if tree.get("paused"):
        header += "  ⏸ (globally paused — nothing fires)"
    lines = [header, ""]
    for label, items in groups.items():
        lines.append(f"### {label}")
        for j in items:
            enabled = j.get("enabled")
            sched = j.get("cron") or "(no schedule)"
            if j.get("timezone"):
                sched += f" [{j['timezone']}]"
            lines.append(f"- **{j['name']}** — {'active' if enabled else 'inactive'}")
            if j.get("description"):
                lines.append(f"  - {j['description']}")
            lines.append(f"  - schedule: `{sched}`")
            if enabled:  # an inactive job never fires, so next-run is meaningless
                lines.append(f"  - next run: {j.get('next_run_at') or '—'}")
            lines.append(f"  - uuid: `{j['uuid']}`")
        lines.append("")
    return "\n".join(lines).strip()


def get_kanban_overview(ctx: QueryContext) -> str:
    """Markdown overview of the /kanban page: every board grouped by folder,
    each with its task count and a per-column breakdown (task titles, with an
    @ marker for assigned tasks). Reads the persisted tree + per-board contents
    (db.kanban_load_tree / db.kanban_load_board)."""
    try:
        tree = db.kanban_load_tree()
    except Exception as e:
        return f"(could not load kanban tree: {type(e).__name__}: {e})"
    folders = tree.get("folders", [])
    boards = tree.get("boards", [])
    if not boards:
        return "No kanban boards exist yet. Create one at http://127.0.0.1:5000/kanban"

    # Resolve a board's folder to a breadcrumb label ("Parent / Child").
    by_id = {f["uuid"]: f for f in folders}

    def folder_label(fid: str | None) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            node = by_id[fid]
            parts.append(node["name"])
            fid = node.get("parentId")
        return " / ".join(reversed(parts))

    # Group boards by folder, preserving the page's saved order.
    groups: dict[str, list] = {}
    for b in boards:
        label = folder_label(b.get("folderId")) or "(ungrouped)"
        groups.setdefault(label, []).append(b)

    total_tasks = sum(b.get("taskCount", 0) for b in boards)
    bnoun = "board" if len(boards) == 1 else "boards"
    tnoun = "task" if total_tasks == 1 else "tasks"
    lines = [
        f"**{len(boards)} kanban {bnoun}, {total_tasks} {tnoun}** — http://127.0.0.1:5000/kanban",
        "",
    ]
    for label, items in groups.items():
        lines.append(f"### {label}")
        for b in items:
            count = b.get("taskCount", 0)
            lines.append(f"- **{b['name']}** — {count} task(s)")
            try:
                data = db.kanban_load_board(UUID(b["uuid"]))
            except Exception:
                data = None
            if not data:
                continue
            tasks_by_col: dict[str, list] = {}
            for t in data["tasks"]:
                tasks_by_col.setdefault(t["columnUuid"], []).append(t)
            for col in data["columns"]:
                col_tasks = tasks_by_col.get(col["uuid"], [])
                if not col_tasks:
                    lines.append(f"  - {col['name']} (0): _(empty)_")
                    continue
                titles = ", ".join(
                    t["title"] + (" @" if t.get("agentUuid") else "") for t in col_tasks
                )
                lines.append(f"  - {col['name']} ({len(col_tasks)}): {titles}")
        lines.append("")
    return "\n".join(lines).strip()


def get_cwd(ctx: QueryContext) -> str:
    return str(_REPO_DIR)


def get_runtime_info(ctx: QueryContext) -> str:
    return f"{platform.python_implementation()} {platform.python_version()} @ {sys.executable}"


def get_test_status(ctx: QueryContext) -> str:
    """No CI is wired up here, so report a structural summary instead of a
    pretend status. Counts pytest files under the repo."""
    test_files = sorted(_REPO_DIR.glob("test_*.py")) + sorted(_REPO_DIR.glob("tools/test_*.py"))
    return (
        f"No automated test runner is tracked. "
        f"{len(test_files)} pytest file(s) on disk: "
        + ", ".join(p.name for p in test_files)
    )


# --- project ------------------------------------------------------------------


def get_current_chatroom(ctx: QueryContext) -> str:
    """The name of the chatroom the question was asked in. Fall back to the repo
    dir when there's no room context."""
    try:
        room = db.db.session.query(db.Chatroom).filter_by(uuid=ctx.room_uuid).first()
        if room and room.name:
            return room.name
    except Exception:
        pass
    return _REPO_DIR.name


def list_chatrooms(ctx: QueryContext) -> str:
    """List the chat rooms grouped by the folder they sit in (the /chat
    left-panel tree). These are chatrooms — folders of chatrooms — not
    "projects"; reads db.chat_load_tree."""
    try:
        tree = db.chat_load_tree()
    except Exception as e:
        return f"(could not list chatrooms: {type(e).__name__}: {e})"
    folders = tree.get("folders", [])
    rooms = tree.get("rooms", [])
    if not rooms:
        return "No chatrooms yet."

    # Resolve a room's folder to a breadcrumb label ("Parent / Child").
    by_id = {f["id"]: f for f in folders}

    def folder_label(fid: str | None) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            node = by_id[fid]
            parts.append(node["name"])
            fid = node.get("parentId")
        return " / ".join(reversed(parts))

    # Group rooms by folder, preserving the page's saved order.
    groups: dict[str, list] = {}
    for r in rooms:
        label = folder_label(r.get("folderId")) or "(top level)"
        groups.setdefault(label, []).append(r)

    noun = "chatroom" if len(rooms) == 1 else "chatrooms"
    lines = [f"**{len(rooms)} {noun}** — http://127.0.0.1:5000/chat", ""]
    for label, items in groups.items():
        lines.append(f"### {label}")
        for r in items:
            lines.append(f"- **{r['name']}** — {r.get('member_count', 0)} member(s)")
        lines.append("")
    return "\n".join(lines).strip()


def get_todo_list(ctx: QueryContext) -> str:
    """Surface TODO/FIXME markers from tracked source as a poor-man's todo list."""
    out = _git("grep", "-nE", "TODO|FIXME", "--", ":^memory/", ":^docs/", timeout=10.0)
    if out.startswith("("):
        return "(git grep failed or no matches)"
    lines = out.splitlines()
    return f"{len(lines)} TODO/FIXME marker(s):\n" + "\n".join(f"- {l}" for l in lines[:15]) + (
        f"\n…and {len(lines) - 15} more" if len(lines) > 15 else ""
    )


def get_outdated_dependencies(ctx: QueryContext) -> str:
    """`pip list --outdated` against the project venv. Hits PyPI — give it a
    real timeout. `--disable-pip-version-check` suppresses the trailing
    "[notice] A new release of pip is available" line that otherwise gets
    appended to stdout and breaks JSON parsing; stderr is dropped for the same
    reason."""
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "--disable-pip-version-check",
                "list", "--outdated", "--format=json",
            ],
            cwd=_REPO_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20.0,
            check=True,
        )
        raw = proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return "(pip list --outdated: timed out after 20s)"
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return f"(pip: {e})"
    try:
        items = json.loads(raw)
    except Exception:
        return f"(could not parse pip output: {raw[:120]})"
    if not items:
        return "All pinned packages are up to date."
    return f"{len(items)} outdated package(s):\n" + "\n".join(
        f"- {it['name']} {it['version']} → {it['latest_version']}" for it in items[:15]
    ) + (f"\n…and {len(items) - 15} more" if len(items) > 15 else "")


# --- meta ---------------------------------------------------------------------


def get_memory_stats(ctx: QueryContext) -> str:
    """Where the QueryAgent stores its memory and how much is there."""
    kb_path = _REPO_DIR / "data" / "question_answer.jsonl"
    jsonl_count = 0
    try:
        jsonl_count = sum(1 for line in kb_path.read_text().splitlines() if line.strip())
    except Exception:
        pass
    embed_count = 0
    try:
        with psycopg.connect(db.psycopg_dsn(), connect_timeout=2) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM data_query_agent_kb")
            row = cur.fetchone()
            embed_count = int(row[0]) if row else 0
    except Exception:
        pass
    return (
        f"Q&A registry: {jsonl_count} entries in data/question_answer.jsonl, "
        f"{embed_count} embedded rows in data_query_agent_kb (one per question alternate)."
    )


def get_last_match_explanation(ctx: QueryContext) -> str:
    """Read this room's previous `debug-query` row and explain how the last
    answer was selected. Skips the row from the *current* call (which was just
    posted before the handler ran)."""
    try:
        msgs = db.list_room_messages(ctx.room_uuid)
    except Exception as e:
        return f"(could not read room: {type(e).__name__}: {e})"
    debugs = [m for m in msgs if m.get("kind") == "debug-query"]
    if len(debugs) < 2:
        return "(no prior match to explain in this room)"
    prev = debugs[-2]
    try:
        data = json.loads(prev["text"])
        m = data.get("match") or {}
    except Exception:
        return f"(could not parse prior debug row: {prev.get('text', '')[:120]})"
    if not m or m.get("method") == "none":
        return (
            f"For {data.get('query')!r} I didn't find a confident match "
            f"({m.get('reason') or 'below threshold'})."
        )
    return (
        f"For {data.get('query')!r}: matched qa_id={m.get('qa_id')!r} via "
        f"{m.get('method')!r} (score={m.get('score')})."
    )


# --- social -------------------------------------------------------------------


def get_status_casual(ctx: QueryContext) -> str:
    return random.choice([
        "Doing fine — Postgres is responding and the embedding endpoint is alive.",
        "Good. What can I look up?",
        "All systems go.",
    ])


_JOKES = [
    "I tried to write a recursive function but it just kept calling itself.",
    "There are 10 kinds of people in the world: those who understand binary, and those who don't.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "A SQL query walks into a bar, walks up to two tables and asks: 'Can I join you?'",
    "I would tell you a UDP joke, but you might not get it.",
]


def generate_joke(ctx: QueryContext) -> str:
    return random.choice(_JOKES)


# --- helpers ------------------------------------------------------------------


def _humanize_seconds(secs: int) -> str:
    """A small-talk-style uptime string."""
    if secs < 60:
        return f"{secs}s"
    minutes, secs = divmod(secs, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


HANDLERS = {
    # identity
    "get_capabilities": get_capabilities,
    "get_version": get_version,
    "get_model_info": get_model_info,
    # system
    "get_system_health": get_system_health,
    "get_current_datetime": get_current_datetime,
    "get_process_uptime": get_process_uptime,
    "get_host_uptime": get_host_uptime,
    "get_system_resources": get_system_resources,
    "get_host_info": get_host_info,
    "get_gpu_info": get_gpu_info,
    "get_connectivity": get_connectivity,
    "get_local_ip": get_local_ip,
    # dev
    "get_git_repo_path": get_git_repo_path,
    "get_git_branch": get_git_branch,
    "get_git_status": get_git_status,
    "get_last_git_commit": get_last_git_commit,
    "get_git_remote": get_git_remote,
    "get_git_overview": get_git_overview,
    "get_cron_overview": get_cron_overview,
    "get_kanban_overview": get_kanban_overview,
    "get_cwd": get_cwd,
    "get_runtime_info": get_runtime_info,
    "get_test_status": get_test_status,
    # project
    "get_current_chatroom": get_current_chatroom,
    "list_chatrooms": list_chatrooms,
    "get_todo_list": get_todo_list,
    "get_outdated_dependencies": get_outdated_dependencies,
    # meta
    "get_memory_stats": get_memory_stats,
    "get_last_match_explanation": get_last_match_explanation,
    # social
    "get_status_casual": get_status_casual,
    "generate_joke": generate_joke,
}
