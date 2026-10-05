#!/usr/bin/env python3
"""
Source connectivity checker for the Sourced Intel.

This is step 0 of the pipeline: before writing ANY business logic we must verify
each public data source with a REAL request. Never assume API fields from memory.

Design rules (from the spec):
  - Every source is isolated in its own try/except; one failure never blocks others.
  - Results are printed to stdout only. No files are written.
  - Zero third-party dependencies, so this runs before the venv exists.

Usage:
    python3 scripts/check_sources.py
    python3 scripts/check_sources.py --only gdelt
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# GDELT enforces "one request every 5 seconds" and blocks shared egress IPs hard.
GDELT_MIN_INTERVAL = 6.0
GDELT_BACKOFF = (20, 40, 60)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

_last_gdelt_call = 0.0


def http_get(url: str, timeout: int = 20, extra_headers: dict | None = None) -> tuple[int, bytes]:
    """Minimal GET helper returning (status_code, body_bytes)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, identity",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                with contextlib.suppress(Exception):
                    body = gzip.decompress(body)
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}".encode()


def decode(body: bytes) -> str:
    for enc in ("utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# ---------------------------------------------------------------------------
# S1 - GDELT DOC 2.0 API (global news, no key required)
# ---------------------------------------------------------------------------
def check_gdelt(query: str = "energy") -> bool:
    section("S1  GDELT Doc API  (global news / no key)")
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    # NOTE: no `timespan` parameter here on purpose. `timespan=3months` makes GDELT
    # return HTTP 200 with an empty object, silently (docs/PITFALLS.md #1). The
    # pipeline therefore asks for the unfiltered window and applies the date filter
    # locally via parsing.cutoff_from_timespan.
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": "5",
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    print(f"URL: {url}")

    global _last_gdelt_call
    for attempt, backoff in enumerate((0, *GDELT_BACKOFF), start=1):
        wait = max(0.0, GDELT_MIN_INTERVAL - (time.time() - _last_gdelt_call), backoff)
        if wait:
            print(f"  waiting {wait:.0f}s before request (rate limit guard)...")
            time.sleep(wait)
        _last_gdelt_call = time.time()
        status, body = http_get(url, timeout=25)
        print(f"  attempt {attempt}: http={status} bytes={len(body)}")

        if status == 200:
            try:
                data = json.loads(decode(body))
            except json.JSONDecodeError as exc:
                print(f"  FAIL: JSON decode error -> {exc}")
                print(f"  body head: {decode(body)[:200]}")
                return False
            if not data:
                print("  WARN: 200 OK but EMPTY object {} - query matched nothing")
                return False
            articles = data.get("articles") or []
            print(f"  OK: top-level keys = {list(data.keys())}, articles = {len(articles)}")
            if articles:
                print(f"  ARTICLE FIELDS: {list(articles[0].keys())}")
                for art in articles[:3]:
                    print(
                        f"    - {art.get('seendate')} | {art.get('domain')} | "
                        f"{art.get('language')}/{art.get('sourcecountry')} | "
                        f"{(art.get('title') or '')[:70]}"
                    )
                print("  NOTE: artlist mode returns NO article body and NO summary.")
            return True

        if status == 429:
            print("  rate limited (429), will back off")
            continue

        print(f"  FAIL: body head = {decode(body)[:200]}")
        return False

    print("  FAIL: exhausted retries (rate limit)")
    return False


# ---------------------------------------------------------------------------
# S2 - DataV GeoAtlas (China administrative boundaries, no key required)
# ---------------------------------------------------------------------------
def check_datav(adcode: str = "440300") -> bool:
    section(f"S2  DataV GeoAtlas  (admin boundaries, adcode={adcode})")
    url = f"https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
    print(f"URL: {url}")
    status, body = http_get(url)
    print(f"  http={status} bytes={len(body)}")
    if status != 200:
        print(f"  FAIL: body head = {decode(body)[:200]}")
        return False
    try:
        data = json.loads(decode(body))
    except json.JSONDecodeError as exc:
        print(f"  FAIL: JSON decode error -> {exc}")
        return False
    feats = data.get("features") or []
    print(f"  OK: type={data.get('type')}  features={len(feats)}")
    for feat in feats[:4]:
        props = feat.get("properties", {})
        print(
            f"    - adcode={props.get('adcode')} name={props.get('name')} "
            f"level={props.get('level')} centroid={props.get('centroid')} "
            f"geom={feat.get('geometry', {}).get('type')}"
        )
    return True


# ---------------------------------------------------------------------------
# S3/S4 - static HTML list pages (industry / financial news portals)
# ---------------------------------------------------------------------------
CANDIDATE_PAGES = [
    ("bjx", "https://news.bjx.com.cn/"),
    ("china5e", "https://www.china5e.com/"),
    ("szgov_policy", "https://www.sz.gov.cn/cn/xxgk/zfxxgj/zcfg/index.html"),
]

_TAG_RE = __import__("re").compile(r"<[^>]+>")
_ANCHOR_RE = __import__("re").compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', __import__("re").S
)


def check_html_pages() -> dict[str, bool]:
    section("S3/S4  Static HTML list pages (candidate deep sources)")
    results: dict[str, bool] = {}
    for name, url in CANDIDATE_PAGES:
        print(f"\n-- {name}: {url}")
        try:
            status, body = http_get(url, timeout=20)
            print(f"   http={status} bytes={len(body)}")
            if status != 200 or len(body) < 2048:
                print("   FAIL: page too small or non-200 (likely JS-rendered SPA)")
                results[name] = False
                continue
            html = decode(body)
            anchors = _ANCHOR_RE.findall(html)
            usable = []
            seen = set()
            for href, raw in anchors:
                text = unescape(_TAG_RE.sub("", raw)).strip()
                if len(text) >= 10 and text not in seen:
                    seen.add(text)
                    usable.append((text, href))
            print(f"   anchors={len(anchors)}  usable_titles={len(usable)}")
            if not usable:
                print("   FAIL: no usable <a> titles found")
                results[name] = False
                continue
            for text, href in usable[:5]:
                print(f"     - {text[:64]}  ->  {href[:70]}")
            results[name] = True
        except Exception as exc:
            print(f"   FAIL: {type(exc).__name__}: {exc}")
            results[name] = False
    return results


# ---------------------------------------------------------------------------
# LLM - Zhipu GLM (OpenAI-compatible endpoint)
# ---------------------------------------------------------------------------
def check_glm(api_key: str | None) -> bool:
    section("LLM  Zhipu GLM (OpenAI-compatible)")
    base = "https://open.bigmodel.cn/api/paas/v4"
    print(f"URL: {base}/chat/completions")

    # Read the model, base URL and reasoning policy from the same config the
    # pipeline uses, so a retired model name (docs/PITFALLS.md #16) or a bad
    # thinking setting (#22) surfaces here rather than three nodes deep.
    model = "glm-4.7"
    thinking_mode = "off"
    try:
        sys.path.insert(
            0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        )
        from sourced_intel.config import get_settings
        from sourced_intel.llm_keys import key_problem

        settings = get_settings()
        model = settings.glm_model
        thinking_mode = settings.glm_thinking
        base = settings.glm_base_url
        print(f"model: {model}   thinking: {thinking_mode}")
        if api_key:
            problem = key_problem(api_key)
            if problem:
                print(f"  KEY FORMAT: {problem}")
    except Exception as exc:
        print(f"  (could not read project config, using {model}: {exc})")

    if not api_key:
        print("  SKIP: GLM_API_KEY not set (export it or put it in .env)")
        return False

    # --- what can this key actually reach? -----------------------------------
    # A model name that is not on this list returns a 429 code 1305 that reads
    # like a rate limit but means "no such model", which is a genuinely
    # misleading failure mode. Ask the server instead of assuming.
    try:
        req = urllib.request.Request(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            catalog = json.loads(decode(resp.read()))
        ids = [m.get("id") for m in catalog.get("data", [])]
        print(f"  models available to this key ({len(ids)}): {', '.join(ids)}")
        if ids and model not in ids:
            print(f"  !! GLM_MODEL={model} is NOT in that list - it will fail at runtime")
    except Exception as exc:
        print(f"  (could not list models: {type(exc).__name__}: {exc})")

    # --- chat probe ----------------------------------------------------------
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        # Reasoning models need headroom: a 16-token budget is entirely consumed
        # by the hidden thinking channel, and the caller sees an empty answer.
        "max_tokens": 300,
        "temperature": 0,
    }
    if thinking_mode == "off":
        body["thinking"] = {"type": "disabled"}
    elif thinking_mode == "low":
        body["reasoning_effort"] = "low"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            data = json.loads(decode(resp.read()))
    except urllib.error.HTTPError as exc:
        print(f"  FAIL: http={exc.code} body={decode(exc.read())[:300]}")
        return False
    except Exception as exc:
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return False
    elapsed = (time.time() - started) * 1000
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = (message.get("content") or "").strip()
    usage = data.get("usage", {}) or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if not content:
        print(
            f"  FAIL: answer was empty (finish_reason={choice.get('finish_reason')!r}, "
            f"reasoning_tokens={reasoning}). The thinking channel consumed the budget "
            "or the model name is wrong."
        )
        return False
    print(f"  OK: {elapsed:.0f}ms  reply={content!r}")
    print(f"  usage={usage}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="run one check: gdelt | datav | html | glm")
    parser.add_argument("--query", default="energy", help="GDELT probe query")
    args = parser.parse_args()

    import os

    key = os.environ.get("GLM_API_KEY")
    if not key:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("GLM_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        print("(loaded GLM_API_KEY from .env)")
                        break

    print("Sourced Intel - data source connectivity check")
    print(f"python {sys.version.split()[0]}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results: dict[str, bool] = {}
    only = args.only
    if only in (None, "gdelt"):
        results["S1 gdelt"] = check_gdelt(args.query)
    if only in (None, "datav"):
        results["S2 datav"] = check_datav()
    if only in (None, "html"):
        results.update(check_html_pages())
    if only in (None, "glm"):
        results["LLM glm"] = check_glm(key)

    section("SUMMARY")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(1 for v in results.values() if v)
    print(f"\n  {passed}/{len(results)} sources reachable")
    print("  Reminder: a failing source must degrade gracefully, never crash the pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
