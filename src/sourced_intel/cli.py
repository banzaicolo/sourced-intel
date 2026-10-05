"""Command-line entry point.

    sourced-intel demo                 render the bundled sample report (no network)
    sourced-intel run "your question"  execute the whole pipeline
    sourced-intel stats                summarise the runs recorded under data/runs
    sourced-intel preflight            check environment, config and data sources
    sourced-intel ui                   start the Streamlit demo
    sourced-intel api                  start the FastAPI service

Every command is importable lazily. `demo` in particular is deliberately free of
LangGraph and httpx imports: it has to work on a fresh checkout with only Jinja2
and Pydantic present, because it is the first thing a visitor will try.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEMO_OUT = PROJECT_ROOT / "examples" / "demo-report.html"


def _fail(message: str, hint: str | None = None) -> int:
    print(f"error: {message}", file=sys.stderr)
    if hint:
        print(f"       {hint}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------
def cmd_demo(args: argparse.Namespace) -> int:
    from . import demo
    from .report.render import render_html
    from .schemas import Aggregation, Analysis, IntelItem, QuerySpec

    payload = demo.build()
    out = Path(args.out).expanduser().resolve()

    try:
        written = render_html(
            run_id=payload["run_id"],
            user_query=payload["user_query"],
            query=QuerySpec.model_validate(payload["query"]),
            aggregation=Aggregation.model_validate(payload["aggregation"]),
            analysis=Analysis.model_validate(payload["analysis"]),
            items=[IntelItem.model_validate(row) for row in payload["items"]],
            rejected=[IntelItem.model_validate(row) for row in payload["rejected"]],
            failed_sources=payload["failed_sources"],
            errors=payload["errors"],
            summary=payload["summary"],
            threshold=payload["threshold"],
            model_name=payload["model_name"],
            output_path=out,
            raw_docs=payload["raw_docs"],
            elapsed_seconds=payload["elapsed_seconds"],
            is_sample=True,
        )
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")

    print(f"wrote {written}  ({written.stat().st_size / 1024:,.0f} KB)")
    if written.suffix != ".html":
        # render_html degrades to Markdown on a template error. Silence here
        # would hide exactly the failure this command exists to surface.
        return _fail(
            "the template failed, so a Markdown file was written instead",
            "the traceback is above; this is the bug to fix",
        )
    print("open it with:  open " + str(written) if sys.platform == "darwin" else str(written))
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    from .graph import run_pipeline
    from .run_ids import InvalidRunId, validate_run_id

    if args.run_id is not None:
        try:
            validate_run_id(args.run_id)
        except InvalidRunId as exc:
            return _fail(str(exc), "run ids have to be safe as directory names under data/runs")

    try:
        result = run_pipeline(args.query, run_id=args.run_id)
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")

    summary = result["summary"]
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["report_path"] else 1

    print(f"run        {summary['run_id']}")
    print(f"status     {summary['status']}")
    print(f"elapsed    {summary['elapsed_seconds']} s")
    print(f"documents  {summary['raw_docs']} fetched -> {summary['intel_items']} records")
    if summary["failed_sources"]:
        print(f"degraded   {', '.join(summary['failed_sources'])}")
    if summary["clarification"]:
        print(f"clarify    {summary['clarification']}")
    if summary["report_path"]:
        print(f"report     {summary['report_path']}  ({summary['report_format']})")
    print(f"trace      {summary['run_dir']}/trace.jsonl")
    return 0 if summary["report_path"] else 1


# ---------------------------------------------------------------------------
# stats / preflight / servers
# ---------------------------------------------------------------------------
def cmd_stats(args: argparse.Namespace) -> int:
    import runpy

    script = PROJECT_ROOT / "scripts" / "run_stats.py"
    argv = [str(script)]
    if args.last:
        argv += ["--last", str(args.last)]
    for name in args.exclude or []:
        argv += ["--exclude", name]
    if args.json:
        argv += ["--json"]
    sys.argv = argv
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    import runpy

    script = PROJECT_ROOT / "scripts" / "preflight.py"
    sys.argv = (
        [str(script)]
        + (["--json"] if args.json else [])
        + (["--skip-net"] if args.skip_net else [])
    )
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def cmd_ui(_: argparse.Namespace) -> int:
    import subprocess

    return subprocess.call(
        [sys.executable, "-m", "streamlit", "run", str(PROJECT_ROOT / "app" / "streamlit_app.py")]
    )


def cmd_api(args: argparse.Namespace) -> int:
    import subprocess

    return subprocess.call(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sourced_intel.api:app",
            "--app-dir",
            str(PROJECT_ROOT / "src"),
            "--host",
            args.host,
            "--port",
            str(args.port),
        ]
    )


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sourced-intel",
        description="Multi-source intelligence mining: one question in, one sourced report out.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="render the bundled sample report (no network, no key)")
    p_demo.add_argument("--out", default=str(DEFAULT_DEMO_OUT), help="output .html path")
    p_demo.set_defaults(func=cmd_demo)

    p_run = sub.add_parser("run", help="run the pipeline for a question")
    p_run.add_argument("query", help="natural-language question")
    p_run.add_argument(
        "--run-id",
        default=None,
        help="optional run id; must match run-[A-Za-z0-9][A-Za-z0-9._-]* (default: generated)",
    )
    p_run.add_argument("--json", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_stats = sub.add_parser("stats", help="summarise the recorded runs")
    p_stats.add_argument("--last", type=int, default=None)
    p_stats.add_argument("--exclude", action="append", default=[])
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

    p_pre = sub.add_parser("preflight", help="check environment, config and sources")
    p_pre.add_argument("--json", action="store_true")
    p_pre.add_argument("--skip-net", action="store_true")
    p_pre.set_defaults(func=cmd_preflight)

    sub.add_parser("ui", help="start the Streamlit demo").set_defaults(func=cmd_ui)

    p_api = sub.add_parser("api", help="start the FastAPI service")
    p_api.add_argument("--host", default="127.0.0.1")
    p_api.add_argument("--port", type=int, default=8000)
    p_api.set_defaults(func=cmd_api)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
