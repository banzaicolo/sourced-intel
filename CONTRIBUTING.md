# Contributing

Thanks for looking. This is a portfolio project, so it is maintained in the open rather
than actively solicited — but issues and pull requests are welcome, and this file says what
a good one looks like here.

## Getting set up

```bash
git clone <your-fork> && cd sourced-intel
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -m "not slow" -q        # 230 tests, ~3 s, no network, no credential
```

You do **not** need an API key to work on most of this repo. The offline suite covers the
pipeline nodes, the report contract, the demo fixture and the CLI; only the single
`slow`-marked test needs a live Zhipu credential.

Two diagnostics help when something is wrong before you have installed anything:

```bash
python3 scripts/preflight.py            # env, config, credential, every data source
python3 scripts/rehearse_glm.py --docs 3   # the real chain, standard library only
```

## What this project cares about

Ranked, because trade-offs are real:

1. **Correctness over speed.** A run that finishes in 40 s by dropping a batch is worse than
   one that takes 90 s and finishes. If a change makes things faster, check what it stopped
   doing. (Two entries in `docs/PITFALLS.md` exist because this was got wrong first.)
2. **No fabricated numbers, ever.** Every count, sum and share is computed in
   `nodes/aggregate.py`. If you find yourself passing a figure through the model, stop.
3. **Disclosed failure over hidden success.** A degraded run is fine. A run that reports
   `completed` while having quietly dropped a third of its input is not.
4. **Few dependencies.** HTML parsing, amount/date parsing, aggregation, the map and the
   chart are all standard library or inline SVG. That is not minimalism for its own sake:
   it is why the parsing layer could be unit-tested before a virtualenv existed.

## Adding a data source

Source adapters live in `src/sourced_intel/tools/sources/` and implement the small interface
in `base.py`. A new source is welcome if it satisfies all of these:

- **You have probed it with a real request** and can paste the raw response fields into the
  pull request. Three of the sources in the original brief were dead or JS-only; claiming a
  source works is not evidence that it does.
- It degrades rather than raising. `safe_fetch` exists so one dead source cannot fail a run.
- It declares its own pacing. GDELT rate-limits shared IPs to ~1 success per 20–40 s and
  says so in `config.py`.
- It has a fixture from a live page under `tests/fixtures/`, and a parser test against it.

## Tests

- Every behavioural change needs a test that fails without it. Please check that it does —
  a test that passes both ways is a comment with extra steps.
- Report tests go in `tests/test_report_contract.py`. The template is allowed to change its
  markup freely; the **numbers, section ordering, citation registry and escaping** are the
  contract.
- Tests that touch the network or a live credential must carry `@pytest.mark.slow`.
  CI runs `-m "not slow"`, so an unmarked network test turns the build red on purpose.
- Do not weaken an assertion to make it pass. If an assertion is wrong, say why in the
  commit message and change it to test the thing that actually matters.

## Documentation

`docs/PITFALLS.md` is the most valuable file in the repo. An entry belongs there when you
have **observed** something surprising — with a reproduction command — not when you suspect
it. Mark each entry `[Measured]` or `[Inferred]` and keep the distinction honest; a number
that came from a single run is an anecdote, and saying so is the point.

## Commits and pull requests

- One logical change per commit. Conventional-commit prefixes (`feat:`, `fix:`, `docs:`,
  `test:`, `chore:`) are used in the history and are appreciated but not enforced.
- In the PR description, say **what you measured**, not just what you changed. "Halved the
  batch on 429; ten runs per configuration, 3-in-flight went from 4/10 to 10/10" is the
  standard this repo tries to hold.

## Code style

`ruff` is configured in `pyproject.toml` (`make lint`). It is not a CI gate — see the
comment in `.github/workflows/ci.yml` for why — so please run it locally if you can.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
