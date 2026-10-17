# Changelog

Notable changes only. This project follows [Semantic Versioning](https://semver.org);
while the major version is `0`, a minor bump may change behaviour.

---

## [Unreleased]

Hardening pass after an external review. Nothing here changes what a run produces; all of it
changes what a run can be made to do.

### Security

- **Run ids are validated before they become paths.** `POST /runs` and
  `sourced-intel run --run-id` accepted an arbitrary string that was joined straight onto
  `data/runs`, so `../escape` created a directory next to the runs root rather than inside
  it. Ids must now match `run-[A-Za-z0-9][A-Za-z0-9._-]*`, and `Settings.run_dir()` checks
  the resolved path stays under `runs_dir`. The read endpoints (`GET /runs/{id}`,
  `/report`, `/trace`) reject a malformed id with **400** instead of probing the disk.
  Covered by `tests/test_run_id_safety.py`.
- **Docker no longer publishes the API to the network.** Both services bind `127.0.0.1`,
  so `POST /runs` - unauthenticated and billed against a real LLM account - is not
  reachable from the LAN. Exposing it is a deliberate edit; see README, "Exposing the API",
  and set `SOURCED_INTEL_API_TOKEN` when you do.
- **`GET /health` no longer echoes the API key's last four characters.** The dict reports
  `set`/`unset`; local diagnostics that want a mask call `llm_keys.masked_key()`.
- **LangGraph 0.3.34 → 1.2.16.** `pip-audit` reported eight advisories across
  langchain-core, langgraph, langgraph-checkpoint and langgraph-sdk. The 1.x line clears
  all of them; the offline suite passes unchanged on Python 3.10 and 3.12.

### Added

- **Optional bearer-token auth and a run cap for the REST API.** With
  `SOURCED_INTEL_API_TOKEN` set, every route except `/health` requires
  `Authorization: Bearer <token>` and answers `401` otherwise (constant-time compare).
  `POST /runs` admits at most `MAX_CONCURRENT_RUNS` executions (default `1`) and answers
  `429` with `Retry-After` beyond that. The permit is released on the success and the
  crash path, so a failing run cannot brick the service. Covered by
  `tests/test_api_guardrails.py`.
- **`requirements.lock`**, a universal, hash-carrying lock compiled at the Python 3.10
  floor. CI installs from it with `--require-hashes`; `make lock` regenerates it. The
  same file installs on 3.10 and 3.12 and runs the full offline suite from a clean env.

### Fixed

- **Context trimming is now enforced by the wrapper, not by each node.** `graph.node_payload()`
  hands a node only its `state.NODE_STATE_SLICES` entry. This caught a real bypass:
  `07_quality_gate` read `report_path`, a key absent from its own allow-list, and the
  trimming claim in the README rested on seven nodes remembering to call `node_context()`.
  `tests/test_context_trimming.py` fails if a new node has no slice.

### Changed

- CI's lint job is a gate: `continue-on-error` is gone, and `ruff format --check` runs
  alongside `ruff check`. The tree is clean under both.
- `docs/` is excluded from `ruff format` on purpose - its code fences keep hand-aligned
  columns that the formatter would collapse.

---

## [0.2.0] — 2026-09-25

The release that turns a working pipeline into something a stranger can evaluate.

### Added

- **`sourced-intel` command line** (`src/sourced_intel/cli.py`) with `demo`, `run`, `stats`,
  `preflight`, `ui` and `api` subcommands, plus `python -m sourced_intel`.
- **A bundled offline demo run** (`src/sourced_intel/demo.py`). Its aggregation is produced by
  calling the real `nodes.aggregate` on the sample records, so the demo cannot drift from
  the code it demonstrates; the records' URLs use the RFC 2606 `.example` domain, so no
  sample citation can resolve to a real page.
- **Citations are now a document-wide registry.** One URL gets one number, shared by the
  findings, the evidence table and a new **References** section. Previously the evidence
  table numbered rows and every finding restarted its own `[1] [2]`, so two different
  documents could carry the same number on the same page.
- **The map and the trend chart are inline SVG.** Both are rendered server-side and drawn by
  the browser with no script and no CDN request, so they survive a lost network and print
  correctly. Leaflet still upgrades the map to interactive tiles when it loads.
- **Client-side evidence-table filter** (text search, event-type chips, live row count)
  layered over a server-rendered table, so the table still works with JavaScript off.
- **Light/dark theme** from one set of CSS custom properties, applied before first paint so
  there is no flash of the wrong colours.
- **A printing stylesheet**, a reading-progress hairline, a gliding table-of-contents
  indicator and one restrained scroll-reveal gesture (`prefers-reduced-motion` respected).
- `pyproject.toml` (installable, with a console script and a `ruff` configuration),
  `scripts/demo.py` (demo without installing the package), `scripts/run_stats.py`
  (reproduces the performance distribution), `docs/screenshots/`.
- CI: a job that runs with **no installs at all** (proving the stdlib-only claim), the
  offline suite on Python 3.10 and 3.12, an advisory lint job, and a job that renders the
  demo report and uploads it as an artefact.
- `README.zh-CN.md`, `CONTRIBUTING.md`, and `.github/ISSUE_TEMPLATE/` (bug + feature).

### Fixed

- **`Aggregation.with_subject`** — the share column divided by the true number of records
  that named a subject, while the label beside it summed the top-15 subject list. Correct
  numbers next to a wrong denominator; both now use the same figure.
- **An empty run crashed the renderer.** The findings hint referenced `nums.references` even
  when no sources existed, so an empty result fell back to Markdown and looked successful.
- **An empty run fabricated a figure.** "Top-3 concentration" printed `0.0%` when no subject
  was named; it now renders as `-` (absence is not zero).
- **Version was defined in three places.** `__version__` is now the single source, read by
  both `pyproject.toml` (dynamic) and the report's `pipeline` footer.
- **The trend chart's amount axis** said 亿元 while plotting 万元.
- **`_subject_rows` sorts by count**, so the ranking no longer depends on the model's
  ordering.
- **Four unused imports** that a lint gate would have caught.
- **`scripts/preflight.py`** no longer imports `parse_article`, which it never called.

### Changed

- README rewritten around what is verifiable: three anti-hallucination rules, the report
  gallery, and the four engineering requirements with a command to check each one.
- `.env.example`, `docs/` and the report copy now distinguish what was measured from what
  was inferred.

---

## [0.1.0] — 2026-09-18

First working pipeline: seven LangGraph nodes, two live data sources, a single-file HTML
report, state snapshots, tracing, per-node context trimming and a quality gate. The five
pitfalls from this round are entries 1–30 of `docs/PITFALLS.md`.

[0.2.0]: https://github.com/banzaicolo/sourced-intel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/banzaicolo/sourced-intel/releases/tag/v0.1.0
