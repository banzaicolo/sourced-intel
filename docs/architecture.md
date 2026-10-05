# Architecture

## 1. Component view

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Interfaces                                                               │
│   Streamlit UI (app/streamlit_app.py)      FastAPI (sourced_intel/api.py)   │
│                    └──────────┬───────────────────────┘                   │
│                               ▼                                          │
├──────────────────────────────────────────────────────────────────────────┤
│ Orchestration — LangGraph (sourced_intel/graph.py)                         │
│   StateGraph(GraphState) · 7 nodes · per-node instrumentation            │
│   wrapper: timing → trace.jsonl · state → snapshot · exception → errors  │
├──────────────────────────────────────────────────────────────────────────┤
│ Nodes (sourced_intel/nodes/)                                               │
│   ①parse_intent ②retrieve ③extract ④aggregate ⑤analyze ⑥report ⑦gate     │
├──────────────────────────────────────────────────────────────────────────┤
│ Capabilities                                                             │
│   llm.py      GLM (OpenAI-compatible) │ Ollama (offline fallback)        │
│   tracing.py  JSONL tracer                                               │
│   state.py    snapshot I/O + context-trimming allow-lists                │
│   report/     Jinja2 → single-file HTML (Markdown fallback)              │
├──────────────────────────────────────────────────────────────────────────┤
│ Data access (tools/)                                                     │
│   http.py     shared httpx client · per-host pacing · retry/backoff      │
│   htmlparse.py  dependency-free HTML extraction (regex, documented)      │
│   sources/    SourceAdapter → GdeltAdapter │ China5eAdapter              │
├──────────────────────────────────────────────────────────────────────────┤
│ External                                                                 │
│   GDELT DOC 2.0 · china5e.com · DataV GeoAtlas · Zhipu GLM               │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. Data flow and the shape of the state

The pipeline is **linear with one branch** — the branch exists only so that an ambiguous
question is handed back to the user rather than guessed at.

```
user_query ──► QuerySpec ──► list[RawDoc] ──► list[IntelItem] ──┬─► Aggregation ─┐
                                                                │                 ├─► Analysis ──► report.html
                                                                └─► (threshold) ──┘
```

`GraphState` is a `TypedDict` with three **accumulating** channels, declared with reducers so a
node can return only its own increment:

```python
failed_sources: Annotated[list[str], operator.add]
errors:         Annotated[list[dict[str, Any]], operator.add]
node_meta:      Annotated[dict[str, dict[str, Any]], _merge_node_meta]
```

`node_meta` needs a custom merge function (`operator.add` would raise on dicts). Because
reducers are not applied when writing a snapshot, `graph.py::_apply_update()` mirrors the
merge semantics so the file on disk matches the in-memory state.

## 3. Concurrency model

| Stage | Concurrency | Why |
|---|---|---|
| Retrieval | one thread per adapter (§2 adapters today) | sources are independent; one slow source must not delay another |
| Extraction | `ThreadPoolExecutor`, `EXTRACT_MAX_CONCURRENCY=4`, batches of 8 | keeps prompt size bounded and wall-clock time proportional to `ceil(n/8)/4` |
| Article fetching | up to 8 workers, bounded | a 40-item section page would otherwise cost 40 sequential round-trips |

`asyncio.gather(..., return_exceptions=True)` is the textbook answer for stage 2, but the whole
codebase is synchronous on purpose: the HTTP client is `httpx.Client`, providers are synchronous,
and threads keep stack traces readable. Concurrency is I/O-bound in every case.

## 4. Error-handling strategy

Four independent layers, so no single failure mode can kill a run:

| Layer | Mechanism | Failure becomes |
|---|---|---|
| HTTP | retry with exponential backoff, per-host pacing, non-retryable statuses returned as data | `FetchResult(ok=False, error=...)` |
| Source | `SourceAdapter.safe_fetch` never raises | entry in `failed_sources` + `errors` |
| Node | try/except in the graph wrapper | `errors` entry; the pipeline continues |
| Output | Jinja2 failure → Markdown; LLM narrative failure → template-built summary | a degraded but complete report |

The two failure modes that are **deliberately fatal to nothing** are: an unreachable source and
an unparseable model response. Both are recorded, both are printed in the report.

## 5. Prompt management

Prompts are versioned files, not string literals:

- location: `src/sourced_intel/prompts/NN_name.md`
- each has a `**Version:**` line that is parsed into the trace (`prompt_version` field)
- each has a `## System` block and a `## User template` block, loaded by `prompts_loader.py`
- the user template uses `{placeholder}` substitution with a `__missing__`-safe mapping, so an
  unfilled placeholder surfaces in the prompt rather than crashing the node

Why files: the brief lists "the prompt file for each node" as a deliverable, and a prompt that
lives in a Python string is not reviewable by the person who owns the domain knowledge.

## 6. Performance budget

**Measured: 41–110 s for a full run, median 61 s over ten recorded runs.** (Eleven were recorded;
the eleventh is excluded because it was deliberately configured to one concurrent GLM request, to
establish the ceiling, and took 262 s.) The original target was 60 s, and the first runs did come
in at 41–62 s — but two of those runs were silently discarding a fifth of the extraction to 429s.
With every batch completing, the figure is higher and the record count is real. See
`docs/PITFALLS.md` #32–#34; the two numbers are only comparable at equal record counts.

| Stage | Measured across 10 runs | Median | Notes |
|---|---|---|---|
| Intent parsing | 3.7–12.0 s | 5.2 s | one small GLM call |
| Retrieval | **0.9–42.5 s** | 9.2 s | the widest term and the least ours: 36 china5e bodies over the public internet. GDELT adds a 4 s handshake timeout on the first attempt only |
| Extraction | 7.5–33.5 s | 27.2 s | `ceil(n/8)` batches, `GLM_MAX_CONCURRENCY=2` in flight, 0.5 s minimum spacing between call starts |
| Aggregation | <1 ms | — | pure Python, in-memory |
| Analysis + narrative | 10.5–41.3 s | 17.2 s | one GLM call — **the latency, not the work**, swings fourfold run to run |
| Report generation | 3.5–10.2 s | 6.9 s | one Jinja2 pass; the geo JSON is disk-cached |

**The spread is the point of this table.** A single "about 60 s" told a reviewer nothing about
where to look when a run is slow, and two of the three wide terms belong to other people's
servers. Reproduce the table from the traces the pipeline writes:

    python3 scripts/run_stats.py                     # every run on disk
    python3 scripts/run_stats.py --exclude 205122    # minus the 1-concurrency experiment

The script prints how many runs it sampled, so these figures do not silently rot: re-running the
pipeline changes the sample, and the docs above are a snapshot of the run recorded on 2026-09-25.
A freshly unpacked copy reports "no runs recorded yet" until it has run once — the numbers are
evidence from the developer's machine, not constants of the system.

**GDELT is the only unbounded term we can bound ourselves.** It gets one attempt with a 4 s
budget of its own; if it fails, a marker with a 10-minute TTL (`GDELT_FAILURE_TTL_SECONDS`) makes
the next run skip the live attempt entirely — the source is still reported as failed, just
without the wait. Across all recorded runs GDELT returned **zero** documents: it is unreachable
from this network, so every report is built from china5e alone and lists GDELT under coverage.

**Concurrency is a data setting, not a speed setting.** `GLM_MAX_CONCURRENCY` and
`GLM_MIN_INTERVAL_SECONDS` are enforced by `llm.ProviderPacer`. Measured on the free tier: the
ceiling is **2 concurrent requests** — 3 concurrent completed only 4–5 of 10 calls whether the
spacing was 0.0 s, 0.5 s or 1.0 s, and 4 concurrent did no better. Inside the ceiling the spacing
is not load-bearing (2 concurrent passed 12/12 at both 0.0 s and 0.5 s), so the 0.5 s default is
cheap insurance rather than a tuning knob. A rejected batch is now **halved and retried** instead
of dropped, so exceeding the quota costs a few extra calls rather than a fifth of the corpus.

## 7. Storage layout

```
data/
├── cache/
│   ├── gdelt/<sha1>.json    30-minute TTL, keyed on the composed query
│   ├── gdelt/<sha1>.failed  failure marker, 10-minute TTL - skips the live attempt
│   └── geo/<adcode>_full.json  district boundaries, fetched once
└── runs/<run_id>/
    ├── report.html
    ├── trace.jsonl          append-only, one line per node
    └── snapshots/
        ├── 01_parse_intent.json
        ├── 02_retrieve.json
        ├── ...
        └── 99_final.json
```

`snapshots/` is the replay surface: `load_snapshot()` revives Pydantic models from JSON, so any
step can be inspected or resumed without re-running the pipeline.
