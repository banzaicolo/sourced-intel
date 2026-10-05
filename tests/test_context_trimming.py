"""Regression tests for the context-trimming guarantee.

`README.md` claims each node receives only the state slice it needs, enforced by
structure. That was only half true: the graph wrapper handed every node the
whole state, and each node voluntarily called `node_context()`. `07_quality_gate`
had already drifted - it read `report_path`, a key absent from its own
allow-list. These tests make the wrapper, not the nodes, the enforcement point.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from sourced_intel.graph import NODES, build_graph, node_payload
from sourced_intel.state import NODE_STATE_SLICES, node_context
from sourced_intel.tracing import Tracer

# A state with every key a node could want, to prove the trimming is real and
# not just an artefact of a sparse input.
FULL_STATE: dict[str, Any] = {
    "run_id": "run-test-0001",
    "user_query": "深圳储能产业最近三个月的公开动向",
    "query": {"canonical": "深圳储能"},
    "raw_docs": [{"url": "https://example.com/a"}],
    "intel_items": [{"subject": "A"}],
    "aggregation": {"total": 1},
    "analysis": {"summary": "s"},
    "report_path": "data/runs/run-test-0001/report.html",
    "failed_sources": [],
    "errors": [],
    "trace": [],
    "status": "running",
}


def test_every_graph_node_has_an_allow_list() -> None:
    """A new node must declare its slice, it does not inherit "everything"."""
    for _key, display_name, _fn in NODES:
        assert display_name in NODE_STATE_SLICES, f"{display_name} has no state slice"


@pytest.mark.parametrize("display_name", sorted(NODE_STATE_SLICES))
def test_node_payload_only_contains_allowed_keys(display_name: str) -> None:
    payload = node_payload(display_name, FULL_STATE)  # type: ignore[arg-type]
    allowed = set(NODE_STATE_SLICES[display_name])
    assert set(payload) <= allowed, f"{display_name} was handed {set(payload) - allowed}"


def test_node_payload_forwards_unknown_nodes_in_full() -> None:
    """The documented fallback for a node that is not in the table."""
    assert set(node_context("99_unknown", FULL_STATE)) == set(FULL_STATE)  # type: ignore[arg-type]


def test_quality_gate_can_see_report_path() -> None:
    payload = node_payload("07_quality_gate", FULL_STATE)  # type: ignore[arg-type]
    assert payload.get("report_path") == FULL_STATE["report_path"]
    # ...and still cannot see the document bodies it has no business reading.
    assert "raw_docs" not in payload


def test_wrapper_hands_the_node_a_trimmed_state() -> None:
    """The real end-to-end check, through `build_graph`."""
    seen: dict[str, set[str]] = {}

    def spy_parse(state: Any) -> dict:
        seen["01_parse_intent"] = set(state)
        return {"query": {"canonical": "x"}}

    def spy_retrieve(state: Any) -> dict:
        seen["02_retrieve"] = set(state)
        return {"raw_docs": []}

    monkeypatched = (
        ("parse_intent", "01_parse_intent", spy_parse),
        ("retrieve", "02_retrieve", spy_retrieve),
    )

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "run-test-0001"
        run_dir.mkdir(parents=True)
        graph = _graph_with(monkeypatched, run_dir)
        graph.invoke(FULL_STATE)

    assert seen["01_parse_intent"] <= set(NODE_STATE_SLICES["01_parse_intent"])
    assert seen["02_retrieve"] <= set(NODE_STATE_SLICES["02_retrieve"])
    # The point: these were in the input state and must not have been forwarded.
    for forbidden in ("raw_docs", "intel_items", "aggregation", "analysis"):
        assert forbidden not in seen["01_parse_intent"]
    for forbidden in ("raw_docs", "intel_items", "analysis", "report_path"):
        assert forbidden not in seen["02_retrieve"]


def _graph_with(nodes: tuple, run_dir: Path):
    """Compile the graph against a stub node list, without touching the real one."""
    import sourced_intel.graph as graph_module

    original = graph_module.NODES
    graph_module.NODES = nodes
    try:
        return build_graph(run_dir, Tracer(run_dir))
    finally:
        graph_module.NODES = original
