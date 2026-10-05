#!/usr/bin/env python3
"""Render the bundled sample report without installing the package.

    python3 scripts/demo.py
    python3 scripts/demo.py --out /tmp/sample.html

`pip install -e .` is the normal way to get the `sourced-intel` command, but it
needs a network to fetch a build backend and it writes into a virtualenv. This
shim exists so the very first thing a visitor tries - *what does the output
actually look like?* - works from a bare clone with only the runtime
requirements installed and nothing else:

    pip install -r requirements.txt
    python3 scripts/demo.py

It reaches the same code path as `sourced-intel demo`; the only thing it adds is
`src/` on `sys.path`.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sourced_intel.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["demo", *sys.argv[1:]]))
