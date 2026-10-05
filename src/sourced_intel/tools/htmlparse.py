"""HTML parsing helpers - standard library only.

Every regex here was written against a live page and is documented with the exact
markup it expects. Keeping this module dependency-free means the parsing rules can
be regression-tested without installing BeautifulSoup or lxml.

Verified shapes (china5e.com, 2026-09-18):

  list page
    <li class="singleline"><span>2026-09-18</span>
      <a href="https://www.china5e.com/news/news-1209445-1.html"
         title="储能项目数量首超光伏..." target="_blank">储能项目数量首超光伏...</a></li>

  article page
    <h1>TITLE</h1>
    <div class="showtitinfo">2026-08-25 16:07:37 ... 来源 远光软件</div>
    <div id="showcontent" class="showcontent">BODY</div>
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin

# --- china5e list pages ----------------------------------------------------
LIST_ITEM_RE = re.compile(
    r'<li class="singleline">\s*<span>([\d\-/]+)</span>\s*'
    r'<a\s+href="([^"]+)"\s+title="([^"]*)"',
    re.S,
)

# --- china5e article pages -------------------------------------------------
ARTICLE_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
ARTICLE_META_RE = re.compile(r'<div class="showtitinfo">(.*?)</div>', re.S)
ARTICLE_BODY_RE = re.compile(
    r'<div id="showcontent"[^>]*>(.*?)</div>\s*(?:<div class="showpages|<!--)', re.S
)
ARTICLE_BODY_FALLBACK_RE = re.compile(r'<div id="showcontent"[^>]*>(.*)', re.S)
# The literal "来源：" label is rare on this site; the label usually sits inside an
# HTML comment and only the attribution text survives tag stripping, so both shapes
# are supported.
SOURCE_RE = re.compile(r"来源[：:]\s*([^\s<]{2,30})")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")
VIEWS_RE = re.compile(r"(?:浏览|点击)(?:次数)?[：:].*$", re.S)
NOISE_CHARS = " \u3000\u00a0·|、-—"

# --- generic ---------------------------------------------------------------
ANCHOR_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TAG_RE = re.compile(r"<[^>]+>")
BLOCK_END_RE = re.compile(r"</(p|div|section|li|h[1-6]|tr)>", re.I)
BR_RE = re.compile(r"<br\s*/?>", re.I)
WS_RE = re.compile(r"[ \t\r\f\v\u00a0]+")
BLANK_RE = re.compile(r"\n{3,}")


@dataclass
class ListRow:
    title: str
    url: str
    date_text: str = ""


@dataclass
class Article:
    title: str = ""
    body: str = ""
    source_name: str | None = None
    date_text: str = ""
    extra: dict = field(default_factory=dict)


def clean_text(html_fragment: str) -> str:
    """Strip tags/entities, keep paragraph breaks, collapse runs of whitespace."""
    if not html_fragment:
        return ""
    text = SCRIPT_STYLE_RE.sub(" ", html_fragment)
    # Comments must go before generic tag stripping: a comment can contain a '>'
    # (e.g. `<!-- ... <span id="hits"></span>-->`), which would otherwise leave a
    # stray "-->" in the extracted text.
    text = COMMENT_RE.sub(" ", text)
    text = BLOCK_END_RE.sub("\n", text)
    text = BR_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    text = unescape(text)
    text = WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return BLANK_RE.sub("\n\n", text).strip()


def parse_list_items(html: str, base_url: str = "") -> list[ListRow]:
    """Extract (date, url, title) rows from a china5e section page."""
    rows: list[ListRow] = []
    for date_text, href, title in LIST_ITEM_RE.findall(html or ""):
        title = unescape(title).strip()
        if len(title) < 6:
            continue
        rows.append(
            ListRow(
                title=title,
                url=urljoin(base_url, href) if base_url else href,
                date_text=date_text.replace("/", "-"),
            )
        )
    return rows


def extract_source_name(meta_text: str) -> str | None:
    """Pull the source attribution out of a `showtitinfo` block.

    Observed shapes:
      "2026-08-25 16:07:37 ... 来源 远光软件"   (label inside a comment)
      "2026-08-25 16:07:37 来源：远光软件"        (plain label)
    """
    if not meta_text:
        return None
    found = SOURCE_RE.search(meta_text)
    if found:
        return found.group(1).strip()

    text = VIEWS_RE.sub("", TIMESTAMP_RE.sub("", meta_text))
    for line in text.split("\n"):
        candidate = line.strip(NOISE_CHARS)
        if 2 <= len(candidate) <= 30:
            return candidate
    return None


def parse_article(html: str) -> Article:
    """Extract title / publish info / body from a china5e article page."""
    article = Article()
    if not html:
        return article

    match = ARTICLE_H1_RE.search(html)
    if match:
        article.title = clean_text(match.group(1))

    match = ARTICLE_META_RE.search(html)
    if match:
        meta = clean_text(match.group(1))
        article.date_text = meta
        article.source_name = extract_source_name(meta)

    match = ARTICLE_BODY_RE.search(html) or ARTICLE_BODY_FALLBACK_RE.search(html)
    if match:
        article.body = clean_text(match.group(1))
    return article


def extract_anchors(html: str, min_title_len: int = 10) -> list[tuple[str, str]]:
    """Generic (title, href) extraction - used by scripts/check_sources.py."""
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for href, raw in ANCHOR_RE.findall(html or ""):
        text = clean_text(raw)
        if len(text) >= min_title_len and text not in seen:
            seen.add(text)
            results.append((text, href))
    return results
