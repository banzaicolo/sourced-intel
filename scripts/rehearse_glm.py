#!/usr/bin/env python3
"""End-to-end rehearsal of the live path, using ONLY the standard library.

Why this exists: the pipeline's runtime dependencies (pydantic, langgraph,
jinja2, streamlit) live in the virtualenv, but the parts that actually decide
whether the *data* is any good - fetch, parse, prompt, LLM call, amount
cross-check - need none of them. This script wires those real components
together against the real network and the real Zhipu endpoint, so the chain can
be proven before anything is installed.

It deliberately reuses the project's own modules rather than reimplementing them:

    sourced_intel.tools.htmlparse   list/article parsing
    sourced_intel.parsing           amounts, dates, noise filtering, URLs
    sourced_intel.prompts_loader    the versioned prompt files
    sourced_intel.config            model name, thinking policy, key

Usage:
    python3 scripts/rehearse_glm.py                    # 3 documents, live GLM
    python3 scripts/rehearse_glm.py --docs 6
    python3 scripts/rehearse_glm.py --provider ollama  # zero-cost local run
    python3 scripts/rehearse_glm.py --json             # machine-readable result
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.config import get_settings
from sourced_intel.llm_keys import masked_key
from sourced_intel.parsing import (
    cutoff_from_timespan,
    is_noise_title,
    normalise_url,
    parse_datetime,
    reconcile_amount,
)
from sourced_intel.prompts_loader import load_prompt
from sourced_intel.tools.htmlparse import parse_article, parse_list_items

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# The deep source this project settled on, plus the section that carries the
# industry we demo with. Kept here (not in a source adapter) so this script stays
# importable without pydantic.
LIST_URL = "https://www.china5e.com/new-energy/stored-energy/"
SITE_BASE = "https://www.china5e.com"

SSL_CTX = ssl.create_default_context()
BODY_CHARS = 1500


# ---------------------------------------------------------------------------
# tiny HTTP helper
# ---------------------------------------------------------------------------
def fetch(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        raw = resp.read()
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# stage 1 - retrieve (real network)
# ---------------------------------------------------------------------------
def collect_documents(limit: int, timespan: str) -> tuple[list[dict], dict]:
    stats = {"listed": 0, "outside_window": 0, "noise": 0, "body_failed": 0}
    html = fetch(LIST_URL)
    rows = parse_list_items(html, SITE_BASE)
    stats["listed"] = len(rows)

    cutoff = cutoff_from_timespan(timespan)
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        title = (row.title or "").strip()
        url = normalise_url(row.url)
        if not title or not url or url in seen:
            continue
        if is_noise_title(title):
            stats["noise"] += 1
            continue
        published = parse_datetime(row.date_text)
        if cutoff is not None and published is not None and published < cutoff:
            # Both sides are timezone-aware; comparing naive to aware is the
            # classic TypeError this pipeline hit in rehearsal round one.
            stats["outside_window"] += 1
            continue
        seen.add(url)
        candidates.append({"title": title, "url": url, "published_at": published})
        if len(candidates) >= limit:
            break

    documents: list[dict] = []
    for doc in candidates:
        try:
            article = parse_article(fetch(doc["url"]))
        except Exception as exc:
            stats["body_failed"] += 1
            doc["body_error"] = f"{type(exc).__name__}: {exc}"
            documents.append(doc)
            continue
        doc["body"] = article.body
        doc["source_name"] = article.source_name
        doc["date_text"] = article.date_text
        if not doc.get("published_at"):
            doc["published_at"] = parse_datetime(article.date_text)
        documents.append(doc)
    return documents, stats


def collect_by_urls(urls: list[str]) -> tuple[list[dict], dict]:
    """Fetch an explicit list of article URLs.

    Used to reproduce the worked examples embedded in `prompts/03_extract.md`:
    section listings reorder between requests, so pinning URLs is the only way to
    replay those examples byte for byte.
    """
    stats = {"listed": len(urls), "outside_window": 0, "noise": 0, "body_failed": 0}
    documents: list[dict] = []
    for url in urls:
        doc: dict = {"title": "", "url": normalise_url(url), "published_at": None}
        try:
            article = parse_article(fetch(doc["url"]))
        except Exception as exc:
            stats["body_failed"] += 1
            doc["body_error"] = f"{type(exc).__name__}: {exc}"
            documents.append(doc)
            continue
        doc["title"] = article.title
        doc["body"] = article.body
        doc["source_name"] = article.source_name
        doc["date_text"] = article.date_text
        doc["published_at"] = parse_datetime(article.date_text)
        documents.append(doc)
    return documents, stats


def render_documents(documents: list[dict]) -> str:
    blocks: list[str] = []
    for index, doc in enumerate(documents, start=1):
        body = (doc.get("body") or "").strip()
        published = doc.get("published_at")
        blocks.append(
            "\n".join(
                [
                    f"[doc_id={index}]",
                    f"title: {doc['title']}",
                    "published_at: " + (published.strftime("%Y-%m-%d") if published else "unknown"),
                    f"url: {doc['url']}",
                    "body: "
                    + (body[:BODY_CHARS] if body else "(headline only - no body available)"),
                ]
            )
        )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# stage 2 - the model call (real endpoint)
# ---------------------------------------------------------------------------
def _thinking_fields(policy: str) -> dict:
    if policy == "off":
        return {"thinking": {"type": "disabled"}}
    if policy == "low":
        return {"reasoning_effort": "low"}
    return {}


def call_glm(settings, prompt_text: str, system: str, max_tokens: int) -> tuple[dict, float]:
    body = {
        "model": settings.glm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    body.update(_thinking_fields(settings.glm_thinking))

    def post(payload: dict) -> tuple[dict, float]:
        started = time.time()
        req = urllib.request.Request(
            f"{settings.glm_base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.glm_api_key}",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8")), time.time() - started

    try:
        return post(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        # GLM error 1210: this model always thinks and rejects thinking=disabled.
        # The message tells you to "use low, high or max", which is the
        # reasoning_effort parameter, NOT thinking.type. Downgrade once and retry.
        if "1210" in detail and settings.glm_thinking == "off":
            print("  (model always thinks - retrying with reasoning_effort=low, 3x token budget)")
            body.pop("thinking", None)
            body["reasoning_effort"] = "low"
            body["max_tokens"] = max_tokens * 3
            return post(body)
        raise SystemExit(f"GLM call failed: HTTP {exc.code} {detail[:400]}") from None


def call_ollama(settings, prompt_text: str, system: str, max_tokens: int) -> tuple[dict, float]:
    payload = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt_text},
        ],
        "stream": False,
        # Without think=false the reasoning channel swallows the whole budget and
        # `content` comes back empty. Verified 2026-09-18.
        "think": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": max_tokens},
    }
    started = time.time()
    req = urllib.request.Request(
        f"{settings.ollama_base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    elapsed = time.time() - started
    return (
        {
            "choices": [{"message": {"content": data.get("message", {}).get("content", "")}}],
            "model": data.get("model"),
            "usage": {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
                "total_tokens": (data.get("prompt_eval_count", 0) + data.get("eval_count", 0)),
            },
        },
        elapsed,
    )


# ---------------------------------------------------------------------------
# stage 3 - validate the model's answer against the sources
# ---------------------------------------------------------------------------
def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"no JSON in model output: {text[:200]!r}")


def validate(payload: dict, documents: list[dict]) -> tuple[list[dict], list[str]]:
    """Cross-check every amount the model transcribed against the source text.

    This is the pipeline's central anti-hallucination rule: the model supplies
    values, the code verifies them, and disagreement is surfaced rather than
    silently resolved.
    """
    notes: list[str] = []
    accepted: list[dict] = []
    for raw in payload.get("items") or []:
        doc_id = raw.get("doc_id")
        if not isinstance(doc_id, int) or not (1 <= doc_id <= len(documents)):
            notes.append(f"dropped a record with unusable doc_id={doc_id!r}")
            continue
        doc = documents[doc_id - 1]
        check = reconcile_amount(
            raw.get("amount_raw"), title=doc["title"], body=doc.get("body") or ""
        )

        record = dict(raw)
        record["source_url"] = doc["url"]  # never taken from the model
        record["amount_source"] = check.source
        record["amount_value"] = check.value
        record["amount_conflict"] = check.conflict
        if check.conflict:
            notes.append(
                f"doc {doc_id}: model said {raw.get('amount_raw')!r}, source text says "
                f"{check.raw!r} - flagged for review, not silently replaced"
            )
        if check.source == "regex":
            notes.append(
                f"doc {doc_id}: model missed the amount, code recovered {check.raw!r} from the source"
            )
        accepted.append(record)
    return accepted, notes


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", type=int, default=3, help="how many articles to pull")
    parser.add_argument(
        "--urls",
        nargs="+",
        help=(
            "fetch these exact article URLs instead of the section listing - use this "
            "to reproduce the examples embedded in prompts/03_extract.md"
        ),
    )
    parser.add_argument("--timespan", default="3m", help="recency window, e.g. 3m / 7d")
    parser.add_argument("--provider", default=None, choices=["glm", "ollama"])
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args()

    settings = get_settings()
    provider = args.provider or settings.llm_provider
    prompt = load_prompt("03_extract")

    if not args.json:
        print("=" * 72)
        print("END-TO-END REHEARSAL  (standard library only, live network)")
        print("=" * 72)
        print(f"provider : {provider}")
        if provider == "glm":
            print(f"model    : {settings.glm_model}   thinking={settings.glm_thinking}")
            print(f"key      : {masked_key(settings.glm_api_key)}")
        else:
            print(f"model    : {settings.ollama_model} (local)")
        print(f"prompt   : {prompt.name} {prompt.version}  ({prompt.path.name})")
        print(f"source   : {LIST_URL}")
        print()

    t_total = time.time()

    # --- retrieve ---------------------------------------------------------
    t0 = time.time()
    if args.urls:
        documents, stats = collect_by_urls(args.urls)
    else:
        documents, stats = collect_documents(args.docs, args.timespan)
    t_fetch = time.time() - t0
    if not documents:
        raise SystemExit(
            "no documents survived filtering - check the network or the list page markup"
        )
    if not args.json:
        print(f"[1] RETRIEVE  {t_fetch:.1f}s")
        print(
            f"    listed={stats['listed']}  noise_skipped={stats['noise']}  "
            f"outside_window={stats['outside_window']}  body_failed={stats['body_failed']}"
        )
        for index, doc in enumerate(documents, start=1):
            body_len = len(doc.get("body") or "")
            print(f"    [{index}] {doc['title'][:52]}  ({body_len} chars)")
        print()

    # --- extract ----------------------------------------------------------
    user_prompt = prompt.render(
        industry="energy storage",
        region="China",
        timespan=args.timespan,
        documents=render_documents(documents),
    )
    t0 = time.time()
    if provider == "glm":
        response, t_llm = call_glm(settings, user_prompt, prompt.system, max_tokens=2048)
    else:
        response, t_llm = call_ollama(settings, user_prompt, prompt.system, max_tokens=2048)
    usage = response.get("usage") or {}
    content = (response.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")

    if not args.json:
        print(f"[2] EXTRACT   {t_llm:.1f}s")
        print(
            f"    model={response.get('model')}  tokens={usage.get('total_tokens')} "
            f"(prompt {usage.get('prompt_tokens')} / completion {usage.get('completion_tokens')}"
            + (f" / reasoning {reasoning_tokens}" if reasoning_tokens else "")
            + ")"
        )
        preview = content.strip().replace("\n", " ")[:160]
        print(f"    raw: {preview}...")
        print()

    try:
        payload = extract_json(content)
    except ValueError as exc:
        print(f"[3] VALIDATE  FAILED - {exc}")
        return 1

    accepted, notes = validate(payload, documents)

    if args.json:
        print(
            json.dumps(
                {
                    "provider": provider,
                    "model": response.get("model"),
                    "documents": [{"title": d["title"], "url": d["url"]} for d in documents],
                    "items": accepted,
                    "notes": notes,
                    "usage": usage,
                    "timings": {
                        "fetch_s": round(t_fetch, 1),
                        "llm_s": round(t_llm, 1),
                        "total_s": round(time.time() - t_total, 1),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("[3] VALIDATE")
    print(f"    records accepted: {len(accepted)} / {len(payload.get('items') or [])} proposed")
    for record in accepted:
        amount = record.get("amount_raw") or "-"
        mark = (
            "*"
            if record.get("amount_source") == "regex"
            else ("!" if record.get("amount_conflict") else "")
        )
        print(
            f"    - [{record.get('event_type') or '?'!s:11}] "
            f"{str(record.get('subject') or '(no subject)')[:22]:22} "
            f"| {amount + mark:9} | {record.get('location_name') or '-'!s:7} "
            f"| {record.get('event_time') or '(no date)'!s:11} "
            f"| conf={record.get('confidence')}"
        )
        print(f"        {record['source_url']}")
    if notes:
        print("    cross-check notes:")
        for note in notes:
            print(f"      - {note}")
    print()

    total = time.time() - t_total
    print("-" * 72)
    print(f"total wall time: {total:.1f}s   (fetch {t_fetch:.1f}s + model {t_llm:.1f}s)")
    print("legend: * amount recovered by code from the source text, not the model")
    print("        ! model and source text disagree - queued for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
