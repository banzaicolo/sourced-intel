"""Tests for the analysis node tolerating the model's JSON shape.

`json_mode` guarantees only that the reply parses as JSON - not that it matches
our schema. These tests pin down the two deviations GLM actually produced in a
live run, both of which used to cost us the entire analysis: the findings list
arriving under an "answer" key, and confidence arriving as the word "high".

That combination is worth a test rather than a comment because the failure is
silent. `Analysis.findings` defaults to an empty list, so a renamed key looks
exactly like "the model had nothing to say": the node swaps in its deterministic
fallback, the report still renders, and nothing in the output looks wrong. The
only visible trace was `fallback: true` in the tracer.

Run:
    pytest tests/test_analyze_payload.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.nodes.analyze import (  # noqa: E402
    _coerce_confidence,
    _normalise_analysis_payload,
)
from sourced_intel.schemas import Analysis  # noqa: E402


def _finding(confidence: object) -> dict:
    return {
        "headline": "储能项目密集开工",
        "detail": "多个项目宣布开工或并网。",
        "evidence": ["https://www.china5e.com/news/news-1208952-1.html"],
        "confidence": confidence,
    }


class TestConfidenceCoercion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0.9, 0.9),
            ("0.9", 0.9),
            ("90", 0.9),
            (90, 0.9),
            ("high", 0.9),
            ("HIGH", 0.9),
            ("medium", 0.6),
            ("low", 0.3),
            (None, 0.5),
            ("nonsense", 0.5),
            (True, 0.5),  # bool is not a confidence
            (-1, 0.0),
            (1, 1.0),
        ],
    )
    def test_coercion(self, raw, expected):
        assert _coerce_confidence(raw) == pytest.approx(expected)

    def test_always_returns_float(self):
        assert isinstance(_coerce_confidence("high"), float)


class TestPayloadNormalisation:
    def test_answer_wrapper_is_unwrapped(self):
        """The exact shape GLM returned in a live rehearsal."""
        payload = {"answer": [_finding("high")]}
        analysis = Analysis.model_validate(_normalise_analysis_payload(payload))
        assert len(analysis.findings) == 1
        assert analysis.findings[0].confidence == pytest.approx(0.9)

    def test_bare_list_is_wrapped(self):
        analysis = Analysis.model_validate(_normalise_analysis_payload([_finding(0.7)]))
        assert len(analysis.findings) == 1

    def test_canonical_payload_is_untouched(self):
        analysis = Analysis.model_validate(
            _normalise_analysis_payload({"findings": [_finding(0.8)], "trend_note": "up"})
        )
        assert analysis.findings[0].confidence == pytest.approx(0.8)
        assert analysis.trend_note == "up"

    def test_canonical_findings_key_wins_over_aliases(self):
        payload = {"findings": [_finding(0.8)], "answer": [_finding(0.1), _finding(0.1)]}
        analysis = Analysis.model_validate(_normalise_analysis_payload(payload))
        assert len(analysis.findings) == 1

    def test_unusable_payload_yields_empty_list_not_a_crash(self):
        for payload in ({}, {"answer": "not a list"}, None, 42, {"findings": "nope"}):
            analysis = Analysis.model_validate(_normalise_analysis_payload(payload))
            assert analysis.findings == []

    def test_non_dict_entries_are_skipped(self):
        analysis = Analysis.model_validate(
            _normalise_analysis_payload({"findings": ["junk", _finding(0.5)]})
        )
        assert len(analysis.findings) == 1

    def test_confidence_is_numeric_once_validated(self):
        analysis = Analysis.model_validate(
            _normalise_analysis_payload({"answer": [_finding("high")]})
        )
        assert isinstance(analysis.findings[0].confidence, float)
