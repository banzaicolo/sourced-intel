"""Node 1 - intent parsing.

Natural-language question -> validated `QuerySpec`.
On any failure the node refuses to guess: it returns a clarification request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import (
    DEFAULT_ADCODE,
    DEFAULT_REGION,
    REGION_DISPLAY,
    REGION_TO_ADCODE,
    match_region,
)
from ..llm import LLMError, get_llm
from ..prompts_loader import load_prompt
from ..schemas import GraphState, IntentParseResult, QuerySpec
from ..state import node_context

logger = logging.getLogger(__name__)

NODE_NAME = "01_parse_intent"


def resolve_region(region_text: str | None) -> tuple[str, str]:
    """Map a free-text region onto (adcode, display name).

    The ranking rule - district beats city, longer key beats shorter - lives in
    `config.match_region` so there is only one implementation of it. This
    function only adds the default fallback for unknown or empty input.
    """
    adcode = match_region(region_text)
    if adcode:
        return adcode, REGION_DISPLAY.get(adcode, adcode)
    return DEFAULT_ADCODE, REGION_DISPLAY[DEFAULT_ADCODE]


def parse_intent(state: GraphState) -> dict:
    ctx = node_context("01_parse_intent", state)
    user_query = (ctx.get("user_query") or "").strip()
    prompt = load_prompt(NODE_NAME)
    llm = get_llm()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    attempt_errors: list[str] = []
    parsed: IntentParseResult | None = None
    usage: dict = {}

    for attempt in range(1, 3):  # initial try + 1 retry, as specified
        user_prompt = prompt.render(today=today, user_query=user_query)
        if attempt_errors:
            user_prompt += (
                "\n\nYour previous answer was rejected:\n"
                + "\n".join(attempt_errors)
                + "\nReturn corrected JSON only."
            )
        try:
            response = llm.chat(
                user_prompt,
                system=prompt.system,
                temperature=0.0,
                max_tokens=512,
                json_mode=True,
                retries=1,
            )
        except LLMError as exc:
            attempt_errors.append(f"provider error: {exc}")
            continue

        usage = {
            "model": response.model,
            "provider": response.provider,
            "prompt_version": prompt.version,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.total_tokens,
            "llm_attempts": attempt,
        }
        try:
            payload = response.json_payload()
        except LLMError as exc:
            attempt_errors.append(str(exc))
            continue
        try:
            candidate = IntentParseResult.model_validate(payload)
        except Exception as exc:
            attempt_errors.append(f"schema violation: {exc}")
            continue
        if candidate.needs_clarification or not candidate.industry.strip():
            clarification = candidate.clarification_question or (
                "Please specify the industry and the region you care about."
            )
            return {
                "query": None,
                "clarification": clarification,
                "status": "needs_clarification",
                "node_meta": {NODE_NAME: usage},
            }
        parsed = candidate
        break

    if parsed is None:
        return {
            "query": None,
            "clarification": (
                "I could not turn that sentence into a search query. "
                "Please name an industry and a region, e.g. "
                "'energy storage in Shenzhen over the last 3 months'."
            ),
            "status": "needs_clarification",
            "errors": [
                {
                    "node": NODE_NAME,
                    "type": "intent_parse_failed",
                    "detail": "; ".join(attempt_errors) or "model produced no usable output",
                }
            ],
            "node_meta": {NODE_NAME: usage},
        }

    new_errors: list[dict] = []
    adcode, display = resolve_region(parsed.region)
    known_region = parsed.region.strip() in {"", DEFAULT_REGION} or any(
        key in parsed.region for key in REGION_TO_ADCODE
    )
    if not known_region:
        new_errors.append(
            {
                "node": NODE_NAME,
                "type": "region_unmapped",
                "detail": f"'{parsed.region}' not in the region table; defaulted to {display}",
            }
        )

    query = QuerySpec(
        industry=parsed.industry.strip(),
        region_adcode=adcode,
        region_name=display,
        timespan=parsed.timespan or "3m",
        intel_types=parsed.intel_types,
        keywords=parsed.keywords,
        raw_query=user_query,
    )

    return {
        "query": query,
        "clarification": None,
        "status": "running",
        "errors": new_errors,
        "node_meta": {NODE_NAME: usage},
    }
