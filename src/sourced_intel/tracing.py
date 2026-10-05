"""Lightweight JSONL tracer.

One line per node execution, so the whole run can be replayed and timed without
pulling in a heavyweight observability stack.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Tracer:
    """Appends one JSON object per node to `trace.jsonl`."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "trace.jsonl"
        self._records: list[dict[str, Any]] = []

    def record(
        self,
        node: str,
        *,
        duration_ms: float,
        status: str = "ok",
        model: str | None = None,
        prompt_version: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        retries: int = 0,
        error: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "node": node,
            "started_at": _utc_iso(),
            "duration_ms": round(duration_ms, 1),
            "status": status,
            "model": model,
            "prompt_version": prompt_version,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "retries": retries,
            "error": error,
        }
        entry.update(extra)
        self._records.append(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    @contextmanager
    def span(self, node: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Time a node and record the result, even if it raises."""
        started = time.perf_counter()
        ctx: dict[str, Any] = {}
        try:
            yield ctx
        except Exception as exc:
            self.record(
                node,
                duration_ms=(time.perf_counter() - started) * 1000,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
                **{**kwargs, **ctx},
            )
            raise
        else:
            self.record(
                node,
                duration_ms=(time.perf_counter() - started) * 1000,
                status=ctx.pop("status", "ok"),
                **{**kwargs, **ctx},
            )

    def load_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def summary(self) -> dict[str, Any]:
        records = self._records or self.load_records()
        total = sum(r.get("duration_ms", 0) for r in records)
        tokens = sum((r.get("total_tokens") or 0) for r in records)
        return {
            "nodes_executed": len(records),
            "total_duration_ms": round(total, 1),
            "total_tokens": tokens,
            "errors": sum(1 for r in records if r.get("status") == "error"),
            "slowest_node": max(records, key=lambda r: r.get("duration_ms", 0))["node"]
            if records
            else None,
        }
