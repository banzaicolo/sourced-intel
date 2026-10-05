"""Summarise the recorded runs in data/runs/ into the numbers the docs quote.

The README and docs/architecture.md quote a performance distribution rather than a
single figure, because two of the three dominant terms are other people's servers.
Any quoted number has to be reproducible, so this script is what produces them:

    python3 scripts/run_stats.py            # all runs on disk
    python3 scripts/run_stats.py --last 5   # the five most recent

It reads only `data/runs/<run-id>/trace.jsonl` and the final snapshot, so it needs
no network, no credentials and no virtualenv.

    --json    emit the summary as JSON (for a dashboard or a CI check)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = PROJECT_ROOT / "data" / "runs"

NODES = (
    "01_parse_intent",
    "02_retrieve",
    "03_extract",
    "04_aggregate",
    "05_analyze",
    "06_generate_report",
    "07_quality_gate",
)


def read_run(run_dir: Path) -> dict | None:
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        return None

    durations: dict[str, float] = {}
    for line in trace.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        durations[record.get("node", "?")] = round(record.get("duration_ms", 0) / 1000.0, 2)

    if not durations:
        return None

    documents = items = 0
    failed_sources: list[str] = []
    report_format = None
    final = run_dir / "snapshots" / "99_final.json"
    if final.exists():
        try:
            state = json.loads(final.read_text(encoding="utf-8")).get("state", {})
        except json.JSONDecodeError:
            state = {}
        documents = len(state.get("raw_docs") or [])
        items = len(state.get("intel_items") or [])
        failed_sources = list(state.get("failed_sources") or [])
        report_format = ((state.get("node_meta") or {}).get("06_generate_report") or {}).get(
            "report_format"
        )

    return {
        "run_id": run_dir.name,
        "durations": durations,
        "total": round(sum(durations.values()), 2),
        "documents": documents,
        "items": items,
        "failed_sources": failed_sources,
        # An HTML report is the deliverable; a markdown one means the template raised
        # and the renderer fell back. Worth counting rather than assuming.
        "report_format": report_format,
    }


def spread(values: list[float]) -> str:
    if not values:
        return "-"
    if len(values) == 1:
        return f"{values[0]:.1f}"
    return f"{min(values):.1f}-{max(values):.1f}"


def summarise(runs: list[dict]) -> dict:
    totals = [r["total"] for r in runs]
    summary = {
        "runs": len(runs),
        "total": {
            "min": round(min(totals), 1),
            "max": round(max(totals), 1),
            "median": round(statistics.median(totals), 1),
        },
        "nodes": {},
        "documents": spread([float(r["documents"]) for r in runs]),
        "items": spread([float(r["items"]) for r in runs]),
        "markdown_fallbacks": sum(1 for r in runs if r["report_format"] == "markdown"),
        "runs_with_a_failed_source": sum(1 for r in runs if r["failed_sources"]),
    }
    for node in NODES:
        values = [r["durations"][node] for r in runs if node in r["durations"]]
        if values:
            summary["nodes"][node] = {
                "min": round(min(values), 1),
                "max": round(max(values), 1),
                "median": round(statistics.median(values), 1),
            }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last", type=int, default=0, help="only the N most recent runs")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="SUBSTRING",
        help=(
            "drop runs whose id contains SUBSTRING. Repeatable. Used to quote a "
            "distribution that excludes a deliberately misconfigured experiment - "
            "name the run you removed rather than quietly picking a shorter window."
        ),
    )
    parser.add_argument(
        "--runs-dir",
        default=str(RUNS_DIR),
        help="where the recorded runs live (default: <project>/data/runs)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    if not runs_dir.exists():
        print(f"no runs recorded yet at {runs_dir}", file=sys.stderr)
        return 1

    run_dirs = sorted((d for d in runs_dir.iterdir() if d.is_dir()), key=lambda d: d.name)
    runs = [r for r in (read_run(d) for d in run_dirs) if r is not None]
    if args.exclude:
        runs = [r for r in runs if not any(pat in r["run_id"] for pat in args.exclude)]
    if args.last:
        runs = runs[-args.last :]
    if not runs:
        print(f"no readable trace.jsonl under {runs_dir}", file=sys.stderr)
        return 1

    summary = summarise(runs)
    if args.exclude:
        summary["excluded"] = args.exclude

    if args.json:
        print(json.dumps({"summary": summary, "runs": runs}, indent=2, ensure_ascii=False))
        return 0

    print(f"{len(runs)} run(s) in {runs_dir}")
    print()
    print(f"{'node':22} {'min':>8} {'median':>8} {'max':>8}")
    for node, stats in summary["nodes"].items():
        print(f"{node:22} {stats['min']:8.2f} {stats['median']:8.2f} {stats['max']:8.2f}")
    print()
    total = summary["total"]
    print(f"{'TOTAL':22} {total['min']:8.2f} {total['median']:8.2f} {total['max']:8.2f}")
    print()
    if args.exclude:
        print(f"excluded: {', '.join(args.exclude)}")
    print(f"documents per run : {summary['documents']}")
    print(f"records per run   : {summary['items']}")
    print(f"runs with a failed source : {summary['runs_with_a_failed_source']}")
    print(f"markdown fallbacks        : {summary['markdown_fallbacks']}  (must be 0)")
    if summary["markdown_fallbacks"]:
        print("  a non-zero count means the HTML template raised; see PITFALLS #31")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
