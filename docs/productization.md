# From demo to product

**Current state.** A working single-tenant pipeline: one question in, one report out, with
per-node tracing and recoverable state. It runs from a `docker compose up`.

**Target state.** A multi-tenant intelligence service where an analyst subscribes to an
industry/region pair and receives scheduled briefings, with human review of low-confidence
records in the loop.

## Path

| Phase | Work | Why this order |
|---|---|---|
| **1. Harden the sources** | Replace HTML scraping for the two most valuable sources with official APIs or licensed feeds; add a source-health dashboard and automatic circuit-breaking per source | Scraping is the most fragile part of the system. Everything downstream is worthless if retrieval silently degrades — and today it degrades *visibly*, which is the right default but not a product |
| **2. Human-in-the-loop review** | Turn the `needs_review` queue and the low-confidence appendix into a review UI: confirm / correct / discard, with the correction written back as a labelled example | The pipeline already refuses to invent data and already isolates what it is unsure about. Reviewing 5 records instead of reading 50 articles is where the time saving actually lands |
| **3. Persistence** | Swap the filesystem for PostgreSQL + object storage; keep `run_id` + `snapshots` as the audit primitive | The state model is already serialisable and step-addressable, so this is a storage change, not a redesign |
| **4. Scheduled briefings** | A worker that re-runs a saved `QuerySpec` on a cron and diffs against the previous run, emitting *"what changed since last week"* instead of a full report | This is the product the buyer actually wants. It needs phase 3 to know what "last week" was |
| **5. Retrieval quality** | Entity resolution across sources (the same company written three ways), plus embeddings over article bodies for semantic de-duplication and cross-source corroboration | Only worth doing once volume is real; today's 20–60 documents do not justify it, and the `by_subject` concentration metric already surfaces the problem |
| **6. Multi-tenant + access** | Tenant isolation, per-tenant source credentials, row-level permissions on the report API | Compliance work that only becomes necessary at the second customer |

## What is already production-shaped

| Concern | Status |
|---|---|
| Auditability | Every run leaves an immutable `run_id` directory: trace, per-step snapshots, and a report in which every claim carries a source URL |
| Cost control | Token usage is recorded per node in `trace.jsonl`; extraction batches and trims context; a local Ollama model is a drop-in fallback |
| Failure transparency | Failed sources and recorded errors are printed in the report rather than swallowed |
| Interface stability | The REST surface (`/runs`, `/runs/{id}/report`, `/runs/{id}/trace`) is already the shape a front-end would consume |
| Deployment | Single image, one command, healthcheck included, artefacts on a mounted volume |

## What is explicitly not solved yet

- **Schema evolution on sources.** A source changing its markup breaks an adapter silently
  (the fixture-based tests are the tripwire, but they only cover the sources we have fixtures for).
- **Deduplication across languages.** The same event reported in Chinese and English by two
  sources currently counts twice.
- **Confidence calibration.** The `confidence` value comes from the model's self-report. It is
  filtered and surfaced but not yet measured against a labelled set, so it should be read as a
  ranking signal, not a probability.
