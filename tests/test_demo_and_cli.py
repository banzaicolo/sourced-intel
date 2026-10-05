"""Tests for the bundled demo run, the CLI, and the visual-layer helpers.

The demo is not decoration: it is the first thing a visitor runs, and it is also
the fixture that keeps the template exercised offline. So it gets the same kind of
contract tests as the pipeline.

The interesting assertions here are the ones about *consistency*: the demo's
numbers are produced by the real aggregator, and these tests recompute them
independently from the records to make sure that stays true. A fixture with
hand-typed totals drifts the moment either side changes, and a demo whose own
numbers disagree with its own table is worse than no demo.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel import cli, demo  # noqa: E402
from sourced_intel.report.render import (  # noqa: E402
    _build_sources,
    _counters,
    _svg_map,
    format_amount_compact,
)
from sourced_intel.schemas import Aggregation, Analysis, IntelItem  # noqa: E402

SECTION_RE = re.compile(r'<section id="([a-z]+)"[^>]*>')


# ---------------------------------------------------------------------------
# The demo fixture
# ---------------------------------------------------------------------------
class TestDemoFixture:
    def test_it_builds(self):
        payload = demo.build()
        assert payload["items"], "the demo has no records to show"
        assert payload["rejected"], "the demo never exercises the low-confidence appendix"

    def test_every_record_survives_schema_validation(self):
        payload = demo.build()
        for row in payload["items"] + payload["rejected"]:
            IntelItem.model_validate(row)

    def test_no_flagged_record_reaches_the_findings(self):
        """Node 6 excludes `needs_review` regardless of confidence; the demo must too."""
        payload = demo.build()
        assert all(not i["needs_review"] for i in payload["items"])
        assert all(i["needs_review"] or i["confidence"] < 0.6 for i in payload["rejected"])

    def test_the_aggregation_is_derived_from_the_records(self):
        """Recompute every headline figure from the records and compare."""
        payload = demo.build()
        items = payload["items"]
        agg = payload["aggregation"]

        assert agg["total_items"] == len(items)
        assert agg["with_amount"] == sum(1 for i in items if i["amount"] is not None)
        assert agg["total_amount"] == pytest.approx(sum(i["amount"] or 0.0 for i in items))
        assert sum(b["count"] for b in agg["by_month"]) == len(items)
        assert sum(agg["by_event_type"].values()) == len(items)
        # `by_subject` is capped at the top 15, so summing it can be smaller than
        # the number of records that named a subject - never larger.
        assert sum(s["count"] for s in agg["by_subject"]) <= sum(
            1 for i in items if i["subject"].strip()
        )
        assert agg["with_subject"] == sum(1 for i in items if i["subject"].strip())
        # Every record is either in a region bucket or counted as unmapped.
        assert sum(b["count"] for b in agg["by_region"]) + agg["unmapped_items"] == len(items)

    def test_the_region_table_and_the_evidence_table_agree(self):
        payload = demo.build()
        buckets = {b["adcode"]: b for b in payload["aggregation"]["by_region"]}
        expected: dict[str, tuple[int, float]] = {}
        unmapped = 0
        for item in payload["items"]:
            adcode = item["adcode"]
            if adcode is None:
                unmapped += 1
                continue
            assert adcode in buckets, (
                f"record {item['title']!r} resolved to {adcode}, which has no region row"
            )
            count, total = expected.get(adcode, (0, 0.0))
            expected[adcode] = (count + 1, total + (item["amount"] or 0.0))

        assert unmapped == payload["aggregation"]["unmapped_items"]
        for adcode, (count, total) in expected.items():
            assert buckets[adcode]["count"] == count
            assert buckets[adcode]["total_amount"] == pytest.approx(total, abs=0.01)

    def test_every_share_is_stated_against_the_true_named_total(self):
        """The denominator printed next to the share column must be the real one.

        Regression this locks down: `by_subject` is capped at 15 rows, so the
        template's `sum(row.count)` understated the denominator whenever a window
        produced more than 15 distinct subjects - and the share values beside it
        had been computed against the full count. The numbers were right and the
        label was wrong, which is the harder kind of wrong to notice.
        """
        payload = demo.build()
        agg = payload["aggregation"]
        shown = sum(s["count"] for s in agg["by_subject"])
        assert shown < agg["with_subject"], (
            "this fixture no longer exercises the truncation case; add subjects"
        )
        top = agg["by_subject"][0]
        assert top["share"] == pytest.approx(top["count"] / agg["with_subject"], abs=1e-4)

    def test_the_concentration_figure_matches_the_subject_table(self):
        payload = demo.build()
        counts = sorted((s["count"] for s in payload["aggregation"]["by_subject"]), reverse=True)
        named = sum(counts)
        assert payload["analysis"]["subject_concentration"]["top3_share"] == pytest.approx(
            sum(counts[:3]) / named, abs=1e-4
        )

    def test_finding_two_quotes_the_region_table_it_claims_to_quote(self):
        """The finding's numbers are written by code, so they cannot go stale."""
        payload = demo.build()
        ranked = sorted(
            (b for b in payload["aggregation"]["by_region"] if b["total_amount"] > 0),
            key=lambda b: -b["total_amount"],
        )
        detail = payload["analysis"]["findings"][1]["detail"]
        assert ranked[0]["region_name"] in detail
        assert ranked[1]["region_name"] in detail
        assert f"{ranked[0]['total_amount'] / 1e8:,.2f}" in detail

    def test_no_source_url_can_resolve_to_a_real_page(self):
        """`.example` is reserved by RFC 2606 precisely so a sample cannot lie."""
        payload = demo.build()
        urls = [i["source_url"] for i in payload["items"] + payload["rejected"]]
        urls += [u for f in payload["analysis"]["findings"] for u in f["evidence"]]
        assert urls, "the demo cites nothing"
        for url in urls:
            host = url.split("/")[2]
            assert host.endswith(".example"), f"a sample citation points at a live domain: {host}"

    def test_it_is_deterministic(self):
        first = {k: v for k, v in demo.build().items() if k != "generated_at"}
        second = {k: v for k, v in demo.build().items() if k != "generated_at"}
        assert first == second


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------
class TestCli:
    def test_demo_writes_html(self, tmp_path, capsys):
        out = tmp_path / "sample.html"
        assert cli.main(["demo", "--out", str(out)]) == 0
        assert out.exists()
        assert not out.with_suffix(".md").exists(), "the template fell back to Markdown"
        assert "<html" in out.read_text(encoding="utf-8").lower()

    def test_demo_report_contains_every_section(self, tmp_path):
        out = tmp_path / "sample.html"
        cli.main(["demo", "--out", str(out)])
        html = out.read_text(encoding="utf-8")
        ids = SECTION_RE.findall(html)
        assert ids == [
            "summary",
            "figures",
            "geo",
            "trend",
            "findings",
            "subjects",
            "evidence",
            "references",
            "appendix",
            "coverage",
        ]

    def test_demo_report_says_that_it_is_a_sample(self, tmp_path):
        out = tmp_path / "sample.html"
        cli.main(["demo", "--out", str(out)])
        html = out.read_text(encoding="utf-8")
        assert "Sample data" in html
        assert ".example" in html, "the reader is not told the links cannot resolve"

    def test_demo_report_leaves_no_unrendered_jinja(self, tmp_path):
        out = tmp_path / "sample.html"
        cli.main(["demo", "--out", str(out)])
        html = out.read_text(encoding="utf-8")
        assert "{{" not in html and "{%" not in html

    def test_the_sample_colophon_does_not_point_at_a_trace_file_that_does_not_exist(self, tmp_path):
        out = tmp_path / "sample.html"
        cli.main(["demo", "--out", str(out)])
        html = out.read_text(encoding="utf-8")
        assert f"data/runs/{demo.DEMO_RUN_ID}/trace.jsonl" not in html
        assert "Bundled sample data" in html

    def test_an_unknown_command_is_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            cli.main(["definitely-not-a-command"])

    def test_every_subcommand_is_reachable(self):
        parser = cli.build_parser()
        actions = [a for a in parser._subparsers._group_actions if hasattr(a, "choices")]
        assert actions, "no subparsers registered"
        assert set(actions[0].choices) == {"demo", "run", "stats", "preflight", "ui", "api"}


# ---------------------------------------------------------------------------
# Map rendering
# ---------------------------------------------------------------------------
def _feature(name: str, count: int, ring: list[list[float]]) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "name": name,
            "adcode": "440300",
            "__count": count,
            "__intensity": 0.5,
            "__amount": 0,
            "centroid": ring[0],
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


SQUARE = [[114.0, 22.5], [114.1, 22.5], [114.1, 22.6], [114.0, 22.6], [114.0, 22.5]]


class TestStaticMap:
    def test_no_geometry_produces_no_svg(self):
        assert _svg_map({"features": []}, Aggregation()) == ""

    def test_geometry_becomes_paths(self):
        geo = {"features": [_feature("南山区", 3, SQUARE)]}
        svg = _svg_map(geo, Aggregation())
        assert svg.startswith('<svg class="choropleth"')
        assert "<path d=" in svg
        assert "南山区" in svg

    def test_a_degenerate_ring_is_skipped_rather_than_crashing(self):
        geo = {"features": [_feature("坏数据", 1, [[114.0, 22.5], [114.1, 22.5]])]}
        assert _svg_map(geo, Aggregation()) == ""

    def test_a_feature_without_properties_does_not_crash(self):
        geo = {
            "features": [
                {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [SQUARE]}}
            ]
        }
        svg = _svg_map(geo, Aggregation())
        assert "<path d=" in svg

    def test_names_are_html_escaped(self):
        """The GeoJSON comes from a third party; it is not trusted markup."""
        geo = {"features": [_feature("<script>alert(1)</script>", 1, SQUARE)]}
        svg = _svg_map(geo, Aggregation())
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg

    def test_the_label_carries_the_count_and_nothing_repeats_it(self):
        """Shading gives magnitude; the printed number gives the value.

        A sized circle at the centroid used to encode the same count a third
        time, on top of the label, under the label's own halo. Anything that
        draws a marker inside the static SVG is now a regression.
        """
        geo = {"features": [_feature("南山区", 3, SQUARE)]}
        svg = _svg_map(geo, Aggregation())
        assert "<circle" not in svg
        assert "南山区" in svg
        assert ">3</tspan>" in svg
        assert "fill-opacity=" in svg, "the shading ramp is what encodes magnitude"


# ---------------------------------------------------------------------------
# Formatting, registry and counters
# ---------------------------------------------------------------------------
class TestFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "-"),
            (0, "-"),
            (-5, "-"),
            (9_999, "9,999"),
            (73_000_000.0, "7,300万"),
            (86_000_000.0, "8,600万"),
            (110_000_000.0, "1.10亿"),
            (18_600_000_000.0, "186.00亿"),
        ],
    )
    def test_compact_amounts(self, value, expected):
        assert format_amount_compact(value) == expected

    def test_compact_amounts_never_show_a_pointless_decimal(self):
        assert format_amount_compact(50_000_000.0) == "5,000万"
        assert "万" in format_amount_compact(50_000_000.0)

    def test_garbage_does_not_raise(self):
        assert format_amount_compact("not a number") == "-"


def _item(url: str, title: str = "t", source: str = "china5e", **kw) -> IntelItem:
    return IntelItem(title=title, source_url=url, source=source, **kw)


class TestSourceRegistry:
    def test_one_url_one_number(self):
        items = [_item("https://a.example/1"), _item("https://a.example/1", "second")]
        registry, index = _build_sources(items, Analysis(), [])
        assert len(registry) == 1
        assert registry[0]["n"] == 1
        assert index["https://a.example/1"] == 1

    def test_numbering_follows_the_table_then_the_findings(self):
        items = [_item("https://a.example/1"), _item("https://a.example/2")]
        analysis = Analysis(
            findings=[
                {
                    "headline": "h",
                    "detail": "",
                    "confidence": 0.5,
                    "evidence": ["https://a.example/2", "https://orphan.example/x"],
                },
            ]
        )
        _registry, index = _build_sources(items, analysis, [])
        assert index["https://a.example/1"] == 1
        assert index["https://a.example/2"] == 2
        assert index["https://orphan.example/x"] == 3

    def test_a_first_writer_with_an_empty_title_is_filled_in_later(self):
        analysis = Analysis(
            findings=[
                {
                    "headline": "h",
                    "detail": "",
                    "confidence": 0.5,
                    "evidence": ["https://a.example/1"],
                },
            ]
        )
        registry, _ = _build_sources([_item("https://a.example/1", "the title")], analysis, [])
        assert registry[0]["title"] == "the title"

    def test_blank_urls_are_ignored(self):
        registry, _ = _build_sources([_item("")], Analysis(), [])
        assert registry == []


class TestCounters:
    def test_it_counts_documents_and_sources(self):
        items = [
            _item("https://a.example/1"),
            _item("https://a.example/2"),
            _item("https://b.example/1"),
        ]
        counters = _counters(items, raw_docs=9)
        assert counters["documents"] == 3
        assert counters["raw_docs"] == 9

    def test_it_reports_conflicts_and_regex_recoveries(self):
        items = [
            _item("https://a.example/1", amount_conflict=True),
            _item("https://a.example/2", amount_source="regex"),
        ]
        counters = _counters(items, raw_docs=None)
        assert counters["conflicts"] == 1
        assert counters["regex_amounts"] == 1

    def test_raw_docs_is_optional(self):
        assert _counters([], raw_docs=None)["raw_docs"] is None


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------
class TestUntrustedTextIsEscaped:
    def test_a_record_title_cannot_inject_markup(self, tmp_path):
        payload = demo.build()
        payload["items"][0]["title"] = '<img src=x onerror="alert(1)">'
        items = [IntelItem.model_validate(i) for i in payload["items"]]
        from sourced_intel.report.render import render_html
        from sourced_intel.schemas import QuerySpec

        out = tmp_path / "r.html"
        render_html(
            run_id="r",
            user_query="q",
            query=QuerySpec.model_validate(payload["query"]),
            aggregation=Aggregation.model_validate(payload["aggregation"]),
            analysis=Analysis.model_validate(payload["analysis"]),
            items=items,
            rejected=[],
            failed_sources=[],
            errors=[],
            summary=payload["summary"],
            threshold=0.5,
            model_name="m",
            output_path=out,
        )
        html = out.read_text(encoding="utf-8")
        assert out.suffix == ".html"
        assert "<img src=x" not in html
        assert "&lt;img" in html
