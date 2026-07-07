"""CLI: python -m research "query" -> cited markdown report on stdout
(progress on stderr), or --out FILE."""

from __future__ import annotations

import argparse
import sys


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
    parser.add_argument("--out", default=None, help="write the report to this file")
    args = parser.parse_args(argv)

    import db

    from research import pipeline
    from research.config import ResearchConfig

    # ModelCaller reads model groups through Flask-SQLAlchemy, which needs an
    # app context; push one for the process (the agents/__main__.py pattern).
    db.make_app().app_context().push()

    config = ResearchConfig(
        model_group=args.model_group,
        search_provider=args.search,
        fetcher=args.fetcher,
        max_subtasks=args.max_subtasks,
    )
    try:
        report = pipeline.run_deep_research(args.query, config)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    markdown = report.render_markdown()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        print(f"report written to {args.out}", file=sys.stderr)
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
