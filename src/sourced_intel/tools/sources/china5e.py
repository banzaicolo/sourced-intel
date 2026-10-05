"""china5e.com (China Energy Network) adapter - the deep source.

Unlike GDELT's artlist mode, this site gives us a publish timestamp, a source
attribution and the FULL article body, which is what makes amount/entity
extraction possible. The markup shapes were verified against live pages on
2026-09-18 and are documented in `tools/htmlparse.py` - change the regex there,
not here.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from ...config import get_settings
from ...parsing import (
    cutoff_from_timespan,
    is_noise_title,
    normalise_url,
    parse_datetime,
)
from ...schemas import QuerySpec, RawDoc
from ..htmlparse import parse_article, parse_list_items
from ..http import get_http
from .base import SourceAdapter

logger = logging.getLogger(__name__)

BASE = "https://www.china5e.com"

# (keyword variants, section path, canonical label)
# Covers both Chinese and English queries because the interview is in English
# while the source itself is Chinese.
SECTION_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("储能", "energy storage", "storage", "battery", "ess", "锂电", "钠电", "液流"),
        "/new-energy/stored-energy/",
        "储能",
    ),
    (("光伏", "solar", "pv", "photovoltaic"), "/new-energy/solar-power/", "光伏"),
    (("风电", "wind power", "wind energy"), "/new-energy/wind-energy/", "风电"),
    (("氢能", "hydrogen"), "/new-energy/others/", "氢能"),
    (("生物质", "biomass"), "/new-energy/biomass-energy/", "生物质能"),
    (("新能源汽车", "electric vehicle"), "/new-energy/clean-energy-vehicles/", "清洁能源汽车"),
    (("新能源", "new energy", "renewable"), "/new-energy/", "新能源"),
    (("电网", "power grid"), "/power/power-gird/", "电网"),
    (("核电", "nuclear"), "/power/nuclear-power/", "核电"),
    (("水电", "hydropower"), "/power/hydro-power/", "水电"),
    (("电力", "power", "electricity"), "/power/", "电力"),
    (("煤炭", "coal"), "/coal/", "煤炭"),
    (("石油", "oil"), "/oil-gas/oil/", "石油"),
    (("天然气", "lng"), "/oil-gas/gas/", "天然气"),
    (("油气", "oil and gas"), "/oil-gas/", "油气"),
    (("碳中和", "carbon neutral"), "/carbon-neutrality/", "碳中和"),
    (("节能", "energy conservation"), "/energy-conservation/", "节能环保"),
    (("分布式", "distributed energy"), "/distributed-energy/", "分布式能源"),
    (("能源战略", "energy strategy", "policy"), "/energy-economy/energy-strategy/", "能源战略"),
    (("能源经济", "energy economy"), "/energy-economy/", "政策与经济"),
    (("项目", "project", "investment"), "/project/", "项目"),
)

DEFAULT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("/new-energy/stored-energy/", "储能"),
    ("/energy-economy/energy-strategy/", "能源战略"),
    ("/project/", "项目"),
)


class China5eAdapter(SourceAdapter):
    name = "china5e"
    description = "China Energy Network (china5e.com) - industry news with full article body"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.http = get_http()

    # -- section selection -------------------------------------------------
    @staticmethod
    def resolve_sections(query: QuerySpec) -> list[tuple[str, str]]:
        """Map the query's industry keywords onto site sections."""
        haystack = " ".join([query.industry, *query.keywords]).lower()
        chosen: list[tuple[str, str]] = []
        for keywords, path, label in SECTION_RULES:
            if any(kw.lower() in haystack for kw in keywords) and path not in {
                p for p, _ in chosen
            }:
                chosen.append((path, label))
        if not chosen:
            chosen = list(DEFAULT_SECTIONS)
        return chosen

    # -- list pages --------------------------------------------------------
    def _list_page(self, section: str, page: int) -> tuple[list[dict], str | None]:
        path = section if page <= 1 else f"{section}index_{page}.html"
        url = urljoin(BASE, path)
        result = self.http.get(url)
        if not result.ok:
            return [], f"{section} page {page}: {result.error}"

        rows: list[dict] = []
        skipped_noise = 0
        for row in parse_list_items(result.text, BASE):
            # Live feeds interleave advertisements with real articles. Dropping them
            # here saves an article fetch AND stops a small model from turning a
            # "厂家推荐" row into a record - see parsing.is_noise_title.
            if is_noise_title(row.title):
                skipped_noise += 1
                continue
            rows.append(
                {
                    "title": row.title,
                    "url": normalise_url(row.url),
                    "published_at": parse_datetime(row.date_text),
                }
            )
        if skipped_noise:
            logger.info("china5e: dropped %d promotional row(s) from %s", skipped_noise, section)
        return rows, None

    # -- article pages -----------------------------------------------------
    def _article(self, url: str) -> dict:
        """Fetch and parse one article. Never raises - returns empty fields instead."""
        result = self.http.get(url, retries=0, min_interval=0.0)
        if not result.ok:
            return {"title": "", "body": "", "source_name": None, "published_at": None}
        article = parse_article(result.text)
        return {
            "title": article.title,
            "body": article.body,
            "source_name": article.source_name,
            "published_at": parse_datetime(article.date_text),
        }

    # -- public API --------------------------------------------------------
    def fetch(self, query: QuerySpec, limit: int = 50) -> list[RawDoc]:
        sections = self.resolve_sections(query)
        max_pages = 2 if limit > 20 else 1
        cutoff = cutoff_from_timespan(query.timespan)

        # 1) collect list-page rows
        candidates: list[dict] = []
        seen_urls: set[str] = set()
        list_errors: list[str] = []
        for section, label in sections:
            for page in range(1, max_pages + 1):
                rows, error = self._list_page(section, page)
                if error:
                    list_errors.append(error)
                    continue
                for row in rows:
                    if row["url"] in seen_urls:
                        continue
                    if cutoff and row["published_at"] and row["published_at"] < cutoff:
                        continue
                    row["section"] = label
                    seen_urls.add(row["url"])
                    candidates.append(row)
                if len(candidates) >= limit * 2:
                    break
            if len(candidates) >= limit * 2:
                break

        if list_errors and not candidates:
            raise RuntimeError("; ".join(list_errors[:3]))

        # 2) prefer rows whose title matches the industry terms; fall back to the
        #    newest rows so the pipeline still has data to work with.
        matched = [row for row in candidates if self._matches_query(row["title"], query)]
        pool = (matched if len(matched) >= 5 else candidates)[:limit]

        # 3) fetch article bodies concurrently (bounded)
        workers = max(1, min(self.settings.extract_max_concurrency * 2, 8))
        articles: list[dict] = []
        if pool:
            with ThreadPoolExecutor(max_workers=workers) as pool_exec:
                articles = list(pool_exec.map(lambda row: self._article(row["url"]), pool))

        docs: list[RawDoc] = []
        for row, art in zip(pool, articles, strict=True):
            docs.append(
                RawDoc(
                    source=self.name,
                    url=row["url"],
                    title=art["title"] or row["title"],
                    published_at=art["published_at"] or row["published_at"],
                    raw_text=art["body"],
                    domain="china5e.com",
                    language="Chinese",
                )
            )
        logger.info(
            "china5e: %d candidates -> %d docs (%d with body)",
            len(candidates),
            len(docs),
            sum(1 for d in docs if d.has_body),
        )
        return docs

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _matches_query(title: str, query: QuerySpec) -> bool:
        """A Chinese title matches when any known Chinese variant of the industry
        appears in it; English terms are matched case-insensitively."""
        lowered = title.lower()
        return any(term and term.lower() in lowered for term in query.search_terms)
