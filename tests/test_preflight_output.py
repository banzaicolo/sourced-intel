"""Output-contract guards for `scripts/preflight.py`.

Regression this locks down: `--json` used to print the human-readable trace
first and the JSON blob last, so `json.loads(stdout)` raised. Anything that
consumed preflight output programmatically (CI, a reviewing agent) got a
`JSONDecodeError` instead of results. The two modes are now mutually exclusive.

The script is run as a subprocess on purpose: the contract is about what lands
on stdout, which is exactly what an external consumer sees. `--skip-net` keeps
this offline and fast, so it runs in the normal (non-slow) suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "preflight.py"


def run_preflight(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


class TestPreflightOutputContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Sampled before anything runs: every probe assertion below is a delta
        # against this, never an absolute claim about the whole directory.
        cls._probe_baseline = probes()
        cls.json_proc = run_preflight("--skip-net", "--json")
        cls.human_proc = run_preflight("--skip-net")

    def test_json_mode_stdout_is_valid_json_end_to_end(self):
        self.assertEqual(self.json_proc.returncode, 0, self.json_proc.stderr[-500:])
        payload = json.loads(self.json_proc.stdout)  # must not raise
        self.assertIn("results", payload)
        self.assertIn("hard_failures", payload)
        self.assertTrue(payload["results"], "expected at least one check result")
        for item in payload["results"]:
            self.assertIn("check", item)
            self.assertIn(item["status"], {"PASS", "FAIL", "WARN", "SKIP"})

    def test_json_mode_contains_no_human_trace(self):
        """Section banners and the [ OK ] rows must not leak into the JSON stream."""
        for marker in ("=== 1.", "pre-flight check", "[  OK  ]", "summary:"):
            self.assertNotIn(
                marker,
                self.json_proc.stdout,
                f"human-readable trace leaked into --json output: {marker!r}",
            )

    def test_human_mode_still_prints_the_full_report(self):
        out = self.human_proc.stdout
        self.assertIn("pre-flight check", out)
        for n in ("=== 1.", "=== 2.", "=== 3.", "=== 4.", "=== 5."):
            self.assertIn(n, out, f"section banner {n} missing from human output")
        self.assertIn("summary:", out)

    def test_human_mode_does_not_recommend_make(self):
        """`make` does not exist on Windows; the closing hint must not suggest it."""
        self.assertNotIn("make venv", self.human_proc.stdout)
        self.assertNotIn("make test", self.human_proc.stdout)

    def test_concurrent_preflights_all_succeed(self):
        """Six preflights at once must all exit 0 and all report a writable data dir.

        Regression: the writability probe wrote and unlinked a FIXED filename
        (`data/.preflight`). Concurrent runs therefore deleted each other's
        probe, and whichever unlinked second raised `FileNotFoundError` - which
        is recorded as a HARD failure, so the exit code became 1 and the check
        claimed a perfectly writable directory was broken.

        This is not hypothetical: running the suite while a container or a
        second CI job does the same thing is a normal day. Observed 5 of 6
        concurrent processes failing before the fix, 0 of 6 after.
        """
        procs = [
            subprocess.Popen(
                [sys.executable, str(SCRIPT), "--skip-net", "--json"],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        outputs = [p.communicate(timeout=180)[0] for p in procs]

        for index, proc in enumerate(procs):
            payload = json.loads(outputs[index])  # every stream stays pure JSON
            failed = [r["check"] for r in payload["results"] if r["status"] == "FAIL"]
            self.assertEqual(proc.returncode, 0, f"preflight #{index} failed: {failed}")
            self.assertEqual(payload["hard_failures"], 0, f"preflight #{index}: {failed}")

        # Six processes, six probes, six cleanups. Reported as a delta: a probe
        # left behind by some other run against this checkout is not this test's
        # business, but a probe from these six is.
        added = probes() - self.__class__._probe_baseline
        self.assertEqual(added, set(), f"probe files left behind: {sorted(added)}")

    def test_preflight_leaves_no_probe_file_behind(self):
        """The writability probe must clean up after itself on the sequential path too."""
        before = probes()
        for _ in range(3):
            run_preflight("--skip-net", "--json")
        added = probes() - before
        self.assertEqual(added, set(), f"probe files left in data/: {sorted(added)}")


def probes() -> set[str]:
    """Names of preflight probe files currently sitting in `data/`."""
    data_dir = ROOT / "data"
    if not data_dir.exists():
        return set()
    return {p.name for p in data_dir.glob(".preflight*")}


class TestCleanupFailureIsReported(unittest.TestCase):
    """A probe that cannot be removed must be reported, not swallowed.

    Regression: the first fix for the probe race wrapped the unlink in
    `except OSError: pass`. Probe files then accumulated in `data/` — eleven per
    suite run — with nothing anywhere saying why. The failure was invisible in
    the one environment that would have shown it, because on an ordinary machine
    the unlink simply succeeds.

    Asserted by **injection**, not by making a real file undeletable:
    "undeletable" is not portable, and under a sandbox that intercepts deletes a
    genuinely un-removed file and a swallowed error look identical. A `Temporary
    Directory` stands in for `data/` so this test cannot leave anything behind
    even when its own cleanup is refused.
    """

    def test_cleanup_failure_is_recorded_as_a_warning(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import preflight

        with tempfile.TemporaryDirectory() as tmp:
            fake_settings = SimpleNamespace(
                data_dir=Path(tmp),
                redacted=lambda: {"stub": True},
            )

            saved_results = list(preflight._results)
            saved_quiet = preflight._QUIET
            saved_get_settings = preflight.get_settings
            preflight._results.clear()
            preflight._QUIET = True
            preflight.get_settings = lambda: fake_settings

            original_unlink = Path.unlink

            def refusing(self, *args, **kwargs):
                raise PermissionError("simulated: file is busy")

            Path.unlink = refusing
            try:
                preflight.check_runtime()
            finally:
                Path.unlink = original_unlink
                preflight.get_settings = saved_get_settings

            try:
                reported = {r["check"]: r for r in preflight._results}

                self.assertIn("data directory writable", reported, "writability never ran")
                self.assertEqual(
                    reported["data directory writable"]["status"],
                    "PASS",
                    "the directory was writable; only the cleanup failed",
                )
                self.assertIn(
                    "preflight probe cleaned up",
                    reported,
                    "a cleanup failure was swallowed instead of reported",
                )
                self.assertEqual(reported["preflight probe cleaned up"]["status"], "WARN")

                blocking = [r for r in preflight._results if r["hard"] and r["status"] == "FAIL"]
                self.assertEqual(
                    blocking,
                    [],
                    "a cleanup failure is a warning, not a reason to block a run",
                )
            finally:
                preflight._results.clear()
                preflight._results.extend(saved_results)
                preflight._QUIET = saved_quiet


if __name__ == "__main__":
    unittest.main()
