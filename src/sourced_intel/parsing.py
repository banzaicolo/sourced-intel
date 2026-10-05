"""Pure text-parsing helpers - standard library only.

Kept free of third-party imports on purpose: this module is the part of the
pipeline that can be (and is) unit-tested without a virtualenv.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------
_AMOUNT_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>亿元|亿|千万元|万元|万|million|billion|bn|mn|yuan)",
    re.IGNORECASE,
)

_UNIT_FACTOR: dict[str, float] = {
    "亿元": 1e8,
    "亿": 1e8,
    "千万元": 1e7,
    "万元": 1e4,
    "万": 1e4,
    "million": 1e6,
    "mn": 1e6,
    "billion": 1e9,
    "bn": 1e9,
    "yuan": 1.0,
}


def parse_amount(text: str | None) -> tuple[float | None, str | None]:
    """Convert '12.5亿元' -> (1_250_000_000.0, '12.5亿元').

    The largest match wins. Returns (None, None) when nothing matches - a missing
    amount must stay missing rather than being guessed.
    """
    if not text:
        return None, None
    best: tuple[float, str] | None = None
    for match in _AMOUNT_RE.finditer(text):
        try:
            num = float(match.group("num").replace(",", ""))
        except ValueError:
            continue
        unit = match.group("unit")
        factor = _UNIT_FACTOR.get(unit) or _UNIT_FACTOR.get(unit.lower())
        if factor is None:
            continue
        # Binary floats make 8.2 * 1e8 come out as 819999999.9999999. Amounts are
        # money: round to the cent so downstream sums and comparisons are stable.
        value = round(num * factor, 2)
        if best is None or value > best[0]:
            best = (value, match.group(0).strip())
    return best if best else (None, None)


# ---------------------------------------------------------------------------
# Datetimes
# ---------------------------------------------------------------------------
_GDELT_TS = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")
# A year+month with NO day, e.g. "2026-01" or "2026年1月". Real sources write
# "今年1月已并网试运行" and a model that turns that into 2026-01-01 has invented a
# day that nobody stated. Anchored, so it never swallows a complete date.
_YEAR_MONTH_RE = re.compile(r"^(\d{4})[-/年](\d{1,2})月?$")
_DATE_PATTERNS = (
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})"),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"),
)


def parse_datetime(value: str | None) -> datetime | None:
    """Best-effort parser for the formats our sources actually emit.

    Handles GDELT's compact `20260820T233000Z` stamp plus the usual Chinese and
    ISO date spellings. Returns None rather than raising on anything else.
    """
    if not value:
        return None
    value = value.strip()

    match = _GDELT_TS.match(value)
    if match:
        y, mo, d, h, mi, s = (int(g) for g in match.groups())
        try:
            return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
        except ValueError:
            return None

    # Month-level precision. Kept because monthly trend aggregation is a headline
    # feature, but the day is set to the 1st and the caller keeps the raw string,
    # so nothing downstream can mistake it for a stated day.
    match = _YEAR_MONTH_RE.match(value)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), 1, tzinfo=timezone.utc)
        except ValueError:
            return None

    for pattern in _DATE_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        parts = [int(g) for g in match.groups()]
        while len(parts) < 5:
            parts.append(0)
        try:
            return datetime(parts[0], parts[1], parts[2], parts[3], parts[4], tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Timespan
# ---------------------------------------------------------------------------
_TIMESPAN_RE = re.compile(r"^(\d+)\s*([dwm])$", re.IGNORECASE)


def timespan_to_days(timespan: str | None) -> int | None:
    """'3m' -> 90, '2w' -> 14, '10d' -> 10. None when unparseable.

    Note: GDELT's own `timespan` parameter silently returned empty results for
    '3months', so the window is always applied locally through this helper.
    """
    if not timespan:
        return None
    match = _TIMESPAN_RE.match(timespan.strip())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    return {"d": value, "w": value * 7, "m": value * 30}[unit]


def cutoff_from_timespan(timespan: str | None) -> datetime | None:
    from datetime import timedelta

    days = timespan_to_days(timespan)
    if days is None:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


# ---------------------------------------------------------------------------
# LLM JSON extraction
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class JsonExtractionError(ValueError):
    """Raised when no JSON value can be recovered from a model response."""


def extract_json(text: str) -> object:
    """Recover a JSON value from a model response.

    Handles plain JSON, ```json fenced blocks, and JSON embedded in prose.
    """
    if not text or not text.strip():
        raise JsonExtractionError("empty response")

    candidate = _FENCE_RE.sub("", text.strip())
    for attempt in (candidate, text):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise JsonExtractionError(f"no JSON found in response: {text[:200]!r}")


# ---------------------------------------------------------------------------
# URL hygiene
# ---------------------------------------------------------------------------
_TRACKING_PARAMS = ("utm_", "gclid", "fbclid", "spm", "from=")


def normalise_url(url: str) -> str:
    """Strip fragments and common tracking parameters so duplicates collapse."""
    if not url:
        return ""
    url = url.split("#", 1)[0].strip()
    if "?" in url:
        base, _, query = url.partition("?")
        kept = [
            part
            for part in query.split("&")
            if part and not any(part.lower().startswith(p) for p in _TRACKING_PARAMS)
        ]
        url = base + ("?" + "&".join(kept) if kept else "")
    return url


# ---------------------------------------------------------------------------
# Feed noise filtering
# ---------------------------------------------------------------------------
# VERIFIED 2026-09-18: a real china5e section page returned 30 rows, of which some
# were advertisements sitting between real articles, e.g.
#   "全行业超细粉体加工｜气流粉碎机整线解决方案厂家推荐，从试样、定制到出口项目综合测评"
# A small model will happily turn a row like that into a "record". Dropping them at
# the feed stage removes the problem before it costs an article fetch or an LLM call.
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"厂家推荐|厂家直销|供应商推荐|解决方案厂家|整线解决方案"),
    re.compile(r"诚招|招商加盟|代理招募|火热报名"),
    re.compile(r"专题研讨|研讨会|培训班|研修班|征稿|问卷调查"),
    re.compile(r"招聘|诚聘|招贤"),
    re.compile(r"^(广告|推广)[:：]"),
)

# A second structural signal: marketing rows are built as pipe-separated keyword
# lists. Two or more separators is a list, not a headline. Only the keyword rules
# above were validated against real rows - this one is a guard, not a headline
# detector, so it deliberately stays strict.
_NOISE_PIPE_RE = re.compile(r"[｜|].*[｜|]")


def is_noise_title(title: str) -> bool:
    """True when a feed row is an advertisement or an announcement, not news."""
    text = (title or "").strip()
    if len(text) < 6:
        return True
    if _NOISE_PIPE_RE.search(text):
        return True
    return any(pattern.search(text) for pattern in _NOISE_PATTERNS)


# ---------------------------------------------------------------------------
# Amount reconciliation (deterministic verifier)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AmountCheck:
    """Outcome of cross-checking the model's transcription against a regex scan.

    `source` records where the surviving value came from, so the report can show
    provenance instead of pretending every number came from the same place.
    """

    value: float | None = None
    raw: str | None = None
    source: str = "none"  # "llm" | "regex" | "none"
    conflict: bool = False
    code_raw: str | None = None

    def __bool__(self) -> bool:
        return self.value is not None


def reconcile_amount(
    llm_raw: str | None,
    title: str = "",
    body: str = "",
    tolerance: float = 0.01,
) -> AmountCheck:
    """Prefer the model's literal transcription, but never lose a number it missed.

    - The model transcribed an amount  -> keep it; flag `conflict` when an
      independent regex scan of the same text disagrees by more than `tolerance`.
    - The model returned nothing but the regex found one -> use the regex value and
      label it `regex`. This is a verbatim substring of the source text, not an
      inference, so it is safe to publish as long as the provenance is visible.
    - Neither found anything -> no amount. A missing amount stays missing; it is
      never guessed.

    Caveat: `parse_amount` returns the LARGEST match, which is the right guess for
    headline amounts ("总投资12.5亿元") and the wrong guess for a table of figures.
    That is exactly why `conflict` exists - a disagreeing pair goes to human review.
    """
    llm_value, llm_normalised = parse_amount(llm_raw)
    code_value, code_raw = parse_amount(f"{title} {body}".strip())

    if llm_value is not None:
        conflict = False
        if code_value is not None and llm_value > 0:
            conflict = abs(code_value - llm_value) / llm_value > tolerance
        return AmountCheck(
            value=llm_value,
            raw=llm_normalised or llm_raw,
            source="llm",
            conflict=conflict,
            code_raw=code_raw,
        )

    if code_value is not None:
        return AmountCheck(
            value=code_value, raw=code_raw, source="regex", conflict=False, code_raw=code_raw
        )

    return AmountCheck()
