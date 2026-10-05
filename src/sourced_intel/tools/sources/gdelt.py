"""GDELT DOC 2.0 adapter - the breadth source.

Verified against the live API on 2026-09-18:
  - response shape      : {"articles": [ ... ]}
  - article fields      : url, url_mobile, title, seendate, socialimage,
                          domain, language, sourcecountry
  - NO body, NO summary : artlist mode returns headlines only
  - hard rate limit     : ~1 request per 20-40s from a shared egress IP, else 429
  - `timespan=3months`  : silently yields an empty {} - filter by date locally

Because of the rate limit every response is cached on disk and the adapter makes
at most one live attempt per run. A cache miss that ends in 429 degrades to "no
GDELT documents" instead of failing the pipeline.

Two budgets keep this optional source from slowing the run down:

  - `GDELT_TIMEOUT_SECONDS` (default 4s) instead of the global HTTP timeout. From
    mainland China this host often dies in the TLS handshake rather than
    answering 429, and waiting the full 10s on every run is pure dead time.
  - a failure marker with a TTL. After one failure the adapter short-circuits for
    `GDELT_FAILURE_TTL_SECONDS` (default 10 min). The source is still reported as
    FAILED with the original reason - it is skipped, never hidden.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from ...config import get_settings
from ...parsing import cutoff_from_timespan, normalise_url
from ...schemas import QuerySpec, RawDoc, parse_datetime
from ..http import get_http
from .base import SourceAdapter

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
CACHE_TTL_SECONDS = 1800  # 30 min: a demo should never wait on GDELT twice


class GdeltAdapter(SourceAdapter):
    name = "gdelt"
    description = "GDELT DOC 2.0 global news index (headlines only, no article body)"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.http = get_http()
        self.cache_dir = self.settings.cache_dir / "gdelt"

    # -- query building ----------------------------------------------------
    @staticmethod
    def build_query(query: QuerySpec) -> str:
        """Compose a GDELT query string from the resolved terms.

        GDELT treats a bare space as AND, so multi-word terms are quoted and the
        variants are OR-ed together.
        """
        parts: list[str] = []
        for term in query.search_terms[:4]:
            term = term.strip()
            if not term:
                continue
            parts.append(f'"{term}"' if " " in term else term)
        if not parts:
            return query.industry or "energy"
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

    # -- cache -------------------------------------------------------------
    def _cache_path(self, key: str):
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, key: str) -> dict | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, payload: dict) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(key).write_text(
                json.dumps(
                    {"cached_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
                ),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - cache is best effort
            logger.warning("gdelt cache write failed: %s", exc)

    # -- failure marker ----------------------------------------------------
    def _failure_path(self, key: str):
        return self.cache_dir / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.failed"

    def _recent_failure(self, key: str) -> bool:
        path = self._failure_path(key)
        if not path.exists():
            return False
        return time.time() - path.stat().st_mtime <= self.settings.gdelt_failure_ttl

    def _mark_failure(self, key: str) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._failure_path(key).touch()
        except OSError:  # pragma: no cover - marker is best effort
            pass

    def _clear_failure(self, key: str) -> None:
        with contextlib.suppress(OSError):  # pragma: no cover
            self._failure_path(key).unlink(missing_ok=True)

    # -- public API --------------------------------------------------------
    def fetch(self, query: QuerySpec, limit: int = 50) -> list[RawDoc]:
        q = self.build_query(query)

        # Short-circuit a source we already know is unreachable. The run finishes
        # faster and the failure is still reported - just without the wait.
        if self._recent_failure(q):
            raise RuntimeError(
                f"gdelt unreachable (cached failure, retried after "
                f"{self.settings.gdelt_failure_ttl:.0f}s)"
            )

        params = {
            "query": q,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(min(max(limit, 10), 250)),
        }
        # NOTE: `timespan` is intentionally absent - it produced empty results in
        # testing. The window is applied locally against `seendate`.
        result = self.http.get(
            ENDPOINT,
            params=params,
            retries=0,
            timeout=self.settings.gdelt_timeout,
            min_interval=self.settings.gdelt_min_interval,
        )

        payload: dict = {}
        if result.ok:
            try:
                payload = json.loads(result.text)
            except json.JSONDecodeError:
                logger.warning("gdelt returned non-JSON payload (%s bytes)", len(result.text))
                payload = {}

        # A 200 response carrying an empty object means the request was rejected
        # in practice (bad parameter, silent rate limit) - treat it as a failure.
        live_ok = bool(payload.get("articles"))
        if live_ok:
            self._write_cache(q, payload)
            self._clear_failure(q)
        else:
            cached = self._read_cache(q)
            if cached:
                logger.info(
                    "gdelt live fetch unusable (%s) - serving from cache", result.error or "empty"
                )
                payload = cached.get("payload", {})
            elif not result.ok:
                self._mark_failure(q)
                raise RuntimeError(f"gdelt unavailable: {result.error}")
            else:
                logger.warning("gdelt returned an empty result set for query=%s", q)
                payload = {}

        cutoff = cutoff_from_timespan(query.timespan)
        docs: list[RawDoc] = []
        for article in payload.get("articles", []) or []:
            url = normalise_url(article.get("url") or "")
            title = (article.get("title") or "").strip()
            if not url or not title:
                continue
            published_at = parse_datetime(article.get("seendate"))
            if cutoff and published_at and published_at < cutoff:
                continue
            docs.append(
                RawDoc(
                    source=self.name,
                    url=url,
                    title=title,
                    published_at=published_at,
                    raw_text="",  # GDELT artlist gives no body - extraction uses the title
                    domain=article.get("domain"),
                    language=article.get("language"),
                )
            )
        logger.info("gdelt returned %d documents (query=%s)", len(docs), q)
        return docs[:limit]
