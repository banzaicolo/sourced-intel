"""Source adapter contract.

Every source implements the same `fetch()` signature so node 2 can fan out over
them with `asyncio.gather(..., return_exceptions=True)` and degrade gracefully
when one adapter fails.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ...schemas import QuerySpec, RawDoc

logger = logging.getLogger(__name__)


class SourceAdapter(ABC):
    """Base class for all retrieval sources."""

    name: str = "base"
    #: Human-readable description used in the report's "sources" section.
    description: str = ""

    @abstractmethod
    def fetch(self, query: QuerySpec, limit: int = 50) -> list[RawDoc]:
        """Return documents matching `query`. Must never raise for HTTP reasons."""
        raise NotImplementedError

    # -- helpers shared by adapters ---------------------------------------
    @staticmethod
    def _normalise(url: str) -> str:
        return url.split("#", 1)[0].strip()

    def safe_fetch(self, query: QuerySpec, limit: int = 50) -> tuple[list[RawDoc], str | None]:
        """`fetch` that never raises: returns (docs, error_message)."""
        try:
            docs = self.fetch(query, limit=limit)
            return docs, None
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("source %s failed: %s", self.name, message)
            return [], message
