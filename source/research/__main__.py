"""CLI: python -m research "query" -> cited markdown report on stdout
(progress on stderr), or --out FILE. With --out, a JSONL KPI/event log is
written next to it (override with --events)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m research",
        description="Deep research: turn a query into a cited markdown report.",
    )
    parser.add_argument("query")
    parser.add_argument(
        "--search",
        default="auto",
        choices=["auto", "brave", "ddg", "searxng", "firecrawl"],
        help="search provider (auto = first configured of brave, searxng, firecrawl, ddg)",
    )
    parser.add_argument(
        "--fetcher",
        default="plain",
        choices=["plain", "firecrawl"],
        help="page fetcher (firecrawl handles JS-heavy pages, needs FIRECRAWL_API_KEY)",
    )
    parser.add_argument(
        "--model-group",
        default="research",
        help="model group (name or uuid) from the /models page",
    )
    parser.add_argument("--max-subtasks", type=int, default=5)
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=120.0,
        help="per-model timeout floor in seconds; configured model timeouts "
        "below this are raised to it",
    )
    parser.add_argument("--out", default=None, help="write the report to this file")
    parser.add_argument(
        "--events",
        default=None,
        help="write a JSONL KPI/event log to this file (default with --out: "
        "the report path with a .events.jsonl suffix)",
    )
    args = parser.parse_args(argv)

    import db

    from research import pipeline
    from research.config import ResearchConfig
    from research.telemetry import Telemetry

    # ModelCaller reads model groups through Flask-SQLAlchemy, which needs an
    # app context; push one for the process (the agents/__main__.py pattern).
    db.make_app().app_context().push()

    config = ResearchConfig(
        model_group=args.model_group,
        search_provider=args.search,
        fetcher=args.fetcher,
        max_subtasks=args.max_subtasks,
        llm_timeout_s=args.llm_timeout,
    )
    events_path = args.events
    if events_path is None and args.out:
        events_path = str(Path(args.out).with_suffix(".events.jsonl"))
    telemetry = Telemetry(events_path) if events_path else None

    try:
        report = pipeline.run_deep_research(args.query, config, telemetry=telemetry)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if events_path:
            print(f"events written to {events_path}", file=sys.stderr)
        return 1

    markdown = report.render_markdown()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        print(f"report written to {args.out}", file=sys.stderr)
    else:
        print(markdown)
    if events_path:
        print(f"events written to {events_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
