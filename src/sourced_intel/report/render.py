"""Report renderer: Jinja2 -> single-file HTML, with a Markdown fallback.

GeoJSON boundaries are downloaded once per adcode and cached on disk, so a second
run needs no network for the map.

The HTML is deliberately *one* file: no build step, no bundler, no local asset
directory. Anything the page needs at read time is either inlined or fetched from
a CDN with a documented offline fallback (see the `*Fallback` blocks in the
template). That is what makes a report forwardable by email.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import __version__
from ..config import REGION_DISPLAY, get_settings
from ..schemas import Aggregation, Analysis, IntelItem, QuerySpec
from ..tools.http import get_http

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "report.html.j2"

GEODATA_URL = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def format_amount(value: float | None) -> str:
    """Human-readable CNY amount (Chinese unit convention).

    Used for the headline figure, where the unit word is spelled out: "1.86 亿元".
    """
    if not value:
        return "-"
    if value >= 1e8:
        return f"{value / 1e8:,.2f} 亿元"
    if value >= 1e4:
        return f"{value / 1e4:,.1f} 万元"
    return f"{value:,.0f} 元"


def format_amount_compact(value: float | None) -> str:
    """Same convention, but terse enough for a table cell.

    Kept separate from `format_amount` because the two have different jobs: a
    headline has room for a space and a word, a table cell does not, and a
    column of figures reads better when the precision is uniform. Rounding stops
    at 3 significant-ish figures - "7,300万" rather than "7,300.0万", which is
    the same number with a decimal that implies precision it does not have.
    """
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if not number or number < 0:
        return "-"
    if number >= 1e8:
        return f"{number / 1e8:,.2f}亿"
    if number >= 1e7:
        return f"{number / 1e4:,.0f}万"
    if number >= 1e4:
        return f"{number / 1e4:,.1f}万"
    return f"{number:,.0f}"


def _load_geojson(adcode: str) -> dict[str, Any]:
    """Fetch district boundaries for `adcode`, cached under data/cache/geo."""
    settings = get_settings()
    cache_file = settings.cache_dir / "geo" / f"{adcode}_full.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("geo cache unreadable, refetching: %s", cache_file)

    result = get_http().get(GEODATA_URL.format(adcode=adcode), retries=1)
    if not result.ok:
        logger.warning("geojson fetch failed for %s: %s", adcode, result.error)
        return {"type": "FeatureCollection", "features": []}
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        return {"type": "FeatureCollection", "features": []}
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def _decorate_geojson(geojson: dict, aggregation: Aggregation) -> dict:
    """Attach per-district counts/amounts so the map can shade by intensity."""
    by_adcode = {b.adcode: b for b in aggregation.by_region}
    counts = [b.count for b in aggregation.by_region] or [0]
    max_count = max(counts) or 1
    for feature in geojson.get("features", []):
        props = feature.setdefault("properties", {})
        adcode = str(props.get("adcode", ""))
        bucket = by_adcode.get(adcode)
        count = bucket.count if bucket else 0
        props["__count"] = count
        props["__amount"] = round(bucket.total_amount, 2) if bucket else 0
        props["__intensity"] = round(count / max_count, 4) if count else 0.0
    return geojson


def _map_points(geojson: dict, aggregation: Aggregation) -> list[dict]:
    """Circle markers at district centroids, capped for rendering speed."""
    settings = get_settings()
    by_adcode = {b.adcode: b for b in aggregation.by_region}
    points: list[dict] = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        adcode = str(props.get("adcode", ""))
        center = props.get("centroid") or props.get("center")
        if not center or len(center) != 2:
            continue
        bucket = by_adcode.get(adcode)
        count = bucket.count if bucket else 0
        if count == 0:
            continue
        points.append(
            {
                "name": props.get("name") or REGION_DISPLAY.get(adcode, adcode),
                "adcode": adcode,
                "lat": center[1],
                "lng": center[0],
                "count": count,
                "amount": round(bucket.total_amount, 2) if bucket else 0,
            }
        )
    points.sort(key=lambda p: -p["count"])
    return points[: settings.max_map_points]


def _nice_scale(max_value: float, ticks: int = 5) -> tuple[float, float]:
    """Round an axis top up to a readable number and return (top, step).

    An axis that ends at 7 with gridlines every 1.75 is technically correct and
    unreadable; one that ends at 8 with lines every 2 is not.
    """
    import math

    if max_value <= 0:
        return 1.0, 1.0 / max(1, ticks)
    raw = max_value / max(1, ticks)
    exponent = math.floor(math.log10(raw)) if raw > 0 else 0
    base = 10.0**exponent
    step = 10 * base
    for multiplier in (1, 2, 2.5, 5, 10):
        if multiplier * base >= raw:
            step = multiplier * base
            break
    top = step * max(1, math.ceil(max_value / step))
    return top, step


def _svg_trend(aggregation: Aggregation) -> str:
    """A static bar+line chart, inlined so the trend survives with no CDN.

    The text-only fallback this replaces was honest but useless: "the chart
    library could not be loaded" is a poor way to learn that July had five
    records. Rendering the chart server-side means it also *prints*, which a
    canvas never does well, and it is the same trick the map uses.

    Colours come from CSS classes so the SVG follows the theme; see the
    `svg.trend` block in the template.
    """
    from html import escape

    months = aggregation.by_month
    if not months:
        return ""

    width, height = 900.0, 320.0
    pad_left, pad_right, pad_top, pad_bottom = 46.0, 58.0, 30.0, 44.0
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    counts = [b.count for b in months]
    # The amount axis is in 亿元, not 万元. A right-hand axis labelled up to
    # 400,000 is technically correct and impossible to read; the same series in
    # 亿元 runs 0-40 and matches the unit used in the headline figure.
    amounts = [b.total_amount / 1e8 for b in months]
    top_counts, step_counts = _nice_scale(max(counts))
    top_amounts, step_amounts = _nice_scale(max(amounts))

    def x_at(index: int) -> float:
        if len(months) == 1:
            return pad_left + plot_w / 2
        return pad_left + (index + 0.5) * (plot_w / len(months))

    def y_at(value: float) -> float:
        return pad_top + plot_h - (value / top_counts) * plot_h

    def y1_at(value: float) -> float:
        return pad_top + plot_h - (value / top_amounts) * plot_h

    parts: list[str] = []

    # horizontal gridlines, shared by both axes (they are drawn to the same box)
    value = 0.0
    while value <= top_counts + 1e-9:
        y = y_at(value)
        parts.append(
            f'<line class="grid" x1="{pad_left:.1f}" y1="{y:.1f}" '
            f'x2="{pad_left + plot_w:.1f}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{pad_left - 8:.1f}" y="{y + 3.5:.1f}" '
            f'text-anchor="end">{value:,.0f}</text>'
        )
        value += step_counts

    # right-hand axis for the amount series
    value = 0.0
    while value <= top_amounts + 1e-9:
        y = y1_at(value)
        parts.append(
            f'<text class="tick" x="{pad_left + plot_w + 8:.1f}" y="{y + 3.5:.1f}" '
            f'text-anchor="start">{value:,.0f}</text>'
        )
        value += step_amounts

    series = [(x_at(i), y1_at(a)) for i, a in enumerate(amounts)] if any(amounts) else []

    # The area goes down first, with the bars on top of it. Painting it last
    # tints the upper half of every bar and makes the two series look like one
    # blended shape instead of two overlaid ones.
    if len(series) > 1:
        base = pad_top + plot_h
        d = (
            f"M {series[0][0]:.1f},{base:.1f} "
            + " L ".join(f"{x:.1f},{y:.1f}" for x, y in series)
            + f" L {series[-1][0]:.1f},{base:.1f} Z"
        )
        parts.append(f'<path class="area" d="{d}"/>')

    slot = plot_w / max(1, len(months))
    bar_w = max(6.0, min(46.0, slot * 0.46))
    for index, bucket in enumerate(months):
        x = x_at(index)
        y = y_at(bucket.count)
        parts.append(
            f'<rect class="bar" x="{x - bar_w / 2:.1f}" y="{y:.1f}" '
            f'width="{bar_w:.1f}" height="{max(0.0, pad_top + plot_h - y):.1f}" rx="3"/>'
            f"<title>{escape(bucket.month)}: {bucket.count} record(s), "
            f"{bucket.total_amount / 1e8:,.2f} 亿元</title>"
        )
        if bucket.count:
            parts.append(
                f'<text class="val" x="{x:.1f}" y="{y - 6:.1f}" '
                f'text-anchor="middle">{bucket.count}</text>'
            )
        parts.append(
            f'<text class="tick" x="{x:.1f}" y="{pad_top + plot_h + 18:.1f}" '
            f'text-anchor="middle">{escape(bucket.month)}</text>'
        )

    if series:
        parts.append(
            f'<polyline class="line" points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in series)}"/>'
        )
        for x, y in series:
            parts.append(f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3.4"/>')

    parts.append(f'<text class="axistitle" x="{pad_left:.1f}" y="{pad_top - 8:.1f}">RECORDS</text>')
    parts.append(
        f'<text class="axistitle" x="{pad_left + plot_w:.1f}" y="{pad_top - 8:.1f}" '
        f'text-anchor="end">AMOUNT (\u4ebf\u5143)</text>'
    )

    aria = escape(
        "Monthly record counts and disclosed amounts: "
        + "; ".join(f"{b.month} {b.count}" for b in months)
    )
    return (
        f'<svg class="trend" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="{aria}">'
        f"{''.join(parts)}</svg>"
    )


# ---------------------------------------------------------------------------
# Static choropleth
# ---------------------------------------------------------------------------
# The interactive map needs Leaflet from a CDN and tiles from AutoNavi. That is
# two network dependencies standing between a reader and the one visual in the
# report that shows *where* things happened. So the geometry is projected into an
# inline SVG as well: it is drawn by the browser with no script and no request,
# and the Leaflet layer replaces it only when it actually loads.
#
# The fill uses `currentColor` with an opacity ramp rather than baked-in hex, so
# the SVG follows the light/dark theme automatically instead of needing a second
# copy of every colour.
_MAP_W, _MAP_H, _MAP_PAD = 960, 560, 26


def _mercator(lon: float, lat: float) -> tuple[float, float]:
    """Web-Mercator, so the static map has the same shape as the tile map."""
    import math

    clamped = max(-85.0, min(85.0, lat))
    return (
        math.radians(lon),
        math.log(math.tan(math.pi / 4 + math.radians(clamped) / 2)),
    )


def _round_coords(node: Any, ndigits: int = 4) -> Any:
    """Round every coordinate in a nested GeoJSON ring list."""
    if isinstance(node, (int, float)):
        return round(float(node), ndigits)
    if isinstance(node, list):
        return [_round_coords(item, ndigits) for item in node]
    return node


def _rings(geometry: dict) -> list[list[list[float]]]:
    """Every outer ring of a Polygon/MultiPolygon, ignoring holes.

    Holes are dropped deliberately: at district scale the only holes in this
    dataset are enclaves a few hundred metres across, and drawing them would
    double the path data for a visual difference nobody can see.
    """
    kind = (geometry or {}).get("type")
    coords = (geometry or {}).get("coordinates") or []
    if kind == "Polygon":
        return [coords[0]] if coords else []
    if kind == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def _svg_map(geojson: dict, aggregation: Aggregation) -> str:
    """Project the boundaries into an inline SVG choropleth."""
    from html import escape

    features = geojson.get("features") or []
    if not features:
        return ""

    projected: list[tuple[dict, list[list[tuple[float, float]]]]] = []
    xs: list[float] = []
    ys: list[float] = []
    for feature in features:
        rings: list[list[tuple[float, float]]] = []
        for ring in _rings(feature.get("geometry") or {}):
            if len(ring) < 3:
                continue
            pts: list[tuple[float, float]] = []
            for pair in ring:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                mx, my = _mercator(float(pair[0]), float(pair[1]))
                pts.append((mx, my))
                xs.append(mx)
                ys.append(my)
            if len(pts) >= 3:
                rings.append(pts)
        if rings:
            projected.append((feature, rings))

    if not xs or not ys:
        return ""

    span_x = max(xs) - min(xs) or 1e-9
    span_y = max(ys) - min(ys) or 1e-9
    scale = min((_MAP_W - 2 * _MAP_PAD) / span_x, (_MAP_H - 2 * _MAP_PAD) / span_y)
    offset_x = (_MAP_W - span_x * scale) / 2 - min(xs) * scale
    # Mercator y grows northwards; SVG y grows downwards, hence the flip.
    offset_y = (_MAP_H - span_y * scale) / 2 + max(ys) * scale

    def project(mx: float, my: float) -> tuple[float, float]:
        return (mx * scale + offset_x, offset_y - my * scale)

    paths: list[str] = []
    for feature, rings in projected:
        props = feature.get("properties") or {}
        intensity = float(props.get("__intensity") or 0.0)
        count = int(props.get("__count") or 0)
        name = str(props.get("name") or props.get("adcode") or "")
        amount = float(props.get("__amount") or 0.0)
        d = "".join(
            "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in (project(*p) for p in ring)) + "Z"
            for ring in rings
        )
        tip = escape(f"{name}: {count} record(s)" + (f", {amount:,.0f} CNY" if amount else ""))
        # 0.06 keeps a zero-count district visible as an outline instead of a hole.
        # The ramp is wide because opacity on a pale background compresses
        # differences: the number on the label, not the shade, is the datum.
        opacity = round(0.06 + 0.62 * intensity, 3)
        paths.append(
            f'<path d="{d}" fill="currentColor" fill-opacity="{opacity}" '
            f'fill-rule="evenodd"><title>{tip}</title></path>'
        )

    # District labels, greedily de-cluttered: a label that would sit on top of one
    # already placed is dropped rather than overprinted.
    #
    # These carry the count, so there is deliberately no second marker layer. The
    # SVG used to also draw a sized circle at each centroid, which is where the
    # label already sits: two encodings of one datum on one point, and the
    # label's halo erased the circle anyway. Shading gives magnitude, the printed
    # number gives the value, and the interactive layer draws its own markers
    # because it has no labels.
    placed: list[tuple[float, float, float, float]] = []
    labels: list[str] = []
    for feature, _ in sorted(
        projected, key=lambda pair: -(pair[0].get("properties") or {}).get("__count", 0)
    ):
        props = feature.get("properties") or {}
        count = int(props.get("__count") or 0)
        if count <= 0:
            continue
        name = str(props.get("name") or "")
        if not name:
            continue
        centre = props.get("centroid") or props.get("center")
        if not centre or len(centre) != 2:
            continue
        x, y = project(*_mercator(float(centre[0]), float(centre[1])))
        w = len(name) * 12.5 + 26
        box = (x - w / 2, y - 11, x + w / 2, y + 11)
        if any(
            box[0] < other[2] and box[2] > other[0] and box[1] < other[3] and box[3] > other[1]
            for other in placed
        ):
            continue
        placed.append(box)
        labels.append(
            f'<text x="{x:.1f}" y="{y:.1f}" class="lbl">{escape(name)}'
            f'<tspan class="lbl-n" dx="4">{count}</tspan></text>'
        )

    aria = escape(
        "District boundaries of the query region, shaded by record count; "
        f"{len(paths)} area(s) drawn, {len(labels)} labelled."
    )
    return (
        f'<svg class="choropleth" viewBox="0 0 {_MAP_W} {_MAP_H}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" aria-label="{aria}">'
        f'<g class="shapes">{"".join(paths)}</g>'
        f'<g class="labels">{"".join(labels)}</g>'
        f"</svg>"
    )


def _as_int(value: Any) -> int:
    """Coerce a free-form count to an int, never raising."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _subject_rows(aggregation: Aggregation) -> list[dict[str, Any]]:
    """Guarantee the shape the template prints.

    `Aggregation.by_subject` is typed `list[dict[str, Any]]` - free-form by
    design - so a missing `share`/`count`/`total_amount` key must degrade to a
    sensible value instead of raising. The cost of raising here is unusually
    high: `render_html` catches everything and writes the Markdown report
    instead, so one missing key silently turns "the formatted HTML report" into
    "a plain Markdown file" and nobody notices until a reviewer opens it.

    Rows are returned in descending subject count order, which is the order
    `aggregate` already produces; sorting here as well would be a second place
    to keep the ranking rule in sync.
    """
    raw_rows = [row for row in (aggregation.by_subject or []) if isinstance(row, dict)]
    # The denominator is the *true* number of records that named a subject, not
    # the sum over this truncated list. `by_subject` is capped at the top 15, so
    # the two differ whenever a window produced more than 15 distinct subjects -
    # and the report prints the denominator verbatim next to every share.
    denominator = _as_int(aggregation.with_subject) or sum(
        _as_int(row.get("count")) for row in raw_rows
    )

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        subject = str(raw.get("subject") or "").strip()
        if not subject:
            continue

        count = _as_int(raw.get("count"))

        try:
            share = float(raw.get("share"))
        except (TypeError, ValueError):
            share = 0.0
        # A model-provided share can arrive as a percentage (66.0) or be absent
        # entirely; both cases are recomputed from counts rather than trusted.
        if share <= 0 and denominator:
            share = count / denominator
        elif share > 1:
            share = share / 100

        try:
            amount = float(raw.get("total_amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0

        rows.append({"subject": subject, "count": count, "share": share, "total_amount": amount})
    rows.sort(key=lambda row: -row["count"])
    return rows


def _subject_denominator(aggregation: Aggregation, rows: list[dict[str, Any]]) -> int:
    """The number the share column is stated against."""
    return _as_int(aggregation.with_subject) or sum(row["count"] for row in rows)


def _display_date(item: IntelItem) -> str:
    """Show the date at the precision the source actually stated.

    A source that only gave a month must not be rendered as the 1st of that
    month - that invents a day nobody published. `event_time_raw` carries the
    original wording; `event_time` (month resolved to its 1st) exists only so
    monthly aggregation has a sortable key.
    """
    if item.event_time_raw:
        return item.event_time_raw
    if item.event_time:
        return item.event_time.strftime("%Y-%m-%d")
    return "-"


def host_of(url: str) -> str:
    """Bare hostname for a source link, so the table stays readable."""
    try:
        netloc = urlsplit(str(url)).netloc or str(url)
    except ValueError:
        netloc = str(url)
    return netloc[4:] if netloc.startswith("www.") else netloc


def _build_sources(
    items: list[IntelItem],
    analysis: Analysis,
    rejected: list[IntelItem],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Number every distinct source exactly once for the whole document.

    Before this existed, the evidence table numbered *rows* and each finding
    restarted its own `[1] [2] [3]` list. Two different documents could carry the
    same number on the same page, which is exactly the ambiguity a citation is
    supposed to remove. Now one URL = one number, shared by the findings, the
    evidence table and the reference list.

    Registration order is deliberate: documents cited in the evidence table come
    first, so numbering reads 1..N down the table, and anything a finding cites
    that did not survive the quality gate is appended afterwards.
    """
    registry: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def register(url: str | None, title: str = "", source: str = "") -> int | None:
        if not url:
            return None
        if url in index:
            known = registry[index[url] - 1]
            # First writer wins, but fill in blanks a later caller can supply.
            if not known["title"] and title:
                known["title"] = title
            if not known["source"] and source:
                known["source"] = source
            return index[url]
        entry = {
            "n": len(registry) + 1,
            "url": url,
            "host": host_of(url),
            "title": title or "",
            "source": source or "",
        }
        registry.append(entry)
        index[url] = entry["n"]
        return entry["n"]

    for item in items:
        register(item.source_url, item.title, item.source)
    for finding in analysis.findings:
        for url in finding.evidence:
            register(url)
    for item in rejected:
        register(item.source_url, item.title, item.source)

    return registry, index


def _counters(items: list[IntelItem], raw_docs: int | None) -> dict[str, Any]:
    """A handful of provenance figures for the colophon.

    Kept separate from `Aggregation` on purpose: these describe the *run*
    (how much was fetched versus how much survived), not the findings, and the
    aggregation node never sees the raw document count.
    """
    by_source: dict[str, int] = {}
    for item in items:
        key = item.source or host_of(item.source_url) or "unknown"
        by_source[key] = by_source.get(key, 0) + 1
    return {
        "raw_docs": raw_docs,
        "documents": len({i.source_url for i in items}),
        "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])),
        "conflicts": sum(1 for i in items if i.amount_conflict),
        "regex_amounts": sum(1 for i in items if i.amount_source == "regex"),
    }


def _sections(
    *, has_appendix: bool, has_coverage: bool, has_sources: bool = False
) -> tuple[dict[str, int], list[dict]]:
    """Assign section numbers once, so the body and the table of contents agree."""
    ordered = [
        ("summary", "Executive summary"),
        ("figures", "Key figures"),
        ("geo", "Geographic distribution"),
        ("trend", "Monthly trend"),
        ("findings", "Findings"),
        ("subjects", "Subject activity"),
        ("evidence", "Evidence table"),
    ]
    if has_sources:
        ordered.append(("references", "References"))
    if has_appendix:
        ordered.append(("appendix", "Low-confidence appendix"))
    if has_coverage:
        ordered.append(("coverage", "Coverage & failures"))

    nums = {key: idx for idx, (key, _) in enumerate(ordered, start=1)}
    toc = [{"num": nums[key], "id": key, "title": title} for key, title in ordered]
    return nums, toc


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _jinja_env():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    def pct(value: float) -> str:
        """Format a 0-1 fraction as a percentage with a sane fallback."""
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return "-"

    def amount(value: Any) -> str:
        """Compact CNY figure for tight table cells."""
        return format_amount_compact(value)

    env.filters["host"] = host_of
    env.filters["pct"] = pct
    env.filters["amount"] = amount
    env.globals["display_date"] = _display_date
    env.globals["host_of"] = host_of
    return env


def render_html(
    *,
    run_id: str,
    user_query: str,
    query: QuerySpec,
    aggregation: Aggregation,
    analysis: Analysis,
    items: list[IntelItem],
    rejected: list[IntelItem],
    failed_sources: list[str],
    errors: list[dict],
    summary: dict,
    threshold: float,
    model_name: str,
    output_path: Path,
    raw_docs: int | None = None,
    elapsed_seconds: float | None = None,
    default_theme: str = "auto",
    is_sample: bool = False,
) -> Path:
    geojson = _decorate_geojson(_load_geojson(query.region_adcode), aggregation)
    map_points = _map_points(geojson, aggregation)
    subject_rows = _subject_rows(aggregation)
    # The analysis node computes this, but if that node failed the report would
    # print "0.0%" - a fabricated figure, in the one place the whole project
    # promises never to fabricate one. Fall back to computing it here.
    concentration = analysis.subject_concentration or {}
    subject_named_total = _subject_denominator(aggregation, subject_rows)
    top3 = concentration.get("top3_share", 0.0) or 0.0
    if not top3 and subject_named_total:
        top3 = (
            sum(sorted((r["count"] for r in subject_rows), reverse=True)[:3]) / subject_named_total
        )
    # No record named a subject: a concentration share is meaningless, and
    # printing "0.0%" would fabricate a figure in the one place this report
    # promises never to. `None` renders as "-" via the `pct` filter.
    if not subject_named_total:
        top3 = None
    sources, cite = _build_sources(items, analysis, rejected)
    nums, toc = _sections(
        has_appendix=bool(rejected),
        has_coverage=bool(failed_sources or errors),
        has_sources=bool(sources),
    )

    # Two consumers of the same geometry: an inline SVG that needs no network,
    # and a Leaflet layer that does. Both get coordinates rounded to ~10 m.
    payload_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                **feature,
                "geometry": {
                    **(feature.get("geometry") or {}),
                    "coordinates": _round_coords(
                        (feature.get("geometry") or {}).get("coordinates")
                    ),
                },
            }
            for feature in geojson.get("features", [])
        ],
    }

    context = {
        "lang": "en",
        "kicker": "Regional Industry Intelligence Report",
        "title": f"{query.industry} · {query.region_name} · last {query.timespan}",
        "run_id": run_id,
        "user_query": user_query,
        "query": query,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "summary": summary,
        "aggregation": aggregation,
        "analysis": analysis,
        "items": items,
        "rejected": rejected,
        "failed_sources": failed_sources,
        "errors": errors,
        "threshold": threshold,
        "model_name": model_name,
        "pipeline_version": __version__,
        "nums": nums,
        "toc": toc,
        "metrics": {"amount_display": format_amount(aggregation.total_amount)},
        # The template formats this with the `pct` filter; formatting it here as
        # well would be a second place to keep in sync.
        "concentration": {"top3_share": top3},
        "subject_rows": subject_rows,
        # Shares are computed against the records that actually named a subject,
        # so the report states that denominator explicitly instead of implying it
        # is the full record count.
        "subject_named_total": subject_named_total,
        "sources": sources,
        "cite": cite,
        "colophon": _counters(items, raw_docs),
        "elapsed_seconds": elapsed_seconds,
        # A sample report has to say so, in the document itself and not only in
        # the README: a screenshot of it will travel further than the README.
        "is_sample": is_sample,
        "default_theme": default_theme if default_theme in ("auto", "light", "dark") else "auto",
        "svg_map": _svg_map(geojson, aggregation),
        "has_map": bool(geojson.get("features") or map_points),
        "svg_trend": _svg_trend(aggregation),
        "geojson": json.dumps(payload_geojson, ensure_ascii=False, separators=(",", ":")),
        "map_points": json.dumps(map_points, ensure_ascii=False),
        # Client-side filter index: the table is rendered server-side (so the page
        # works with JavaScript off), and this only powers the search box on top
        # of it. Keeping it out of the DOM is what makes filtering O(rows)
        # without re-parsing markup.
        "row_index": json.dumps(
            [
                {
                    "n": i + 1,
                    "type": item.event_type,
                    "cite": cite.get(item.source_url, 0),
                    "amount": item.amount or 0.0,
                }
                for i, item in enumerate(items)
            ],
            ensure_ascii=False,
        ),
    }

    try:
        template = _jinja_env().get_template(TEMPLATE_NAME)
        html = template.render(**context)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")
        return output_path
    except Exception as exc:
        logger.error("HTML rendering failed (%s), falling back to Markdown", exc)
        return render_markdown(
            query=query,
            aggregation=aggregation,
            analysis=analysis,
            items=items,
            summary=summary,
            failed_sources=failed_sources,
            run_id=run_id,
            output_path=output_path.with_suffix(".md"),
            user_query=user_query,
        )


def render_markdown(
    *,
    query: QuerySpec,
    aggregation: Aggregation,
    analysis: Analysis,
    items: list[IntelItem],
    summary: dict,
    failed_sources: list[str],
    run_id: str,
    output_path: Path,
    user_query: str,
) -> Path:
    """Degraded output that still carries every conclusion and every source link."""
    lines: list[str] = [
        f"# {query.industry} · {query.region_name} · last {query.timespan}",
        "",
        f"> Query: {user_query}",
        f"> Run: `{run_id}`",
        "",
        "## Executive summary",
        "",
        summary.get("executive_summary", ""),
        "",
        f"_{summary.get('method_note', '')}_",
        "",
        "## Key figures",
        "",
        f"- Structured records: **{aggregation.total_items}**",
        f"- Records with a stated amount: **{aggregation.with_amount}**",
        f"- Total disclosed amount: **{format_amount(aggregation.total_amount)}**",
        f"- Regions covered: **{len(aggregation.by_region)}**",
        "",
        "## Findings",
        "",
    ]
    if analysis.findings:
        for finding in analysis.findings:
            lines.append(f"### {finding.headline}")
            lines.append("")
            if finding.detail:
                lines.append(finding.detail)
                lines.append("")
            for url in finding.evidence:
                lines.append(f"- source: {url}")
            lines.append("")
    else:
        lines += ["_No findings produced._", ""]

    lines += [
        "## Evidence table",
        "",
        "| # | Date | Title | Subject | Amount | Source |",
        "|---|---|---|---|---|---|",
    ]
    for idx, item in enumerate(items, start=1):
        date = _display_date(item)
        lines.append(
            f"| {idx} | {date} | {item.title} | {item.subject or '-'} | "
            f"{item.amount_raw or '-'} | [link]({item.source_url}) |"
        )

    # The HTML report draws these two as an inlined SVG. Markdown cannot, so it
    # carries the same numbers as tables - without them the degraded output would
    # silently drop the geographic and temporal shape of the window.
    if aggregation.by_region:
        lines += [
            "",
            "## Regional distribution",
            "",
            "| Area | Code | Records | Amount |",
            "|---|---|---|---|",
        ]
        for row in aggregation.by_region:
            lines.append(
                f"| {row.region_name} | {row.adcode or '-'} | {row.count} | "
                f"{format_amount(row.total_amount)} |"
            )

    if aggregation.by_month:
        lines += ["", "## Monthly trend", "", "| Month | Records | Amount |", "|---|---|---|"]
        for month in aggregation.by_month:
            lines.append(f"| {month.month} | {month.count} | {format_amount(month.total_amount)} |")

    # Same numbering as the HTML build: one URL, one number, wherever it appears.
    registry, _index = _build_sources(items, analysis, rejected=[])
    if registry:
        lines += ["", "## References", ""]
        for entry in registry:
            label = entry["title"] or entry["host"]
            lines.append(f"{entry['n']}. [{label}]({entry['url']}) — {entry['host']}")

    if failed_sources:
        lines += ["", "## Failed sources", ""] + [f"- `{s}`" for s in failed_sources]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
