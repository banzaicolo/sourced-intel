"""Node 6 - report generation.

The LLM writes only the narrative opening; every figure and every table comes from
the aggregation step. If rendering fails the pipeline degrades to Markdown instead
of losing the run.
"""

from __future__ import annotations

import json
import logging

from ..config import get_settings
from ..llm import get_llm
from ..prompts_loader import load_prompt
from ..report.render import format_amount, render_html
from ..schemas import Aggregation, Analysis, GraphState, IntelItem, QuerySpec
from ..state import node_context

logger = logging.getLogger(__name__)

NODE_NAME = "06_generate_report"
PROMPT_NAME = "06_report"


def _fallback_summary(query: QuerySpec, aggregation: Aggregation, analysis: Analysis) -> dict:
    if analysis.insufficient_data:
        text = (
            f"The pipeline extracted only {aggregation.total_items} structured record(s) for "
            f"{query.industry} in {query.region_name}, which is below the threshold for a "
            f"reliable conclusion. No finding is asserted."
        )
    else:
        text = (
            f"Across the last {query.timespan}, {aggregation.total_items} structured records "
            f"matched {query.industry} in {query.region_name}. "
            f"{aggregation.with_amount} of them state an amount, totalling "
            f"{format_amount(aggregation.total_amount)}. "
            f"Activity spans {len(aggregation.by_region)} region(s) and "
            f"{len(aggregation.by_month)} month(s)."
        )
    return {
        "executive_summary": text,
        "method_note": (
            "Generated automatically from the records listed in the evidence table; "
            "the narrative model was unavailable for this run."
        ),
    }


def generate_report(state: GraphState) -> dict:
    ctx = node_context("06_generate_report", state)
    query: QuerySpec | None = ctx.get("query")
    aggregation: Aggregation | None = ctx.get("aggregation")
    analysis: Analysis | None = ctx.get("analysis")
    items: list[IntelItem] = ctx.get("intel_items") or []
    failed_sources: list[str] = ctx.get("failed_sources") or []
    errors: list[dict] = ctx.get("errors") or []
    user_query: str = ctx.get("user_query") or ""
    run_id = ctx.get("run_id") or "run"

    settings = get_settings()
    errors_out: list[dict] = []
    usage: dict = {"prompt_version": "unknown"}

    if query is None or aggregation is None:
        return {
            "report_path": None,
            "report_format": None,
            "status": "failed",
            "errors": [
                {
                    "node": NODE_NAME,
                    "type": "missing_state",
                    "detail": "query or aggregation absent",
                }
            ],
        }

    analysis = analysis or Analysis(insufficient_data=True)

    # ---- narrative (LLM) -------------------------------------------------
    summary: dict | None = None
    prompt = load_prompt(PROMPT_NAME)
    user_prompt = prompt.render(
        industry=query.industry,
        region=query.region_name,
        timespan=query.timespan,
        total_items=aggregation.total_items,
        with_amount=aggregation.with_amount,
        total_amount=f"{aggregation.total_amount:,.0f}",
        by_region=json.dumps([b.model_dump() for b in aggregation.by_region], ensure_ascii=False),
        by_month=json.dumps([b.model_dump() for b in aggregation.by_month], ensure_ascii=False),
        by_event_type=json.dumps(aggregation.by_event_type, ensure_ascii=False),
        by_subject=json.dumps(aggregation.by_subject, ensure_ascii=False),
        findings=json.dumps([f.model_dump() for f in analysis.findings], ensure_ascii=False),
        failed_sources=json.dumps(failed_sources, ensure_ascii=False),
        errors=json.dumps(errors[:5], ensure_ascii=False),
    )
    try:
        response = get_llm().chat(
            user_prompt,
            system=prompt.system,
            temperature=0.3,
            max_tokens=800,
            json_mode=True,
            retries=1,
        )
        usage.update(
            {
                "model": response.model,
                "prompt_version": prompt.version,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
            }
        )
        payload = response.json_payload()
        summary = {
            "executive_summary": str(payload.get("executive_summary", "")).strip(),
            "method_note": str(payload.get("method_note", "")).strip(),
        }
        if not summary["executive_summary"]:
            summary = None
    except Exception as exc:
        logger.warning("narrative generation failed: %s", exc)
        errors_out.append({"node": NODE_NAME, "type": "narrative_failed", "detail": str(exc)[:200]})
        summary = None

    if summary is None:
        summary = _fallback_summary(query, aggregation, analysis)
        usage["fallback"] = True

    # ---- quality split (threshold applied before rendering) --------------
    threshold = settings.confidence_threshold
    passed = [i for i in items if not i.needs_review and i.confidence >= threshold]
    rejected = [i for i in items if i not in passed]
    if not passed and items:
        # Never show an empty report: fall back to the best available records.
        ranked = sorted(items, key=lambda i: i.confidence, reverse=True)
        passed = ranked[:20]
        rejected = ranked[20:]
        errors_out.append(
            {
                "node": NODE_NAME,
                "type": "threshold_relaxed",
                "detail": f"no record reached confidence {threshold}; showing top {len(passed)} by score",
            }
        )

    # ---- render ----------------------------------------------------------
    run_dir = settings.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "report.html"
    model_name = usage.get("model") or "n/a"

    try:
        written = render_html(
            run_id=run_id,
            user_query=user_query,
            query=query,
            aggregation=aggregation,
            analysis=analysis,
            items=passed,
            rejected=rejected,
            failed_sources=failed_sources,
            errors=errors,
            summary=summary,
            threshold=threshold,
            model_name=model_name,
            output_path=output_path,
        )
    except Exception as exc:
        errors_out.append({"node": NODE_NAME, "type": "render_failed", "detail": str(exc)[:300]})
        return {
            "report_path": None,
            "report_format": None,
            "status": "failed",
            "errors": errors_out,
            "node_meta": {NODE_NAME: usage},
        }

    report_format = "html" if written.suffix == ".html" else "markdown"
    usage["report_format"] = report_format
    usage["report_path"] = str(written)
    usage["items_in_report"] = len(passed)
    usage["items_in_appendix"] = len(rejected)

    if report_format != "html":
        # `render_html` degrades to Markdown on any template error rather than
        # failing the run - good for resilience, bad for visibility. Record it as
        # an error so it reaches the trace and the API response instead of only
        # the log. See docs/PITFALLS.md #31.
        errors_out.append(
            {
                "node": NODE_NAME,
                "type": "html_render_fallback",
                "detail": "HTML rendering failed; this run was written as Markdown instead",
            }
        )

    return {
        "report_path": str(written),
        "report_format": report_format,
        "errors": errors_out,
        "node_meta": {NODE_NAME: usage},
    }
