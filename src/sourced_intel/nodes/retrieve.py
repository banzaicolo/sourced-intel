"""Node 2 - multi-source retrieval.

Fans out over every registered adapter concurrently. A failing source never
blocks the others: its name is recorded in `failed_sources` and the report says so.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..schemas import GraphState, QuerySpec, RawDoc
from ..state import node_context
from ..tools.sources import build_adapters

logger = logging.getLogger(__name__)

NODE_NAME = "02_retrieve"

PER_SOURCE_LIMIT = 60


def _dedupe(docs: list[RawDoc]) -> list[RawDoc]:
    """Drop duplicate URLs and near-duplicate headlines across sources."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[RawDoc] = []
    for doc in docs:
        url_key = doc.url.split("?", 1)[0].rstrip("/").lower()
        title_key = "".join(doc.title.split()).lower()[:40]
        if url_key in seen_urls or (title_key and title_key in seen_titles):
            continue
        seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        unique.append(doc)
    return unique


def retrieve(state: GraphState) -> dict:
    ctx = node_context("02_retrieve", state)
    query: QuerySpec | None = ctx.get("query")
    if query is None:
        return {
            "raw_docs": [],
            "status": "failed",
            "errors": [
                {"node": NODE_NAME, "type": "missing_query", "detail": "no QuerySpec in state"}
            ],
        }

    adapters = build_adapters()
    docs: list[RawDoc] = []
    failed: list[str] = []
    new_errors: list[dict] = []
    per_source: dict[str, int] = {}

    with ThreadPoolExecutor(max_workers=max(1, len(adapters))) as pool:
        futures = {
            pool.submit(adapter.safe_fetch, query, PER_SOURCE_LIMIT): adapter
            for adapter in adapters
        }
        for future in as_completed(futures):
            adapter = futures[future]
            try:
                source_docs, error = future.result()
            except Exception as exc:
                source_docs, error = [], f"{type(exc).__name__}: {exc}"

            # The GDELT adapter raises on a hard outage; adapters that return an
            # empty list plus an error message are handled the same way here.
            if error and not source_docs:
                failed.append(adapter.name)
                new_errors.append(
                    {
                        "node": NODE_NAME,
                        "type": "source_failed",
                        "source": adapter.name,
                        "detail": error,
                    }
                )
                logger.warning("source %s contributed nothing: %s", adapter.name, error)
            elif error:
                failed.append(adapter.name)
                new_errors.append(
                    {
                        "node": NODE_NAME,
                        "type": "source_degraded",
                        "source": adapter.name,
                        "detail": error,
                    }
                )
            per_source[adapter.name] = len(source_docs)
            docs.extend(source_docs)

    unique_docs = _dedupe(docs)
    unique_docs.sort(
        key=lambda d: (
            d.published_at is None,
            -(d.published_at.timestamp() if d.published_at else 0),
        )
    )

    meta = {
        "per_source_counts": per_source,
        "total_raw": len(docs),
        "after_dedupe": len(unique_docs),
        "failed_sources": failed,
    }

    if not unique_docs:
        new_errors.append(
            {
                "node": NODE_NAME,
                "type": "no_documents",
                "detail": "every source returned zero usable documents",
            }
        )
        return {
            "raw_docs": [],
            "failed_sources": failed,
            "errors": new_errors,
            "node_meta": {NODE_NAME: meta},
            "status": "no_data",
        }

    return {
        "raw_docs": unique_docs,
        "failed_sources": failed,
        "errors": new_errors,
        "node_meta": {NODE_NAME: meta},
    }
