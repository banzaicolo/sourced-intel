"""Node 5 - correlation analysis.

The model receives pre-computed figures only. Every finding it returns must cite
source URLs that exist in our own item list - citations are validated, and an
uncited finding is dropped rather than trusted.
"""

from __future__ import annotations

import json
import logging

from ..llm import LLMError, get_llm
from ..prompts_loader import load_prompt
from ..schemas import (
    Aggregation,
    Analysis,
    Finding,
    GraphState,
    IntelItem,
    QuerySpec,
)
from ..state import node_context

logger = logging.getLogger(__name__)

NODE_NAME = "05_analyze"
PROMPT_NAME = "05_analyze"

MIN_ITEMS_FOR_ANALYSIS = 5


def subject_concentration(aggregation: Aggregation) -> dict:
    """Top-3 share of named subjects. Computed here, never by the model."""
    subjects = aggregation.by_subject or []
    total = sum(s.get("count", 0) for s in subjects)
    if not total:
        return {"top3_share": 0.0, "top_subjects": [], "named_subjects": 0}
    top3 = subjects[:3]
    return {
        "top3_share": round(sum(s["count"] for s in top3) / total, 4),
        "top_subjects": [s["subject"] for s in top3],
        "named_subjects": total,
    }


# ---------------------------------------------------------------------------
# Tolerating the model's JSON
#
# json_mode guarantees syntactically valid JSON, not *our* schema. GLM was
# observed returning the findings list under an "answer" key and writing
# confidence as the word "high". Both are harmless in themselves, but the first
# one is silent: Analysis.findings defaults to an empty list, so a renamed key
# looks exactly like "the model had nothing to say" and the report quietly falls
# back to template text. Normalising here fixes the shape before validation and
# keeps prompt wording from being the only thing standing between us and an
# empty analysis.
# ---------------------------------------------------------------------------
_FINDING_LIST_KEYS: tuple[str, ...] = (
    "findings",
    "answer",
    "results",
    "items",
    "conclusions",
    "analysis",
)

_CONFIDENCE_WORDS: dict[str, float] = {
    "very high": 0.95,
    "high": 0.9,
    "medium": 0.6,
    "moderate": 0.6,
    "med": 0.6,
    "low": 0.3,
    "very low": 0.2,
    "uncertain": 0.3,
    "unknown": 0.5,
}


def _coerce_confidence(value: object) -> float:
    """Accept 0.9, '0.9', '90' or 'high'; anything unusable becomes the neutral 0.5."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[text]
        try:
            value = float(text)
        except ValueError:
            return 0.5
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    number = float(value)
    while number > 1.0:  # 90 and 900 both mean 0.9
        number /= 10.0
    return max(0.0, min(1.0, number))


def _normalise_analysis_payload(payload: object) -> dict:
    """Reshape the model's JSON into the form `Analysis` expects.

    Covers the two deviations GLM actually produces in practice: a bare JSON
    array, and the findings list nested under a key of the model's own choosing.
    """
    if isinstance(payload, list):
        payload = {"findings": payload}
    data: dict = dict(payload) if isinstance(payload, dict) else {}

    if not isinstance(data.get("findings"), list):
        for key in _FINDING_LIST_KEYS:
            if isinstance(data.get(key), list):
                data["findings"] = data[key]
                break
        else:
            data["findings"] = []

    data["findings"] = [
        {**raw, "confidence": _coerce_confidence(raw.get("confidence"))}
        for raw in data["findings"]
        if isinstance(raw, dict)
    ]
    return data


def _fallback_analysis(
    aggregation: Aggregation, items: list[IntelItem], reason: str, insufficient: bool
) -> Analysis:
    """Deterministic summary so the report always has content."""
    if insufficient:
        headline = "Insufficient data - no conclusion drawn"
        detail = (
            f"Only {aggregation.total_items} structured records were extracted, below the "
            f"minimum of {MIN_ITEMS_FOR_ANALYSIS} required for a conclusion."
        )
        return Analysis(
            findings=[Finding(headline=headline, detail=detail, evidence=[], confidence=0.9)],
            insufficient_data=True,
        )

    regions = aggregation.by_region
    top_region = regions[0] if regions else None
    evidence = [i.source_url for i in items[:3]]
    findings = [
        Finding(
            headline=(
                f"{aggregation.total_items} records extracted; "
                f"{aggregation.with_amount} carry a stated amount"
            ),
            detail=(
                f"Total disclosed amount is {aggregation.total_amount:,.0f} CNY across "
                f"{len(regions)} region(s) and {len(aggregation.by_month)} month(s)."
            ),
            evidence=evidence,
            confidence=0.6,
        )
    ]
    if top_region:
        findings.append(
            Finding(
                headline=f"Most active region: {top_region.region_name} ({top_region.count} records)",
                detail=(
                    f"{top_region.region_name} accounts for {top_region.count} of "
                    f"{aggregation.total_items} records."
                ),
                evidence=evidence,
                confidence=0.55,
            )
        )
    return Analysis(findings=findings, insufficient_data=False)


def analyze(state: GraphState) -> dict:
    ctx = node_context("05_analyze", state)
    aggregation: Aggregation | None = ctx.get("aggregation")
    query: QuerySpec | None = ctx.get("query")
    # Citation pool only: this node reads url/subject/title/amount, never raw_text.
    items: list[IntelItem] = ctx.get("intel_items") or []

    if aggregation is None:
        return {
            "analysis": Analysis(insufficient_data=True),
            "errors": [
                {"node": NODE_NAME, "type": "no_aggregation", "detail": "aggregate step missing"}
            ],
            "node_meta": {NODE_NAME: {"skipped": True}},
        }

    concentration = subject_concentration(aggregation)
    insufficient = aggregation.total_items < MIN_ITEMS_FOR_ANALYSIS

    if insufficient:
        analysis = _fallback_analysis(aggregation, items, "below minimum", True)
        return {
            "analysis": analysis,
            "node_meta": {NODE_NAME: {"insufficient_data": True, "llm_called": False}},
        }

    # Only metadata goes to the model - never raw article text.
    documents = [
        {
            "id": idx,
            "date": item.event_time.strftime("%Y-%m-%d") if item.event_time else None,
            "subject": item.subject,
            "event_type": item.event_type,
            "amount_raw": item.amount_raw,
            "region": item.location_name,
            "url": item.source_url,
            "title": item.title[:80],
        }
        for idx, item in enumerate(items[:80], start=1)
    ]

    prompt = load_prompt(PROMPT_NAME)
    valid_urls = {i.source_url for i in items}

    user_prompt = prompt.render(
        industry=query.industry if query else "",
        region=query.region_name if query else "",
        timespan=query.timespan if query else "",
        total_items=aggregation.total_items,
        with_amount=aggregation.with_amount,
        total_amount=f"{aggregation.total_amount:,.0f}",
        by_region=json.dumps([b.model_dump() for b in aggregation.by_region], ensure_ascii=False),
        by_month=json.dumps([b.model_dump() for b in aggregation.by_month], ensure_ascii=False),
        by_event_type=json.dumps(aggregation.by_event_type, ensure_ascii=False),
        by_subject=json.dumps(aggregation.by_subject, ensure_ascii=False),
        documents=json.dumps(documents, ensure_ascii=False),
    )

    usage: dict = {"prompt_version": prompt.version, "llm_called": True}
    analysis: Analysis | None = None
    errors: list[dict] = []

    for attempt in (1, 2):
        try:
            response = get_llm().chat(
                user_prompt,
                system=prompt.system,
                temperature=0.2,
                max_tokens=1600,
                json_mode=True,
                retries=1,
            )
        except LLMError as exc:
            errors.append({"node": NODE_NAME, "type": "llm_error", "detail": str(exc)})
            break

        usage.update(
            {
                "model": response.model,
                "prompt_tokens": usage.get("prompt_tokens", 0) + response.prompt_tokens,
                "completion_tokens": usage.get("completion_tokens", 0) + response.completion_tokens,
                "total_tokens": usage.get("total_tokens", 0) + response.total_tokens,
                "llm_attempts": attempt,
            }
        )
        try:
            payload = response.json_payload()
            candidate = Analysis.model_validate(_normalise_analysis_payload(payload))
        except Exception as exc:
            errors.append(
                {"node": NODE_NAME, "type": "analysis_parse_error", "detail": str(exc)[:300]}
            )
            if attempt == 1:
                user_prompt += "\n\nYour previous answer was rejected: return valid JSON only."
                continue
            break

        # Validate citations: drop findings that cite URLs we never retrieved.
        kept: list[Finding] = []
        dropped_citations: list[list[str]] = []
        for finding in candidate.findings:
            good = [u for u in finding.evidence if u in valid_urls]
            if not good:
                dropped_citations.append(finding.evidence[:3])
                errors.append(
                    {
                        "node": NODE_NAME,
                        "type": "finding_without_valid_source",
                        "detail": finding.headline[:120],
                        "cited": finding.evidence[:3],
                    }
                )
                continue
            kept.append(finding.model_copy(update={"evidence": good}))
        candidate = candidate.model_copy(update={"findings": kept})
        if not candidate.findings:
            # Distinguish "the model returned no findings at all" from "every
            # finding it returned cited an unknown URL" - they need different
            # fixes, and guessing here is how wrong conclusions get baked in.
            errors.append(
                {
                    "node": NODE_NAME,
                    "type": "all_findings_dropped",
                    "detail": (
                        "model returned an empty findings list"
                        if not dropped_citations
                        else "every finding lacked a verifiable source URL"
                    ),
                    "findings_returned": len(candidate.findings) + len(dropped_citations),
                    "dropped_citations": dropped_citations[:5],
                    "insufficient_data": candidate.insufficient_data,
                    "payload_keys": (
                        sorted(payload.keys())
                        if isinstance(payload, dict)
                        else type(payload).__name__
                    ),
                    "payload_preview": json.dumps(payload, ensure_ascii=False)[:800],
                }
            )
            break
        candidate = candidate.model_copy(update={"subject_concentration": concentration})
        analysis = candidate
        break

    if analysis is None:
        analysis = _fallback_analysis(aggregation, items, "llm unavailable", False)
        usage["fallback"] = True

    return {
        "analysis": analysis,
        "errors": errors,
        "node_meta": {NODE_NAME: usage},
    }
