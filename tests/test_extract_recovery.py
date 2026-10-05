"""Node 3 recovery tests: a rejected batch must be split, never silently dropped.

The regression these cover, measured on 2026-09-25: `_extract_batch` caught
`LLMError` and `break`-ed, which discarded *every* document in the batch. GLM's
concurrency ceiling is 2, node 3 runs 4 threads, so 2 of 5 batches were rejected
on every run - and the run still reported `completed`. The same 36 documents
produced 7, 15 and 29 records across three runs, and nothing in the status
distinguished the good run from the bad ones.

These tests use a fake provider, so they are offline and instant; the real
end-to-end behaviour is covered by the `slow` test in test_pipeline_smoke.py.

Run:
    pytest tests/test_extract_recovery.py -v
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.config import get_settings  # noqa: E402
from sourced_intel.llm import LLMAuthError, LLMError, LLMResponse  # noqa: E402
from sourced_intel.nodes import extract as extract_mod  # noqa: E402
from sourced_intel.nodes.extract import (  # noqa: E402
    MAX_SPLIT_DEPTH,
    _extract_batch,
    extract,
)
from sourced_intel.schemas import GraphState, RawDoc  # noqa: E402
from sourced_intel.state import initial_state  # noqa: E402

DOC_ID_RE = re.compile(r"\[doc_id=(\d+)\]")


def make_docs(n: int) -> tuple[list[tuple[int, RawDoc]], dict[int, RawDoc]]:
    indexed = [
        (
            i,
            RawDoc(
                source="china5e",
                url=f"https://example.com/news-{i}",
                title=f"深圳储能项目{i}",
                raw_text="正文内容" * 40,
            ),
        )
        for i in range(1, n + 1)
    ]
    return indexed, dict(indexed)


class FakeProvider:
    """A provider that rejects requests at or above a size, and records every call.

    `reject_when_over` mirrors the real failure mode: the request is refused
    before any tokens are produced, because the quota will not admit it.
    """

    def __init__(
        self,
        *,
        reject_when_over: int | None = None,
        always_reject: bool = False,
        auth_failure: bool = False,
    ) -> None:
        self.reject_when_over = reject_when_over
        self.always_reject = always_reject
        self.auth_failure = auth_failure
        self.calls: list[list[int]] = []

    @property
    def batch_sizes(self) -> list[int]:
        return [len(c) for c in self.calls]

    def chat(self, prompt: str, **kwargs) -> LLMResponse:
        doc_ids = [int(m) for m in DOC_ID_RE.findall(prompt)]
        self.calls.append(doc_ids)

        if self.auth_failure:
            raise LLMAuthError("glm failed after 1 attempts: HTTP 401 unauthorized")

        too_big = self.reject_when_over is not None and len(doc_ids) > self.reject_when_over
        if self.always_reject or too_big:
            raise LLMError("glm failed after 1 attempts: HTTP 429 code 1302 rate limit")

        payload = {
            "items": [
                {
                    "doc_id": doc_id,
                    "title": f"深圳储能项目{doc_id}",
                    "subject": f"主体{doc_id}",
                    "amount_raw": "1亿元",
                    "event_time": "2026-09-01",
                    "event_type": "project",
                    "confidence": 0.8,
                }
                for doc_id in doc_ids
            ]
        }
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            model="fake-glm",
            provider="fake",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


@pytest.fixture
def settings():
    return get_settings()


def install(monkeypatch, provider: FakeProvider) -> None:
    monkeypatch.setattr(extract_mod, "get_llm", lambda: provider)


# ---------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------
class TestRejectedBatchIsRecovered:
    def test_rejected_batch_is_split_instead_of_dropped(self, monkeypatch, settings):
        """A 4-document batch refused as a whole must come back as 2 + 2."""
        provider = FakeProvider(reject_when_over=2)
        install(monkeypatch, provider)
        batch, index = make_docs(4)

        items, usage, _errors = _extract_batch(batch, index, settings)

        assert provider.batch_sizes == [4, 2, 2], "the whole batch failed, then each half ran"
        assert len(items) == 4, "every document must survive the rejection"
        assert usage["items_extracted"] == 4

    def test_no_document_is_lost_when_the_first_half_also_fails(self, monkeypatch, settings):
        """fail at 8 and at 4: the 2-document requests must still recover."""
        provider = FakeProvider(reject_when_over=3)
        install(monkeypatch, provider)
        batch, index = make_docs(8)

        items, _, _ = _extract_batch(batch, index, settings)

        # The retry is depth-first, so the request order is 8, 4, 2, 2, 4, 2, 2.
        # Assert the multiset rather than the order - the order is an implementation
        # detail, the set of request sizes is the contract.
        assert sorted(provider.batch_sizes, reverse=True) == [8, 4, 4, 2, 2, 2, 2]
        assert len({i.source_url for i in items}) == 8

    def test_split_is_recorded_in_the_errors_list(self, monkeypatch, settings):
        """A degradation that is not recorded cannot be counted or alerted on."""
        provider = FakeProvider(reject_when_over=2)
        install(monkeypatch, provider)
        batch, index = make_docs(4)

        _, _, errors = _extract_batch(batch, index, settings)

        split_errors = [e for e in errors if e["type"] == "batch_split"]
        assert len(split_errors) == 1
        assert "batch of 4" in split_errors[0]["detail"]
        assert split_errors[0]["node"] == "03_extract"

    def test_usage_is_summed_across_the_halves(self, monkeypatch, settings):
        """The cost of the recovery must stay attributable to the node."""
        provider = FakeProvider(reject_when_over=2)
        install(monkeypatch, provider)
        batch, index = make_docs(4)

        _, usage, _ = _extract_batch(batch, index, settings)

        # Two successful halves, 10 prompt + 5 completion tokens each.
        assert usage["prompt_tokens"] == 20
        assert usage["completion_tokens"] == 10
        assert usage["total_tokens"] == 30
        assert usage["model"] == "fake-glm"

    def test_a_clean_batch_never_splits(self, monkeypatch, settings):
        provider = FakeProvider()
        install(monkeypatch, provider)
        batch, index = make_docs(8)

        items, _, errors = _extract_batch(batch, index, settings)

        assert provider.batch_sizes == [8]
        assert len(items) == 8
        assert errors == []


# ---------------------------------------------------------------------------
# Bounds: recovery must not become an infinite retry storm
# ---------------------------------------------------------------------------
class TestRecoveryBounds:
    def test_halving_stops_at_single_documents(self, monkeypatch, settings):
        """A provider that rejects everything ends at 1-document requests."""
        provider = FakeProvider(always_reject=True)
        install(monkeypatch, provider)
        batch, index = make_docs(8)

        items, _, _errors = _extract_batch(batch, index, settings)

        assert items == []
        assert max(provider.batch_sizes) == 8
        assert min(provider.batch_sizes) == 1
        # 1 + 2 + 4 + 8 calls: each level is bounded, and no level is repeated.
        assert provider.batch_sizes.count(1) == 8
        assert len(provider.calls) == 2 ** (MAX_SPLIT_DEPTH + 1) - 1 == 15

    def test_total_loss_is_reported_per_document(self, monkeypatch, settings):
        """Nothing recovered means every document is accounted for as an error."""
        provider = FakeProvider(always_reject=True)
        install(monkeypatch, provider)
        batch, index = make_docs(4)

        _, _, errors = _extract_batch(batch, index, settings)

        assert len([e for e in errors if e["type"] == "llm_error"]) == 4
        assert all(e["node"] == "03_extract" for e in errors)

    def test_auth_errors_are_never_retried_or_split(self, monkeypatch, settings):
        """A bad credential is permanent; splitting would only multiply dead calls."""
        provider = FakeProvider(auth_failure=True)
        install(monkeypatch, provider)
        batch, index = make_docs(8)

        items, _, errors = _extract_batch(batch, index, settings)

        assert provider.calls == [list(range(1, 9))], "exactly one attempt"
        assert items == []
        assert [e["type"] for e in errors] == ["llm_auth_error"]


# ---------------------------------------------------------------------------
# The node, end to end through its public entry point
# ---------------------------------------------------------------------------
class TestExtractNodeReportsRecovery:
    def make_state(self, n: int) -> GraphState:
        state = initial_state("run-recovery-test", "深圳市储能产业动向")
        indexed, _ = make_docs(n)
        state["raw_docs"] = [doc for _, doc in indexed]
        return state

    def test_node_keeps_the_corpus_and_counts_the_splits(self, monkeypatch):
        provider = FakeProvider(reject_when_over=4)
        install(monkeypatch, provider)

        update = extract(self.make_state(8))

        meta = update["node_meta"]["03_extract"]
        assert meta["batches"] == 1
        assert meta["batches_split"] == 1
        assert meta["documents"] == 8
        assert meta["items"] == 8, "a rejection must not shrink the report"

    def test_meta_does_not_claim_the_worst_case_is_normal(self, monkeypatch):
        """When everything is rejected, splits are counted and no items appear."""
        provider = FakeProvider(always_reject=True)
        install(monkeypatch, provider)

        update = extract(self.make_state(4))

        meta = update["node_meta"]["03_extract"]
        assert meta["items"] == 0
        assert meta["batches_split"] == 3  # 4 -> 2+2 -> 1+1 and 1+1
        assert any(e["type"] == "no_items_extracted" for e in update["errors"])
