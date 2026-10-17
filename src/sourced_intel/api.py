"""FastAPI entry point - the productisation path.

Run:
    uvicorn sourced_intel.api:app --reload --app-dir src

Endpoints:
    GET  /health                    liveness + effective configuration
    POST /runs                      run the pipeline for a query (synchronous)
    GET  /runs/{run_id}             run summary from disk
    GET  /runs/{run_id}/report      the rendered report
    GET  /runs/{run_id}/trace       per-node trace records

Every route except /health requires `Authorization: Bearer <SOURCED_INTEL_API_TOKEN>`
when that variable is set. `POST /runs` additionally admits at most
`MAX_CONCURRENT_RUNS` executions at a time (default 1) and answers 429 beyond it.
With no token set, the gate is open - which is why the default bind is loopback.
"""

from __future__ import annotations

import hmac
import json
import sys
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sourced_intel.config import get_settings
from sourced_intel.graph import run_pipeline
from sourced_intel.run_ids import InvalidRunId, validate_run_id
from sourced_intel.state import list_snapshots, load_snapshot

app = FastAPI(
    title="Sourced Intel API",
    version="0.1.0",
    description=(
        "Multi-source intelligence mining: one natural-language question in, "
        "one sourced report out."
    ),
)


class RunRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500, description="Natural-language question")
    run_id: str | None = Field(
        None,
        description=(
            "Optional caller-supplied run id. Must match `run-[A-Za-z0-9][A-Za-z0-9._-]*`; "
            "ids become directory names, so anything that could traverse a path is rejected "
            "with 400."
        ),
    )


#: One permit per allowed concurrent run. `POST /runs` is synchronous, so
#: without this an exposed service turns every caller into a paid worker.
_slots_lock = threading.Lock()
_run_slots: threading.BoundedSemaphore | None = None
_run_slots_size = 0


def _slot_pool() -> threading.BoundedSemaphore:
    """The run semaphore, rebuilt if `MAX_CONCURRENT_RUNS` changed."""
    global _run_slots, _run_slots_size
    size = max(1, get_settings().max_concurrent_runs)
    with _slots_lock:
        if _run_slots is None or _run_slots_size != size:
            _run_slots = threading.BoundedSemaphore(size)
            _run_slots_size = size
        return _run_slots


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate; disabled when no token is configured.

    The comparison is constant-time and byte-wise: a caller must not be able to
    learn the token from response timing, and a non-ASCII guess must not turn
    into a 500 from `compare_digest`.
    """
    expected = get_settings().api_token
    if not expected:
        return
    scheme, _, presented = (authorization or "").partition(" ")
    supplied = presented.strip().encode("utf-8")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _validated_run_id(run_id: str) -> str:
    """Turn a caller-supplied run id into a 400 instead of a filesystem lookup."""
    try:
        return validate_run_id(run_id)
    except InvalidRunId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "config": settings.redacted()}


@app.post("/runs", dependencies=[Depends(require_api_token)])
def create_run(payload: RunRequest) -> JSONResponse:
    run_id = _validated_run_id(payload.run_id) if payload.run_id is not None else None
    slots = _slot_pool()
    if not slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail=(
                f"{get_settings().max_concurrent_runs} run(s) already in flight; "
                "retry when one finishes"
            ),
            headers={"Retry-After": "30"},
        )
    try:
        try:
            result = run_pipeline(payload.query, run_id=run_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    finally:
        slots.release()

    summary = result["summary"]
    return JSONResponse(
        {
            "run_id": summary["run_id"],
            "status": summary["status"],
            "elapsed_seconds": summary["elapsed_seconds"],
            "clarification": summary["clarification"],
            "report_url": f"/runs/{summary['run_id']}/report" if summary["report_path"] else None,
            "documents": summary["raw_docs"],
            "records": summary["intel_items"],
            "failed_sources": summary["failed_sources"],
            "trace_summary": summary["trace_summary"],
        }
    )


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_token)])
def get_run(run_id: str) -> dict:
    run_id = _validated_run_id(run_id)
    settings = get_settings()
    snapshot_dir = settings.snapshots_dir(run_id)
    snapshots = list_snapshots(snapshot_dir)
    if not snapshots:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")

    final = load_snapshot(snapshot_dir, "99_final") if "99_final" in snapshots else None
    state = final or {}
    return {
        "run_id": run_id,
        "snapshots": snapshots,
        "status": state.get("status"),
        "failed_sources": state.get("failed_sources", []),
        "errors": state.get("errors", []),
        "report_path": state.get("report_path"),
        "records": len(state.get("intel_items", []) or []),
    }


@app.get("/runs/{run_id}/report", dependencies=[Depends(require_api_token)])
def get_report(run_id: str):
    run_id = _validated_run_id(run_id)
    settings = get_settings()
    run_dir = settings.run_dir(run_id)
    html_path = run_dir / "report.html"
    md_path = run_dir / "report.md"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    if md_path.exists():
        return JSONResponse({"format": "markdown", "content": md_path.read_text(encoding="utf-8")})
    raise HTTPException(status_code=404, detail=f"no report for run {run_id}")


@app.get("/runs/{run_id}/trace", dependencies=[Depends(require_api_token)])
def get_trace(run_id: str) -> dict:
    run_id = _validated_run_id(run_id)
    settings = get_settings()
    trace_path = settings.run_dir(run_id) / "trace.jsonl"
    if not trace_path.exists():
        raise HTTPException(status_code=404, detail=f"no trace for run {run_id}")
    records = [
        json.loads(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {"run_id": run_id, "nodes": len(records), "records": records}
