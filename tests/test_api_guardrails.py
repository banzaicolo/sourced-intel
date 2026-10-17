"""The REST API's two deployment guardrails: a bearer token and a run cap.

Both are opt-in in name only - the token is off by default because the server
binds 127.0.0.1, and the cap is on by default because every `POST /runs` spends
real LLM quota. The tests pin the behaviour an exposing operator relies on.
"""

from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

import sourced_intel.api as api_module
from sourced_intel.api import app
from sourced_intel.config import Settings

TOKEN = "s3cret-token"
UNKNOWN_RUN = "run-20260101-000000-abcdef"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides) -> Settings:
    settings = dataclasses.replace(Settings(), **overrides)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    return settings


def _summary(run_id: str) -> dict:
    return {
        "summary": {
            "run_id": run_id,
            "status": "completed",
            "elapsed_seconds": 0.1,
            "clarification": None,
            "report_path": None,
            "raw_docs": 0,
            "intel_items": 0,
            "failed_sources": [],
            "trace_summary": {},
        }
    }


def test_health_stays_open_and_reports_the_guardrails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, api_token=TOKEN)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["config"]["api_auth"] == "bearer"
    assert response.json()["config"]["max_concurrent_runs"] == 1


@pytest.mark.parametrize("suffix", ["", "/report", "/trace"])
def test_reads_require_the_token_when_one_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    _configure(monkeypatch, api_token=TOKEN)
    path = f"/runs/{UNKNOWN_RUN}{suffix}"

    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path, headers={"Authorization": TOKEN}).status_code == 401
    # The right token gets past auth and falls through to the ordinary 404.
    assert client.get(path, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 404


def test_a_non_ascii_guess_is_a_401_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP clients refuse to send this, but the dependency must not 500 either."""
    _configure(monkeypatch, api_token=TOKEN)
    with pytest.raises(api_module.HTTPException) as caught:
        api_module.require_api_token("Bearer 密钥")
    assert caught.value.status_code == 401


def test_posts_require_the_token_when_one_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, api_token=TOKEN)
    response = client.post("/runs", json={"query": "深圳储能"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_without_a_token_the_api_stays_usable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loopback default must not need a credential to run the demo."""
    _configure(monkeypatch, api_token="")
    monkeypatch.setattr(api_module, "run_pipeline", lambda q, run_id=None: _summary("run-ok"))
    response = client.post("/runs", json={"query": "深圳储能"})
    assert response.status_code == 200
    assert response.json()["run_id"] == "run-ok"


def test_a_run_beyond_the_cap_is_429_and_the_cap_is_not_leaked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, max_concurrent_runs=1)
    monkeypatch.setattr(api_module, "run_pipeline", lambda q, run_id=None: _summary("run-ok"))

    slots = api_module._slot_pool()
    assert slots.acquire(blocking=False), "the pool test assumes one free slot"
    try:
        response = client.post("/runs", json={"query": "深圳储能"})
    finally:
        slots.release()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"

    # ...and the rejected request did not consume a permit: the next one runs.
    assert client.post("/runs", json={"query": "深圳储能"}).status_code == 200


def test_a_crashed_run_releases_its_permit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, max_concurrent_runs=1)

    def boom(query: str, run_id: str | None = None) -> dict:
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(api_module, "run_pipeline", boom)
    assert client.post("/runs", json={"query": "深圳储能"}).status_code == 500

    slots = api_module._slot_pool()
    assert slots.acquire(blocking=False), "the permit leaked on the error path"
    slots.release()
