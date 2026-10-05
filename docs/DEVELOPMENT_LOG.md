# Development Process Document · DEVELOPMENT LOG

> **Chinese working document**; not part of the English deliverable set.
> Purpose: let another agent (or another person) take over / verify this project **without re-hitting the same pitfalls**.
> Each entry records "what was done / why / how it was verified / current status".

---

## 0. Suggested Reading Order

1. This document (progress and verification boundaries)
2. `docs/PITFALLS.md` (14 pitfalls, must-read before writing code)
3. `README.md` (English delivery instructions, up and running in 5 minutes)
4. `docs/workflow.md` (node contracts and I/O instances)

---

## 1. Timeline (2026-09-18, actual time spent vs. the spec's schedule)

| Clock | Spec schedule | What was actually done | Output |
|---|---|---|---|
| 12:33 | — | Read spec, set up workspace, **probe data sources first** | probe conclusions |
| 12:35–12:47 | 0:00–0:40 source connectivity validation | 12 real requests, judged each source alive/dead (with three criteria: DNS / SPA / rate-limiting) | **conclusion: S3/S4 in section 4 of the spec are basically unusable**, S1 has hard constraints |
| 12:47–12:58 | — | Confirmed three decisions with the user (LLM=Zhipu GLM, source strategy, directory) | directory `sourced-intel/` |
| 12:48–12:56 | 0:40–1:30 graph+prompt+Schema | Schema finalized, 4 prompts, directory skeleton | `schemas.py`, `prompts/*.md` |
| 12:52–12:58 | — | **Discovered GDELT fields contain no body text + rate-limited to 2 min/request**, reversed source priority | source strategy finalized |
| 12:58–13:05 | — | **Refactor**: extracted pure logic into dependency-free modules, finally able to run tests | `parsing.py`, `tools/htmlparse.py` |
| 13:05–13:10 | — | 39 dependency-free unit tests all green (1 was red at first, see pitfall 9) | `tests/test_parsing.py` + fixtures |
| 13:10–13:30 | 1:30–4:30 main trunk | LangGraph orchestration + 7 nodes + 2 adapters + report template + UI/API + Docker | whole repo |

**Deviation from schedule**: the source-validation phase ran ~7 minutes over budget, but **bought back two key insights** (GDELT has no body text, rate-limited to 2 min/request), avoiding ~2 hours of rework later. This trade was worth it.

---

## 2. Key Decisions and Their Rationale

### Decision 1: Reverse the primary/secondary order of data sources (china5e primary / GDELT secondary)

| Basis | Content |
|---|---|
| 【measured】 | GDELT may succeed only once per run after ~20–40s, and **only has titles**; china5e responds in seconds, **has full body text**, has dates, has pagination (427 pages × 40 items) |
| 【measured】 | GDELT's Chinese titles are usable (`language=Chinese`), but the body-text fetch path (Baijiahao) is blocked by a security verification |
| 【inferred】 | The acceptance criterion "report within 60 seconds" cannot be met with GDELT; can be met with china5e |

**Where it lands in code**: `tools/sources/__init__.py::DEFAULT_SOURCE_ORDER = ("gdelt", "china5e")` (both run, but GDELT sends only 1 request + 30-minute disk cache + degrades on failure).

### Decision 2: Do not introduce BeautifulSoup / pandas / folium

| Basis | Content |
|---|---|
| 【measured】 | The target pages are extremely regular in structure; regex + `html.unescape` is fully sufficient |
| 【benefit】 | Dependencies dropped from 13 to 8; **and the core parsing layer can be unit-tested outside the virtual env** (the only way to verify early this time) |

**Cost**: regex is more brittle than CSS selectors. **Countermeasure**: freeze real page snippets into test fixtures (`tests/fixtures/*.html`); when the site is redesigned, the tests go red first.

### Decision 3: Let the model output only `doc_id`, never a URL

**Reason**: hallucinated links are the most fatal failure mode of this kind of system — every item in the report must be "clickable back to the source".

**Where it lands**: `schemas.LLMIntelItem` **has no `source_url` field**; `extract.py::_to_intel_item()` backfills the URL from our own doc table using `doc_id`. **The model structurally cannot forge provenance.**

**Likewise**: if a finding from `05_analyze` references a URL not in this run's fetch set, **that conclusion is dropped outright** (the `valid_urls` check in `analyze.py`).

### Decision 4: All numbers are computed by code; the LLM only interprets them

**Where it lands**: `aggregate.py` pure-Python statistics → `analyze.py` feeds the **computed numbers** to the model; the prompt explicitly states "you are a transcriber, not an author", "do not introduce external knowledge", "numbers must be copied verbatim from the input".

---

## 3. Verification Boundaries (Important: another agent should start here)

### ✅ Verified (reproducible)

| Item | Verification method | Result |
|---|---|---|
| Authenticity of the 14 pitfalls | See the reproduction commands for each in `PITFALLS.md` | All obtained from real requests |
| Pure parsing-layer logic | `python3 -m unittest discover -s tests -p "test_parsing.py" -v` | **58/58 passed** (0.08 s, zero third-party dependencies) |
| China Energy Network (中国能源网) list-page parsing | `tests/test_parsing.py::TestChina5eListFixture` | Real page fixture, all 6 list items correctly extracted |
| China Energy Network (中国能源网) article-page parsing | `tests/test_parsing.py::TestChina5eArticleFixture` | Title/time/source/body (1836 chars) all correct |
| Syntax of all Python files | `python3 -m compileall src scripts app tests` | All 25 files passed |
| DataV GeoAtlas fields | `scripts/check_sources.py --only datav` | `adcode / name / centroid / level / geometry` confirmed by measurement |

### ⚠️ Unverified (must do next)

| Item | Why not done | How to do it |
|---|---|---|
| **Runtime behavior of `pydantic` / `langgraph` / `httpx` / `jinja2`** | The local Python is a clean environment, **the user explicitly forbade the AI from installing dependencies itself** (he wanted to do it himself) | Create venv, install `requirements.txt`, then run `pytest tests/ -v` |
| **Real end-to-end run** | Same as above + needs GLM API key | `pytest tests/ -m slow -v`, or `streamlit run app/streamlit_app.py` |
| **Actual extraction quality of Zhipu GLM** | Same as above | After it runs, check extraction accuracy in `data/runs/*/report.html` |
| **Docker build** | Needs Docker running + network to pull packages | `docker compose up --build` |
| **Real call to GDELT adapter** | Depends on httpx, and rate-limited very slowly | First run waits 20–40 s; second run hits cache |

> **Advice to the verifier**: `graph.py` / each `nodes/*.py` is code **never verified at runtime** (only syntax-checked).
> They are written per the public semantics of LangGraph and Pydantic v2, but **do not assume they run correctly on the first try**.
> Check first: how pydantic v2's `model_validate` tolerates LLM output, and the merge order of LangGraph's `Annotated` reducer.

---

## 4. Directory and Responsibilities

```
sourced-intel/
├── scripts/check_sources.py     ★ dependency-free source probe (run this first)
├── src/sourced_intel/
│   ├── parsing.py               ★ dependency-free pure logic (amount / date / JSON / URL)
│   ├── schemas.py               Pydantic v2 models + GraphState (with reducers)
│   ├── config.py                config + 32-entry admin-region lookup table
│   ├── state.py                 snapshot read/write + **context-trimming allow-list**
│   ├── tracing.py               JSONL tracer
│   ├── llm.py                   GLM / Ollama dual provider
│   ├── graph.py                 LangGraph orchestration + instrumentation wrapper
│   ├── nodes/*.py               the 7 nodes
│   ├── tools/htmlparse.py       ★ dependency-free HTML parsing (regex annotated against the source page)
│   ├── tools/http.py            rate limiter + retry + UA
│   ├── tools/sources/           GDELT / china5e adapters
│   └── report/render.py         Jinja2 → single-file HTML (Markdown fallback)
├── app/streamlit_app.py         demo UI
├── src/sourced_intel/api.py       FastAPI (productisation path)
├── tests/test_parsing.py        ★ dependency-free, 58 cases, all green
├── scripts/preflight.py         ★ dependency-free full-pipeline check; runs with nothing installed
├── src/sourced_intel/llm_keys.py  ★ dependency-free key-shape validation (reused by preflight)
├── tests/test_pipeline_smoke.py needs pydantic; run once deps are installed
└── tests/fixtures/*.html        ★ real page fragments (these go red first when a site is redesigned)
```

★ = parts that have actually been verified.

---

## 5. Where the Four Engineering Requirements Are Implemented (what the interviewer will look at)

| Requirement | File | One-line explanation |
|---|---|---|
| State snapshot | `state.py::save_snapshot` + `graph.py` wrapper | One JSON per node, plus `99_final`; `load_snapshot()` can resume from any step |
| Exception recovery | per-node try/except in `graph.py` + `SourceAdapter.safe_fetch` | Crashed node writes to `errors` and keeps running; one failed source writes to `failed_sources`; retry cap 2 |
| Tracing | `tracing.py` | Each line contains `node / duration_ms / model / prompt_version / prompt_tokens / completion_tokens / total_tokens / retries` |
| Context trimming | `state.py::NODE_STATE_SLICES` + `node_context()` | **Whitelist** mechanism: the extract node cannot see `analysis`, the analyze node cannot see `raw_text`; `node_payload_stats()` outputs the trimmed char count for auditing |

---

## 6. Next Steps (by priority)

1. **Set up venv and install deps** (commands in `README.md` Quickstart)
2. Run `pytest tests/ -v -m "not slow"` → fix runtime bugs (expect pydantic/LangGraph detail issues)
3. Fill `GLM_API_KEY` in `.env`, run `pytest tests/ -m slow -v` for the first end-to-end run
4. Use real run results to **backfill** the I/O instances in section 3 of `docs/workflow.md` (currently structural examples, not real data)
5. Warm the GDELT cache once before screen recording (avoid a 2-minute stall while recording)
6. `docker compose up --build` to verify one-command startup

---

# Round 2 · First Real Pipeline Test (2026-09-18 afternoon)

## 7. What This Round Did

After the user provided a GLM key, they asked for "full test → refine → explain". Because there was no virtual environment (pydantic/langgraph not installed), **split into two parts**: first use dependency-free means to verify everything verifiable, then explain the parts that require installing deps to verify.

### 7.1 What the dependency-free part actually ran

| Verification item | Means | Result |
|---|---|---|
| GLM credentials | 4 header/baseURL combinations | ❌ **401**, key format wrong (see PITFALLS pitfall 15) |
| ollama structured output | Real HTTP, `format=json` + `think` switch comparison | ✅ 1.8s / 57 token valid JSON output |
| List page → article page → parse | Real fetch of 3 articles, using the project's own parsing module | ✅ source name, time, body all correct |
| Real prompt file + real LLM | Loaded `prompts/03_extract.md` (v1.2) as-is and fed to ollama | ✅ 3 records, JSON compliant |
| Code-side amount extraction vs LLM | `parse_amount` cross-check | ✅ code found `23.37亿` missed by the model |
| DataV geo source | Real fetch | ✅ 9 districts, centroid correct |

**Conclusion**: the data path (fetch → parse → amount/time extraction) and the LLM path **both halves are verified with real data**; what remains unverified is pydantic validation, LangGraph orchestration, Jinja2 templates, Streamlit UI.

### 7.2 Five bugs exposed by real data (all fixed)

| # | Problem | Impact | Fix |
|---|---|---|---|
| 1 | ollama without `think:false` → `content` empty | **Offline fallback completely unusable**, and 11× slower | Request body fixed to `think:false`; empty content raises explicit error |
| 2 | Default model `glm-4-flash` has been taken down | Main path directly unusable | Changed to `glm-4.7-flash` — **but Round 3's real test found this name does not exist either**, finally changed to `glm-4.7` (pitfall 16) |
| 3 | 401 treated as a retryable error | Blindly retried 3× + masked the real cause | Added `LLMAuthError`, never retried |
| 4 | Amount float error `8.2e8 → 819999999.9999999` | Report could show a wrong amount | `round(value, 2)` |
| 5 | List-page ad rows treated as intel | Wastes one fetch + one LLM call, and a 4B model really will write it as a record | `is_noise_title()` filters at list stage |

### 7.3 Capabilities added this round

| Added | Why |
|---|---|
| `scripts/preflight.py` | **Can tell "why it won't run" without installing anything**. Outputs raw evidence (HTTP code / byte count / error text), supports `--json` for other agents to consume. This is the most valuable output of this round |
| `llm_keys.py` | Key shape validation must be dependency-free, otherwise preflight pulls in httpx too |
| `parsing.reconcile_amount()` | Amounts the model missed are backfilled by code from the source text, and **tagged with source** `amount_source="regex"`; when model and code disagree, set `amount_conflict` into a manual queue, **do not silently pick one** |
| `IntelItem.amount_source / amount_conflict` | Lets the report show the **origin** of each number (model transcription / code scan / conflict) |
| `GLM_MAX_CONCURRENCY` | Zhipu free tier is 1 concurrency, ~1 req/s; exceeding it yields 429 |
| `LLMRateLimit` independent backoff | Rate-limit recovers at second-level, socket error recovers at millisecond-level, **should not share one retry cadence** |

### 7.4 Still unverified (honest boundary)

- ❌ `graph.py` / 7 nodes / `report/render.py` / Streamlit / FastAPI: **only passed syntax check**
- ❌ GLM end-to-end: **blocked by a bad key**, and the key must be re-copied by the user himself
- ❌ Jinja2 template rendering: template added amount-source markers, needs first run after deps installed to confirm

**Next**: user fills in the correct key + sets up venv, then runs `pytest tests/ -v` and `pytest tests/ -m slow -v`.

---

# Round 3 · Full Pipeline Validation After the Real Key Worked (2026-09-18 afternoon)

## 8. Starting point of this round

Last round got stuck on GLM key format. This round the user sent back a real key and asked for "full test → refine → explain". This round **got the whole real pipeline working** and dug out 4 new pitfalls.

### 8.1 Verification results with the real key

| Item | Command | Result |
|---|---|---|
| Model catalog | `GET /api/paas/v4/models` | ✅ 200, **11 available models** (see pitfall 16) |
| Basic chat | `curl ... glm-4.7` | ✅ 200, 1.5s, **13 tokens** |
| Structured extraction | `rehearse_glm.py` | ✅ 6.7s / 3421 tokens, all fields correct |
| **End-to-end full pipeline** | `rehearse_glm.py --docs 3` | ✅ **7.8s** completed fetch → parse → extract → amount check |

**Against the spec's acceptance criterion "report within 60 seconds"**: measured fetch 1.1s + model 6.7s = **7.8s**, ample margin.

### 8.2 Four new pitfalls dug out this round (all fixed)

| # | Pitfall | Impact | Fix |
|---|---|---|---|
| **22** | GLM 5.x forces thinking; error text pointed "low/high/max" at the **wrong parameter name** | Main-path model unusable; thinking eats the budget and `content` is empty | Added `GLM_THINKING`; on 1210 auto-degrade to `reasoning_effort: low` and scale `max_tokens` ×3 |
| **23** | Model **fabricated** "this January" into `2026-01-01` | Fabricated fact, a date no one ever said appears in the report | Prompt changed to "copy at the precision of the original" + `parse_datetime` supports `YYYY-MM` (anchored regex) + added `IntelItem.event_time_raw` |
| **24** | **Parallel-editing the same file** in one message → later write overwrites earlier | Edit reported success but didn't take effect, wasted debugging time | Serialize edits to the same file; verify with **runtime** after editing; clear `__pycache__` before verifying |
| **25** | Same logic written in two places, `llm.py` and `preflight.py` | Same bug fixed twice; the health-check script more fragile than the pipeline, **misleading the debugging direction** | Thinking strategy extracted into single source of truth `Settings.glm_thinking` |

### 8.3 ⚠️ A self-correction that must be recorded: I misjudged the model last round

See `docs/PITFALLS.md` **pitfall 21**.

| | Content |
|---|---|
| Last round's wrong conclusion | Molten-salt project: "model gave `山东省` (Shandong Province) ❌, correct is `甘肃省酒泉市` (Jiuquan City, Gansu Province)" |
| This round's verbatim check of the source | "甘肃" (Gansu) **0** times, "酒泉" (Jiuquan) **0** times, "山东" (Shandong) **3** times |
| Truth | **The model was right, I was wrong.** I attributed the geography fact from another article to this one |
| Worse | This fabricated "correct answer" was also written into the **worked example** of `prompts/03_extract.md` — i.e. actively feeding the model wrong knowledge |

**Fix**: all three worked examples swapped to real documents + verbatim-checked outputs; added `rehearse_glm.py --urls` so examples can be **replayed verbatim**.

**Lesson**: **"Correct answers" must likewise come from reproducible evidence, not from memory.**
Judging the model with an unverified reference answer reverses the entire conclusion — and this error is especially insidious because it happens precisely when "it looks like you're verifying seriously".

### 8.4 Capabilities added this round

| Added | Why |
|---|---|
| `scripts/rehearse_glm.py` | **Run the full real pipeline dependency-free** (fetch → parse → prompt → GLM → amount cross-validation). Supports `--urls` precise replay, `--json` for other agents to consume |
| `Settings.glm_thinking` | Turns off the thinking channel: a greeting **130 → 13 tokens** (13×) |
| `GLMClient` auto-degrade on 1210 | No code change needed when switching to a thinking-forced model |
| `IntelItem.event_time_raw` | Date precision visible, no longer fabricated |
| `preflight`'s model catalog check | When model name is wrong, **errors clearly and lists all options**, instead of a misleading 429 |
| `check_sources.py` lists available models | Same as above, exposed early |

### 8.5 Verification status at end of this round

| Level | Status |
|---|---|
| Dependency-free unit tests | ✅ **58/58 passed** (0.08 s) |
| Syntax compile | ✅ `compileall` all passed |
| Health-check script | ✅ **15 passed, 0 hard failures** |
| Real end-to-end | ✅ **7.8 s** completed fetch → parse → LLM → amount check |
| Prompt examples reproducible | ✅ three URLs via `--urls` replayable verbatim |
| **Still not runtime-verified** | ⚠️ `graph.py` / 7 nodes / `report/render.py` / Streamlit / FastAPI — **deps not installed, only syntax-checked** |

### 8.6 Next steps (require the user; the AI does not install itself)

```bash
cd sourced-intel
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
source .venv/bin/activate
pytest tests/ -v -m "not slow"      # offline
pytest tests/ -m slow -v            # real end-to-end
streamlit run app/streamlit_app.py  # UI
```

---

## 9. Round 9: Deps installed → full-pipeline runtime verification → fixed 3 real bugs

**Trigger**: after the user installed deps, they asked to run the final tests. This is the **first time** the project ran the full pipeline **in a real runtime environment** — the "unverified list" from the previous 8 rounds could finally be cleared, and the clearing process immediately exposed 3 bugs that **only surface at runtime**, one of them completely silent.

### 9.1 Environment (measured)

| Item | Result |
|---|---|
| venv | Python **3.12.14** (managed by uv) |
| Key versions | langgraph 0.3.34 / pydantic 2.13.5 / streamlit 1.64.0 / fastapi 0.141.1 / pytest 9.1.1 |
| Clash proxy | **Not running** (no process, nothing listening on 7897) — and **not needed** |
| Tsinghua / Ali mirrors | ✅ 37 KB index **0.30 s / 0.19 s** |
| Direct to pypi.org | ❌ unreachable |

**Conclusion: just use the domestic mirror to install deps; no need for a proxy.** (Also see pitfall 28: the macOS wheel for `uvloop` is called `universal2`; judging with `grep arm64` leads to the wrong conclusion "macOS can't install it".)

### 9.2 The 3 bugs fixed this round

| # | Bug | Symptom | Root cause | Fix |
|---|---|---|---|---|
| 1 | **All district display names wrong** | `resolve_region("深圳市南山区")` (Nanshan District, Shenzhen; 深圳市 = Shenzhen) → adcode right but name is "深圳市" (Shenzhen) | The 3rd column of the 9 district rows in `REGION_TABLE` all filled with "深圳市" (Shenzhen) (copy-paste residue) | 3rd column changed to each district's own name |
| 2 | **Equal-length match overridden by city level** | Title contains "深圳南山" (Shenzhen Nanshan) → falls to 440300 | `len(key) > len(best_key)` strict greater-than; both `"深圳"` (Shenzhen) / `"南山"` (Nanshan) are 2 chars, first registered wins | Extracted `config.match_region()`, sorted by `(admin level, key length)` |
| 3 | **LLM output silently discarded** | `05_analyze`'s `fallback: true`, report all fallback text | Model put the list under the `"answer"` key + `confidence:"high"`; `findings` defaults to empty list → silent degrade | Added normalization layer `_normalise_analysis_payload` + `_coerce_confidence`; prompt bump **v1.2** |

See `docs/PITFALLS.md` **pitfall 26 / 27**. Pitfall 26 is the most worth recording this round — **failure was completely silent; the report rendered normally and status stayed completed**.

Bugs 1 and 2 are **two faces of the same module**: `parse_intent.resolve_region` and `aggregate.infer_adcode` each copied a matching logic (a rerun of pitfall 25), so when fixing, they were extracted together into a single source of truth.

### 9.3 Bug 3 before/after comparison (same code, similar data)

| Metric | Before fix | After fix |
|---|---|---|
| `errors` | 2 entries (`all_findings_dropped`) | **0 entries** |
| `05_analyze.fallback` | `true` | **not triggered** |
| `findings` | **0 entries** | **4 entries, real model output** |
| `confidence` | — | 0.9 / 0.9 / 0.9 / 0.6 |
| Evidence chain | — | 1–2 real URLs per entry |

### 9.4 Added this round

| Added | Why |
|---|---|
| `config.match_region()` | Single source of truth for place-name matching, sorted by `(admin level, key length)`; level derived directly from adcode, no extra column maintained |
| `analyze._normalise_analysis_payload()` | Folds any shape (`answer`/`results`/bare array) into `findings` |
| `analyze._coerce_confidence()` | `"high"` / `"90"` / `90` / out-of-range values → unified to 0–1 float |
| `analyze.py` diagnostic fields | Records `payload_keys` / `payload_preview` / `findings_returned` in `errors` — makes "silent failure" visible |
| `tests/test_analyze_payload.py` | **19 regression tests**, lock the normalization contract |
| `prompts/05_analyze.md` → **v1.2** | Explicitly "top-level key can only be `findings`", `confidence` must be a number |

### 9.5 Verification status at end of Round 9 (all runtime-measured)

| Level | Status |
|---|---|
| Unit tests | ✅ **106 passed** (0 errors, 0.68 s) — 102 at the time of this round; 4 output-contract tests were added during final hardening |
| Dependency-free unit tests | ✅ **58/58** (0.07 s, consistent with README) |
| Real end-to-end | ✅ **1 passed / 42 s** |
| Health-check script | ✅ **15 passed, 1 warning (GDELT 429, expected), 0 hard failures** |
| Streamlit UI | ✅ started successfully, HTTP **200** |
| FastAPI | ✅ `/docs` **200**, 5 endpoints ready |
| `POST /runs` | ✅ **http 200 / 41.6 s**, 39 documents → 15 records, `status=completed` |
| State snapshot | ✅ each run writes `snapshots/01..07 + 99_final.json` (measured 1.2 MB) |
| Tracing | ✅ `trace.jsonl` records per-node duration / tokens / retries / failed sources |
| **Unverified list** | ✅ **cleared** — modules marked "syntax-check only" in the previous 8 rounds are now all runtime-verified |

### 9.6 About `data/runs/`

Inside are **real-run artifacts** (reports / snapshots / traces); they are artifacts, not source, already covered by `.gitignore`. Just clear before delivery; affects no logic.

### 9.7 Known but **harmless** leftovers

| Item | Note |
|---|---|
| `LangChainPendingDeprecationWarning: allowed_objects` | Upstream deprecation notice from langgraph, currently no functional impact. Fixing it touches the checkpoint serde config; risk > benefit — **leave as-is for now**, logged here |
| GDELT 429 | By design a non-critical path (pitfall 1/2/3); `failed_sources` records it faithfully, does not block the pipeline |
| `data/runs/` accumulation | Pure artifacts, can be cleaned anytime |

### 9.8 A self-correction this round (second of its kind)

I checked the `app/` directory, saw only `streamlit_app.py`, and concluded "the REST API promised by README doesn't exist, the api service in compose will fail to start", **and was already about to implement one**.

In fact `src/sourced_intel/api.py` has always existed; `sourced_intel.api:app` written in compose matches exactly.

This is the **same kind of error** as pitfall 21 (fabricating "甘肃酒泉" (Gansu Jiuquan) from memory to accuse the model): treating an assumption as fact. **Before saying "it doesn't exist", you must run it or search once.** See pitfall 30.

---

# Round 10 · Report rendering verified against real data → found two silent-failure bugs (2026-09-25)

## 10.1 Where this round started

Round 9 ended with a rewritten report template, an "optimised to 54.4 s" claim, and a delivery
copy that had been verified with Docker but never compared file-by-file against the development
tree. Three things were therefore open: the new template had not been checked against a real
dataset, the changes lived only in the delivery copy, and the docs still quoted the old figures.

## 10.2 What this round did

| # | Action | Result |
|---|---|---|
| 1 | `diff -rq` development vs delivery | 6 files had drifted: `config.py`, `render.py`, `report.html.j2`, `gdelt.py`, `.env.example`, `README.md`. Synced back — the two trees are now identical apart from `.env` |
| 2 | Wrote `tests/test_report_contract.py` before looking at the template output | **24 tests, 8 of which failed immediately** and exposed a silent fallback (see 10.3) |
| 3 | Re-rendered the newest saved real run with the new template | HTML path taken, 8 sections, TOC ids and numbers both match the body, `2026-09` preserved as `2026-09` (not padded to `2026-09-01`), source links show `china5e.com` |
| 4 | Ran the pipeline end to end, twice | 61.7 s and 50.3 s — and **both runs logged two 429s in `03_extract`** |
| 5 | Wired the GLM concurrency settings (see 10.4), measured the safe value, re-ran | 93.3 s, **0 rate-limit errors**, 19 records instead of 7 |
| 6 | Corrected the docs | PITFALLS 31–33 added, Pitfall 18 corrected (its fix had never been wired), README runtime claim replaced, `architecture.md` §6 given measured numbers, both stale numbers I was about to write into the README were themselves measured first |

## 10.3 Bug 1 — the formatted report could silently become a Markdown file

`render_html` catches every exception and falls back to `render_markdown`. The new template
did `s.share * 100` over `Aggregation.by_subject`, which is typed `list[dict[str, Any]]`.
Rendering with a subject row that lacked `share` raised `UndefinedError`:

```
ERROR sourced_intel.report.render: HTML rendering failed ('dict object' has no attribute
'share'), falling back to Markdown
```

The run still reported `status: completed`. The only difference was the deliverable's
extension. In production the aggregate node always supplies all four keys, so this never
fired — but nothing enforced that, and the cost of it firing is the whole formatted report.

**Fixed at the layer that owns the shape**: `render._subject_rows()` normalises the free-form
list into a guaranteed shape and recomputes a missing or percentage-style `share` from the
counts. `generate_report` now records an `html_render_fallback` error when the returned path is
not `.html`, so a downgrade reaches the trace instead of only the log.

Two smaller corrections came out of the same pass: the `pct` Jinja filter existed but was never
used while Python pre-formatted the same value (**now one source of truth**), and the subject
hint claimed shares were relative to every record when the code divides by the records that
named a subject (**the denominator is now computed and printed**).

## 10.4 Bug 2 — `GLM_MAX_CONCURRENCY` was a documented setting that nothing read

`grep -rn "glm_max_concurrency" src/` matched `config.py` only. Node 3 runs
`ceil(36/8) = 5` batches through a `ThreadPoolExecutor` sized by `EXTRACT_MAX_CONCURRENCY` (4),
so four GLM requests went out simultaneously with no spacing — while `config.py` stated
*"the pipeline serialises GLM calls by default"*.

A 429 makes `_extract_batch` `break`, abandoning the **entire batch**. Both real runs in 10.2
lost 2 of 5 batches. Measured across the three runs: 7, 15 and 29 records from the same 36
documents.

**Fixed** by `llm.ProviderPacer` (semaphore + start-to-start interval, applied only around the
POST, released before the recursive 1210 path). The defaults were then **measured rather than
guessed**:

| configuration | calls throttled |
|---|---|
| 4 concurrent, no pacing | 3 of 5 |
| **2 concurrent, 0.5 s apart** | **0 of 5** |
| 1 concurrent, 1.0 s apart | 0 of 5, ~40% slower end to end |

Default is now 2 concurrent / 0.5 s, in `config.py`, `.env.example` and `.env`.

## 10.5 Round 10 verification status

| Level | Status |
|---|---|
| Full suite | ✅ **150 passed** (was 107; +24 report contract, +11 GDELT budgets, +8 LLM pacing) |
| Offline subset | ✅ **149 passed in 1.1 s**, 1 deselected (the slow live test) |
| Dependency-free layer | ✅ **58/58** |
| Real end-to-end | ✅ completed, `report_format=html`, 9 sections, TOC consistent |
| Rate-limit errors | ✅ **0** (was 2 per run) |
| Dead config sweep | ✅ `glm_max_concurrency`, `glm_min_interval` now read; `gdelt_timeout`, `gdelt_failure_ttl` were already wired in Round 9 |

## 10.6 The lesson worth keeping

Two of the three bugs this round were the same bug: **a mechanism that was believed to be in
place and was not**. A config key nobody reads, and an exception handler that turns a failure
into a different file extension. Neither produced an error, neither showed up in a status code,
and both were found only by rendering real data and running the real pipeline and then
**reading the record counts**, not the elapsed time.

The README previously promised "under 60 seconds". It now says about 90 and explains why. The
slow number is the true one; the fast one was fast because a fifth of the retrieval had been
thrown away.

---

# Round 11 · Measuring the ceiling, then removing the failure mode behind it (2026-09-25)

## 11.1 Where this round started

Round 10 left one open question - "is 3 concurrent safe, or only 2?" - and one uncomfortable
fact: the 429 fix was a *mitigation*. It stopped the rate limit firing at the default settings,
but `_extract_batch` still handled a rejected request by discarding the entire batch. Nothing
about that failure mode had changed; only the odds of triggering it had.

## 11.2 The concurrency ceiling, measured properly

Round 10 compared two configurations with five calls each. This round swept the space, repeated
the chosen setting, and separated *concurrency* from *spacing*:

| configuration | calls completed | wall clock |
|---|---|---|
| 4 concurrent, 0.5 s apart | 4 of 10 | 8.3 s |
| 3 concurrent, no spacing | 2 of 10 | 3.6 s |
| 3 concurrent, 0.5 s apart | 5 of 10 | 9.5 s |
| 3 concurrent, 1.0 s apart | 4 of 10 | 19.3 s |
| **2 concurrent, 0.5 s apart** | **10 of 10, and 10 of 10 again** | 23.1 s / 20.3 s |
| 2 concurrent, no spacing | 12 of 12 | 40.3 s |
| 2 concurrent, 0.25 s apart | 12 of 12 | 28.4 s |

Two conclusions, both of which changed the documentation:

1. **The ceiling is two concurrent requests, and it is hard.** Three fails in every variant
   regardless of spacing, so the answer to "is 3 safe?" is no. The default of 2 is not a
   conservative guess, it is the measured maximum.
2. **Spacing inside the ceiling is not load bearing.** 2 concurrent passed 12/12 at 0.0 s and at
   0.25 s. So `GLM_MIN_INTERVAL_SECONDS=0.5` was kept as cheap insurance, *not* as a tuning knob,
   and lowering it was rejected: the saving is about 2 s on a run whose wall clock varies by
   ±15 s. The dominant term is server-side queueing - a request asking for eight tokens measured
   a p50 latency of 15-31 s, which is the same effect that makes `05_analyze` swing between 10 s
   and 41 s.

## 11.3 The actual fix: a rejected batch is halved, not dropped

`_extract_batch` used to `break` on any non-auth `LLMError`. That abandoned all eight documents
in the batch. Replaced with `_split_and_retry`: halve, recurse, bounded by `MAX_SPLIT_DEPTH = 3`
(8 -> 4 -> 2 -> 1). The loss bound went from 8 documents to 1.

Three deliberate properties:

* **Auth errors are exempt.** `LLMAuthError` subclasses `LLMError` and its documented contract is
  "never retried". Splitting a bad credential would turn one doomed call into fifteen.
* **Cost stays attributable.** Token counters from both halves are summed back into the node's
  usage, so a recovery is not a hole in the cost accounting.
* **The degradation is countable.** `node_meta["03_extract"]["batches_split"]` reports how many
  splits a run needed. "This run kept its data at the cost of three retries" is now visible
  without reading the error list.

## 11.4 Making the performance claim checkable

The docs quoted single latency numbers. Three of the terms are external and vary fourfold, so a
single number was never going to survive contact with a reader. Added `scripts/run_stats.py`,
which reads the traces the pipeline already writes and prints the distribution:

```
$ python3 scripts/run_stats.py --exclude 205122        # stdlib only, no venv needed
10 run(s) in .../data/runs
node                        min   median      max
02_retrieve                0.90     9.20    42.50
03_extract                 7.50    27.20    33.50
05_analyze                10.50    17.20    41.30
TOTAL                     41.40    60.90   109.50
markdown fallbacks        : 0  (must be 0)
```

`--exclude` takes the run id, so excluding the 1-concurrency experiment is a **named, reviewable
act** rather than a quietly shortened window. The `markdown fallbacks` line is the Round 10 bug
(Pitfall 31) turned into a standing check. 13 tests cover the script, because a documented number
is only worth as much as the tool that produced it.

## 11.5 Round 11 verification status

| Level | Status |
|---|---|
| Full suite | ✅ **173 passed** (was 150; +10 extract recovery, +13 run_stats) |
| Offline subset | ✅ **172 passed in 1.2 s**, 1 deselected |
| Dependency-free layer | ✅ **58/58** |
| Real end-to-end | ✅ completed, `report_format=html`, 9 sections, TOC consistent, `batches_split=0` |
| Rate-limit errors | ✅ 0 |
| `run_stats.py` without a venv | ✅ runs under system `python3`; `--json` validated as pure JSON |
| Docs re-measured | ✅ README, architecture §6 and PITFALLS #32/#33 replaced with the 10-run distribution |

## 11.6 Open observation, deliberately not fixed

8 of 18 records in the final run were flagged `needs_review`, all for the same reason:
`subject missing in source document`. Looking at the titles, the cause is semantic rather than a
lazy model - "山西最大构网型独立共享储能电站并网" names a *project*, not a company, while the
records that passed carry values like `subject="应县储能项目"`, which is a project name in a
field documented as "the company/institution driving the event".

So `subject` is being asked to do two jobs. That is a product decision (add a
`project_name` field? relax the flag? teach the prompt to search the body for the operator?), not
a bug to patch silently, and changing the extraction prompt would invalidate every example
measured in `docs/workflow.md`. Recorded here so it is a decision, not a discovery.

---

# Round 12 · A re-run the user asked for, and the flaky check it exposed (2026-09-25)

## 12.1 Where this round started

The user's question was six words long: *"you did test it yourself, right?"* Round 11 had reported
172 passed in the delivery copy. Re-running it live — instead of quoting the earlier result — gave
**171 passed, 1 failed**, and the failing test passed when run on its own.

The cause was not the code the test named. It was that two suites were running at once, one in the
development tree and one in the delivery copy.

## 12.2 The first hypothesis, and the correction it needed

`scripts/preflight.py` checked "data directory writable" with a **fixed filename**:

```python
probe = data_dir / ".preflight"
probe.write_text("ok")
probe.unlink()
```

Two processes doing that concurrently both write `data/.preflight`; whichever unlinks second raises
`FileNotFoundError`. That is POSIX semantics, and it is what the code looked like it was doing — but
inside a tooling environment that intercepts deletes it **cannot be measured**, because a refused
`unlink` and a caught `FileNotFoundError` are the same event from the outside.

The failures that launched this round carried the answer in their own text, and it was read past:

```
data directory writable: .../data: [safe-delete] .../.preflight
```

`[safe-delete]` is the delete interception installed by the tooling that ran the command. Those
processes were not racing; the tooling was refusing their unlink. So the fixed-name race is recorded
as **[Inferred]** — code plus filesystem semantics — and not as a measurement. The table that had
been staged for this section ("1 of 6 before, 10 of 10 after") was a tooling artefact and is not
used.

## 12.3 The measurement that did hold up: files on disk

`Glob` against the real filesystem — deliberately not `find`, because under tooling that intercepts
deletes, `find -delete` reports success and removes nothing — found **eleven**
`.preflight-*.tmp` files in the development tree and **eleven** in the delivery copy. Eleven is exactly the number of preflights
`tests/test_preflight_output.py` launches in one run: 2 in `setUpClass`, 6 concurrent, 3 sequential.
Two independent trees, same count, matching the call sites.

**Fix, first attempt — and its own bug.** `mkstemp` for a unique name, unlink in a `finally`,
guarded by `except OSError: pass`. The new leftover assertion then found the files immediately:
the swallow is the exact pattern this project keeps removing (Pitfall 31), reintroduced inside the
fix for a different bug — and invisible in the one environment that would have shown it, since on
a real machine the unlink succeeds. The second version reports a cleanup failure as a **WARN**
carrying the path and the error, explicitly not a hard failure: writability and tidiness are
separate facts and the output now keeps them separate. Verified by injecting a `PermissionError`
into `Path.unlink`, not by reading the code:

```
  PASS  | data directory writable     | .../data
  WARN  | preflight probe cleaned up | could not remove .../.preflight-n01yf1dp.tmp (PermissionError: ...)
exit code 0, hard_failures 0
```

## 12.4 A test that reported on its neighbours

The leftover assertion first read "the contents of `data/` must be empty". That is a claim about the
whole machine rather than about this test, and it failed the moment another suite ran alongside it.
Both leftover assertions now sample a baseline in `setUpClass` and compare the **delta**: they report
on the probes these tests created and on nothing else.

## 12.5 Round 12 verification status

| Level | Status |
|---|---|
| Delivery copy, run alone | ✅ **175 passed**, 1 deselected |
| Development copy | ✅ **175 passed**, 1 deselected |
| Trees identical | ✅ `diff -rq` clean apart from `.env` and runtime data |
| Concurrent preflight | ✅ all exit 0, `hard_failures: 0` |
| Cleanup-failure path | ✅ reports WARN, exit code stays 0 (verified by injection) |
| Probe files on disk | ✅ **0** in both trees — 11 + 11 removed; PITFALLS #35 records how they got there |

## 12.6 The lessons

**A test that passes alone and fails in a suite is reporting a real interaction.** Do not re-run it
until it goes green.

**A tool's view of the filesystem is not the filesystem.** `find -delete` under delete-intercepted
tooling reported success and left every file in place; that is what sent half this round chasing a
race that may never have fired. `Glob`, which does not go through that sandbox, showed the truth in one call.
When a measurement and a hypothesis disagree, check the measurement; when two measurements disagree,
the instrument is the first thing to question — and a failure message containing a token you did not
predict (`[safe-delete]`) deserves its own explanation before it is filed under your theory.

**Every bug this round was found by running something, not by reading something** — and the
correction in 12.2 was found by reading the failure text that had been sitting in the output the
whole time.

## 12.7 A note for whoever validates this next

Both false signals this round came from the tooling that ran the commands, not from the project:

* `find -delete` reported success and removed nothing, because the tooling intercepts deletes.
* `pytest` reported **48 errors** in an unpacked copy of the delivery zip, every traceback inside the
  tooling's own `sitecustomize.py`:

  ```
  PermissionError: EEXIST: file already exists, mkdir
      '.../T/pytest-of-<user>'
  ```

  It brokers `mkdir` as well as deletes, so pytest's own `tmp_path` fixture cannot be set up. The
  same command, in the same directory, on the same tree:

  ```
  $ python -m pytest tests/ -m "not slow" -q          # 127 passed, 48 errors
  $ python -E -m pytest tests/ -m "not slow" -q       # 175 passed
  ```

The rule that falls out: **when validating a delivered artefact, know which layer produced the
verdict.** The zip was never at fault — the unpacked copy is byte-for-byte the verified tree — but a
validation path that runs through ambient tooling is not the customer's path, and its verdicts have
to be attributed before they are believed.

# Round 13 · From interview demo to a public portfolio piece (2026-09-26)

## 13.1 The goal changed, and that changed what "done" means

The interview is over, and the demo is no longer the point. The new target is a **public repository
on GitHub**, which shifts the definition of done from "it runs on my machine" to "a stranger can
clone it, run `python3 scripts/demo.py`, and see a report without an API key — and can check every
claim in the README against the tests."

That reframing produced three workstreams, in this order:

1. Make the report itself the strongest thing in the repo (a stranger's first impression is the
   screenshot, not the code).
2. Give the repo the furniture a public project needs: license, packaging, CI, contribution and
   issue templates, a bilingual README, a committed example report.
3. Run the **adversarial edge tests** that a public repo will attract, and fix what they surface.

## 13.2 The report visual redesign

The report was rebuilt around an editorial layout rather than a stack of cards, following a survey
of current design practice (Apple's site as one reference, but deliberately not copied; several
other editorial-report and data-journalism references carried more weight):

- **Non-symmetric figure grid** — the disclosed-amount KPI is the lead, rendered as a large number
  with `tabular-nums`, not a card in a 12-column grid.
- **Light/dark from one token set**, applied in an inline `<head>` script *before* first paint so a
  report opened from disk never flashes the wrong colours.
- **One source, one number** — a global citation registry shared by the findings, the evidence
  table and the reference list, so the same number never points at two documents.
- **Server-rendered inline SVG** for both the choropleth map and the monthly trend, so the report
  stays self-contained and prints correctly with no CDN and no JavaScript; Leaflet and Chart.js
  were demoted to optional enhancements. (Chart.js was removed outright — the SVG's native
  `<title>` tooltips already cover it.)

## 13.3 Repo furniture

- `LICENSE` (MIT), `pyproject.toml` (`pip install -e .` → `sourced-intel`), `.github/workflows/ci.yml`
  with four jobs including a **zero-install** one that runs `unittest` with the standard library
  alone, so the "no dependencies to run the parser" claim is a CI-enforced fact rather than a
  sentence.
- `cli.py` / `__main__.py` (demo / run / stats / preflight / ui / api), and a `demo.py` that builds
  the sample report from bundled data **without importing LangGraph or httpx**.
- `CONTRIBUTING.md`, `CHANGELOG.md`, `.github/ISSUE_TEMPLATE/` (bug + feature), `README.zh-CN.md`.

## 13.4 The edge tests found two real bugs

A deliberately hostile pass — empty run, single record, HTML/JS injection, missing geometry, no
trend data, every record rejected, 1e15 amounts, two records sharing one URL — found two defects
that the normal path never touched, both now recorded in `docs/PITFALLS.md` (#36, #37):

1. **An empty run crashed the renderer.** The findings hint referenced `nums.references` even when
   `_sections` had not emitted that key (no sources), so the renderer fell back to Markdown and
   looked successful. The hint is now conditional on sources existing.
2. **An empty run fabricated a figure.** "Top-3 concentration" printed `0.0%` for a run where no
   subject was named. `0` and `None` are not the same fact; the share now renders as `-`.

Both are locked by `TestEmptyRun`, and the suite grew from 230 to **232 passing**.

## 13.5 Verification status

- `pytest tests/ -m "not slow"` → **232 passed**, 6 subtests, ~2 s, no network.
- `python3 scripts/demo.py` → writes `examples/demo-report.html` (199 KB), no un-rendered Jinja, no
  CDN chart dependency, all five README screenshots re-rendered from this file.

## 13.6 The lesson worth keeping

**The empty path is not a corner case; it is a first-class input.** Every public function gets
fuzzed by strangers with inputs the author never imagined, and the empty run is the cheapest one to
try. Two independent defects were hiding there precisely because every development and demo run
until now had *some* data. An adversarial pass over a renderer — not a linter, not a type check,
not a review — is what exposed them, and it is what turns "works on my machine" into "works when
cloned."


