"""Budgets and the failure marker for the optional GDELT source.

GDELT is the *breadth* source: nice to have, never allowed to hold a run
hostage. Two mechanisms implement that, and both are easy to break silently
because the failure they cause looks like "the report is just a bit thinner".

  * `GDELT_TIMEOUT_SECONDS` - from mainland China this host usually dies in the
    TLS handshake rather than answering 429. Waiting the global 10s timeout on
    every run is pure dead time: node 02 measured 4.5s cold (paying the 4s
    handshake timeout) against 0.9s on the next run, which short-circuits.
  * a failure marker with a TTL (`GDELT_FAILURE_TTL_SECONDS`) - after one
    failure the adapter short-circuits instead of re-waiting. The source is
    still reported as failed; it is skipped, never hidden.

No network is touched: the HTTP layer is replaced with a recording stub.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.config import REGION_DISPLAY, REGION_TO_ADCODE  # noqa: E402
from sourced_intel.schemas import QuerySpec  # noqa: E402
from sourced_intel.tools.http import FetchResult  # noqa: E402
from sourced_intel.tools.sources.gdelt import CACHE_TTL_SECONDS, GdeltAdapter  # noqa: E402


def make_query() -> QuerySpec:
    adcode = REGION_TO_ADCODE["深圳"]
    return QuerySpec(
        industry="energy storage",
        region_adcode=adcode,
        region_name=REGION_DISPLAY[adcode],
        timespan="3m",
        keywords=[],
        raw_query="深圳 储能 项目 投资",
    )


class FakeHttp:
    """Records every get() call so the arguments the adapter passes can be asserted."""

    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        return self.result


def ok_payload(articles: int = 1) -> FetchResult:
    body = (
        '{"articles": [{"url": "https://example.com/a", "title": "深圳储能项目开工",'
        ' "seendate": "20260916T101500Z", "domain": "example.com"}]}'
    )
    if articles == 0:
        body = '{"articles": []}'
    return FetchResult(
        url="https://api.gdeltproject.org/api/v2/doc/doc", status=200, text=body, ok=True
    )


def failed_result() -> FetchResult:
    return FetchResult(
        url="https://api.gdeltproject.org/api/v2/doc/doc",
        status=0,
        text="",
        ok=False,
        error="ConnectTimeout: TLS handshake timed out",
    )


def make_adapter(tmp_path: Path, **overrides) -> GdeltAdapter:
    """An adapter pointed at a private cache dir, so no test can see another's state."""
    adapter = GdeltAdapter()
    adapter.settings = replace(adapter.settings, data_dir=tmp_path, **overrides)
    adapter.cache_dir = adapter.settings.cache_dir / "gdelt"
    return adapter


def age_marker(adapter: GdeltAdapter, seconds: float) -> Path:
    """Push the failure marker's mtime into the past, so its TTL looks expired."""
    markers = list(adapter.cache_dir.glob("*.failed"))
    assert markers, "expected a failure marker to exist"
    stamp = time.time() - seconds
    for marker in markers:
        os.utime(marker, (stamp, stamp))
    return markers[0]


# ---------------------------------------------------------------------------
# The short timeout
# ---------------------------------------------------------------------------
class TestOwnTimeout:
    def test_gdelt_gets_its_own_budget_not_the_global_http_timeout(self, tmp_path):
        adapter = make_adapter(tmp_path, gdelt_timeout=1.5, gdelt_min_interval=3.0)
        fake = FakeHttp(ok_payload())
        adapter.http = fake

        adapter.fetch(make_query())

        assert fake.calls, "the adapter never called the HTTP layer"
        call = fake.calls[0]
        assert call["timeout"] == 1.5
        assert call["timeout"] < adapter.settings.http_timeout, (
            "the global timeout is longer than GDELT's own budget - the point of the setting is lost"
        )

    def test_pacing_and_retry_policy_are_still_passed_through(self, tmp_path):
        """A cached 429 must not be retried: one live attempt per run is the contract."""
        adapter = make_adapter(tmp_path, gdelt_min_interval=7.0)
        fake = FakeHttp(ok_payload())
        adapter.http = fake

        adapter.fetch(make_query())

        call = fake.calls[0]
        assert call["min_interval"] == 7.0
        assert call["retries"] == 0

    def test_the_configured_value_is_what_reaches_the_network(self, tmp_path):
        """Guards the setting being declared but never read - which is how it broke before."""
        adapter = make_adapter(tmp_path, gdelt_timeout=2.25)
        fake = FakeHttp(ok_payload())
        adapter.http = fake

        adapter.fetch(make_query())

        assert fake.calls[0]["timeout"] == 2.25


# ---------------------------------------------------------------------------
# The failure marker
# ---------------------------------------------------------------------------
class TestFailureMarker:
    def test_a_failure_is_marked_and_raised(self, tmp_path):
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=600.0)
        adapter.http = FakeHttp(failed_result())

        with pytest.raises(RuntimeError) as excinfo:
            adapter.fetch(make_query())

        assert "unavailable" in str(excinfo.value)
        assert len(list(adapter.cache_dir.glob("*.failed"))) == 1

    def test_the_next_call_short_circuits_without_touching_the_network(self, tmp_path):
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=600.0)
        adapter.http = FakeHttp(failed_result())
        with pytest.raises(RuntimeError):
            adapter.fetch(make_query())

        retry = FakeHttp(ok_payload())
        adapter.http = retry
        with pytest.raises(RuntimeError) as excinfo:
            adapter.fetch(make_query())

        assert retry.calls == [], "the adapter retried a source it already knew was unreachable"
        assert "unreachable" in str(excinfo.value)

    def test_the_short_circuit_error_names_the_source_and_the_retry_delay(self, tmp_path):
        """The failure must stay attributable - skipped, never hidden."""
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=600.0)
        adapter.http = FakeHttp(failed_result())
        with pytest.raises(RuntimeError):
            adapter.fetch(make_query())

        adapter.http = FakeHttp(ok_payload())
        with pytest.raises(RuntimeError) as excinfo:
            adapter.fetch(make_query())

        message = str(excinfo.value)
        assert adapter.name in message
        assert "600" in message

    def test_the_marker_expires_and_a_live_attempt_is_made_again(self, tmp_path):
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=60.0)
        adapter.http = FakeHttp(failed_result())
        with pytest.raises(RuntimeError):
            adapter.fetch(make_query())

        age_marker(adapter, seconds=61)

        retry = FakeHttp(ok_payload())
        adapter.http = retry
        docs = adapter.fetch(make_query())

        assert retry.calls, "no live attempt was made after the failure marker expired"
        assert len(docs) == 1

    def test_a_failure_ttl_of_zero_retries_every_run(self, tmp_path):
        """Documented escape hatch: set the TTL to 0 to check the source on every run."""
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=0.0)
        adapter.http = FakeHttp(failed_result())
        with pytest.raises(RuntimeError):
            adapter.fetch(make_query())

        retry = FakeHttp(ok_payload())
        adapter.http = retry
        adapter.fetch(make_query())

        assert retry.calls

    def test_a_successful_fetch_clears_the_marker(self, tmp_path):
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=60.0)
        adapter.http = FakeHttp(failed_result())
        with pytest.raises(RuntimeError):
            adapter.fetch(make_query())
        age_marker(adapter, seconds=61)

        adapter.http = FakeHttp(ok_payload())
        adapter.fetch(make_query())

        assert not list(adapter.cache_dir.glob("*.failed")), (
            "the marker survived a successful fetch - the next run would skip a working source"
        )

    def test_the_marker_is_isolated_per_query(self, tmp_path):
        """One failing query must not disable GDELT for a different query."""
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=600.0)
        adapter.http = FakeHttp(failed_result())
        with pytest.raises(RuntimeError):
            adapter.fetch(make_query())

        other = make_query().model_copy(update={"industry": "hydrogen"})
        retry = FakeHttp(ok_payload())
        adapter.http = retry
        adapter.fetch(other)

        assert retry.calls, "a failure for one query disabled an unrelated query"


# ---------------------------------------------------------------------------
# The existing cache contract, pinned while we are here
# ---------------------------------------------------------------------------
class TestResponseCache:
    def test_a_cached_response_is_not_refetched_on_the_next_run(self, tmp_path):
        """CACHE_TTL_SECONDS exists so a demo never waits on GDELT twice."""
        adapter = make_adapter(tmp_path, gdelt_failure_ttl=0.0)
        first = FakeHttp(ok_payload())
        adapter.http = first
        adapter.fetch(make_query())
        assert first.calls

        second = FakeHttp(failed_result())
        adapter.http = second
        docs = adapter.fetch(make_query())

        assert len(docs) == 1, "the cached payload was not served when the live fetch failed"
        assert CACHE_TTL_SECONDS >= 600


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
