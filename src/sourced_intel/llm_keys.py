"""API-key sanity checks - standard library only.

Kept apart from `llm.py` on purpose: `llm.py` imports httpx, but the credential
check must be available to `scripts/preflight.py` before the virtualenv exists.

--------------------------------------------------------------------------------
Why this exists (VERIFIED 2026-09-18)
--------------------------------------------------------------------------------
A wrong GLM key produces:

    {"error":{"code":"401","message":"令牌已过期或验证不正确"}}

"token expired or incorrect" sends you looking for an expired key. The far more
common cause is a **truncated paste**. Zhipu's documented key shape is

    <32-char hex id>.<16-char secret>          e.g. a1b2...9f0a.BcDeFgHiJkLmNoPq

The console lists a 32-character key **id** next to the real key, and copying only
that id yields exactly this 401. We hit that case ourselves: a bare 32-hex string
was rejected by all four of `Authorization: Bearer`, bare `Authorization`,
`x-api-key`, and the international endpoint.

--------------------------------------------------------------------------------
Design rule: only reject what we have confirmed is wrong
--------------------------------------------------------------------------------
Sources disagree on whether every key is `id.secret` (the trufflehog detector and
the platform docs) or whether some accounts get an `sk-` prefixed key. Rather than
encode a guess, this module flags **one** confidently-diagnosed failure mode - a
bare 32-hex key id - and leaves every other shape to the API. A false "your key is
malformed" on a perfectly good credential is worse than no check at all.
"""

from __future__ import annotations

import re

# The confirmed truncation signature: a 32-character hex string with no dot.
# This is a key *id*, never a whole key.
BARE_KEY_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# The documented accepted shape, used for informational output only.
ZHIPU_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}\.[A-Za-z0-9]{16}$")
SK_PREFIX_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$")

CONSOLE_HINT = "re-copy the full value from <https://open.bigmodel.cn/> -> API Keys"


def is_bare_key_id(key: str) -> bool:
    """True for the confirmed truncation case: 32 hex chars and nothing else."""
    return bool(BARE_KEY_ID_RE.match((key or "").strip()))


def looks_like_zhipu_key(key: str) -> bool:
    """True when the shape matches a documented Zhipu key. Informational only."""
    key = (key or "").strip()
    return bool(ZHIPU_KEY_RE.match(key) or SK_PREFIX_RE.match(key))


def key_problem(key: str) -> str | None:
    """Return a human-readable explanation of a *confirmed* problem, else None."""
    key = (key or "").strip()
    if not key:
        return "GLM_API_KEY is empty. Set it in .env (see .env.example)."
    if any(ch.isspace() for ch in key):
        return "GLM_API_KEY contains an internal line break or space - it was pasted badly."
    if is_bare_key_id(key):
        return (
            f"GLM_API_KEY is only {len(key)} hex characters with no dot, which is the "
            "key *id* shown next to the key in the console - not the key. A Zhipu key "
            "is '<32-hex id>.<16-char secret>', so the part after the dot is missing. "
            f"Fix: {CONSOLE_HINT}."
        )
    # Any other shape is allowed through: the API is the authority.
    return None


def masked_key(key: str) -> str:
    """A display mask for local diagnostics only: `***<last 4>`.

    This is deliberately *not* what `Settings.redacted()` returns. That dict is
    served by `GET /health` to anyone who can reach the service, so it reports
    `set`/`unset` and nothing else. A human at a terminal checking "did I paste
    the right key?" is a different audience, and gets a tail.
    """
    key = (key or "").strip()
    if not key:
        return "<unset>"
    if len(key) < 8:
        return "***"
    return f"***{key[-4:]}"
