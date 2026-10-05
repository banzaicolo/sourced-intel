# Workflow: nodes, tools, prompts and I/O contracts

> Delivered artifact for requirement 1 of the brief ("工作流图（节点 + 工具 + prompt 摘要）+
> 每个节点的输入输出实例" — *workflow diagram (nodes + tools + prompt summary) plus a
> worked input/output example for every node*).

---

## 1. Workflow diagram

![Workflow diagram — seven nodes with their tools, prompt versions, and the run artefacts they write](workflow-diagram.png)

```mermaid
flowchart TD
    U([User: one natural-language question]) --> N1

    subgraph N1[① 01_parse_intent]
        direction TB
        N1T["Tool: LLM (GLM) · temperature 0<br/>prompt: prompts/01_parse_intent.md v1.0<br/>schema: IntentParseResult"]
    end
    N1 -->|QuerySpec| N2
    N1 -->|ambiguous| CLR([Clarification question returned to user · pipeline stops])

    subgraph N2[② 02_retrieve]
        direction TB
        N2T1["Tool: GdeltAdapter<br/>GDELT DOC 2.0 · no key · 1 req/20-40s"]
        N2T2["Tool: China5eAdapter<br/>section pages + article bodies"]
        N2T3["Tool: RateLimiter + disk cache<br/>tools/http.py"]
    end
    N2 -->|"list[RawDoc] · failed_sources[]"| N3

    subgraph N3[③ 03_extract]
        direction TB
        N3T["Tool: LLM (GLM) · temperature 0<br/>prompt: prompts/03_extract.md v1.3<br/>batched 8 docs · 2 in flight, 0.5s apart<br/>body trimmed to 1500 chars<br/>rejected batch halved + retried, not dropped"]
    end
    N3 -->|list[IntelItem]| N4
    N3 -.->|"subject missing → needs_review"| REV([Human review queue])

    N4[④ 04_aggregate<br/>pure Python · no LLM] --> N5
    N5[⑤ 05_analyze<br/>LLM explains pre-computed numbers<br/>prompt: prompts/05_analyze.md v1.2] --> N6
    N6[⑥ 06_generate_report<br/>Jinja2 + Leaflet + Chart.js<br/>prompt: prompts/06_report.md v1.0] --> N7
    N7[⑦ 07_quality_gate<br/>confidence threshold + retry budget] --> R([report.html])

    N4 -->|"GeoJSON + monthly buckets"| N6
    N5 -->|"findings + evidence URLs"| N6

    N2 -.->|snapshot| SNAP[(data/runs/RUN_ID/snapshots/*.json)]
    N3 -.->|snapshot| SNAP
    N1 -.->|trace| TR[(data/runs/RUN_ID/trace.jsonl)]

    classDef tool fill:#eaf1ff,stroke:#1b5fd9,color:#16181d
    classDef node fill:#ffffff,stroke:#16181d,color:#16181d
    class N1T,N2T1,N2T2,N2T3,N3T tool
    class N1,N2,N3,N4,N5,N6,N7 node
```

## 2. Node contracts

| # | Node | Tool / model | Prompt | Input | Output | Failure behaviour |
|---|---|---|---|---|---|---|
| ① | `01_parse_intent` | GLM, temp 0, JSON mode | `01_parse_intent.md` v1.0 | `user_query` | `QuerySpec` | invalid JSON → 1 retry with the validation error appended → still invalid → clarification question, no guessing |
| ② | `02_retrieve` | httpx adapters, concurrent | – | `QuerySpec` | `list[RawDoc]`, `failed_sources` | one source failing never blocks the others; its name lands in `failed_sources` and in the report |
| ③ | `03_extract` | GLM, temp 0, batched × 8 | `03_extract.md` v1.3 | `raw_docs` (trimmed) | `list[IntelItem]` | rejected request → the batch is **halved and retried** (8→4→2→1, max 3 levels) instead of dropped; a bad credential is never retried; missing `subject` → `needs_review=True`; the URL comes from our own doc list, never from the model |
| ④ | `04_aggregate` | pure Python | – | `intel_items` | `Aggregation` (region / month / type / subject buckets) | no geo field → aggregate by the region name text |
| ⑤ | `05_analyze` | GLM, temp 0.2 | `05_analyze.md` v1.2 | `aggregation` **only** | `Analysis.findings[]` | `<5` records → "insufficient data, no conclusion"; a finding without a verifiable URL is dropped |
| ⑥ | `06_generate_report` | Jinja2 → single-file HTML | `06_report.md` v1.0 | everything above | `report.html` | render error → Markdown fallback; narrative error → template-built summary |
| ⑦ | `07_quality_gate` | rules | – | run products | `QualityReport` | `confidence < 0.6` → appendix；retry > 2 → flagged |

## 3. Per-node input/output examples

### ① `01_parse_intent`

**Input**
```json
{ "user_query": "深圳市储能产业最近三个月的公开动向" }
```

**Output** (`QuerySpec`)
```json
{
  "industry": "储能",
  "region_adcode": "440300",
  "region_name": "深圳市",
  "timespan": "3m",
  "intel_types": ["project", "policy", "funding", "capacity", "partnership", "other"],
  "keywords": ["储能项目", "新型储能"],
  "raw_query": "深圳市储能产业最近三个月的公开动向"
}
```

### ② `02_retrieve`

**Input**: the `QuerySpec` above.

**Output** (`list[RawDoc]` + `failed_sources`) — field names verified against live responses on 2026-09-18:

```json
{
  "raw_docs": [
    {
      "source": "china5e",
      "url": "https://www.china5e.com/news/news-1209422-1.html",
      "title": "总投资3.3亿元!墨玉储能项目即将投入商业运营",
      "published_at": "2026-09-18T00:00:00Z",
      "raw_text": "……full article body, up to 1500 chars go to the model……",
      "fetched_at": "2026-09-18T05:12:03Z",
      "domain": "china5e.com",
      "language": "Chinese"
    },
    {
      "source": "gdelt",
      "url": "https://baijiahao.baidu.com/s?id=1874084929546832257",
      "title": "新型储能成为第一大调节性资源",
      "published_at": "2026-08-17T04:45:00Z",
      "raw_text": "",
      "fetched_at": "2026-09-18T05:12:41Z",
      "domain": "baijiahao.baidu.com",
      "language": "Chinese"
    }
  ],
  "failed_sources": []
}
```

Note the asymmetry: `china5e` supplies a body, GDELT supplies a headline only. The extraction prompt handles both and is told explicitly when no body exists.

### ③ `03_extract`

**Input** (one batch, trimmed)
```
[doc_id=1]
title: 总投资3.3亿元!墨玉储能项目即将投入商业运营
published_at: 2026-09-18
url: https://www.china5e.com/news/news-1209422-1.html
body: <first 1500 characters>
```

**Output** (`list[IntelItem]`) — captured from a **live `glm-4.7` run on 2026-09-18**.
Reproduce it with
`python3 scripts/rehearse_glm.py --urls https://www.china5e.com/news/news-1209422-1.html`
```json
[
  {
    "title": "总投资3.3亿元!墨玉储能项目即将投入商业运营",
    "subject": "皖能集团",
    "amount": 330000000.0,
    "amount_raw": "3.3亿元",
    "amount_source": "llm",
    "amount_conflict": false,
    "event_time": "2026-01-01T00:00:00+00:00",
    "event_time_raw": "2026-01",
    "location_name": "和田地区",
    "adcode": null,
    "event_type": "project",
    "confidence": 0.9,
    "source_url": "https://www.china5e.com/news/news-1209422-1.html",
    "source": "china5e",
    "needs_review": false,
    "review_reason": null
  },
  {
    "title": "全球首个熔盐耦合二氧化碳发电项目在华能开工",
    "subject": "华能山东分公司",
    "amount": null,
    "amount_raw": null,
    "amount_source": "none",
    "amount_conflict": false,
    "event_time": "2026-09-16T00:00:00+00:00",
    "event_time_raw": "2026-09-16",
    "location_name": "山东省",
    "adcode": null,
    "event_type": "project",
    "confidence": 0.9,
    "source_url": "https://www.china5e.com/news/news-1209430-1.html",
    "source": "china5e",
    "needs_review": false,
    "review_reason": null
  }
]
```

Three things worth pointing at in that output:

| Field | Why it is there |
|---|---|
| `amount_source: "llm"` | The model transcribed it and the code's own scan agreed. Would read `"regex"` if the model had missed it and the code had recovered it, or carry `amount_conflict: true` if the two disagreed. First record's source is `llm`, second has no amount at all. |
| `event_time_raw: "2026-01"` | The article says "今年1月" — a month, no day. This field preserves that precision. `event_time` is resolved to the 1st purely so monthly aggregation works; the report displays `event_time_raw` so no reader is shown a day nobody stated (docs/PITFALLS.md #23). |
| `source_url` | Never taken from the model. It only ever returns a `doc_id`; the URL is re-attached from our own fetch list, which makes a hallucinated citation structurally impossible. |

### ④ `04_aggregate`

**Output** (`Aggregation`)
```json
{
  "total_items": 23,
  "with_amount": 7,
  "total_amount": 1873000000.0,
  "by_region": [
    {"adcode": "440305", "region_name": "南山区", "count": 6, "total_amount": 410000000.0,
     "items": ["……title……"]}
  ],
  "by_month": [{"month": "2026-07", "count": 5, "total_amount": 220000000.0}],
  "by_event_type": {"project": 11, "policy": 4, "funding": 3, "capacity": 5},
  "by_subject": [{"subject": "某能源科技", "count": 4, "share": 0.174, "total_amount": 0.0}],
  "unmapped_items": 3
}
```

### ⑤ `05_analyze`

**Output** (`Analysis`)
```json
{
  "findings": [
    {
      "headline": "南山区的项目记录数居首，占全部记录的 26%",
      "detail": "……",
      "evidence": ["https://www.china5e.com/news/news-1209422-1.html"],
      "confidence": 0.72
    }
  ],
  "subject_concentration": {"top3_share": 0.41, "top_subjects": ["A", "B", "C"], "named_subjects": 17},
  "trend_note": "记录数在 8 月达到峰值后回落。",
  "anomalies": ["单条记录的金额为其余记录中位数的 12 倍"],
  "insufficient_data": false
}
```

### ⑥ `06_generate_report`

**Output**: `data/runs/{run_id}/report.html` — single file, opens offline in a browser. Sections: executive summary, key figures, district map (Leaflet + DataV boundaries), monthly trend (Chart.js), findings with clickable sources, subject table, full evidence table, low-confidence appendix, coverage-and-failures appendix.

### ⑦ `07_quality_gate`

**Output** (`QualityReport`)
```json
{
  "passed": true,
  "passed_items": 19,
  "rejected_items": 4,
  "rejected": ["……items below the threshold……"],
  "failed_sources": [],
  "errors": [],
  "retries_used": {"03_extract": 1},
  "threshold": 0.6
}
```

## 4. The four engineering requirements

| Requirement | Where it lives | How to verify |
|---|---|---|
| **State snapshot** | `state.py::save_snapshot`, wrapper in `graph.py` | `ls data/runs/<run_id>/snapshots/` → one JSON per node plus `99_final`; `load_snapshot()` replays any step |
| **Fault tolerance** | per-node `try/except` in `graph.py`, `SourceAdapter.safe_fetch`, bounded retries | disable a source (`SOURCES` / edit the registry) → run still completes, report lists it under "Coverage & failures" |
| **Tracing** | `tracing.py` → `trace.jsonl` | every line has `node / duration_ms / model / prompt_version / prompt_tokens / completion_tokens / total_tokens` |
| **Context trimming** | `state.py::NODE_STATE_SLICES` + `node_context()` | `node_payload_stats()` reports the trimmed character count; the extract node never receives `analysis`, the analyse node never receives `raw_text` |
