---
name: Bug report
about: Something rendered wrong, crashed, or produced a number that cannot be true
title: "[Bug] "
labels: bug
assignees: ""
---

## What happened

<!-- One or two sentences. What did you run, what did you expect, what did you get. -->

## To reproduce

<!-- The exact command or query. Include the region and time window. -->

```bash
sourced-intel run "…"
```

## What the report shows vs. what it should show

<!--
This project's hard rule: every number in the report must be computed by code,
never by the model. If a figure is wrong, say which section, which number, and
what the source data actually says.
-->

## Environment

- Python version: `python3 --version`
- Install: `pip install -e .` / Docker
- Region and adcode if relevant:
- Attach the report file if it rendered at all (drag `.html` or `.md` here).
