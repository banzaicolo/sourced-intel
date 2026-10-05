"""Contract tests for the HTML report renderer.

These lock down four things that are invisible to a linter but obvious to a
reviewer opening the report:

  * `render_html` **swallows every exception** and falls back to Markdown. That
    is the right runtime behaviour and a terrible test behaviour: a broken Jinja
    template would still "succeed" and hand back a `.md` file. The tests below
    assert the HTML path really produced HTML.
  * the table of contents and the section headings must share one numbering pass
    (`_sections`), otherwise a section is added and the TOC silently lies.
  * a source that stated only a month must not be rendered as the 1st of that
    month - that invents a day nobody published.
  * a source column must stay readable and a citation must resolve to exactly one
    document.

Rendering needs no network: the Shenzhen boundary file is already in
`data/cache/geo`, so the geo lookup is a cache hit.

The section markup is matched with attribute-tolerant patterns (`<section
id="x"[^>]*>`) because the classes on it are presentation, not contract. The
numbers inside it are contract.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.config import REGION_DISPLAY, REGION_TO_ADCODE  # noqa: E402
from sourced_intel.report.render import _display_date, _sections, render_html  # noqa: E402
from sourced_intel.schemas import (  # noqa: E402
    Aggregation,
    Analysis,
    Finding,
    IntelItem,
    MonthBucket,
    QuerySpec,
    RegionBucket,
)

SECTION_RE = re.compile(r'<section id="([a-z]+)"[^>]*>')


def section_ids(html: str) -> list[str]:
    return SECTION_RE.findall(html)


def make_query() -> QuerySpec:
    adcode = REGION_TO_ADCODE["深圳"]
    return QuerySpec(
        industry="energy storage",
        region_adcode=adcode,
        region_name=REGION_DISPLAY[adcode],
        timespan="3m",
        keywords=[],
        raw_query="深圳 储能 项目 投资",
    )


def make_item(**overrides) -> IntelItem:
    base = {
        "title": "总投资3.3亿元!深圳储能项目即将投入商业运营",
        "subject": "某能源公司",
        "amount": 3.3e8,
        "amount_raw": "3.3亿元",
        "event_time": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "event_time_raw": "2026-09",
        "location_name": "南山区",
        "event_type": "project",
        "confidence": 0.9,
        "source_url": "https://www.china5e.com/news/news-1209422-1.html",
        "source": "china5e",
    }
    base.update(overrides)
    return IntelItem(**base)


def make_aggregation(items: list[IntelItem]) -> Aggregation:
    return Aggregation(
        total_items=len(items),
        with_amount=sum(1 for i in items if i.amount),
        total_amount=sum(i.amount or 0.0 for i in items),
        by_region=[
            RegionBucket(
                adcode=REGION_TO_ADCODE["南山区"],
                region_name="南山区",
                count=len(items),
                total_amount=sum(i.amount or 0.0 for i in items),
                items=[i.title for i in items],
            )
        ],
        by_month=[
            MonthBucket(
                month="2026-09", count=len(items), total_amount=sum(i.amount or 0.0 for i in items)
            )
        ],
        by_event_type={"project": len(items)},
        by_subject=[{"subject": "某能源公司", "count": len(items)}],
        unmapped_items=0,
    )


def make_analysis() -> Analysis:
    return Analysis(
        findings=[
            Finding(
                headline="九月投资集中在南山区",
                detail="三条记录中的两条落在南山区。",
                evidence=["https://www.china5e.com/news/news-1209422-1.html"],
                confidence=0.82,
            )
        ],
        subject_concentration={"top3_share": 0.66, "subjects": ["某能源公司"]},
        trend_note="月度总额小幅上升。",
        anomalies=[],
    )


def render(
    tmp_path: Path,
    *,
    items: list[IntelItem] | None = None,
    aggregation: Aggregation | None = None,
    analysis: Analysis | None = None,
    rejected: list[IntelItem] | None = None,
    failed_sources: list[str] | None = None,
    errors: list[dict] | None = None,
) -> tuple[Path, str]:
    """Render once and return (path, html). Path suffix tells us which path ran."""
    items = items if items is not None else [make_item(), make_item(title="第二条记录")]
    out = tmp_path / "report.html"
    path = render_html(
        run_id="run-test-0001",
        user_query="深圳 储能 项目 投资",
        query=make_query(),
        aggregation=aggregation if aggregation is not None else make_aggregation(items),
        analysis=analysis if analysis is not None else make_analysis(),
        items=items,
        rejected=rejected or [],
        failed_sources=failed_sources or [],
        errors=errors or [],
        summary={
            "executive_summary": "本次共采集到若干条深圳储能相关记录。",
            "method_note": "全部数字由代码计算，模型只做归纳。",
        },
        threshold=0.6,
        model_name="glm-4.7",
        output_path=out,
    )
    return path, path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The silent-fallback trap
# ---------------------------------------------------------------------------
class TestTemplateActuallyRenders:
    def test_html_path_is_taken_and_no_markdown_fallback_appears(self, tmp_path):
        """A broken template degrades to `.md` and looks like success from the caller."""
        path, html = render(tmp_path)
        assert path.suffix == ".html", f"renderer fell back to {path.name}"
        assert not path.with_suffix(".md").exists(), "markdown fallback was written"
        assert "<html" in html.lower()

    def test_no_jinja_statement_tags_leak_into_the_output(self, tmp_path):
        """`{%` never appears in rendered output - if it does, the template did not run."""
        _, html = render(tmp_path)
        assert "{%" not in html
        assert "{{" not in html

    def test_a_broken_template_still_produces_a_report(self, tmp_path, monkeypatch):
        """The Markdown fallback is deliberate resilience - pin it so it is not removed."""
        from sourced_intel.report import render as render_mod

        def explode():
            raise RuntimeError("template exploded")

        monkeypatch.setattr(render_mod, "_jinja_env", explode)
        path, text = render(tmp_path)
        assert path.suffix == ".md"
        assert "Executive summary" in text

    def test_all_sections_render_when_appendix_and_coverage_are_present(self, tmp_path):
        _, html = render(
            tmp_path,
            rejected=[make_item(title="低置信度记录", confidence=0.3)],
            failed_sources=["gdelt"],
            errors=[
                {"node": "02_retrieve", "type": "source_failed", "detail": "gdelt unavailable"}
            ],
        )
        for section in (
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
        ):
            assert section in section_ids(html), f"section {section} missing"
        # The optional sections must be absent when there is nothing to put in them.
        _, lean = render(tmp_path)
        assert "appendix" not in section_ids(lean)
        assert "coverage" not in section_ids(lean)

    def test_sections_appear_in_the_order_the_toc_lists_them(self, tmp_path):
        _, html = render(
            tmp_path,
            rejected=[make_item(title="低置信度记录", confidence=0.3)],
            failed_sources=["gdelt"],
        )
        toc = [target for _, target, _ in TestTableOfContents.TOC_RE.findall(html)]
        assert toc == section_ids(html)


# ---------------------------------------------------------------------------
# One numbering pass for the TOC and the body
# ---------------------------------------------------------------------------
class TestTableOfContents:
    TOC_RE = re.compile(r'href="#([a-z]+)" data-target="([a-z]+)">\s*<span class="n">(\d+)</span>')

    def test_toc_and_headings_share_the_same_numbers(self, tmp_path):
        _, html = render(tmp_path)
        toc = self.TOC_RE.findall(html)
        assert toc, "no table-of-contents entries were rendered"

        for href_id, target_id, num in toc:
            assert href_id == target_id, f"TOC anchor and data-target disagree for {href_id}"
            assert href_id in section_ids(html), (
                f"TOC points at a section that does not exist: {href_id}"
            )
            heading_re = re.compile(
                rf'<section id="{re.escape(href_id)}"[^>]*>\s*'
                rf'<div class="sec-head">\s*<span class="n">{num}</span>'
            )
            assert heading_re.search(html), (
                f"TOC says section {href_id} is number {num}, the heading disagrees"
            )

    def test_every_section_is_listed_and_nothing_else_is(self, tmp_path):
        """The TOC is generated, so a missing entry means the two lists diverged."""
        _, html = render(
            tmp_path,
            rejected=[make_item(title="低", confidence=0.3)],
            failed_sources=["gdelt"],
        )
        assert sorted(i for i, _, _ in self.TOC_RE.findall(html)) == sorted(section_ids(html))

    def test_numbers_are_contiguous_and_start_at_one(self, tmp_path):
        _, html = render(tmp_path)
        nums = [int(n) for _, _, n in self.TOC_RE.findall(html)]
        assert nums == list(range(1, len(nums) + 1)), f"section numbering has a gap: {nums}"

    def test_appending_a_section_renumbers_the_toc(self, tmp_path):
        """Guards the real regression: the appendix used to reuse the previous number."""
        _, lean = render(tmp_path)
        _, full = render(
            tmp_path,
            rejected=[make_item(title="低置信度记录", confidence=0.3)],
            failed_sources=["gdelt"],
        )
        lean_ids = [i for i, _, _ in self.TOC_RE.findall(lean)]
        full_ids = [i for i, _, _ in self.TOC_RE.findall(full)]
        assert full_ids == [*lean_ids, "appendix", "coverage"]

    def test_the_section_helper_is_the_single_source_of_truth(self):
        nums, toc = _sections(has_appendix=True, has_coverage=True, has_sources=True)
        assert nums["coverage"] == len(toc)
        assert [entry["id"] for entry in toc][-2:] == ["appendix", "coverage"]
        assert [entry["num"] for entry in toc] == list(range(1, len(toc) + 1))

    def test_references_drop_out_when_there_is_nothing_to_cite(self):
        nums, toc = _sections(has_appendix=False, has_coverage=False, has_sources=False)
        assert "references" not in nums
        assert [entry["id"] for entry in toc] == [
            "summary",
            "figures",
            "geo",
            "trend",
            "findings",
            "subjects",
            "evidence",
        ]


# ---------------------------------------------------------------------------
# Dates keep the precision the source used
# ---------------------------------------------------------------------------
class TestDatePrecision:
    def test_month_only_source_is_not_padded_into_a_fake_day(self, tmp_path):
        _, html = render(tmp_path, items=[make_item(event_time_raw="2026-09")])
        assert "2026-09-01" not in html, "a month-only date was rendered as 'the 1st'"
        assert "2026-09" in html

    def test_day_precision_is_kept_verbatim(self, tmp_path):
        _, html = render(tmp_path, items=[make_item(event_time_raw="2026-09-16")])
        assert "2026-09-16" in html

    def test_year_only_source_is_kept_verbatim(self, tmp_path):
        _, html = render(tmp_path, items=[make_item(event_time_raw="2026")])
        assert "2026-01-01" not in html

    def test_falls_back_to_the_resolved_date_then_to_a_dash(self):
        assert _display_date(make_item(event_time_raw=None)) == "2026-09-01"
        assert _display_date(make_item(event_time_raw=None, event_time=None)) == "-"


# ---------------------------------------------------------------------------
# Source column
# ---------------------------------------------------------------------------
class TestSourcePresentation:
    def test_source_column_shows_a_citation_number_not_a_raw_url(self, tmp_path):
        """A 90-character URL in a 50px column is unreadable; a number is a link.

        Scoped to the table body on purpose: the reference entry further down the
        page *should* spell the URL out, that is what makes it citable.
        """
        _, html = render(tmp_path)
        table = html.split('<table id="evidenceTable">', 1)[1].split("</table>", 1)[0]
        assert ">https://www.china5e.com" not in table, "the raw URL is printed as link text"
        assert "www.china5e.com<" not in table, "the www. prefix was not stripped"
        assert 'class="src-n"' in table, "the source column lost its citation marker"

    def test_the_host_survives_in_the_reference_entry(self, tmp_path):
        """Dropping the URL from the table must not drop it from the document."""
        _, html = render(tmp_path)
        assert "china5e.com" in html

    def test_the_full_url_is_still_reachable_in_the_link(self, tmp_path):
        _, html = render(tmp_path)
        assert "https://www.china5e.com/news/news-1209422-1.html" in html


# ---------------------------------------------------------------------------
# Citations resolve to exactly one document, everywhere
# ---------------------------------------------------------------------------
class TestCitationRegistry:
    """One URL must carry one number for the whole document.

    Before this existed, the evidence table numbered rows and every finding
    restarted its own `[1] [2]`, so two different documents could share a number
    on the same page - the one thing a citation is supposed to prevent.
    """

    REF_RE = re.compile(r'<div class="ref" id="src-(\d+)"')
    CITE_RE = re.compile(r'data-ref="(\d+)"')

    def test_every_cited_number_has_a_reference_entry(self, tmp_path):
        _, html = render(tmp_path)
        defined = {int(n) for n in self.REF_RE.findall(html)}
        cited = {int(n) for n in self.CITE_RE.findall(html)}
        assert cited, "nothing in the report cites a source"
        assert cited <= defined, f"dangling citations: {sorted(cited - defined)}"

    def test_reference_numbers_are_contiguous_from_one(self, tmp_path):
        _, html = render(tmp_path)
        defined = sorted(int(n) for n in self.REF_RE.findall(html))
        assert defined == list(range(1, len(defined) + 1))

    def test_the_same_url_gets_one_number_however_often_it_is_cited(self, tmp_path):
        """Rebuild the registry from the rendered page and check it is a bijection."""
        _, html = render(
            tmp_path,
            items=[make_item(), make_item(title="另一条", source_url=make_item().source_url)],
        )
        # Two records, one distinct URL -> exactly one reference entry.
        assert len(self.REF_RE.findall(html)) == 1

    def test_findings_cite_the_same_registry_as_the_table(self, tmp_path):
        _, html = render(tmp_path)
        # make_analysis() cites the same URL the record does, so it must reuse
        # the number the evidence table assigned rather than a fresh one.
        assert len(self.REF_RE.findall(html)) == 1
        assert html.count('data-ref="1"') >= 2

    def test_a_finding_citing_a_dropped_document_still_resolves(self, tmp_path):
        """The pre-existing `evidence` list on a finding is arbitrary URLs."""
        analysis = make_analysis()
        analysis.findings[0].evidence = [
            "https://www.china5e.com/news/news-1209422-1.html",
            "https://orphan.example/never-in-the-table",
        ]
        out = tmp_path / "report.html"
        path = render_html(
            run_id="run-test-0001",
            user_query="q",
            query=make_query(),
            aggregation=make_aggregation([make_item()]),
            analysis=analysis,
            items=[make_item()],
            rejected=[],
            failed_sources=[],
            errors=[],
            summary={"executive_summary": "x", "method_note": ""},
            threshold=0.6,
            model_name="m",
            output_path=out,
        )
        html = path.read_text(encoding="utf-8")
        defined = {int(n) for n in self.REF_RE.findall(html)}
        cited = {int(n) for n in self.CITE_RE.findall(html)}
        assert path.suffix == ".html"
        assert cited <= defined, (
            f"a finding cited a URL with no reference: {sorted(cited - defined)}"
        )
        assert "orphan.example" in html


# ---------------------------------------------------------------------------
# Free-form inputs must not be able to kill the whole report
# ---------------------------------------------------------------------------
class TestFreeFormInputsCannotKillTheReport:
    """`Aggregation.by_subject` is `list[dict[str, Any]]` - the renderer cannot trust its keys.

    Regression this locks down: the template did `s.share * 100` and
    `"{:,.0f}".format(s.total_amount)`. One entry missing those keys raised
    `UndefinedError`, `render_html` caught it and wrote Markdown instead, and the
    run still reported success. The formatted report quietly disappeared.
    """

    def test_subject_entry_without_share_or_amount_still_renders_html(self, tmp_path):
        items = [make_item()]
        agg = make_aggregation(items)
        agg.by_subject = [{"subject": "某能源公司"}]  # no count, no share, no total_amount

        path, html = render(tmp_path, items=items, aggregation=agg)

        assert path.suffix == ".html", "a partially-populated subject row killed the HTML report"
        assert "某能源公司" in html

    def test_subject_entry_that_is_not_a_dict_is_skipped(self, tmp_path):
        items = [make_item()]
        agg = make_aggregation(items)
        agg.by_subject = ["某能源公司", None, {"subject": "另一家公司", "count": 1}]

        path, html = render(tmp_path, items=items, aggregation=agg)

        assert path.suffix == ".html"
        assert "另一家公司" in html

    def test_missing_share_is_recomputed_from_the_counts(self, tmp_path):
        items = [make_item(), make_item(title="b"), make_item(title="c")]
        agg = make_aggregation(items)
        agg.by_subject = [
            {"subject": "甲", "count": 2},
            {"subject": "乙", "count": 1},
        ]

        _, html = render(tmp_path, items=items, aggregation=agg)

        assert "66.7%" in html, "share was not derived from count / named total"
        assert "33.3%" in html

    def test_percentage_style_share_is_normalised(self, tmp_path):
        items = [make_item()]
        agg = make_aggregation(items)
        agg.by_subject = [{"subject": "甲", "count": 1, "share": 66.0}]

        _, html = render(tmp_path, items=items, aggregation=agg)

        assert "66.0%" in html
        assert "6600.0%" not in html

    def test_empty_subject_list_renders_the_empty_state(self, tmp_path):
        items = [make_item()]
        agg = make_aggregation(items)
        agg.by_subject = []

        path, html = render(tmp_path, items=items, aggregation=agg)
        assert path.suffix == ".html"
        assert "No named subjects were extracted." in html

    def test_share_denominator_is_stated_against_named_records(self, tmp_path):
        """The hint used to claim the denominator was every record, which is not what the code does."""
        _, html = render(tmp_path, failed_sources=None)
        assert re.search(r"record\(s\)\s+that named one", html), (
            "the denominator of the share column is no longer stated"
        )
        assert "relative to all" not in html

    def test_the_table_is_rendered_server_side_so_it_works_without_javascript(self, tmp_path):
        """The filter toolbar is an enhancement; the rows must exist in the markup."""
        _, html = render(tmp_path)
        body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        assert "某能源公司" in body or "总投资3.3亿元" in body


# ---------------------------------------------------------------------------
# A completely empty run must still render, and must not fabricate a figure
# ---------------------------------------------------------------------------
class TestEmptyRun:
    """No items, no regions, no months, no subjects, no sources.

    Two regressions this locks down, both found by *running* an adversarial
    empty run rather than by reading the code:

      * the findings hint referenced `nums.references` unconditionally, but
        `_sections` only emits that key when sources exist - so an empty run
        raised, `render_html` fell back to Markdown, and the caller still saw
        a "success" because a `.md` file appeared;
      * "Top-3 concentration" printed "0.0%" for an empty run, which is a
        fabricated figure in the one place this report promises never to print
        one. It must render as "-" (the `pct` filter's empty state).
    """

    def test_empty_run_renders_html_not_markdown(self, tmp_path):
        path, html = render(tmp_path, items=[], aggregation=Aggregation(), analysis=Analysis())
        assert path.suffix == ".html", "an empty run silently fell back to Markdown"
        assert '<section id="summary"' in html

    def test_empty_run_does_not_fabricate_a_concentration(self, tmp_path):
        _, html = render(tmp_path, items=[], aggregation=Aggregation(), analysis=Analysis())
        assert "Top-3 concentration" in html
        assert "0.0%" not in html, "empty run printed a fabricated top-3 share"


# ---------------------------------------------------------------------------
# Degradation that must survive a printed / offline copy
# ---------------------------------------------------------------------------
class TestDegradation:
    @pytest.mark.parametrize("marker", ["@media print", "mapFallback", '<svg class="trend"'])
    def test_offline_and_print_affordances_are_present(self, tmp_path, marker):
        _, html = render(tmp_path)
        assert marker in html

    def test_no_charting_or_mapping_library_is_required_for_the_numbers(self, tmp_path):
        """Both visuals are inlined SVG, so the data survives a dead CDN.

        The report used to load Chart.js for the trend and Leaflet for the map.
        Losing the network printed "the chart library could not be loaded" over
        an empty box - an honest sentence, but a useless document. Leaflet is
        now the only remaining script tag, and it only adds tiles on top of a
        map that is already drawn.
        """
        _, html = render(tmp_path)
        assert "chart.js" not in html.lower(), "trend chart still depends on a CDN"
        assert html.count("<script src=") <= 1, "more than one external script is loaded"
        assert "<canvas" not in html
        # Bars carry their own tooltips, so no library is needed to read a value.
        assert "<title>" in html.split('<svg class="trend"', 1)[1]

    def test_failed_source_is_disclosed_in_the_coverage_section(self, tmp_path):
        _, html = render(tmp_path, failed_sources=["gdelt"])
        assert "coverage" in section_ids(html)
        assert "gdelt" in html

    def test_the_map_is_drawn_without_any_network_access(self, tmp_path):
        """The choropleth is inlined SVG, so losing the tile server loses tiles, not the map."""
        _, html = render(tmp_path)
        assert '<svg class="choropleth"' in html
        assert html.count("<path d=") > 1, "no district geometry was inlined"

    def test_the_theme_is_applied_before_first_paint(self, tmp_path):
        """An external theme script would flash the wrong colours on every load."""
        _, html = render(tmp_path)
        head = html.split("</head>", 1)[0]
        assert "ia-theme" in head
        assert 'data-theme="auto"' in html

    def test_dark_mode_is_a_token_override_not_a_second_stylesheet(self, tmp_path):
        _, html = render(tmp_path)
        assert 'html[data-theme="dark"]' in html


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
