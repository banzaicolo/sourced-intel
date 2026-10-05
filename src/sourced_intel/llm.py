"""LLM access layer with two interchangeable providers.

  - `glm`    : Zhipu GLM over its OpenAI-compatible REST endpoint (default)
  - `ollama` : a local model, used as the offline fallback

Both return the same `LLMResponse`, so no node needs to know which one is live.
Token usage is always reported so cost can be traced per node.

Failure taxonomy (verified against the live APIs on 2026-09-18):

  LLMAuthError  - 401/403. The credential is wrong. NEVER retried: retrying a bad
                  key just wastes three round trips and hides the real problem.
  LLMRateLimit  - 429. Retried with exponential backoff, honouring Retry-After.
                  Measured on 2026-09-25 against the free tier: it admits TWO
                  concurrent requests. A third is rejected in every variant
                  tested - 3 concurrent passed only 4-5 of 10 calls whether the
                  spacing was 0.0 s, 0.5 s or 1.0 s, and 4 concurrent did no
                  better. Spacing inside the ceiling is not load bearing: at 2
                  concurrent, 0.0 s and 0.5 s both passed 12/12. What dominates
                  wall time is server-side queueing, not our pacing - a request
                  that asks for eight tokens showed a p50 latency of 15-31 s.
  LLMError      - everything else, retried per the configured budget.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings, get_settings
from .llm_keys import key_problem
from .parsing import JsonExtractionError
from .parsing import extract_json as _extract_json

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when a provider fails after exhausting retries."""


class LLMAuthError(LLMError):
    """Credential rejected. Permanent - never retried."""


class LLMRateLimit(LLMError):
    """Quota / concurrency limit hit. Retried with a longer backoff."""


# Credential shape checking lives in `llm_keys.py` (stdlib only) so that
# `scripts/preflight.py` can use it before the virtualenv exists.


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    def json_payload(self) -> Any:
        """Parse the response as JSON, tolerating code fences and prose."""
        try:
            return _extract_json(self.text)
        except JsonExtractionError as exc:
            raise LLMError(str(exc)) from exc


def extract_json(text: str) -> Any:
    """Re-export of the stdlib-only JSON recovery helper."""
    try:
        return _extract_json(text)
    except JsonExtractionError as exc:
        raise LLMError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class ProviderPacer:
    """Process-wide concurrency and pacing guard for one provider.

    Regression this exists for: `GLM_MAX_CONCURRENCY` and
    `GLM_MIN_INTERVAL_SECONDS` were declared in `Settings`, written into
    `.env.example` and described in the config comments as "the pipeline
    serialises GLM calls by default" - while nothing in the code ever read them.
    Node 3 runs its batches through a `ThreadPoolExecutor` sized by
    `EXTRACT_MAX_CONCURRENCY` (4 by default), so four GLM requests left at once
    with no spacing. The free tier answers ``429 code 1302`` to that, and at the
    time a 429 abandoned the whole batch: one real run lost 2 of 5 batches and
    the report dropped from 15 records to 7.

    The batch loss is fixed separately (`extract._split_and_retry` halves a
    rejected batch), so the pacer is now a *cost* control rather than the only
    thing standing between a throttle and missing data. Both were needed: the
    pacer stops the 429 firing, the split stops it hurting.

    Same failure class as the GDELT interval knob - a documented setting that
    silently does nothing. See docs/PITFALLS.md #32-#34.
    """

    def __init__(self, concurrency: int, min_interval: float) -> None:
        self._slots = threading.Semaphore(max(1, int(concurrency)))
        self._lock = threading.Lock()
        self._min_interval = max(0.0, float(min_interval))
        self._last_start = 0.0

    @property
    def min_interval(self) -> float:
        return self._min_interval

    @contextmanager
    def slot(self):
        """Hold one in-flight slot for the duration of the request.

        The calling provider must release this before any recursive retry, or a
        single-slot pacer would deadlock against itself.
        """
        self._slots.acquire()
        try:
            with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last_start)
                if wait > 0:
                    time.sleep(wait)
                self._last_start = time.monotonic()
            yield
        finally:
            self._slots.release()


class BaseLLM:
    provider = "base"
    supports_json_mode = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model = ""

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
        retries: int | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    # -- shared retry wrapper ---------------------------------------------
    def _with_retries(self, fn, retries: int | None):
        """Run `fn(attempt)`, retrying transient failures only.

        Auth failures abort immediately - see the module docstring.
        """
        attempts = (self.settings.http_max_retries if retries is None else retries) + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return fn(attempt)
            except LLMAuthError:
                raise
            except LLMRateLimit as exc:
                last_exc = exc
                if attempt < attempts:
                    # Rate limits recover on a different timescale to transient
                    # socket errors, so the backoff is deliberately longer.
                    delay = max(getattr(exc, "retry_after", 0.0) or 0.0, min(4 * 2**attempt, 30))
                    logger.info("%s rate limited, sleeping %.1fs", self.provider, delay)
                    time.sleep(delay)
            except Exception as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(min(2**attempt, 8))
        raise LLMError(f"{self.provider} failed after {attempts} attempts: {last_exc}")

    def ping(self) -> tuple[bool, str]:
        """Cheap liveness probe used by `scripts/preflight.py`. Never raises."""
        try:
            resp = self.chat("ping", max_tokens=4, retries=0)
        except LLMError as exc:
            return False, str(exc)
        return True, f"{resp.model} replied in {resp.duration_ms:.0f} ms"


class GLMClient(BaseLLM):
    """Zhipu GLM via the OpenAI-compatible /chat/completions endpoint.

    Reasoning-channel handling (verified against the live API on 2026-09-18):

      * `glm-4.7` / `glm-4.5-air` accept ``thinking={"type":"disabled"}``. With the
        channel closed a one-line answer costs ~10 tokens instead of 130, which
        matters because every reasoning token is billed.
      * `glm-5.3-flashx` and the wider glm-5.x family CANNOT be switched off:
        Zhipu answers ``1210 该模型始终思考，不支持关闭思考``. The error text then says
        "please use low, high or max", which reads like a ``thinking.type`` value
        but is actually the ``reasoning_effort`` parameter - sending
        ``thinking={"type":"low"}`` fails with the very same 1210.
      * A reasoning model that runs out of ``max_tokens`` mid-thought returns an
        EMPTY ``content`` string with only ``reasoning_tokens`` billed. That looks
        identical to a broken connection, so we detect it explicitly.

    So `off` is the default, and a 1210 downgrades this client to `low` for the
    rest of the run, granting extra token headroom for the hidden thinking.
    """

    provider = "glm"

    #: Zhipu error code for "this model always thinks".
    ERR_ALWAYS_THINKS = "1210"

    #: How much extra completion budget to grant once thinking is forced on.
    _HEADROOM_WHEN_THINKING = 3.0

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.model = self.settings.glm_model
        problem = key_problem(self.settings.glm_api_key)
        if problem:
            raise LLMAuthError(problem)
        self._thinking = self.settings.glm_thinking or "off"
        self._token_headroom = 1.0
        # One pacer per client, and there is exactly one client per process
        # (`get_llm` caches it), so this is process-wide in practice.
        self._pacer = ProviderPacer(
            self.settings.glm_max_concurrency, self.settings.glm_min_interval
        )
        self._client = httpx.Client(
            timeout=httpx.Timeout(90.0, connect=10.0),
            headers={
                "Authorization": f"Bearer {self.settings.glm_api_key}",
                "Content-Type": "application/json",
            },
        )

    # -- reasoning channel -------------------------------------------------
    def _thinking_fields(self) -> dict[str, Any]:
        if self._thinking == "off":
            return {"thinking": {"type": "disabled"}}
        if self._thinking == "low":
            return {"reasoning_effort": "low"}
        return {}  # "auto" - let the server decide

    def _downgrade_thinking(self, detail: str) -> None:
        """Called once, on the first 1210, to stop fighting the server."""
        if self._thinking == "off":
            logger.warning(
                "GLM model %s always thinks (1210) - switching to reasoning_effort=low "
                "and granting %.0fx completion headroom. %s",
                self.model,
                self._HEADROOM_WHEN_THINKING,
                detail[:160],
            )
            self._thinking = "low"
            self._token_headroom = self._HEADROOM_WHEN_THINKING

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
        retries: int | None = None,
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def build_payload(use_json_mode: bool) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max(int(max_tokens * self._token_headroom), max_tokens),
            }
            payload.update(self._thinking_fields())
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}
            return payload

        state = {"json_mode": json_mode}

        def call(attempt: int) -> LLMResponse:
            started = time.perf_counter()
            # The slot is released before any error handling below, because two
            # of those branches recurse into `call` - holding the only slot while
            # doing so would deadlock.
            with self._pacer.slot():
                resp = self._client.post(
                    f"{self.settings.glm_base_url}/chat/completions",
                    json=build_payload(state["json_mode"]),
                )
            elapsed = (time.perf_counter() - started) * 1000

            if resp.status_code in (401, 403):
                raise LLMAuthError(
                    f"GLM rejected the credential ({resp.status_code}): {resp.text[:200]}. "
                    "Check GLM_API_KEY in .env."
                )
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After") or 0) or 0.0
                exc = LLMRateLimit(f"GLM rate limited (429): {resp.text[:200]}")
                exc.retry_after = retry_after  # type: ignore[attr-defined]
                raise exc
            if resp.status_code >= 500:
                raise LLMError(f"GLM http {resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                body = resp.text[:400]
                # Not every GLM model accepts response_format; drop it once and
                # let the JSON-recovery helper cope with a prose answer.
                if state["json_mode"] and "response_format" in body:
                    logger.info("GLM model %s rejected json mode - retrying without it", self.model)
                    state["json_mode"] = False
                    return call(attempt)
                if self.ERR_ALWAYS_THINKS in body and self._thinking == "off":
                    self._downgrade_thinking(body)
                    return call(attempt)
                raise LLMError(f"GLM http {resp.status_code}: {body}")

            data = resp.json()
            usage = data.get("usage", {}) or {}
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = (message.get("content") or "").strip()
            reasoning_tokens = (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ) or 0

            if not content:
                # Two very different causes, and conflating them costs hours:
                #   (a) the hidden reasoning channel ate the whole budget, so the
                #       answer was never emitted - fix by raising max_tokens;
                #   (b) a genuinely empty body.
                if reasoning_tokens or message.get("reasoning_content"):
                    raise LLMError(
                        f"GLM returned no content but billed {reasoning_tokens} reasoning "
                        f"tokens (finish_reason={choice.get('finish_reason')!r}). The model "
                        f"spent its max_tokens={max_tokens} budget thinking. Raise "
                        "max_tokens or set GLM_THINKING=low for a cheaper thinking budget."
                    )
                raise LLMError(f"GLM returned an empty message: {str(data)[:200]}")

            return LLMResponse(
                text=content,
                model=data.get("model", self.model),
                provider=self.provider,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                duration_ms=round(elapsed, 1),
                attempts=attempt,
                raw=data,
            )

        return self._with_retries(call, retries)


class OllamaClient(BaseLLM):
    """Local Ollama fallback - used when there is no network or no budget."""

    provider = "ollama"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.model = self.settings.ollama_model
        self._client = httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0))

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
        retries: int | None = None,
    ) -> LLMResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # VERIFIED 2026-09-18: qwen3.5:4b is a hybrid-reasoning model. Without
            # `think: false` it spends the whole num_predict budget inside the
            # hidden reasoning channel and returns an EMPTY content string, which
            # surfaces as "empty response" from the JSON helper. Disabling the
            # reasoning channel is what makes local extraction work at all.
            "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"

        def call(attempt: int) -> LLMResponse:
            started = time.perf_counter()
            resp = self._client.post(f"{self.settings.ollama_base_url}/api/chat", json=payload)
            elapsed = (time.perf_counter() - started) * 1000
            if resp.status_code >= 400:
                raise LLMError(f"ollama http {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            done_reason = data.get("done_reason")
            if not content.strip():
                # Distinguish "the model produced nothing" from "the connection
                # broke", so the retry logic and the error report stay honest.
                raise LLMError(
                    f"ollama returned an empty message (done_reason={done_reason}, "
                    f"eval_count={data.get('eval_count')}). "
                    "If this is a reasoning model, make sure think=false is set."
                )
            return LLMResponse(
                text=content,
                model=data.get("model", self.model),
                provider=self.provider,
                prompt_tokens=data.get("prompt_eval_count", 0),
                completion_tokens=data.get("eval_count", 0),
                total_tokens=(data.get("prompt_eval_count", 0) + data.get("eval_count", 0)),
                duration_ms=round(elapsed, 1),
                attempts=attempt,
                raw=data,
            )

        return self._with_retries(call, retries)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_client: BaseLLM | None = None
_fallback_reason: str | None = None


def get_llm(settings: Settings | None = None, reload: bool = False) -> BaseLLM:
    """Return the configured provider, falling back to Ollama if GLM is unusable."""
    global _client, _fallback_reason
    if _client is not None and not reload:
        return _client
    settings = settings or get_settings()
    provider = settings.llm_provider

    if provider == "glm":
        try:
            _client = GLMClient(settings)
            _fallback_reason = None
            return _client
        except LLMAuthError as exc:
            # An unusable credential is a configuration error, not a crash: report
            # it and keep the pipeline running on the local model.
            _fallback_reason = str(exc)
            logger.warning("GLM unusable, falling back to ollama: %s", exc)

    _client = OllamaClient(settings)
    return _client


def last_fallback_reason() -> str | None:
    """Why the factory did not return GLM, if it didn't."""
    return _fallback_reason


def reset_llm() -> None:
    global _client, _fallback_reason
    _client = None
    _fallback_reason = None
