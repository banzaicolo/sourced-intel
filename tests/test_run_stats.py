"""Tests for scripts/run_stats.py - the script the docs quote their numbers from.

A documented performance figure is only worth as much as the tool that produces
it. If `run_stats.py` silently double-counted a node or ignored a malformed line,
the README would quote a wrong number and nothing would notice - which is the
same "documented but not wired" failure this round is otherwise about.

Run:
    pytest tests/test_run_stats.py -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "run_stats.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_stats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_stats"] = module
    spec.loader.exec_module(module)
    return module


run_stats = load_module()


def write_run(root: Path, name: str, durations: dict[str, float], **state) -> Path:
    run_dir = root / name
    (run_dir / "snapshots").mkdir(parents=True)
    lines = [json.dumps({"node": node, "duration_ms": ms}) for node, ms in durations.items()]
    (run_dir / "trace.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (run_dir / "snapshots" / "99_final.json").write_text(
        json.dumps({"state": state}), encoding="utf-8"
    )
    return run_dir


@pytest.fixture
def runs_dir(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    write_run(
        root,
        "run-0001",
        {"01_parse_intent": 5000.0, "02_retrieve": 2000.0},
        raw_docs=[{"url": "a"}, {"url": "b"}],
        intel_items=[{"title": "x"}],
        failed_sources=["gdelt"],
    )
    write_run(
        root,
        "run-0002",
        {"01_parse_intent": 7000.0, "02_retrieve": 40000.0},
        raw_docs=[{"url": "a"}],
        intel_items=[],
    )
    return root


class TestReadingTraces:
    def test_reads_durations_from_each_node(self, runs_dir):
        run = run_stats.read_run(runs_dir / "run-0001")
        assert run["durations"] == {"01_parse_intent": 5.0, "02_retrieve": 2.0}
        assert run["total"] == 7.0

    def test_pulls_document_and_item_counts_from_the_final_snapshot(self, runs_dir):
        run = run_stats.read_run(runs_dir / "run-0001")
        assert run["documents"] == 2
        assert run["items"] == 1
        assert run["failed_sources"] == ["gdelt"]

    def test_a_missing_trace_is_skipped_not_crashed(self, tmp_path):
        empty = tmp_path / "run-broken"
        empty.mkdir()
        assert run_stats.read_run(empty) is None

    def test_a_malformed_line_does_not_abort_the_run(self, tmp_path):
        run_dir = tmp_path / "run-partial"
        run_dir.mkdir()
        (run_dir / "trace.jsonl").write_text(
            '{"node": "01_parse_intent", "duration_ms": 1000}\nnot json at all\n',
            encoding="utf-8",
        )
        run = run_stats.read_run(run_dir)
        assert run["total"] == 1.0

    def test_a_run_with_no_snapshot_still_reports_its_timing(self, tmp_path):
        run_dir = tmp_path / "run-no-snapshot"
        run_dir.mkdir()
        (run_dir / "trace.jsonl").write_text(
            '{"node": "03_extract", "duration_ms": 1500}\n', encoding="utf-8"
        )
        run = run_stats.read_run(run_dir)
        assert run["total"] == 1.5
        assert run["documents"] == 0


class TestSummary:
    def test_min_median_max_are_the_real_spread(self, runs_dir):
        runs = [
            run_stats.read_run(runs_dir / "run-0001"),
            run_stats.read_run(runs_dir / "run-0002"),
        ]
        summary = run_stats.summarise(runs)
        assert summary["total"] == {"min": 7.0, "max": 47.0, "median": 27.0}
        assert summary["nodes"]["02_retrieve"] == {"min": 2.0, "max": 40.0, "median": 21.0}

    def test_nodes_absent_from_every_run_are_omitted(self, runs_dir):
        runs = [run_stats.read_run(runs_dir / "run-0001")]
        summary = run_stats.summarise(runs)
        assert "05_analyze" not in summary["nodes"]

    def test_markdown_fallbacks_are_counted(self, tmp_path):
        root = tmp_path / "runs"
        root.mkdir()
        write_run(root, "run-a", {"06_generate_report": 100.0}, report_format="html")
        runs = [run_stats.read_run(root / "run-a")]
        assert run_stats.summarise(runs)["markdown_fallbacks"] == 0

        meta = {"06_generate_report": {"report_format": "markdown"}}
        write_run(root, "run-b", {"06_generate_report": 100.0}, node_meta=meta)
        runs.append(run_stats.read_run(root / "run-b"))
        assert run_stats.summarise(runs)["markdown_fallbacks"] == 1


class TestCli:
    def test_exclude_drops_a_named_run(self, runs_dir, capsys):
        assert run_stats.main(["--runs-dir", str(runs_dir), "--exclude", "0002"]) == 0
        out = capsys.readouterr().out
        assert "1 run(s)" in out
        assert "excluded: 0002" in out

    def test_last_keeps_the_most_recent(self, runs_dir, capsys):
        run_stats.main(["--runs-dir", str(runs_dir), "--last", "1"])
        out = capsys.readouterr().out
        assert "1 run(s)" in out
        # The newer run totals 47.0 s; the older one totals 7.0 s. Reading the TOTAL
        # row means --last picked the tail of the sorted list, not the head.
        total_line = next(line for line in out.splitlines() if line.startswith("TOTAL"))
        values = [float(part) for part in total_line.split()[1:]]
        assert values == [47.0, 47.0, 47.0]

    def test_json_output_is_pure_json(self, runs_dir, capsys):
        assert run_stats.main(["--runs-dir", str(runs_dir), "--json"]) == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["summary"]["runs"] == 2
        assert len(parsed["runs"]) == 2

    def test_empty_directory_is_a_clean_failure(self, tmp_path, capsys):
        empty = tmp_path / "nothing"
        empty.mkdir()
        assert run_stats.main(["--runs-dir", str(empty)]) == 1
        assert "no readable trace.jsonl" in capsys.readouterr().err

    def test_missing_directory_is_a_clean_failure(self, tmp_path, capsys):
        assert run_stats.main(["--runs-dir", str(tmp_path / "nope")]) == 1
        assert "no runs recorded yet" in capsys.readouterr().err
