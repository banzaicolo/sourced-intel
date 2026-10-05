"""Unit tests for the dependency-free parsing layer.

These run with the bare standard library (`python3 -m unittest`) and also under
pytest. They are deliberately fixture-driven: the HTML fixtures are verbatim
excerpts of live pages, so a markup change on the source shows up here first.

Run:
    python3 -m unittest discover -s tests -p "test_parsing.py" -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.llm_keys import (  # noqa: E402
    is_bare_key_id,
    key_problem,
    looks_like_zhipu_key,
)
from sourced_intel.parsing import (  # noqa: E402
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
from sourced_intel.tools.htmlparse import (  # noqa: E402
    clean_text,
    parse_article,
    parse_list_items,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TestParseAmount(unittest.TestCase):
    def test_yi_yuan(self):
        value, raw = parse_amount("总投资3.3亿元")
        self.assertEqual(value, 3.3e8)
        self.assertEqual(raw, "3.3亿元")

    def test_wan_yuan(self):
        value, _ = parse_amount("投资约 1200万元")
        self.assertEqual(value, 1200e4)

    def test_decimal_comma(self):
        value, _ = parse_amount("1,250万元")
        self.assertEqual(value, 1250e4)

    def test_english_units(self):
        value, _ = parse_amount("a 120 million yuan project")
        self.assertEqual(value, 120e6)

    def test_largest_match_wins(self):
        value, raw = parse_amount("总投资3亿元，其中一期1亿元")
        self.assertEqual(value, 3e8)
        self.assertEqual(raw, "3亿元")

    def test_missing_amount_returns_none(self):
        self.assertEqual(parse_amount("no figures here"), (None, None))
        self.assertEqual(parse_amount(""), (None, None))
        self.assertEqual(parse_amount(None), (None, None))

    def test_bare_number_is_not_an_amount(self):
        # A count such as "50兆瓦" must not be mistaken for money.
        self.assertEqual(parse_amount("装机50兆瓦")[0], None)


class TestParseDatetime(unittest.TestCase):
    def test_gdelt_compact_stamp(self):
        parsed = parse_datetime("20260820T233000Z")
        self.assertEqual(parsed, datetime(2026, 8, 20, 23, 30, 0, tzinfo=timezone.utc))

    def test_iso_with_time(self):
        self.assertEqual(parse_datetime("2026-08-25 16:07:37").day, 25)

    def test_iso_date_only(self):
        self.assertEqual(parse_datetime("2026-09-18").month, 9)

    def test_chinese_date(self):
        parsed = parse_datetime("2026年8月18日")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 8, 18))

    def test_slashes(self):
        self.assertEqual(parse_datetime("2026/8/1").month, 8)

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_datetime("最近"))
        self.assertIsNone(parse_datetime(""))
        self.assertIsNone(parse_datetime(None))

    def test_impossible_date_returns_none(self):
        self.assertIsNone(parse_datetime("2026-02-30"))

    def test_month_precision_keeps_the_month_without_inventing_a_day(self):
        # A source that says "今年1月已并网" has NOT stated a day. Live testing showed
        # the model upgrading that to 2026-01-01, which fabricates a fact. The prompt
        # now requires "2026-01", and this is the parser half of that contract.
        for text in ("2026-01", "2026-1", "2026年1月", "2026/1"):
            parsed = parse_datetime(text)
            self.assertIsNotNone(parsed, text)
            self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 1, 1), text)

    def test_month_rule_does_not_swallow_a_full_date(self):
        parsed = parse_datetime("2026-09-16")
        self.assertEqual((parsed.month, parsed.day), (9, 16))

    def test_relative_month_cannot_be_resolved_without_context(self):
        # "今年1月" needs the publication date to become a year; there is no such
        # context here, so the honest answer is None rather than a guess.
        self.assertIsNone(parse_datetime("今年1月"))
        self.assertIsNone(parse_datetime("1月"))


class TestTimespan(unittest.TestCase):
    def test_units(self):
        self.assertEqual(timespan_to_days("10d"), 10)
        self.assertEqual(timespan_to_days("2w"), 14)
        self.assertEqual(timespan_to_days("3m"), 90)

    def test_invalid_word_form_is_rejected(self):
        # This is the exact form that made GDELT return an empty result set.
        self.assertIsNone(timespan_to_days("3months"))

    def test_cutoff_is_in_the_past(self):
        cutoff = cutoff_from_timespan("3m")
        self.assertLess(cutoff, datetime.now(timezone.utc))


class TestExtractJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_block(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_fence_without_language(self):
        self.assertEqual(extract_json('```\n{"a": 1}\n```'), {"a": 1})

    def test_json_embedded_in_prose(self):
        text = 'Sure! Here is the result:\n{"items": []}\nHope that helps.'
        self.assertEqual(extract_json(text), {"items": []})

    def test_top_level_array(self):
        self.assertEqual(extract_json("[1, 2, 3]"), [1, 2, 3])

    def test_empty_response_raises(self):
        with self.assertRaises(JsonExtractionError):
            extract_json("")

    def test_no_json_raises(self):
        with self.assertRaises(JsonExtractionError):
            extract_json("I cannot help with that.")


class TestNormaliseUrl(unittest.TestCase):
    def test_strips_fragment(self):
        self.assertEqual(normalise_url("https://x.com/a#part"), "https://x.com/a")

    def test_strips_tracking_params(self):
        self.assertEqual(
            normalise_url("https://x.com/a?id=5&utm_source=nl"), "https://x.com/a?id=5"
        )

    def test_drops_query_when_only_tracking(self):
        self.assertEqual(normalise_url("https://x.com/a?utm_source=nl"), "https://x.com/a")


class TestCleanText(unittest.TestCase):
    def test_strips_scripts_and_styles(self):
        html = "<div>keep<script>var x=1;</script><style>.a{}</style></div>"
        self.assertEqual(clean_text(html), "keep")

    def test_converts_entities(self):
        self.assertEqual(clean_text("<p>a&amp;b</p>"), "a&b")

    def test_block_tags_become_newlines(self):
        self.assertEqual(clean_text("<p>one</p><p>two</p>"), "one\ntwo")

    def test_empty_input(self):
        self.assertEqual(clean_text(""), "")


class TestChina5eListFixture(unittest.TestCase):
    """Parses the real section-page markup captured on 2026-09-18."""

    @classmethod
    def setUpClass(cls):
        cls.html = (FIXTURES / "china5e_list.html").read_text(encoding="utf-8")

    def test_extracts_rows(self):
        rows = parse_list_items(self.html, "https://www.china5e.com")
        self.assertGreaterEqual(len(rows), 5, "the fixture holds 6 list rows")

    def test_row_fields(self):
        row = parse_list_items(self.html, "https://www.china5e.com")[0]
        self.assertTrue(row.title)
        self.assertTrue(row.url.startswith("https://www.china5e.com/news/news-"))
        self.assertRegex(row.date_text, r"^\d{4}-\d{2}-\d{2}$")

    def test_dates_parse(self):
        for row in parse_list_items(self.html, "https://www.china5e.com"):
            self.assertIsNotNone(parse_datetime(row.date_text), row.date_text)

    def test_amount_survives_in_a_headline(self):
        # Real headline: "总投资3.3亿元!墨玉储能项目即将投入商业运营"
        titles = [r.title for r in parse_list_items(self.html, "https://www.china5e.com")]
        with_amount = [t for t in titles if parse_amount(t)[0]]
        self.assertTrue(with_amount, f"expected an amount in one of: {titles}")


class TestChina5eArticleFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (FIXTURES / "china5e_article.html").read_text(encoding="utf-8")

    def test_title(self):
        article = parse_article(self.html)
        self.assertIn("远光软件", article.title)

    def test_publish_time_and_source(self):
        article = parse_article(self.html)
        self.assertIsNotNone(parse_datetime(article.date_text))
        self.assertTrue(article.source_name)

    def test_body_is_substantial(self):
        article = parse_article(self.html)
        self.assertGreater(len(article.body), 300)
        self.assertIn("远光软件", article.body)

    def test_empty_html_is_safe(self):
        article = parse_article("")
        self.assertEqual((article.title, article.body), ("", ""))


class TestNoiseFilter(unittest.TestCase):
    """Real promotional rows observed on a live china5e section page (2026-09-18)."""

    def test_promotional_row_is_noise(self):
        self.assertTrue(
            is_noise_title(
                "全行业超细粉体加工｜气流粉碎机整线解决方案厂家推荐，从试样、定制到出口项目综合测评"
            )
        )

    def test_real_headline_is_kept(self):
        for title in (
            "全球首个熔盐耦合二氧化碳发电项目在华能开工",
            "储能项目数量首超光伏 中国绿色能源出海迈入“系统输出”新阶段",
            "总投资3.3亿元!墨玉储能项目即将投入商业运营",
        ):
            with self.subTest(title=title):
                self.assertFalse(is_noise_title(title))

    def test_announcements_are_noise(self):
        self.assertTrue(is_noise_title("关于举办招采智能工具与高质量数据专题研讨会的通知"))

    def test_pipe_delimited_marketing_is_noise(self):
        self.assertTrue(
            is_noise_title("甲｜乙｜丙｜丁｜戊｜己｜庚｜辛｜壬｜癸｜甲｜乙｜丙｜丁｜戊｜己")
        )

    def test_short_and_empty_are_noise(self):
        self.assertTrue(is_noise_title(""))
        self.assertTrue(is_noise_title("储能"))


class TestReconcileAmount(unittest.TestCase):
    """The model transcribes; a regex scan verifies. Provenance must stay visible."""

    def test_model_value_wins_when_it_agrees(self):
        check = reconcile_amount("12.5亿元", body="项目总投资约12.5亿元，规划装机50兆瓦。")
        self.assertEqual(check.source, "llm")
        self.assertEqual(check.value, 1.25e9)
        self.assertFalse(check.conflict)

    def test_regex_fills_a_missed_amount(self):
        check = reconcile_amount(None, title="总投资3.3亿元!墨玉储能项目即将投入商业运营")
        self.assertEqual(check.source, "regex")
        self.assertEqual(check.value, 3.3e8)
        self.assertEqual(check.raw, "3.3亿元")
        self.assertFalse(check.conflict)

    def test_disagreement_is_flagged_not_silently_resolved(self):
        check = reconcile_amount("1亿元", body="项目总投资约12.5亿元。")
        self.assertEqual(check.source, "llm")
        self.assertEqual(check.value, 1e8)
        self.assertTrue(check.conflict)
        self.assertEqual(check.code_raw, "12.5亿元")

    def test_small_relative_difference_is_not_a_conflict(self):
        check = reconcile_amount("12.50亿元", body="总投资12.5亿元。")
        self.assertFalse(check.conflict)

    def test_nothing_found_stays_none(self):
        check = reconcile_amount(None, title="储能项目数量首超光伏", body="行业观察，无具体金额。")
        self.assertIsNone(check.value)
        self.assertEqual(check.source, "none")
        self.assertFalse(bool(check))

    def test_unparseable_model_amount_falls_through_to_regex(self):
        check = reconcile_amount("金额未披露", body="项目总投资约8.2亿元。")
        self.assertEqual(check.source, "regex")
        self.assertEqual(check.value, 8.2e8)


class TestApiKeyShape(unittest.TestCase):
    """A truncated paste must be caught locally, not by a confusing remote 401.

    The check deliberately rejects only the ONE confirmed failure mode (a bare
    32-hex key id). Any other shape is passed through to the API, because a false
    "your key is malformed" on a valid credential is worse than no check.
    """

    def test_documented_shape_passes(self):
        key = "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp"
        self.assertTrue(looks_like_zhipu_key(key))
        self.assertIsNone(key_problem(key))

    def test_bare_hex_id_is_rejected_with_a_specific_message(self):
        key = "0123456789abcdef0123456789abcdef"  # a bare key id: 32 hex chars, no dot
        self.assertTrue(is_bare_key_id(key))
        message = key_problem(key)
        self.assertIn("key *id*", message)
        self.assertIn("open.bigmodel.cn", message)

    def test_unknown_shapes_are_allowed_through(self):
        for key in (
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "some-other-token-shape-without-a-dot",
            "0123456789abcdef0123456789abcdef.short",
        ):
            with self.subTest(key=key):
                self.assertIsNone(key_problem(key), "must not block an unrecognised shape")

    def test_empty_key_is_reported(self):
        self.assertIn("empty", key_problem(""))

    def test_internal_whitespace_is_reported(self):
        # Surrounding whitespace is stripped silently; an INTERNAL break is what
        # actually breaks the credential, and it is reported.
        self.assertIn("line break or space", key_problem("abc def.ghijklmnop"))
        self.assertIsNone(key_problem("  0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp  "))


if __name__ == "__main__":
    unittest.main(verbosity=2)
