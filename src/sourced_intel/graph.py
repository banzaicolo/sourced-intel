"""LangGraph orchestration.

Seven nodes, one linear path with a single early exit (ambiguous query). Every
node is wrapped so that, no matter what happens inside it, we get:

  * a timed record in `trace.jsonl`          (link tracing)
  * a JSON snapshot on disk                  (state snapshot / replay)
  * an entry in `errors` instead of a crash  (fault tolerance)

The wrapper is also where context trimming is enforced, by passing each node only
the state slice listed in `state.NODE_STATE_SLICES`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from langgraph.graph import END, StateGraph

from .config import Settings, get_settings
from .nodes.aggregate import aggregate
from .nodes.analyze import analyze
from .nodes.extract import extract
from .nodes.generate_report import generate_report
from .nodes.parse_intent import parse_intent
from .nodes.quality_gate import quality_gate
from .nodes.retrieve import retrieve
from .schemas import GraphState
from .state import (
    initial_state,
    list_snapshots,
    new_run_id,
    node_context,
    save_snapshot,
    to_jsonable,
)
from .tracing import Tracer

logger = logging.getLogger(__name__)

#: node key -> (display name, callable)
NODES: tuple[tuple[str, str, Callable[[GraphState], dict]], ...] = (
    ("parse_intent", "01_parse_intent", parse_intent),
    ("retrieve", "02_retrieve", retrieve),
    ("extract", "03_extract", extract),
    ("aggregate", "04_aggregate", aggregate),
    ("analyze", "05_analyze", analyze),
    ("generate_report", "06_generate_report", generate_report),
    ("quality_gate", "07_quality_gate", quality_gate),
)

ACCUMULATING_KEYS = ("failed_sources", "errors", "trace")


def node_payload(node: str, state: GraphState) -> GraphState:
    """The state a node function is allowed to see.

    Trimming is applied here, in the wrapper, and not only inside each node.
    A node that forgets to call `node_context()` must not be handed the whole
    state - that is exactly how `07_quality_gate` used to read `report_path`,
    a key missing from its own allow-list. Verified by
    `tests/test_context_trimming.py`.
    """
    return cast(GraphState, node_context(node, state))


def _apply_update(state: GraphState, update: dict) -> GraphState:
    """Mirror LangGraph's reducer semantics so snapshots show the real state."""
    merged: dict[str, Any] = dict(state)
    for key, value in update.items():
        if key == "node_meta" and isinstance(value, dict):
            existing = dict(merged.get("node_meta") or {})
            for node, meta in value.items():
                existing[node] = {**(existing.get(node) or {}), **(meta or {})}
            merged["node_meta"] = existing
        elif key in ACCUMULATING_KEYS and isinstance(value, list):
            merged[key] = list(merged.get(key) or []) + value
        else:
            merged[key] = value
    return GraphState(**merged)  # type: ignore[typeddict-item]


def build_graph(
    run_dir: Path,
    tracer: Tracer,
    settings: Settings | None = None,
) -> Any:
    """Compile the StateGraph with instrumented nodes bound to this run."""
    settings = settings or get_settings()
    snapshot_dir = run_dir / "snapshots"

    def instrument(display_name: str, fn: Callable[[GraphState], dict]):
        def wrapper(state: GraphState) -> dict:
            started = time.perf_counter()
            status = "ok"
            error_text: str | None = None
            try:
                update = fn(node_payload(display_name, state)) or {}
            except Exception as exc:
                logger.exception("node %s crashed", display_name)
                error_text = f"{type(exc).__name__}: {exc}"
                status = "error"
                update = {
                    "errors": [{"node": display_name, "type": "node_crash", "detail": error_text}]
                }

            duration_ms = (time.perf_counter() - started) * 1000
            meta = dict((update.get("node_meta") or {}).get(display_name) or {})
            entry = tracer.record(
                display_name,
                duration_ms=duration_ms,
                status=status,
                model=meta.get("model"),
                prompt_version=meta.get("prompt_version"),
                prompt_tokens=meta.get("prompt_tokens"),
                completion_tokens=meta.get("completion_tokens"),
                total_tokens=meta.get("total_tokens"),
                retries=meta.get("llm_attempts", 0) or 0,
                error=error_text,
                **{
                    k: v
                    for k, v in meta.items()
                    if k
                    not in {
                        "model",
                        "prompt_version",
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "llm_attempts",
                    }
                },
            )

            merged = _apply_update(state, update)
            merged["trace"] = [*list(merged.get("trace") or []), entry]
            try:
                # display_name already carries its ordinal ("01_parse_intent")
                save_snapshot(snapshot_dir, display_name, merged)
            except Exception as exc:
                logger.warning("snapshot write failed for %s: %s", display_name, exc)
                update.setdefault("errors", []).append(
                    {"node": display_name, "type": "snapshot_failed", "detail": str(exc)[:200]}
                )
            return update

        return wrapper

    graph = StateGraph(GraphState)
    for key, display_name, fn in NODES:
        graph.add_node(key, instrument(display_name, fn))

    graph.set_entry_point("parse_intent")

    # The only branch in the graph: an ambiguous question is handed back to the
    # user instead of being guessed at. Everything else is a straight line.
    graph.add_conditional_edges(
        "parse_intent",
        lambda state: "end" if state.get("query") is None else "continue",
        {"end": END, "continue": "retrieve"},
    )
    for index in range(1, len(NODES) - 1):
        graph.add_edge(NODES[index][0], NODES[index + 1][0])
    graph.add_edge(NODES[-1][0], END)
    return graph.compile()


def run_pipeline(
    user_query: str,
    run_id: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute one full run. Returns a summary dict (never raises for data reasons)."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    run_id = run_id or new_run_id()
    run_dir = settings.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    tracer = Tracer(run_dir)
    app = build_graph(run_dir, tracer, settings)
    state = initial_state(run_id, user_query)

    started = time.perf_counter()
    final_state: GraphState = app.invoke(state)  # type: ignore[assignment]
    elapsed = time.perf_counter() - started

    final_state["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not final_state.get("status") or final_state.get("status") == "running":
        final_state["status"] = "completed"

    # Final snapshot, so the terminal state is always on disk too.
    try:
        save_snapshot(settings.snapshots_dir(run_id), "99_final", final_state)
    except Exception as exc:
        logger.warning("final snapshot failed: %s", exc)

    summary = {
        "run_id": run_id,
        "status": final_state.get("status"),
        "elapsed_seconds": round(elapsed, 2),
        "clarification": final_state.get("clarification"),
        "report_path": final_state.get("report_path"),
        "report_format": final_state.get("report_format"),
        "raw_docs": len(final_state.get("raw_docs") or []),
        "intel_items": len(final_state.get("intel_items") or []),
        "failed_sources": final_state.get("failed_sources") or [],
        "errors": len(final_state.get("errors") or []),
        "snapshots": list_snapshots(settings.snapshots_dir(run_id)),
        "trace_summary": tracer.summary(),
        "node_meta": final_state.get("node_meta") or {},
        "run_dir": str(run_dir),
    }
    return {"state": to_jsonable(dict(final_state)), "summary": summary}
