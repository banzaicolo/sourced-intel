"""Allow `python -m sourced_intel ...` without installing the console script."""

from .cli import main

raise SystemExit(main())
