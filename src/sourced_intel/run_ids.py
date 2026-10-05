"""Run-id generation and validation - standard library only.

Kept out of `state.py` on purpose. `config.py` turns a run id into a directory
path, and `config.py` must stay importable before the virtualenv exists (see
`scripts/preflight.py`), so the check it depends on cannot live in a module that
imports pydantic.

Why the check exists: run ids arrive from callers - `sourced-intel run --run-id`
and `POST /runs` - and are joined directly onto `data/runs`. A value like
`../escape` used to create a directory next to `data/runs` rather than inside
it, i.e. an arbitrary-directory write within the process's permissions.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

#: The only shape a run id may take: `run-`, a letter or digit, then up to 64
#: characters from letters/digits/dot/underscore/dash. A leading dot is
#: impossible, so `..` cannot be expressed, and separators are excluded, so the
#: value is always exactly one path segment.
RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class InvalidRunId(ValueError):
    """Raised when a caller-supplied run id cannot be used as a path segment."""


def validate_run_id(run_id: str) -> str:
    """Return `run_id` unchanged, or raise `InvalidRunId`.

    That admits every id `new_run_id()` produces and nothing that can traverse a
    path - neither `../escape`, nor an absolute path, nor a Windows-style
    separator.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise InvalidRunId(
            f"invalid run id {run_id!r}: expected 'run-' followed by letters, digits, "
            "'.', '_' or '-' (for example run-20260926-013012-a1b2c3); run ids are "
            "used as directory names, so this shape is enforced"
        )
    return run_id


def new_run_id(prefix: str = "run") -> str:
    """Generate an id that satisfies `RUN_ID_RE`."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"
