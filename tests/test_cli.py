"""Coverage for the thin CLI dispatch shims that demo-only tests never reach.

`demo` is exercised end-to-end in ``test_demo_and_cli.py``. The *other*
subcommands — ``run``, ``stats``, ``preflight``, ``ui``, ``api`` — are dispatch
bodies over ``run_pipeline``, ``runpy`` and ``subprocess``. They hold no domain
logic, but they do hold real control flow (error paths, argument forwarding,
exit codes) that was previously uncovered. Each boundary is patched at the seam
so no network, LLM or live server is involved.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel import cli  # noqa: E402


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _summary(**kw) -> dict:
    """A realistic ``run_pipeline`` result, so the output branches are exercised."""
    base = {
        "run_id": "run-20261018-000000-abc123",
        "status": "completed",
        "elapsed_seconds": 1.23,
        "raw_docs": 5,
        "intel_items": 3,
        "failed_sources": [],
        "clarification": None,
        "report_path": "/tmp/report.html",
        "report_format": "html",
        "run_dir": "/tmp/run-dir",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# _fail
# ---------------------------------------------------------------------------
class TestFail:
    def test_returns_two_and_prints_to_stderr(self, capsys):
        assert cli._fail("boom") == 2
        assert "error: boom" in capsys.readouterr().err

    def test_hint_is_printed_when_given(self, capsys):
        cli._fail("boom", "try this instead")
        assert "try this instead" in capsys.readouterr().err

    def test_no_hint_indent_when_absent(self, capsys):
        cli._fail("boom")
        assert "       " not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
class TestCmdRun:
    def test_an_invalid_run_id_is_rejected_before_the_pipeline_runs(self, monkeypatch, capsys):
        """`../escape` must never reach the pipeline — it is a path traversal."""
        calls = []
        monkeypatch.setattr("sourced_intel.graph.run_pipeline", lambda *a, **k: calls.append(1))
        rc = cli.cmd_run(_ns(run_id="../escape", query="q", json=False))
        assert rc == 2
        assert "invalid run id" in capsys.readouterr().err
        assert calls == [], "the pipeline must not run for an invalid id"

    def test_a_pipeline_exception_is_reported(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr("sourced_intel.graph.run_pipeline", boom)
        rc = cli.cmd_run(_ns(run_id=None, query="q", json=False))
        assert rc == 2
        assert "RuntimeError: provider down" in capsys.readouterr().err

    def test_json_output_and_zero_exit(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "sourced_intel.graph.run_pipeline", lambda *a, **k: {"summary": _summary()}
        )
        rc = cli.cmd_run(_ns(run_id=None, query="q", json=True))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["run_id"] == "run-20261018-000000-abc123"

    def test_json_output_returns_one_when_no_report(self, monkeypatch):
        monkeypatch.setattr(
            "sourced_intel.graph.run_pipeline",
            lambda *a, **k: {"summary": _summary(report_path=None)},
        )
        assert cli.cmd_run(_ns(run_id=None, query="q", json=True)) == 1

    def test_human_output_prints_every_section(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "sourced_intel.graph.run_pipeline",
            lambda *a, **k: {
                "summary": _summary(
                    failed_sources=["gdelt"],
                    clarification="did you mean Shenzhen?",
                )
            },
        )
        rc = cli.cmd_run(_ns(run_id=None, query="q", json=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "run-20261018-000000-abc123" in out
        assert "completed" in out
        assert "5 fetched -> 3 records" in out
        assert "gdelt" in out
        assert "did you mean Shenzhen?" in out

    def test_human_output_returns_one_when_no_report(self, monkeypatch):
        monkeypatch.setattr(
            "sourced_intel.graph.run_pipeline",
            lambda *a, **k: {"summary": _summary(report_path=None)},
        )
        assert cli.cmd_run(_ns(run_id=None, query="q", json=False)) == 1


# ---------------------------------------------------------------------------
# stats / preflight — both forward argv to a script via runpy
# ---------------------------------------------------------------------------
class TestCmdStats:
    def test_argument_forwarding_and_zero_exit(self, monkeypatch):
        calls = []

        def fake_run_path(path, run_name=None):
            calls.append((path, run_name, sys.argv.copy()))
            return None

        monkeypatch.setattr("runpy.run_path", fake_run_path)
        rc = cli.cmd_stats(_ns(last=3, exclude=["run-x", "run-y"], json=True))
        assert rc == 0
        _, run_name, argv = calls[0]
        assert run_name == "__main__"
        assert argv.count("--exclude") == 2
        assert "run-x" in argv and "run-y" in argv
        assert "--last" in argv and "3" in argv
        assert "--json" in argv

    def test_no_flags_when_absent(self, monkeypatch):
        calls = []

        def fake_run_path(path, run_name=None):
            calls.append(sys.argv.copy())
            return None

        monkeypatch.setattr("runpy.run_path", fake_run_path)
        assert cli.cmd_stats(_ns(last=None, exclude=None, json=False)) == 0
        argv = calls[0]
        assert "--last" not in argv and "--exclude" not in argv and "--json" not in argv

    def test_system_exit_code_is_propagated(self, monkeypatch):
        def fake_run_path(path, run_name=None):
            raise SystemExit(7)

        monkeypatch.setattr("runpy.run_path", fake_run_path)
        assert cli.cmd_stats(_ns(last=None, exclude=None, json=False)) == 7


class TestCmdPreflight:
    def test_flags_forwarded(self, monkeypatch):
        calls = []

        def fake_run_path(path, run_name=None):
            calls.append(sys.argv.copy())
            return None

        monkeypatch.setattr("runpy.run_path", fake_run_path)
        rc = cli.cmd_preflight(_ns(json=True, skip_net=True))
        assert rc == 0
        argv = calls[0]
        assert "--json" in argv and "--skip-net" in argv

    def test_no_flags_when_absent(self, monkeypatch):
        calls = []

        def fake_run_path(path, run_name=None):
            calls.append(sys.argv.copy())
            return None

        monkeypatch.setattr("runpy.run_path", fake_run_path)
        assert cli.cmd_preflight(_ns(json=False, skip_net=False)) == 0
        assert "--json" not in calls[0] and "--skip-net" not in calls[0]

    def test_system_exit_code_is_propagated(self, monkeypatch):
        def fake_run_path(path, run_name=None):
            raise SystemExit(1)

        monkeypatch.setattr("runpy.run_path", fake_run_path)
        assert cli.cmd_preflight(_ns(json=False, skip_net=False)) == 1


# ---------------------------------------------------------------------------
# ui / api — both hand off to subprocess
# ---------------------------------------------------------------------------
class TestCmdUi:
    def test_invokes_streamlit(self, monkeypatch):
        calls = []

        def fake_call(cmd):
            calls.append(cmd)
            return 0

        monkeypatch.setattr("subprocess.call", fake_call)
        assert cli.cmd_ui(_ns()) == 0
        assert calls[0][2] == "streamlit"


class TestCmdApi:
    def test_invokes_uvicorn_with_host_and_port(self, monkeypatch):
        calls = []

        def fake_call(cmd):
            calls.append(cmd)
            return 0

        monkeypatch.setattr("subprocess.call", fake_call)
        assert cli.cmd_api(_ns(host="127.0.0.1", port=8000)) == 0
        cmd = calls[0]
        assert cmd[1] == "-m" and cmd[2] == "uvicorn"
        assert "--host" in cmd and "127.0.0.1" in cmd
        assert "--port" in cmd and "8000" in cmd


# ---------------------------------------------------------------------------
# demo failure paths (the happy path is covered elsewhere)
# ---------------------------------------------------------------------------
class TestCmdDemoFailures:
    def test_render_error_is_reported(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise RuntimeError("template exploded")

        monkeypatch.setattr("sourced_intel.report.render.render_html", boom)
        rc = cli.cmd_demo(_ns(out="/tmp/never-written.html"))
        assert rc == 2
        assert "RuntimeError: template exploded" in capsys.readouterr().err

    def test_markdown_fallback_is_rejected(self, monkeypatch, tmp_path, capsys):
        """A silent Markdown fallback is exactly the bug this command exists to surface."""
        md = tmp_path / "report.md"
        md.write_text("# not html", encoding="utf-8")
        monkeypatch.setattr("sourced_intel.report.render.render_html", lambda *a, **k: md)
        rc = cli.cmd_demo(_ns(out=str(tmp_path / "out.html")))
        assert rc == 2
        assert "Markdown file was written" in capsys.readouterr().err
