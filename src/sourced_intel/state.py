"""Run state helpers: snapshot writing, snapshot loading, and run bookkeeping.

Every node's output is serialised to
`data/runs/{run_id}/snapshots/{NN}_{node}.json` so any run can be inspected or
resumed from an arbitrary step.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .run_ids import RUN_ID_RE, InvalidRunId, new_run_id, validate_run_id
from .schemas import (
    Aggregation,
    Analysis,
    GraphState,
    IntelItem,
    QualityReport,
    QuerySpec,
    RawDoc,
)

# Re-exported for callers that have always imported them from `state`.
__all__ = [
    "RUN_ID_RE",
    "InvalidRunId",
    "initial_state",
    "list_snapshots",
    "load_snapshot",
    "new_run_id",
    "node_context",
    "restore_state",
    "save_snapshot",
    "to_jsonable",
    "validate_run_id",
]

# State keys that must be revived into their concrete model on load.
_MODEL_MAP: dict[str, type[BaseModel]] = {
    "query": QuerySpec,
    "aggregation": Aggregation,
    "analysis": Analysis,
    "quality": QualityReport,
}

_LIST_MODEL_MAP: dict[str, type[BaseModel]] = {
    "raw_docs": RawDoc,
    "intel_items": IntelItem,
}

NODE_ORDER: tuple[str, ...] = (
    "01_parse_intent",
    "02_retrieve",
    "03_extract",
    "04_aggregate",
    "05_analyze",
    "06_generate_report",
    "07_quality_gate",
)


def initial_state(run_id: str, user_query: str) -> GraphState:
    return GraphState(
        run_id=run_id,
        user_query=user_query,
        query=None,
        clarification=None,
        raw_docs=[],
        intel_items=[],
        aggregation=None,
        analysis=None,
        quality=None,
        report_path=None,
        report_format=None,
        failed_sources=[],
        errors=[],
        trace=[],
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        finished_at=None,
        status="running",
    )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def to_jsonable(value: Any) -> Any:
    """Recursively convert state values into JSON-safe primitives."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def from_jsonable(key: str, value: Any) -> Any:
    """Revive a snapshot field into the model the pipeline expects."""
    if value is None:
        return None
    if key in _MODEL_MAP:
        return _MODEL_MAP[key].model_validate(value)
    if key in _LIST_MODEL_MAP:
        model = _LIST_MODEL_MAP[key]
        return [model.model_validate(item) for item in value]
    return value


def restore_state(payload: dict[str, Any]) -> GraphState:
    return GraphState(**{key: from_jsonable(key, val) for key, val in payload.items()})


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
def save_snapshot(snapshot_dir: Path, step: str, state: GraphState) -> Path:
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"{step}.json"
    payload = {
        "step": step,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": to_jsonable(dict(state)),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_snapshot(snapshot_dir: Path, step: str) -> GraphState:
    path = Path(snapshot_dir) / f"{step}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return restore_state(payload["state"])


def list_snapshots(snapshot_dir: Path) -> list[str]:
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        return []
    return sorted(p.stem for p in snapshot_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# Context trimming
# ---------------------------------------------------------------------------
# Explicit allow-lists: each node receives only the state slice it needs.
# This is a stated requirement of the spec - never hand the whole state (or a
# full document) to the model.
NODE_STATE_SLICES: dict[str, tuple[str, ...]] = {
    "01_parse_intent": ("run_id", "user_query"),
    "02_retrieve": ("run_id", "query"),
    "03_extract": ("run_id", "query", "raw_docs"),
    "04_aggregate": ("run_id", "query", "intel_items"),
    # intel_items is on the list so findings can be validated against real URLs.
    # The analyse node reads only url/subject/title/amount from those items -
    # never raw_text. See nodes/analyze.py.
    "05_analyze": ("run_id", "query", "aggregation", "intel_items"),
    "06_generate_report": (
        "run_id",
        "user_query",
        "query",
        "intel_items",
        "aggregation",
        "analysis",
        "failed_sources",
        "errors",
    ),
    # report_path is on the list because the gate's whole job is to decide
    # whether a report was actually produced - without it the status silently
    # became "no_report" for every run once trimming moved into the wrapper.
    "07_quality_gate": (
        "run_id",
        "intel_items",
        "analysis",
        "report_path",
        "failed_sources",
        "errors",
    ),
}


def node_context(node: str, state: GraphState) -> dict[str, Any]:
    """Return only the allowed keys for `node` (context trimming)."""
    allowed = NODE_STATE_SLICES.get(node)
    if allowed is None:
        return dict(state)
    return {key: state.get(key) for key in allowed if key in state}


def node_payload_stats(node: str, state: GraphState) -> dict[str, Any]:
    """Small metrics proving how much context a node actually consumed."""
    ctx = node_context(node, state)
    stats: dict[str, Any] = {"context_keys": len(ctx)}
    for key in ("raw_docs", "intel_items"):
        if key in ctx and isinstance(ctx[key], list):
            stats[f"context_{key}"] = len(ctx[key])
            if key == "raw_docs":
                stats["context_chars"] = sum(
                    min(len(doc.raw_text), 1500) if doc.raw_text else len(doc.title)
                    for doc in ctx[key]
                )
    return stats
