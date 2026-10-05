"""Node 7 - quality gate.

The last step decides whether a run is trustworthy, and records what is not.
Retries are bounded upstream; this node counts them and reports honestly.
"""

from __future__ import annotations

import logging

from ..config import get_settings
from ..schemas import GraphState, IntelItem, QualityReport
from ..state import node_context

logger = logging.getLogger(__name__)

NODE_NAME = "07_quality_gate"

MIN_ITEMS_FOR_PASS = 3
MIN_ITEMS_FOR_PARTIAL = 1


def quality_gate(state: GraphState) -> dict:
    ctx = node_context("07_quality_gate", state)
    items: list[IntelItem] = ctx.get("intel_items") or []
    failed_sources: list[str] = ctx.get("failed_sources") or []
    errors: list[dict] = ctx.get("errors") or []
    report_path = ctx.get("report_path")

    settings = get_settings()
    threshold = settings.confidence_threshold

    passed = [i for i in items if not i.needs_review and i.confidence >= threshold]
    rejected = [i for i in items if i not in passed]

    retries_used: dict[str, int] = {}
    for error in errors:
        node = error.get("node") or "unknown"
        if error.get("type") in {"llm_error", "extract_parse_error", "analysis_parse_error"}:
            retries_used[node] = retries_used.get(node, 0) + 1

    # Bounded-retry policy check: no node may consume more than the configured cap.
    over_budget = {n: c for n, c in retries_used.items() if c > settings.node_max_retries}

    passed_run = bool(report_path) and len(passed) >= MIN_ITEMS_FOR_PASS
    partial = bool(report_path) and MIN_ITEMS_FOR_PARTIAL <= len(passed) < MIN_ITEMS_FOR_PASS

    if passed_run:
        status = "completed"
    elif partial:
        status = "completed_with_warnings"
    else:
        status = "failed" if not report_path else "completed_with_warnings"

    quality = QualityReport(
        passed=passed_run,
        passed_items=len(passed),
        rejected_items=len(rejected),
        rejected=rejected[:50],
        failed_sources=failed_sources,
        errors=errors[:50],
        retries_used=retries_used,
        threshold=threshold,
    )

    meta = {
        "passed": passed_run,
        "passed_items": len(passed),
        "rejected_items": len(rejected),
        "failed_sources": len(failed_sources),
        "errors": len(errors),
        "retries_used": retries_used,
        "retry_budget_exceeded": bool(over_budget),
        "report_path": report_path,
    }

    new_errors: list[dict] = []
    if over_budget:
        new_errors.append(
            {
                "node": NODE_NAME,
                "type": "retry_budget_exceeded",
                "detail": f"nodes over the retry cap ({settings.node_max_retries}): {over_budget}",
            }
        )
    if not report_path:
        new_errors.append(
            {"node": NODE_NAME, "type": "no_report", "detail": "the run produced no report file"}
        )

    return {
        "quality": quality,
        "status": status,
        "errors": new_errors,
        "node_meta": {NODE_NAME: meta},
    }
