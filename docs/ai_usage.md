# How AI was used

The brief explicitly grades *the practical use of AI tools*. This document records the division
of labour and, more importantly, **the verification loop** — what the AI produced, and how each
piece of it was checked before it was allowed into the codebase.

---

## 1. Division of labour

| Phase | AI's job | Human's job |
|---|---|---|
| **Source audit** | Issue real HTTP requests to every candidate source; report raw status codes, byte counts and response fields; classify each source as usable / degraded / dead | Decide which sources survive, and re-check the AI's claims before trusting them |
| **Scenario & scope** | Propose the pipeline shape (7 nodes) and the state model | Cut the scope to what fits the time box; approve the node list |
| **Prompts** | Draft `01_parse_intent.md`, `03_extract.md`, `05_analyze.md`, `06_report.md` including the "never invent" clauses | Define the extraction contract: which fields matter commercially, what counts as "insufficient data" |
| **Implementation** | Write the modules, adapters, report template, tests and fixtures | Review each module against the spec; run the tests; own the acceptance criteria |
| **Verification** | Generate the fixtures and the regression tests | Insist that source claims be *measured*, not recalled |

## 2. Where the AI was wrong, and how it was caught

### Case 1 — the AI planned from the brief instead of from measurements

**What happened.** The first plan adopted the brief's source list (S1–S4) as-is, including the
government procurement portal as the "tender" source.

**How it was caught.** Before writing a single adapter, every source was probed with `curl`:

- `www.ccgp.gov.cn` resolves to `www.ccgp.gov.cn.wscvip.cn` — a **hijacked DNS record**, not
  anti-scraping. The returned page was a 2.9 KB "frequent access" interstitial.
- `deal.ggzy.gov.cn` returns **NXDOMAIN** — the domain is retired.
- Two more candidate pages returned 200 with 1–2 KB of HTML: JS-only shells with no server-rendered list.

**Correction.** The source list was rebuilt from measurements. Three of the four specified
sources were abandoned, and a source that was not in the brief at all (china5e.com) became the
primary depth source because it returns full article bodies.

**Lesson recorded as pitfall 4, 5, 6** in `PITFALLS.md`.

### Case 2 — a parameter that fails silently

**What happened.** The AI wrote the GDELT adapter using `timespan=3months`, reasoning from the
documented parameter names.

**How it was caught.** The connectivity script returned `200 OK` with a body of `{}`. An empty
JSON *object* — not an empty array, and not an error status. That asymmetry is what exposed it:
a valid query with no matches returns `{"articles": []}`.

**Correction.** The `timespan` parameter was dropped entirely. The time window is now applied
locally against `seendate`. The adapter additionally treats "200 with an empty object" as a
failure rather than as "no news exists" — and there is a regression test asserting that
`"3months"` is rejected by the timespan parser.

### Case 3 — a green-looking source that was actually unusable

**What happened.** GDELT clearly worked: real JSON, real Chinese headlines, real URLs.

**How it was caught.** Timing the requests showed roughly **one success per 20–40 seconds**, and
inspecting the payload showed only 8 fields — **no article body, no summary**.

**Correction, and the decision that followed.** This inverted the source priority: GDELT became
an optional breadth source (one attempt per run, 30-minute disk cache, graceful degradation),
and china5e became the load-bearing one. The 60-second acceptance target is only reachable
because of that reversal.

### Case 4 — a test that was red on purpose

**What happened.** The article fixture parsed perfectly except for the source attribution.

**Root cause, found by reading the raw markup rather than guessing.** The site hides the
"来源" label inside an HTML comment:

```html
<div class="showtitinfo">2026-08-25 16:07:37
  <!--来源-->
              远光软件
  <!-- &#12288; 浏览次数：<span id="hits"></span>--></div>
```

Two defects in one place: the label the regex searched for is commented out, and the comment body
contains a `<span>`, so naive tag stripping leaves a stray `-->` in the extracted text.

**Correction.** Strip comments *before* stripping tags (order matters), and fall back to
"the first non-empty line after the timestamp" for the attribution. The test that exposed it is
kept as a regression guard.

## 3. Verification techniques that were actually applied

| Technique | Applied to | Result |
|---|---|---|
| Raw request before parsing code | every source | three dead sources eliminated before a line of parsing was written |
| Format assertion, not eyeballing | GDELT payload | caught the silent-empty-object failure mode |
| Byte-count heuristics | HTML candidates | `< 2048 bytes` reliably identifies a JS-only shell |
| Timing measurement | GDELT | exposed the rate limit that reshaped the architecture |
| Verbatim fixtures from live pages | china5e list + article | made the parsing layer testable without a virtualenv |
| Dependency-free test layer | `parsing.py`, `htmlparse.py` | 58 tests run before any package was installed |
| Structural impossibility over vigilance | citation integrity | the model has no field in which to put a URL |

## 4. What the AI was *not* allowed to do

- **No unverified factual claims about APIs.** Every response shape in this repository was
  observed first. Where a claim is inferred rather than observed, it is labelled as such.
- **No invented data.** When a source is unreachable, the record is absent and the report says so.
  Nothing is synthesised to make a demo look better.
- **No silent failure.** Every degradation is written into `errors`, `failed_sources`, or the
  report's *Coverage & failures* section.
- **No dependency installation.** Environment setup is executed by the human; the AI reports
  the exact commands.

## 5. Honest summary

The AI wrote most of the code. It did not do the judging: which sources to keep, which trade-offs
to accept, what "insufficient data" means, and whether a claim was evidence-backed were all human
decisions. The AI's most valuable contributions were the **measurements** (12 live requests that
invalidated the original plan) and the **test fixtures** — not the generation.

Its most dangerous output was also the most plausible-looking: a source list that read as
authoritative because it was copied from the brief.
