"""Node 3 - structured extraction.

Batched + concurrent LLM calls over the retrieved documents. The model is asked
for a `doc_id`, never for a URL: the real URL is re-attached afterwards from our
own source list, so a hallucinated link can never reach the report.

Anything the model leaves blank stays blank in the language model's own fields - a
missing `subject` marks the record `needs_review` rather than being back-filled.

The single exception is the *amount*, which is verified deterministically: see
`_to_intel_item` and `parsing.reconcile_amount`. A number taken from a regex scan
of the source text is a verbatim quote, not an inference, and it is labelled
`amount_source="regex"` in the report so the provenance stays visible.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import get_settings
from ..llm import LLMAuthError, LLMError, get_llm
from ..prompts_loader import load_prompt
from ..schemas import (
    ExtractionBatch,
    GraphState,
    IntelItem,
    RawDoc,
    parse_datetime,
    reconcile_amount,
)
from ..state import node_context

logger = logging.getLogger(__name__)

NODE_NAME = "03_extract"
PROMPT_NAME = "03_extract"

MAX_ITEMS_PER_DOC = 3

# How many times a rejected batch may be halved before its documents are given
# up on. The default batch is 8 documents, so three halvings reach single-document
# requests: 8 -> 4 -> 2 -> 1.
MAX_SPLIT_DEPTH = 3


def _render_documents(batch: list[tuple[int, RawDoc]]) -> str:
    """Serialise a batch for the prompt, trimming every body (context trimming)."""
    blocks: list[str] = []
    for doc_id, doc in batch:
        body = doc.text_for_llm
        if not doc.has_body:
            body = "(headline only - no body available)"
        published = doc.published_at.strftime("%Y-%m-%d") if doc.published_at else "unknown"
        blocks.append(
            f"[doc_id={doc_id}]\n"
            f"title: {doc.title}\n"
            f"published_at: {published}\n"
            f"url: {doc.url}\n"
            f"body: {body}"
        )
    return "\n\n".join(blocks)


def _to_intel_item(payload: dict, doc: RawDoc, item_index: int) -> IntelItem:
    """Validate one model record against the real document. Never invents fields.

    The amount is the one field where a deterministic check beats trusting the
    model: `reconcile_amount` keeps the model's transcription when it exists, fills
    it from the source text when the model missed it, and flags a disagreement
    between the two. See `parsing.reconcile_amount`.
    """
    amount_check = reconcile_amount(
        payload.get("amount_raw"), title=doc.title, body=doc.text_for_llm
    )
    # `event_time_raw` preserves the precision the source actually had - a full
    # date, or a month. The model is told never to upgrade "今年1月" into a
    # specific day, and this is where that promise is kept.
    raw_event = (payload.get("event_time") or "").strip() or None
    event_time = parse_datetime(raw_event)
    if event_time is None and doc.published_at is not None:
        # The model stated nothing usable. The article's own publish date is a
        # real, sourced fact, so it is the honest fallback.
        event_time = doc.published_at
        raw_event = doc.published_at.strftime("%Y-%m-%d")

    title = (payload.get("title") or "").strip() or doc.title
    subject = (payload.get("subject") or "").strip()
    location = (payload.get("location_name") or "").strip() or None

    item = IntelItem(
        title=title,
        subject=subject,
        amount=amount_check.value,
        amount_raw=amount_check.raw,
        amount_source=amount_check.source,
        amount_conflict=amount_check.conflict,
        event_time=event_time,
        event_time_raw=raw_event,
        location_name=location,
        adcode=None,
        event_type=payload.get("event_type") or "other",
        confidence=float(payload.get("confidence") or 0.0),
        source_url=doc.url,
        source=doc.source,
    )

    if amount_check.conflict:
        item = item.flag_for_review(
            f"amount disagreement: model said {payload.get('amount_raw')!r}, "
            f"source text contains {amount_check.code_raw!r}"
        )
    elif not subject:
        item = item.flag_for_review("subject missing in source document")
    elif item.confidence < 0.3:
        item = item.flag_for_review("confidence below 0.3")
    return item


def _merge_usage(base: dict, extra: dict) -> dict:
    """Sum the token counters; keep the newest non-empty model / prompt version."""
    merged = dict(base)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        merged[key] = int(merged.get(key) or 0) + int(extra.get(key) or 0)
    for key in ("model", "provider", "prompt_version"):
        if extra.get(key):
            merged[key] = extra[key]
    return merged


def _split_and_retry(
    batch: list[tuple[int, RawDoc]],
    doc_index: dict[int, RawDoc],
    settings,
    depth: int,
    cause: Exception,
    carried_usage: dict,
) -> tuple[list[IntelItem], dict, list[dict]]:
    """Re-issue a rejected batch as two halves instead of discarding it.

    Regression this exists for: the reject path used to `break`, which threw away
    *every* document in the batch. A 429 that survives the client's own backoff
    removed 8 of 36 documents from the report while the run still reported
    `completed` - measured across three runs as 7, 15 and 29 records from the
    same 36 documents. Losing one document is a degradation; losing a fifth of
    the corpus silently is a bug.

    Halving helps twice over: the retry is issued later, after the quota window
    has moved, and each half is a smaller request that is easier to admit.
    """
    mid = len(batch) // 2
    halves = [batch[:mid], batch[mid:]]
    logger.warning(
        "batch of %d rejected (%s); retrying as %d+%d", len(batch), cause, mid, len(batch) - mid
    )

    errors: list[dict] = [
        {
            "node": NODE_NAME,
            "type": "batch_split",
            "detail": (
                f"batch of {len(batch)} rejected ({str(cause)[:120]}) - "
                f"retrying as {mid}+{len(batch) - mid}"
            ),
        }
    ]
    items: list[IntelItem] = []
    usage = dict(carried_usage)
    for half in halves:
        half_items, half_usage, half_errors = _extract_batch(
            half, doc_index, settings, depth=depth + 1
        )
        items.extend(half_items)
        errors.extend(half_errors)
        usage = _merge_usage(usage, half_usage)
    usage["items_extracted"] = len(items)
    return items, usage, errors


def _extract_batch(
    batch: list[tuple[int, RawDoc]],
    doc_index: dict[int, RawDoc],
    settings,
    depth: int = 0,
) -> tuple[list[IntelItem], dict, list[dict]]:
    """Run one LLM call for one batch. Returns (items, usage_meta, errors).

    On a rejected request the batch is halved and retried rather than dropped -
    see `_split_and_retry`. `depth` tracks how many halvings have already
    happened, so a permanently failing provider cannot recurse without bound.
    """
    prompt = load_prompt(PROMPT_NAME)
    llm = get_llm()
    items: list[IntelItem] = []
    errors: list[dict] = []

    # The industry/region hint helps the model pick the right subject.
    user_prompt = f"DOCUMENTS:\n{_render_documents(batch)}\n\nReturn JSON only."
    usage: dict = {"prompt_version": prompt.version}

    for attempt in (1, 2):
        try:
            response = llm.chat(
                user_prompt,
                system=prompt.system,
                temperature=0.0,
                max_tokens=2048,
                json_mode=True,
                retries=1,
            )
        except LLMAuthError as exc:
            # A rejected credential is permanent: `llm.py` promises auth errors are
            # never retried, and splitting would only multiply doomed calls.
            errors.append({"node": NODE_NAME, "type": "llm_auth_error", "detail": str(exc)})
            break
        except LLMError as exc:
            if len(batch) > 1 and depth < MAX_SPLIT_DEPTH:
                return _split_and_retry(batch, doc_index, settings, depth, exc, usage)
            errors.append({"node": NODE_NAME, "type": "llm_error", "detail": str(exc)})
            break

        usage.update(
            {
                "model": response.model,
                "provider": response.provider,
                "prompt_tokens": usage.get("prompt_tokens", 0) + response.prompt_tokens,
                "completion_tokens": usage.get("completion_tokens", 0) + response.completion_tokens,
                "total_tokens": usage.get("total_tokens", 0) + response.total_tokens,
            }
        )

        try:
            payload = response.json_payload()
            if isinstance(payload, list):
                payload = {"items": payload}
            parsed = ExtractionBatch.model_validate(payload)
        except Exception as exc:
            errors.append(
                {"node": NODE_NAME, "type": "extract_parse_error", "detail": str(exc)[:300]}
            )
            if attempt == 1:
                user_prompt += (
                    "\n\nYour previous answer was rejected: "
                    f"{str(exc)[:200]}\nReturn valid JSON only, matching the schema exactly."
                )
                continue
            break

        seen_titles: set[str] = set()
        for record in parsed.items:
            doc_id = getattr(record, "doc_id", None)
            doc = doc_index.get(doc_id) if doc_id is not None else None
            if doc is None:
                errors.append(
                    {
                        "node": NODE_NAME,
                        "type": "unknown_doc_id",
                        "detail": f"model referenced doc_id={doc_id} which was not in the batch",
                    }
                )
                continue
            title_key = "".join(record.title.split()).lower()[:40]
            if title_key and title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            items.append(_to_intel_item(record.model_dump(), doc, len(items)))
            if len(items) >= len(batch) * MAX_ITEMS_PER_DOC:
                break
        break

    usage["items_extracted"] = len(items)
    return items, usage, errors


def extract(state: GraphState) -> dict:
    ctx = node_context("03_extract", state)
    docs: list[RawDoc] = ctx.get("raw_docs") or []
    if not docs:
        return {
            "intel_items": [],
            "status": "no_data",
            "errors": [{"node": NODE_NAME, "type": "no_documents", "detail": "nothing to extract"}],
        }

    settings = get_settings()
    batch_size = max(1, settings.extract_batch_size)
    indexed = list(enumerate(docs, start=1))
    batches = [indexed[i : i + batch_size] for i in range(0, len(indexed), batch_size)]
    doc_index = dict(indexed)

    all_items: list[IntelItem] = []
    all_errors: list[dict] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt_version = "unknown"
    model = None

    workers = max(1, min(settings.extract_max_concurrency, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_extract_batch, batch, doc_index, settings): batch for batch in batches
        }
        for future in as_completed(futures):
            try:
                items, usage, errors = future.result()
            except Exception as exc:
                all_errors.append(
                    {
                        "node": NODE_NAME,
                        "type": "batch_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            all_items.extend(items)
            all_errors.extend(errors)
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += usage.get("total_tokens", 0)
            prompt_version = usage.get("prompt_version", prompt_version)
            model = usage.get("model", model)

    # Deduplicate across batches by (subject, title prefix).
    deduped: list[IntelItem] = []
    seen: set[tuple[str, str]] = set()
    for item in all_items:
        key = (item.subject.strip().lower(), "".join(item.title.split()).lower()[:30])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda i: i.confidence, reverse=True)

    meta = {
        "batches": len(batches),
        # Splits are extra LLM calls caused by rejections. Surfacing the count in
        # node_meta means "the run needed 3 retries to keep its data" is visible
        # without reading the error list.
        "batches_split": sum(1 for e in all_errors if e.get("type") == "batch_split"),
        "documents": len(docs),
        "items": len(deduped),
        "needs_review": sum(1 for i in deduped if i.needs_review),
        "with_amount": sum(1 for i in deduped if i.amount is not None),
        "amount_from_code": sum(1 for i in deduped if i.amount_source == "regex"),
        "amount_conflicts": sum(1 for i in deduped if i.amount_conflict),
        "model": model,
        "prompt_version": prompt_version,
        **total_usage,
    }

    if not deduped:
        all_errors.append(
            {
                "node": NODE_NAME,
                "type": "no_items_extracted",
                "detail": "the model returned no usable records for any batch",
            }
        )

    return {
        "intel_items": deduped,
        "errors": all_errors,
        "node_meta": {NODE_NAME: meta},
    }
