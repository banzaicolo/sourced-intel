"""Pydantic v2 schemas shared by every node of the pipeline.

These models do double duty:
  1. they validate the state passed between LangGraph nodes, and
  2. they are handed to the LLM as the contract for its JSON output, so the
     model output is validated with the exact same rules used internally.
"""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

IntelType = Literal["project", "policy", "funding", "capacity", "partnership", "other"]

EVENT_TYPES: tuple[str, ...] = (
    "project",
    "policy",
    "funding",
    "capacity",
    "partnership",
    "other",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _merge_node_meta(
    left: dict[str, dict[str, Any]] | None, right: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    """Merge per-node metadata without losing earlier nodes' entries."""
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (left or {}).items()}
    for node, meta in (right or {}).items():
        merged[node] = {**merged.get(node, {}), **(meta or {})}
    return merged


# ---------------------------------------------------------------------------
# Node 1 - intent parsing
# ---------------------------------------------------------------------------
class QuerySpec(BaseModel):
    """A natural-language question resolved into machine-usable filters."""

    model_config = ConfigDict(extra="ignore")

    industry: str = Field(..., description="Industry / technology keyword, e.g. 'energy storage'")
    region_adcode: str = Field(..., description="Administrative code, e.g. '440300'")
    region_name: str = Field(..., description="Human-readable region name, e.g. '深圳市'")
    timespan: str = Field("3m", description="Lookback window, e.g. '3m', '90d'")
    intel_types: list[str] = Field(default_factory=lambda: list(EVENT_TYPES))
    keywords: list[str] = Field(
        default_factory=list,
        description="Extra search keywords derived from the industry term",
    )
    raw_query: str = Field("", description="The original user sentence, kept for the trace")

    @field_validator("intel_types")
    @classmethod
    def _clean_types(cls, value: list[str]) -> list[str]:
        cleaned = [v for v in (item.strip() for item in value) if v]
        return cleaned or list(EVENT_TYPES)

    @property
    def search_terms(self) -> list[str]:
        """Terms actually sent to the search adapters."""
        terms = [self.industry, *self.keywords]
        seen: list[str] = []
        for term in terms:
            term = term.strip()
            if term and term.lower() not in {s.lower() for s in seen}:
                seen.append(term)
        return seen


class IntentParseResult(BaseModel):
    """LLM-facing wrapper for node 1 output."""

    model_config = ConfigDict(extra="ignore")

    industry: str
    region: str
    timespan: str = "3m"
    intel_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None


# ---------------------------------------------------------------------------
# Node 2 - retrieval
# ---------------------------------------------------------------------------
class RawDoc(BaseModel):
    """One document as returned by a source adapter, before any LLM touches it."""

    model_config = ConfigDict(extra="ignore")

    source: str = Field(..., description="Adapter id: 'gdelt' | 'china5e'")
    url: str
    title: str
    published_at: datetime | None = None
    raw_text: str = ""
    fetched_at: datetime = Field(default_factory=utcnow)
    domain: str | None = None
    language: str | None = None

    @property
    def has_body(self) -> bool:
        return len(self.raw_text.strip()) > 50

    @property
    def text_for_llm(self) -> str:
        """Context trimming: never hand a full page to the model."""
        body = self.raw_text.strip()
        return body[:1500] if body else self.title


# ---------------------------------------------------------------------------
# Node 3 - structured extraction
# ---------------------------------------------------------------------------
class IntelItem(BaseModel):
    """A single structured intelligence item extracted from one RawDoc."""

    model_config = ConfigDict(extra="ignore")

    title: str
    subject: str = ""
    amount: float | None = Field(None, description="Amount in CNY (yuan), not in 亿元")
    amount_raw: str | None = Field(None, description="Original amount text, e.g. '12.5亿元'")
    amount_source: str = Field(
        "none",
        description=(
            "Where `amount_raw` came from: 'llm' (model transcription), "
            "'regex' (deterministic scan of the source text the model missed), "
            "'none'. Provenance is surfaced in the report."
        ),
    )
    amount_conflict: bool = Field(
        False,
        description="The model's amount and the regex scan disagree; send to review.",
    )
    event_time: datetime | None = None
    event_time_raw: str | None = Field(
        None,
        description=(
            "The date exactly as the source stated it - '2026-09-16' when a day was "
            "given, '2026-01' when only a month was. Kept so the report can show the "
            "real precision instead of implying a day nobody stated. A month is "
            "RESOLVED to its 1st into `event_time` purely so monthly aggregation "
            "works; `event_time_raw` is what gets displayed. See docs/PITFALLS.md #24."
        ),
    )
    location_name: str | None = None
    adcode: str | None = None
    event_type: str = "other"
    confidence: float = 0.0
    source_url: str
    source: str = ""
    needs_review: bool = False
    review_reason: str | None = None

    @field_validator("amount")
    @classmethod
    def _positive_amount(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return value if value >= 0 else None

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))

    @field_validator("event_type")
    @classmethod
    def _normalise_type(cls, value: str) -> str:
        value = (value or "other").strip().lower()
        return value if value in EVENT_TYPES else "other"

    def flag_for_review(self, reason: str) -> IntelItem:
        return self.model_copy(update={"needs_review": True, "review_reason": reason})


class LLMIntelItem(BaseModel):
    """Subset of IntelItem the LLM is allowed to emit.

    Note there is no `source_url`: the model only returns a `doc_id`, and the real
    URL is re-attached from our own document list. That makes a hallucinated
    citation structurally impossible.
    """

    model_config = ConfigDict(extra="ignore")

    doc_id: int | None = None
    title: str = ""
    subject: str = ""
    amount_raw: str | None = None
    event_time: str | None = None
    location_name: str | None = None
    event_type: str = "other"
    confidence: float = 0.5


class ExtractionBatch(BaseModel):
    """LLM-facing wrapper: the model returns one array of items."""

    model_config = ConfigDict(extra="ignore")

    items: list[LLMIntelItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Node 4 - spatio-temporal aggregation
# ---------------------------------------------------------------------------
class RegionBucket(BaseModel):
    adcode: str
    region_name: str
    count: int = 0
    total_amount: float = 0.0
    items: list[str] = Field(default_factory=list, description="Titles or subjects in this bucket")


class MonthBucket(BaseModel):
    month: str = Field(..., description="YYYY-MM")
    count: int = 0
    total_amount: float = 0.0


class Aggregation(BaseModel):
    total_items: int = 0
    with_amount: int = 0
    total_amount: float = 0.0
    #: How many records named a subject at all. Kept separately from
    #: `by_subject` because that list is capped at the top 15, so summing it is
    #: not the same number - and the report states this one as the denominator of
    #: every share it prints. Getting this wrong makes a correct share look wrong.
    with_subject: int = 0
    by_region: list[RegionBucket] = Field(default_factory=list)
    by_month: list[MonthBucket] = Field(default_factory=list)
    by_event_type: dict[str, int] = Field(default_factory=dict)
    by_subject: list[dict[str, Any]] = Field(default_factory=list)
    unmapped_items: int = 0


# ---------------------------------------------------------------------------
# Node 5 - correlation analysis
# ---------------------------------------------------------------------------
class Finding(BaseModel):
    """A conclusion the LLM produced from numbers computed in pure code."""

    model_config = ConfigDict(extra="ignore")

    headline: str
    detail: str = ""
    evidence: list[str] = Field(default_factory=list, description="Source URLs backing this claim")
    confidence: float = 0.5


class Analysis(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    subject_concentration: dict[str, Any] = Field(default_factory=dict)
    trend_note: str = ""
    anomalies: list[str] = Field(default_factory=list)
    insufficient_data: bool = False


# ---------------------------------------------------------------------------
# Node 7 - quality gate
# ---------------------------------------------------------------------------
class QualityReport(BaseModel):
    passed: bool = True
    passed_items: int = 0
    rejected_items: int = 0
    rejected: list[IntelItem] = Field(default_factory=list)
    failed_sources: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    retries_used: dict[str, int] = Field(default_factory=dict)
    threshold: float = 0.6


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------
class GraphState(TypedDict, total=False):
    """The single object threaded through every node.

    Fields annotated with `operator.add` are accumulators: a node returns only its
    own increment and LangGraph merges it in. `node_meta` collects per-node
    execution metadata (model, token usage, prompt version).

    Nodes must read only the keys listed for them in `state.NODE_STATE_SLICES` -
    that is the context-trimming requirement made executable.
    """

    run_id: str
    user_query: str
    query: QuerySpec | None
    clarification: str | None
    raw_docs: list[RawDoc]
    intel_items: list[IntelItem]
    aggregation: Aggregation | None
    analysis: Analysis | None
    quality: QualityReport | None
    report_path: str | None
    report_format: str | None
    failed_sources: Annotated[list[str], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
    node_meta: Annotated[dict[str, dict[str, Any]], _merge_node_meta]
    started_at: str
    finished_at: str | None
    status: str


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------
# The pure text-parsing helpers live in `parsing.py` (stdlib only, unit-testable
# without a virtualenv). They are re-exported here for convenience so callers that
# already import schemas do not need a second import.
from .parsing import (  # noqa: E402  (kept at the bottom to avoid an import cycle)
    AmountCheck,
    JsonExtractionError,
    cutoff_from_timespan,
    extract_json,
    is_noise_title,
    normalise_url,
    parse_amount,
    parse_datetime,
    reconcile_amount,
    timespan_to_days,
)

__all__ = [
    "EVENT_TYPES",
    "Aggregation",
    "AmountCheck",
    "Analysis",
    "ExtractionBatch",
    "Finding",
    "GraphState",
    "IntelItem",
    "IntentParseResult",
    "JsonExtractionError",
    "LLMIntelItem",
    "MonthBucket",
    "QualityReport",
    "QuerySpec",
    "RawDoc",
    "RegionBucket",
    "cutoff_from_timespan",
    "extract_json",
    "is_noise_title",
    "normalise_url",
    "parse_amount",
    "parse_datetime",
    "reconcile_amount",
    "timespan_to_days",
    "utcnow",
]
