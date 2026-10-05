"""FastAPI entry point - the productisation path.

Run:
    uvicorn sourced_intel.api:app --reload --app-dir src

Endpoints:
    GET  /health                    liveness + effective configuration
    POST /runs                      run the pipeline for a query (synchronous)
    GET  /runs/{run_id}             run summary from disk
    GET  /runs/{run_id}/report      the rendered report
    GET  /runs/{run_id}/trace       per-node trace records
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
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


@app.post("/runs")
def create_run(payload: RunRequest) -> JSONResponse:
    run_id = _validated_run_id(payload.run_id) if payload.run_id is not None else None
    try:
        result = run_pipeline(payload.query, run_id=run_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

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


@app.get("/runs/{run_id}")
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


@app.get("/runs/{run_id}/report")
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


@app.get("/runs/{run_id}/trace")
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
