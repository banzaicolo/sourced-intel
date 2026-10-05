"""Node 4 - spatio-temporal aggregation.

Pure Python. The LLM is never asked to count, sum or group anything - it only
interprets numbers this node already produced.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from ..config import REGION_DISPLAY, match_region
from ..schemas import (
    Aggregation,
    GraphState,
    IntelItem,
    MonthBucket,
    QuerySpec,
    RegionBucket,
)
from ..state import node_context

logger = logging.getLogger(__name__)

NODE_NAME = "04_aggregate"


def infer_adcode(item: IntelItem) -> str | None:
    """Resolve the item's administrative code.

    Order of trust: explicit adcode > location_name > title > subject. The
    ranking itself lives in `config.match_region`, so a district named in the
    title ('南山') correctly beats the city ('深圳').
    """
    if item.adcode and item.adcode in REGION_DISPLAY:
        return item.adcode

    for text in (item.location_name, item.title, item.subject):
        adcode = match_region(text)
        if adcode:
            return adcode
    return None


def aggregate(state: GraphState) -> dict:
    ctx = node_context("04_aggregate", state)
    items: list[IntelItem] = ctx.get("intel_items") or []
    query: QuerySpec | None = ctx.get("query")

    if not items:
        empty = Aggregation()
        return {
            "aggregation": empty,
            "errors": [{"node": NODE_NAME, "type": "no_items", "detail": "nothing to aggregate"}],
            "node_meta": {NODE_NAME: {"total_items": 0}},
        }

    # Attach the resolved adcode back onto each item so the report can show it.
    resolved: list[IntelItem] = []
    for item in items:
        adcode = infer_adcode(item)
        if adcode != item.adcode:
            item = item.model_copy(update={"adcode": adcode})
        resolved.append(item)

    region_items: dict[str, list[IntelItem]] = defaultdict(list)
    month_items: dict[str, list[IntelItem]] = defaultdict(list)
    unmapped = 0

    for item in resolved:
        if item.adcode:
            region_items[item.adcode].append(item)
        else:
            unmapped += 1
        if item.event_time:
            month_items[item.event_time.strftime("%Y-%m")].append(item)

    # If nothing mapped to a region, fall back to the query region so the map and
    # the region table still carry meaning (documented degradation).
    if not region_items and query is not None:
        region_items[query.region_adcode] = list(resolved)
        unmapped = 0

    by_region = [
        RegionBucket(
            adcode=adcode,
            region_name=REGION_DISPLAY.get(adcode, adcode),
            count=len(bucket),
            total_amount=sum(i.amount or 0.0 for i in bucket),
            items=[i.title[:60] for i in bucket[:10]],
        )
        for adcode, bucket in sorted(region_items.items(), key=lambda kv: -len(kv[1]))
    ]

    by_month = [
        MonthBucket(
            month=month,
            count=len(bucket),
            total_amount=sum(i.amount or 0.0 for i in bucket),
        )
        for month, bucket in sorted(month_items.items())
    ]

    by_event_type = dict(Counter(i.event_type for i in resolved))

    subject_counter = Counter(i.subject for i in resolved if i.subject.strip())
    # The true number of records that named a subject. `by_subject` below is cut
    # to the top 15, so it cannot be recovered from that list - and every share
    # in the report is stated against this number.
    total_named = sum(subject_counter.values())
    by_subject = [
        {
            "subject": subject,
            "count": count,
            "share": round(count / total_named, 4) if total_named else 0.0,
            "total_amount": sum(i.amount or 0.0 for i in resolved if i.subject.strip() == subject),
        }
        for subject, count in subject_counter.most_common(15)
    ]

    aggregation = Aggregation(
        total_items=len(resolved),
        with_amount=sum(1 for i in resolved if i.amount is not None),
        total_amount=sum(i.amount or 0.0 for i in resolved),
        with_subject=total_named,
        by_region=by_region,
        by_month=by_month,
        by_event_type=by_event_type,
        by_subject=by_subject,
        unmapped_items=unmapped,
    )

    meta = {
        "total_items": aggregation.total_items,
        "regions": len(by_region),
        "months": len(by_month),
        "with_amount": aggregation.with_amount,
        "with_subject": total_named,
        "subjects_shown": len(by_subject),
        "unmapped_items": unmapped,
    }

    return {
        "intel_items": resolved,
        "aggregation": aggregation,
        "node_meta": {NODE_NAME: meta},
    }
