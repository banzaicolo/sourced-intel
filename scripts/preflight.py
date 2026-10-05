#!/usr/bin/env python3
"""Pre-flight health check - standard library only, runnable before any dependency is installed.

Answers one question: *can this pipeline actually run right now, and if not, why?*

    python3 scripts/preflight.py            # human-readable
    python3 scripts/preflight.py --json     # machine-readable, for CI or another agent
    python3 scripts/preflight.py --skip-net # config + parsing only

Every check prints the raw evidence (HTTP status, byte count, error text) rather
than a bare pass/fail, so a failure can be diagnosed without re-running anything.
Exit code is 0 when nothing is broken, 1 when at least one hard check fails.

Deliberately imports NOTHING from third-party packages: `sourced_intel.config`,
`sourced_intel.parsing` and `sourced_intel.tools.htmlparse` are stdlib-only by design.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sourced_intel.config import get_settings
from sourced_intel.llm_keys import key_problem, masked_key
from sourced_intel.parsing import (
    is_noise_title,
    parse_amount,
    parse_datetime,
    reconcile_amount,
)
from sourced_intel.tools.htmlparse import parse_list_items

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_results: list[dict] = []
# Set by --json: suppress the human-readable trace so stdout is pure JSON.
_QUIET = False


def record(name: str, status: str, detail: str, hard: bool = True, evidence: str = "") -> None:
    if not _QUIET:
        icon = {PASS: "  OK  ", FAIL: " FAIL ", WARN: " WARN ", SKIP: " SKIP "}[status]
        print(f"[{icon}] {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"         {line}")
        if evidence:
            print(f"         evidence: {evidence[:300]}")
    _results.append(
        {
            "check": name,
            "status": status,
            "hard": hard,
            "detail": str(detail),
            "evidence": evidence[:500],
        }
    )


def fetch(url: str, timeout: float = 15.0, headers: dict | None = None) -> tuple[int, str, float]:
    """Return (status, body, elapsed_seconds). Never raises."""
    started = time.perf_counter()
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
            return resp.status, body, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, "", time.perf_counter() - started
    except Exception as exc:
        return 0, f"__ERROR__ {type(exc).__name__}: {exc}", time.perf_counter() - started


def post_json(
    url: str, payload: dict, headers: dict, timeout: float = 40.0
) -> tuple[int, dict | str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")[:300]
    except Exception as exc:
        return 0, f"__ERROR__ {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
def check_runtime() -> None:
    if not _QUIET:
        print("\n=== 1. runtime and configuration " + "=" * 40)
    version = sys.version.split()[0]
    ok = sys.version_info >= (3, 10)
    record(
        "python >= 3.10",
        PASS if ok else FAIL,
        f"running {version} from {sys.executable}",
    )

    settings = get_settings()
    record(
        "configuration loaded", PASS, json.dumps(settings.redacted(), ensure_ascii=False, indent=2)
    )

    data_dir = settings.data_dir
    probe: Path | None = None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # `mkstemp`, not a fixed name. A shared probe called `.preflight` raced
        # with itself the moment two checks ran at once - the test suite next to
        # a container, or two CI jobs sharing one checkout - because both
        # processes wrote the same path and whichever unlinked second raised
        # FileNotFoundError. That got recorded as a HARD failure, so a perfectly
        # writable directory was reported as broken and the exit code went to 1.
        # The question is whether the directory is writable, not whether this
        # process is the only one using it.
        fd, probe_name = tempfile.mkstemp(dir=data_dir, prefix=".preflight-", suffix=".tmp")
        probe = Path(probe_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("ok")
        record("data directory writable", PASS, str(data_dir))
    except Exception as exc:
        record("data directory writable", FAIL, f"{data_dir}: {exc}")
    finally:
        # Never leave the probe behind: a stray dotfile in the delivered tree is
        # exactly the kind of artefact the packaging rules exclude.
        #
        # A cleanup failure must NOT be silent. Swallowing it would leave a file
        # in the customer's tree and no record of why - and "it was gone when I
        # looked again" is not a diagnosis. It is reported as a warning rather
        # than a failure, because being able to WRITE is the question this check
        # asks; being unable to clean up afterwards is a different, lesser
        # problem that still deserves to be visible.
        if probe is not None:
            try:
                probe.unlink()
            except OSError as exc:
                record(
                    "preflight probe cleaned up",
                    WARN,
                    f"could not remove {probe} ({exc.__class__.__name__}: {exc})",
                    hard=False,
                )


def check_parsing() -> None:
    if not _QUIET:
        print("\n=== 2. parsing layer self-test (no network) " + "=" * 30)
    cases = [
        ("parse_amount('总投资12.5亿元')", parse_amount("总投资12.5亿元")[0], 1.25e9),
        ("parse_amount('3,300万元')", parse_amount("3,300万元")[0], 3.3e7),
        ("parse_amount('no figure here')", parse_amount("no figure here")[0], None),
        (
            "parse_datetime('2026-09-18 11:42:56')",
            parse_datetime("2026-09-18 11:42:56"),
            parse_datetime("2026-09-18 11:42:56"),
        ),
        (
            "parse_datetime('20260820T233000Z')",
            parse_datetime("20260820T233000Z"),
            parse_datetime("20260820T233000Z"),
        ),
        (
            "is_noise_title(advert)",
            is_noise_title("全行业超细粉体加工｜气流粉碎机整线解决方案厂家推荐，综合测评"),
            True,
        ),
        (
            "is_noise_title(real news)",
            is_noise_title("全球首个熔盐耦合二氧化碳发电项目在华能开工"),
            False,
        ),
    ]
    failed = [name for name, got, want in cases if got != want]
    record(
        f"parsing assertions ({len(cases) - len(failed)}/{len(cases)})",
        PASS if not failed else FAIL,
        "all deterministic helpers behave as expected" if not failed else f"failed: {failed}",
    )

    # reconciliation: the model misses the number, the source text has it
    check = reconcile_amount(None, title="总投资3.3亿元!墨玉储能项目即将投入商业运营")
    record(
        "amount reconciliation fills a missed amount",
        PASS if check.source == "regex" and check.value == 3.3e8 else FAIL,
        f"source={check.source} value={check.value} raw={check.raw!r}",
    )
    check = reconcile_amount("12.5亿元", body="项目总投资约12.5亿元，规划装机50兆瓦。")
    record(
        "amount reconciliation agrees with the model",
        PASS if check.source == "llm" and not check.conflict else FAIL,
        f"source={check.source} conflict={check.conflict}",
    )
    check = reconcile_amount("1亿元", body="项目总投资约12.5亿元。")
    record(
        "amount reconciliation flags a disagreement",
        PASS if check.conflict else FAIL,
        f"model=1亿元 code={check.code_raw!r} conflict={check.conflict}",
    )


def check_llm(settings, skip_net: bool) -> None:
    if not _QUIET:
        print("\n=== 3. language model providers " + "=" * 45)
    if skip_net:
        record("glm reachability", SKIP, "skipped (--skip-net)")
        record("ollama reachability", SKIP, "skipped (--skip-net)")
        return

    # -- GLM -----------------------------------------------------------------
    problem = key_problem(settings.glm_api_key)
    if problem:
        record("GLM_API_KEY format", FAIL, problem, hard=False)
    else:
        record("GLM_API_KEY format", PASS, masked_key(settings.glm_api_key))

    # A reasoning model bills every thinking token, and given too small a budget it
    # spends the whole thing inside the hidden channel and returns an empty
    # `content`. This probe originally sent max_tokens=8 with no thinking policy at
    # all, so it timed out against glm-4.7 even though the pipeline itself was fine:
    # the same bug had to be fixed in two places. See docs/PITFALLS.md #22.
    request_body: dict = {
        "model": settings.glm_model,
        "messages": [{"role": "user", "content": "reply with the single word: ok"}],
        "max_tokens": 200,
    }
    if settings.glm_thinking == "off":
        request_body["thinking"] = {"type": "disabled"}
    elif settings.glm_thinking == "low":
        request_body["reasoning_effort"] = "low"

    auth = {"Authorization": f"Bearer {settings.glm_api_key}"} if settings.glm_api_key else {}
    status, body = post_json(
        f"{settings.glm_base_url}/chat/completions", request_body, auth, timeout=90
    )
    if status == 200 and isinstance(body, dict):
        usage = body.get("usage", {}) or {}
        choice = (body.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if not content:
            record(
                "GLM live call",
                FAIL,
                f"empty answer: model={body.get('model')} "
                f"finish_reason={choice.get('finish_reason')!r} reasoning_tokens={reasoning}.\n"
                "The thinking channel consumed the budget, or GLM_MODEL is not a chat model.",
                evidence=str(body)[:300],
            )
        else:
            record(
                "GLM live call",
                PASS,
                f"model={body.get('model')}  thinking={settings.glm_thinking}  "
                f"tokens={usage.get('total_tokens')}\nreply={content[:40]!r}",
            )
    elif status in (401, 403):
        record(
            "GLM live call",
            FAIL,
            f"credential rejected ({status}). The pipeline will fall back to ollama.\n"
            f"Fix: re-copy the full key from <https://open.bigmodel.cn/> -> API Keys.",
            evidence=str(body),
        )
    elif status == 429:
        record(
            "GLM live call",
            WARN,
            "rate limited (429). Read the message before assuming a quota problem: "
            "Zhipu also answers 429 with code 1305 for 'no such model' "
            "(docs/PITFALLS.md #16).",
            evidence=str(body),
        )
    else:
        record("GLM live call", FAIL, f"unexpected status {status}", evidence=str(body))

    # Which models can this key actually reach? A model name that does not exist
    # fails with the misleading 429/1305 above, so compare names explicitly.
    status, body, _ = fetch(f"{settings.glm_base_url}/models", timeout=25, headers=auth)
    if status == 200:
        try:
            available = [m.get("id") for m in json.loads(body).get("data", [])]
        except Exception:
            available = []
        if not available:
            record("GLM model catalogue", WARN, "the /models endpoint returned nothing", hard=False)
        elif settings.glm_model in available:
            record(
                "GLM model catalogue",
                PASS,
                f"GLM_MODEL={settings.glm_model} is offered ({len(available)} models)",
            )
        else:
            record(
                "GLM model catalogue",
                FAIL,
                f"GLM_MODEL={settings.glm_model} is NOT offered by this key. "
                f"Choose one of: {', '.join(available)}",
            )
    else:
        record("GLM model catalogue", WARN, f"could not list models (http {status})", hard=False)

    # -- Ollama --------------------------------------------------------------
    status, body, _ = fetch(f"{settings.ollama_base_url}/api/tags", timeout=6)
    if status == 200:
        try:
            models = [m["name"] for m in json.loads(body).get("models", [])]
        except Exception:
            models = []
        has_target = any(m.startswith(settings.ollama_model.split(":")[0]) for m in models)
        record(
            "ollama reachable",
            PASS if has_target else WARN,
            f"{settings.ollama_base_url} -> {len(models)} model(s): {models}\n"
            + (
                ""
                if has_target
                else f"WARNING: configured OLLAMA_MODEL={settings.ollama_model} is not installed"
            ),
        )
    elif body.startswith("__ERROR__"):
        record("ollama reachable", WARN, f"not running ({body[:120]})", hard=False)
    else:
        record("ollama reachable", WARN, f"http {status}", hard=False)

    # -- live generation round trip -----------------------------------------
    status, body = post_json(
        f"{settings.ollama_base_url}/api/chat",
        {
            "model": settings.ollama_model,
            "messages": [{"role": "user", "content": 'Return exactly this JSON: {"ok": true}'}],
            "stream": False,
            "format": "json",
            # Without think=false a hybrid reasoning model returns an empty message.
            "think": False,
            "options": {"temperature": 0.0, "num_predict": 64},
        },
        {},
        timeout=180,
    )
    if status == 200 and isinstance(body, dict):
        content = (body.get("message") or {}).get("content", "")
        ok = bool(content.strip())
        record(
            "ollama structured generation",
            PASS if ok else FAIL,
            f"model={body.get('model')} eval_count={body.get('eval_count')}\nreply={content[:60]!r}"
            + (
                ""
                if ok
                else "\nEmpty content: check that think=false is set (see docs/PITFALLS.md #16)."
            ),
        )
    else:
        record("ollama structured generation", FAIL, f"status {status}", evidence=str(body))


def check_sources(settings, skip_net: bool) -> None:
    if not _QUIET:
        print("\n=== 4. data sources " + "=" * 56)
    if skip_net:
        record("data sources", SKIP, "skipped (--skip-net)")
        return

    status, body, elapsed = fetch("https://www.china5e.com/new-energy/stored-energy/", timeout=15)
    if status == 200 and len(body) > 5000:
        rows = parse_list_items(body, "https://www.china5e.com/new-energy/stored-energy/")
        noise = sum(1 for r in rows if is_noise_title(r.title))
        record(
            "china5e list page (primary source)",
            PASS if len(rows) >= 5 else WARN,
            f"http 200, {len(body):,} bytes, {len(rows)} usable rows "
            f"({noise} filtered as promotional) in {elapsed:.2f}s",
            evidence=rows[0].title if rows else "",
        )
    elif status == 200:
        record(
            "china5e list page (primary source)",
            WARN,
            f"http 200 but only {len(body)} bytes - markup may have changed",
        )
    else:
        record(
            "china5e list page (primary source)",
            FAIL,
            f"http {status} after {elapsed:.1f}s",
            evidence=body[:200],
        )

    status, body, elapsed = fetch(
        "https://geo.datav.aliyun.com/areas_v3/bound/440300_full.json", timeout=15
    )
    if status == 200:
        try:
            features = json.loads(body)["features"]
            record(
                "DataV GeoAtlas (map boundaries)",
                PASS if features else FAIL,
                f"http 200, {len(body):,} bytes, {len(features)} districts for adcode 440300 in {elapsed:.2f}s\n"
                + ", ".join(f["properties"]["name"] for f in features[:4]),
            )
        except Exception as exc:
            record("DataV GeoAtlas (map boundaries)", FAIL, f"unparseable payload: {exc}")
    else:
        record("DataV GeoAtlas (map boundaries)", FAIL, f"http {status}", evidence=body[:200])

    # GDELT is optional by design - it is rate limited hard and headline-only.
    status, body, elapsed = fetch(
        "https://api.gdeltproject.org/api/v2/doc/doc?query=energy&mode=artlist&format=json&maxrecords=3",
        timeout=20,
    )
    if status == 200:
        try:
            n = len(json.loads(body).get("articles", []))
        except Exception:
            n = 0
        record(
            "GDELT optional breadth source",
            PASS if n else WARN,
            f"http 200, {n} articles in {elapsed:.1f}s",
            hard=False,
        )
    else:
        record(
            "GDELT optional breadth source",
            WARN,
            f"http {status} in {elapsed:.1f}s - expected; GDELT is heavily rate limited "
            "and headline-only, so it is not on the critical path (see docs/PITFALLS.md #2).",
            hard=False,
        )


def check_services(settings) -> None:
    if not _QUIET:
        print("\n=== 5. local ports " + "=" * 58)
    for label, host, port in (("ollama", "127.0.0.1", 11434),):
        try:
            with socket.create_connection((host, port), timeout=2):
                record(f"{label} port {port} open", PASS, f"{host}:{port}")
        except OSError as exc:
            record(f"{label} port {port} open", WARN, f"{host}:{port} refused ({exc})", hard=False)


def main() -> int:
    global _QUIET
    parser = argparse.ArgumentParser(description="Sourced Intel pre-flight check")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON only (no human-readable trace)",
    )
    parser.add_argument("--skip-net", action="store_true", help="skip every network check")
    args = parser.parse_args()

    _QUIET = args.json
    if not args.json:
        print("Sourced Intel - pre-flight check")
        print(f"project root: {ROOT}")

    settings = get_settings()
    check_runtime()
    check_parsing()
    check_llm(settings, args.skip_net)
    check_sources(settings, args.skip_net)
    check_services(settings)

    hard_failures = [r for r in _results if r["hard"] and r["status"] == FAIL]
    warnings = [r for r in _results if r["status"] in (WARN, SKIP)]

    if args.json:
        print(
            json.dumps(
                {"results": _results, "hard_failures": len(hard_failures)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("\n" + "=" * 78)
        print(
            f"summary: {len(_results) - len(hard_failures) - len(warnings)} passed, "
            f"{len(warnings)} warnings/skipped, {len(hard_failures)} hard failures"
        )
        if hard_failures:
            print("\nblocking issues:")
            for item in hard_failures:
                print(f"  - {item['check']}: {item['detail'].splitlines()[0]}")
        else:
            print("\nNothing blocking. Next: install the dependencies, then run the test suite.")
            print(
                "  macOS/Linux : python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
            )
            print(
                "  Windows     : python -m venv .venv && .venv\\Scripts\\activate && pip install -r requirements.txt"
            )
            print(
                "  (Windows has no `make` - the README's 'Option B - local' section lists the same steps)"
            )
        print("=" * 78)

    return 1 if hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
