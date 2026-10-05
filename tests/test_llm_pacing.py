"""The GLM request pacer - `GLM_MAX_CONCURRENCY` / `GLM_MIN_INTERVAL_SECONDS`.

Regression this locks down: both settings were declared in `Settings`, written
into `.env.example`, and described in the config comments as "the pipeline
serialises GLM calls by default". Nothing ever read them. Node 3 sizes its
`ThreadPoolExecutor` from `EXTRACT_MAX_CONCURRENCY` (4), so four GLM requests
went out simultaneously with no spacing; the free tier answered
``429 code 1302`` and each 429 abandoned a whole batch. Two consecutive real runs
lost 2 of 5 batches each - 40% of the retrieved documents never reached the
report.

The pacer is tested directly (deterministic, no network) and then through
`GLMClient` with a stub transport, so the wiring - not just the helper - is
covered.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.config import Settings, get_settings  # noqa: E402
from sourced_intel.llm import GLMClient, ProviderPacer  # noqa: E402

#: A shape `llm_keys.key_problem` accepts, so the client constructs.
FAKE_KEY = "0123456789abcdef0123456789abcdef.Abcdefghijklmnop"


def glm_settings(**overrides) -> Settings:
    base = {
        "glm_api_key": FAKE_KEY,
        "llm_provider": "glm",
        "glm_max_concurrency": 1,
        "glm_min_interval": 0.05,
    }
    base.update(overrides)
    return replace(get_settings(), **base)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("this stub has no JSON body")
        return self._payload


def ok_response() -> FakeResponse:
    return FakeResponse(
        payload={
            "model": "glm-4.7",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


class RecordingTransport:
    """Stands in for `httpx.Client`, recording in-flight count and start times."""

    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls = 0
        self.starts: list[float] = []
        self.max_inflight = 0
        self._inflight = 0
        self._lock = threading.Lock()

    def post(self, url, **kwargs) -> FakeResponse:
        with self._lock:
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
            self.starts.append(time.monotonic())
            index = self.calls
            self.calls += 1
        try:
            time.sleep(0.02)
            if self.responses:
                return self.responses[min(index, len(self.responses) - 1)]
            return ok_response()
        finally:
            with self._lock:
                self._inflight -= 1


def gaps(starts: list[float]) -> list[float]:
    ordered = sorted(starts)
    return [b - a for a, b in pairwise(ordered)]


# ---------------------------------------------------------------------------
# The pacer itself
# ---------------------------------------------------------------------------
class TestProviderPacer:
    def test_one_slot_serialises_and_spaces_the_start_times(self):
        pacer = ProviderPacer(concurrency=1, min_interval=0.05)
        starts: list[float] = []
        inflight = 0
        max_inflight = 0
        lock = threading.Lock()

        def worker() -> None:
            nonlocal inflight, max_inflight
            with pacer.slot():
                with lock:
                    inflight += 1
                    max_inflight = max(max_inflight, inflight)
                    starts.append(time.monotonic())
                time.sleep(0.02)
                with lock:
                    inflight -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert max_inflight == 1, "more than one request was in flight at once"
        assert all(g >= 0.045 for g in gaps(starts)), f"requests were not paced: {gaps(starts)}"

    def test_the_configured_parallelism_is_actually_available(self):
        """A pacer that serialises everything would make the knob a different lie."""
        pacer = ProviderPacer(concurrency=3, min_interval=0.0)
        barrier = threading.Barrier(3)
        reached = []

        def worker() -> None:
            with pacer.slot():
                reached.append(threading.get_ident())
                barrier.wait(timeout=5)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(reached) == 3, "3 slots were configured but only a subset ran concurrently"

    def test_a_zero_interval_does_not_sleep(self):
        pacer = ProviderPacer(concurrency=2, min_interval=0.0)
        started = time.monotonic()
        for _ in range(5):
            with pacer.slot():
                pass
        assert time.monotonic() - started < 0.05


# ---------------------------------------------------------------------------
# The wiring: settings -> GLMClient -> the outbound request
# ---------------------------------------------------------------------------
class TestGLMClientUsesThePacer:
    def test_concurrent_chats_are_serialised_and_spaced(self):
        client = GLMClient(glm_settings(glm_max_concurrency=1, glm_min_interval=0.08))
        transport = RecordingTransport()
        client._client = transport

        threads = [threading.Thread(target=client.chat, args=("hi",)) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert transport.calls == 3
        assert transport.max_inflight == 1, "the pacer did not serialise the concurrent chats"
        assert all(g >= 0.07 for g in gaps(transport.starts)), (
            f"calls were not spaced: {gaps(transport.starts)}"
        )

    def test_the_settings_reach_the_pacer(self):
        client = GLMClient(glm_settings(glm_max_concurrency=4, glm_min_interval=0.33))
        assert client._pacer.min_interval == pytest.approx(0.33)

    def test_a_single_slot_client_does_not_deadlock_on_the_thinking_downgrade(self):
        """The 1210 path recurses into `call`; holding the only slot there would hang forever."""
        client = GLMClient(glm_settings(glm_max_concurrency=1))
        transport = RecordingTransport(
            responses=[
                FakeResponse(
                    status_code=400, text='{"error":{"code":"1210","message":"该模型始终思考"}}'
                ),
                ok_response(),
            ]
        )
        client._client = transport

        result: list[object] = []

        def run() -> None:
            result.append(client.chat("hi"))

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=10)

        assert not thread.is_alive(), "the pacer deadlocked against its own retry path"
        assert transport.calls == 2
        assert client._thinking == "low"
        assert result and getattr(result[0], "text", "") == "ok"


# ---------------------------------------------------------------------------
# The knobs are read from the environment at all
# ---------------------------------------------------------------------------
class TestSettingsAreNotDead:
    def test_both_knobs_are_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GLM_API_KEY", FAKE_KEY)
        monkeypatch.setenv("GLM_MAX_CONCURRENCY", "2")
        monkeypatch.setenv("GLM_MIN_INTERVAL_SECONDS", "0.25")

        settings = Settings.from_env()

        assert settings.glm_max_concurrency == 2
        assert settings.glm_min_interval == pytest.approx(0.25)

    def test_defaults_are_the_measured_safe_point(self):
        """2 in flight is the fastest setting that did not throttle; 4 lost 3 of 5 calls."""
        settings = Settings.from_env()
        assert settings.glm_max_concurrency == 2
        assert settings.glm_min_interval > 0, "unpaced concurrency is what caused the 429s"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
