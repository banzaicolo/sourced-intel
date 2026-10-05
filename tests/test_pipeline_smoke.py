"""Pipeline smoke tests.

Two layers:

  * pure-logic tests that need only pydantic - they run in milliseconds and cover
    the parts of the pipeline where a silent bug would be most damaging
    (region resolution, aggregation, the quality gate, the URL->record binding);
  * one end-to-end test marked `slow`, which needs network access and a live LLM
    credential. It is skipped automatically when `GLM_API_KEY` is unset.

Run:
    pytest tests/ -v
    pytest tests/ -v -m "not slow"      # offline only
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.config import REGION_DISPLAY, REGION_TO_ADCODE  # noqa: E402
from sourced_intel.nodes.aggregate import aggregate, infer_adcode  # noqa: E402
from sourced_intel.nodes.parse_intent import resolve_region  # noqa: E402
from sourced_intel.nodes.quality_gate import quality_gate  # noqa: E402
from sourced_intel.schemas import (  # noqa: E402
    Aggregation,
    IntelItem,
    QuerySpec,
    RawDoc,
    parse_amount,
)
from sourced_intel.state import (  # noqa: E402
    initial_state,
    load_snapshot,
    node_context,
    node_payload_stats,
    restore_state,
    save_snapshot,
    to_jsonable,
)


def make_query(industry: str = "energy storage", keywords: list[str] | None = None) -> QuerySpec:
    return QuerySpec(
        industry=industry,
        region_adcode=REGION_TO_ADCODE["深圳"],
        region_name=REGION_DISPLAY[REGION_TO_ADCODE["深圳"]],
        timespan="3m",
        keywords=keywords or [],
        raw_query="test query",
    )


def make_item(**overrides) -> IntelItem:
    base = {
        "title": "总投资3.3亿元!墨玉储能项目即将投入商业运营",
        "subject": "某能源公司",
        "amount": 3.3e8,
        "amount_raw": "3.3亿元",
        "event_time": datetime(2026, 9, 18, tzinfo=timezone.utc),
        "location_name": None,
        "event_type": "project",
        "confidence": 0.9,
        "source_url": "https://www.china5e.com/news/news-1209422-1.html",
        "source": "china5e",
    }
    base.update(overrides)
    return IntelItem(**base)


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------
class TestRegionResolution:
    def test_district_beats_city(self):
        adcode, name = resolve_region("深圳市南山区")
        assert adcode == REGION_TO_ADCODE["南山区"]
        assert name == "南山区"

    def test_plain_city(self):
        assert resolve_region("深圳")[0] == REGION_TO_ADCODE["深圳"]

    def test_english_falls_back_to_default(self):
        adcode, _ = resolve_region("Shenzhen")
        assert adcode == REGION_TO_ADCODE["深圳"]

    def test_empty_string(self):
        assert resolve_region("")[0] == REGION_TO_ADCODE["深圳"]

    def test_unknown_region_falls_back(self):
        assert resolve_region("火星")[0] == REGION_TO_ADCODE["深圳"]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
class TestAggregation:
    def test_infers_adcode_from_title_text(self):
        item = make_item(title="深圳南山储能项目开工")
        assert infer_adcode(item) == REGION_TO_ADCODE["南山区"]

    def test_prefers_explicit_adcode(self):
        item = make_item(adcode=REGION_TO_ADCODE["宝安区"], title="深圳南山储能项目开工")
        assert infer_adcode(item) == REGION_TO_ADCODE["宝安区"]

    def test_unmapped_item_is_counted(self):
        item = make_item(title="某公司发布新产品", location_name=None, subject="某公司")
        assert infer_adcode(item) is None

    def test_totals_and_buckets(self):
        items = [
            make_item(title="深圳储能项目A", amount=1e8, amount_raw="1亿元"),
            make_item(title="深圳储能项目B", amount=2e8, amount_raw="2亿元"),
            make_item(title="广州储能项目C", amount=None, amount_raw=None),
        ]
        state = {"run_id": "t", "query": make_query(), "intel_items": items}
        update = aggregate(state)
        agg: Aggregation = update["aggregation"]
        assert agg.total_items == 3
        assert agg.with_amount == 2
        assert agg.total_amount == 3e8
        assert agg.by_month, "items carry event_time so a month bucket must exist"

    def test_empty_input_does_not_raise(self):
        update = aggregate({"run_id": "t", "query": make_query(), "intel_items": []})
        assert update["aggregation"].total_items == 0

    def test_amount_never_summed_by_llm(self):
        # Regression guard: totals come from code, so they must be exactly the sum.
        items = [make_item(amount=3.3e8), make_item(amount=1.7e8, title="另一个储能项目")]
        update = aggregate({"run_id": "t", "query": make_query(), "intel_items": items})
        assert update["aggregation"].total_amount == pytest.approx(5.0e8)


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------
class TestQualityGate:
    def test_low_confidence_moves_to_appendix(self):
        high = make_item(confidence=0.9)
        low = make_item(confidence=0.4, title="低置信度储能项目")
        state = {"run_id": "t", "intel_items": [high, low], "report_path": "x.html"}
        update = quality_gate(state)
        report = update["quality"]
        assert report.passed_items == 1
        assert report.rejected_items == 1

    def test_needs_review_is_excluded_even_when_confident(self):
        flagged = make_item(confidence=0.95).flag_for_review("subject missing")
        state = {"run_id": "t", "intel_items": [flagged], "report_path": "x.html"}
        update = quality_gate(state)
        assert update["quality"].passed_items == 0
        assert update["quality"].rejected_items == 1

    def test_status_without_report_is_failed(self):
        state = {"run_id": "t", "intel_items": [make_item()], "report_path": None}
        assert quality_gate(state)["status"] == "failed"

    def test_failed_sources_are_carried_through(self):
        state = {
            "run_id": "t",
            "intel_items": [make_item()],
            "report_path": "x.html",
            "failed_sources": ["gdelt"],
        }
        assert quality_gate(state)["quality"].failed_sources == ["gdelt"]


# ---------------------------------------------------------------------------
# Source binding: the URL must come from our own document list
# ---------------------------------------------------------------------------
class TestSourceBinding:
    def test_model_cannot_supply_a_url(self):
        from sourced_intel.nodes.extract import _to_intel_item

        doc = RawDoc(
            source="china5e",
            url="https://example.com/real-article",
            title="标题",
            raw_text="正文" * 50,
        )
        payload = {
            "title": "标题",
            "subject": "某公司",
            "amount_raw": "1亿元",
            "event_time": "2026-08-18",
            "event_type": "project",
            "confidence": 0.9,
        }
        item = _to_intel_item(payload, doc, 0)
        assert item.source_url == "https://example.com/real-article"

    def test_missing_subject_is_flagged_not_invented(self):
        from sourced_intel.nodes.extract import _to_intel_item

        doc = RawDoc(source="gdelt", url="https://example.com/a", title="标题", raw_text="")
        item = _to_intel_item({"title": "标题", "confidence": 0.8}, doc, 0)
        assert item.subject == ""
        assert item.needs_review is True
        assert item.review_reason

    def test_article_date_is_used_when_model_omits_a_date(self):
        from sourced_intel.nodes.extract import _to_intel_item

        published = datetime(2026, 9, 1, tzinfo=timezone.utc)
        doc = RawDoc(
            source="china5e",
            url="https://example.com/a",
            title="标题",
            raw_text="正文" * 20,
            published_at=published,
        )
        item = _to_intel_item({"subject": "某公司", "confidence": 0.7}, doc, 0)
        assert item.event_time == published


# ---------------------------------------------------------------------------
# Context trimming + snapshots
# ---------------------------------------------------------------------------
class TestContextTrimming:
    def test_extract_node_never_sees_analysis(self):
        state = initial_state("t", "q")
        state["analysis"] = "SHOULD NOT LEAK"
        state["raw_docs"] = [RawDoc(source="s", url="u", title="t", raw_text="x" * 5000)]
        ctx = node_context("03_extract", state)
        assert "analysis" not in ctx
        assert len(ctx["raw_docs"]) == 1

    def test_payload_stats_report_trimmed_size(self):
        state = initial_state("t", "q")
        state["raw_docs"] = [RawDoc(source="s", url="u", title="t", raw_text="x" * 5000)]
        stats = node_payload_stats("03_extract", state)
        # 5000 chars of body must be reported as 1500 after trimming
        assert stats["context_chars"] == 1500


class TestSnapshots:
    def test_roundtrip_preserves_models(self, tmp_path):
        state = initial_state("run-x", "question")
        state["query"] = make_query()
        state["intel_items"] = [make_item()]

        save_snapshot(tmp_path, "01_test", state)
        payload = to_jsonable(dict(state))
        restored = restore_state(payload)

        assert restored["query"].industry == "energy storage"
        assert len(restored["intel_items"]) == 1
        assert isinstance(restored["intel_items"][0].event_time, datetime)

    def test_load_snapshot_from_disk(self, tmp_path):
        state = initial_state("run-y", "q")
        state["query"] = make_query()
        save_snapshot(tmp_path, "01_test", state)
        loaded = load_snapshot(tmp_path, "01_test")
        assert loaded["run_id"] == "run-y"
        assert loaded["query"].region_name == "深圳市"


# ---------------------------------------------------------------------------
# Amount parsing regression (kept here too, so a schema change trips it)
# ---------------------------------------------------------------------------
def test_amount_parsing_survives_schema_import():
    assert parse_amount("总投资3.3亿元")[0] == 3.3e8
    assert parse_amount("no amount") == (None, None)


# ---------------------------------------------------------------------------
# End-to-end (network + credentials required)
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_end_to_end_run():
    """Full pipeline against live sources. Skipped without credentials."""
    import os

    from sourced_intel.config import get_settings
    from sourced_intel.graph import run_pipeline

    settings = get_settings()
    if not settings.glm_api_key and os.environ.get("LLM_PROVIDER") != "ollama":
        pytest.skip("GLM_API_KEY not set - skipping the live end-to-end run")

    result = run_pipeline("深圳市储能产业最近三个月的公开动向")
    summary = result["summary"]

    assert summary["status"] in {"completed", "completed_with_warnings", "no_data"}
    assert summary["snapshots"], "every run must leave step snapshots behind"
    assert (Path(summary["run_dir"]) / "trace.jsonl").exists()

    if summary["report_path"]:
        assert Path(summary["report_path"]).exists()
    # A failing source must degrade, not crash: this is the headline requirement.
    assert isinstance(summary["failed_sources"], list)
