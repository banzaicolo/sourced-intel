"""Regression tests for caller-supplied run ids.

`run_id` reaches the filesystem (`data/runs/<run_id>`) and is accepted from
`POST /runs` and `sourced-intel run --run-id`. Before this existed, the value was
joined onto the runs directory unchecked, so `../escape` created a directory
next to `data/runs` instead of inside it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from sourced_intel.config import Settings
from sourced_intel.run_ids import InvalidRunId, new_run_id, validate_run_id

# Everything that must be refused. The first group traverses; the rest are
# shapes that are simply not run ids.
TRAVERSAL_IDS = [
    "../escape",
    "..",
    "/etc/passwd",
    "run-../../etc",
    "run-..",
    "run-x/../y",
    "run-x/y",
    r"run-x\y",
    "..\\escape",
    ".",
]
MALFORMED_IDS = [
    "",
    "escape",
    "run-",
    "run",
    "RUN-THING",
    "run id",
    "run-x\x00",
    "run-" + "a" * 65,
]


@pytest.mark.parametrize("bad", TRAVERSAL_IDS + MALFORMED_IDS)
def test_validate_run_id_rejects(bad: str) -> None:
    with pytest.raises(InvalidRunId):
        validate_run_id(bad)


def test_validate_run_id_rejects_non_strings() -> None:
    for bad in (None, 17, b"run-x", ["run-x"]):
        with pytest.raises(InvalidRunId):
            validate_run_id(bad)  # type: ignore[arg-type]


def test_generated_ids_are_accepted() -> None:
    for _ in range(50):
        run_id = new_run_id()
        assert validate_run_id(run_id) == run_id


@pytest.mark.parametrize("good", ["run-x", "run-20260926-013012-a1b2c3", "run-1.2_3"])
def test_generated_shape_is_accepted(good: str) -> None:
    assert validate_run_id(good) == good


def _settings_in(data_dir: Path) -> Settings:
    import dataclasses

    return dataclasses.replace(Settings.from_env(), data_dir=data_dir)


@pytest.mark.parametrize("bad", TRAVERSAL_IDS)
def test_run_dir_refuses_to_leave_the_runs_root(bad: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings_in(Path(tmp) / "data")
        settings.ensure_dirs()
        with pytest.raises(InvalidRunId):
            settings.run_dir(bad)


def test_run_dir_stays_inside_the_runs_root_for_a_valid_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        settings = _settings_in(data_dir)
        settings.ensure_dirs()
        run_dir = settings.run_dir("run-20260926-013012-a1b2c3")
        assert run_dir.parent == settings.runs_dir
        assert run_dir.resolve().is_relative_to(settings.runs_dir.resolve())


def test_traversal_cannot_create_a_sibling_directory() -> None:
    """The exact reproduction from the review: `../escape` used to escape."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        settings = _settings_in(data_dir)
        settings.ensure_dirs()
        with pytest.raises(InvalidRunId):
            settings.run_dir("../escape").mkdir(parents=True, exist_ok=True)
        assert not (data_dir / "escape").exists()
        assert not (Path(tmp) / "escape").exists()


def test_redacted_config_does_not_leak_the_key() -> None:
    """`GET /health` serves this dict to anyone who can reach the service."""
    import dataclasses

    secret = "0123456789abcdef0123456789abcdef.SECRETSECRET1234"
    settings = dataclasses.replace(Settings.from_env(), glm_api_key=secret)
    redacted = settings.redacted()
    assert redacted["glm_api_key"] == "<set>"
    assert secret[-4:] not in str(redacted)


def test_redacted_config_reports_an_unset_key() -> None:
    import dataclasses

    settings = dataclasses.replace(Settings.from_env(), glm_api_key="")
    assert settings.redacted()["glm_api_key"] == "<unset>"


# ---------------------------------------------------------------------------
# The HTTP surface: a bad id must be a 400, never a filesystem lookup.
# ---------------------------------------------------------------------------
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sourced_intel.api import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# A NUL byte is rejected by the HTTP client itself before it reaches the app, so
# it is covered at the unit level above instead of here.
@pytest.mark.parametrize("bad", ["escape", "run-..", "run-"])
def test_read_endpoints_reject_a_bad_run_id(client: TestClient, bad: str) -> None:
    for path in (f"/runs/{bad}", f"/runs/{bad}/report", f"/runs/{bad}/trace"):
        response = client.get(path)
        assert response.status_code == 400, (path, response.status_code)
        assert "invalid run id" in response.json()["detail"]


@pytest.mark.parametrize("bad", ["../escape", "run-x/../y", "/etc/passwd"])
def test_create_run_rejects_a_traversing_run_id(client: TestClient, bad: str) -> None:
    """Validation happens before `run_pipeline`, so no network and no directory."""
    response = client.post("/runs", json={"query": "深圳储能", "run_id": bad})
    assert response.status_code == 400
    assert "invalid run id" in response.json()["detail"]


def test_health_never_leaks_the_key_tail(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["config"]["glm_api_key"] in {"<set>", "<unset>"}
