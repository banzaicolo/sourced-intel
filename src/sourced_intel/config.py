"""Runtime configuration, loaded from environment variables / `.env`.

Deliberately dependency-free: a tiny `.env` parser keeps this module importable
from `scripts/check_sources.py` before the virtualenv exists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .run_ids import InvalidRunId, validate_run_id

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Parse a `.env` file into a dict and export it into os.environ."""
    env_path = path or (PROJECT_ROOT / ".env")
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        values[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return values


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    """All tunables in one place. Instantiate via `get_settings()`."""

    llm_provider: str = "glm"
    glm_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # Verified 2026-09-18 against the live /models endpoint. This account can reach
    # only: glm-4.5, glm-4.5-air, glm-4.6, glm-4.7, glm-5, glm-5-turbo, glm-5.1,
    # glm-5.2, glm-5.3, glm-5.3-flash, glm-5.3-flashx.
    # `glm-4-flash` and `glm-4.7-flash` are NOT available - the latter was a guess
    # and returns "429 code 1305", which reads like throttling but means "no such
    # model". glm-4.7 is the default because it is the newest model that can
    # actually switch its reasoning channel off. See docs/PITFALLS.md #16/#22.
    glm_model: str = "glm-4.7"
    # Reasoning-channel policy for chat models:
    #   "off"  - send thinking={"type":"disabled"}. Every reasoning token is billed
    #            and extraction/analysis need none of them.
    #   "low"  - for models that cannot be switched off (the glm-5.x family answers
    #            1210); asks for the cheapest thinking budget via reasoning_effort.
    #   "auto" - send nothing and let the server decide.
    # The client downgrades off -> low by itself on the first 1210 it sees, so this
    # only needs setting by hand to force a mode.
    glm_thinking: str = "off"
    # Zhipu's free tier throttles per ACCOUNT. Measured on 2026-09-25, ten calls
    # per configuration, the ceiling is TWO concurrent requests: three in flight
    # completed only 4-5 of 10 calls whether the spacing was 0.0 s, 0.5 s or
    # 1.0 s, while two in flight completed 10/10 (and 12/12 with no spacing at
    # all). The interval is therefore NOT the tuning knob - inside the ceiling it
    # changes nothing measurable, and the wall clock is dominated by the
    # provider's own queueing (a request asking for eight tokens measured a p50
    # latency of 15-31 s).
    # This stays a data setting rather than a speed setting: exceeding the quota
    # costs a batch split - node 3 halves a rejected batch and retries it rather
    # than dropping it - so a run can get slower without getting wrong.
    # See docs/PITFALLS.md #32-#34.
    glm_max_concurrency: int = 2
    glm_min_interval: float = 0.5
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:4b"

    confidence_threshold: float = 0.6
    http_timeout: float = 10.0
    http_max_retries: int = 2
    node_max_retries: int = 2
    extract_batch_size: int = 8
    extract_max_concurrency: int = 4
    gdelt_min_interval: float = 6.0
    gdelt_max_records: int = 75
    # GDELT is the optional breadth source. From mainland China it frequently
    # fails at the TLS handshake, and it must never hold the run hostage: it gets
    # a short budget of its own instead of the global http_timeout.
    gdelt_timeout: float = 4.0
    # After a failure, stop re-attempting (and re-waiting) for this long. The
    # source is still reported as failed - it is skipped, never hidden.
    gdelt_failure_ttl: float = 600.0
    max_map_points: int = 200

    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    @classmethod
    def from_env(cls, dotenv: bool = True) -> Settings:
        if dotenv:
            load_dotenv()
        data_dir = _env("DATA_DIR", "./data")
        base = PROJECT_ROOT if not os.path.isabs(data_dir) else Path("/")
        path = Path(data_dir) if os.path.isabs(data_dir) else (base / data_dir).resolve()
        return cls(
            llm_provider=_env("LLM_PROVIDER", "glm").lower(),
            glm_api_key=_env("GLM_API_KEY"),
            glm_base_url=_env("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/"),
            glm_model=_env("GLM_MODEL", "glm-4.7"),
            glm_thinking=_env("GLM_THINKING", "off").lower(),
            glm_max_concurrency=_env_int("GLM_MAX_CONCURRENCY", 2),
            glm_min_interval=_env_float("GLM_MIN_INTERVAL_SECONDS", 0.5),
            ollama_base_url=_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=_env("OLLAMA_MODEL", "qwen3.5:4b"),
            confidence_threshold=_env_float("CONFIDENCE_THRESHOLD", 0.6),
            http_timeout=_env_float("HTTP_TIMEOUT_SECONDS", 10.0),
            http_max_retries=_env_int("HTTP_MAX_RETRIES", 2),
            node_max_retries=_env_int("NODE_MAX_RETRIES", 2),
            extract_batch_size=_env_int("EXTRACT_BATCH_SIZE", 8),
            extract_max_concurrency=_env_int("EXTRACT_MAX_CONCURRENCY", 4),
            gdelt_min_interval=_env_float("GDELT_MIN_INTERVAL_SECONDS", 6.0),
            gdelt_max_records=_env_int("GDELT_MAX_RECORDS", 75),
            gdelt_timeout=_env_float("GDELT_TIMEOUT_SECONDS", 4.0),
            gdelt_failure_ttl=_env_float("GDELT_FAILURE_TTL_SECONDS", 600.0),
            max_map_points=_env_int("MAX_MAP_POINTS", 200),
            data_dir=path,
        )

    # -- derived paths -----------------------------------------------------
    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def run_dir(self, run_id: str) -> Path:
        """`data/runs/<run_id>` for a validated run id.

        Every path in the system that depends on a caller-supplied id goes
        through here, so this is the one place the id has to be checked. The
        regex rejects separators and leading dots; the `resolve()` comparison is
        the second line of defence, in case `runs_dir` is reached through a
        symlink that makes a legal-looking id land somewhere else.
        """
        run_id = validate_run_id(run_id)
        candidate = self.runs_dir / run_id
        if not candidate.resolve().is_relative_to(self.runs_dir.resolve()):
            raise InvalidRunId(f"run id {run_id!r} resolves outside the runs directory")
        return candidate

    def snapshots_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "snapshots"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.runs_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)

    def redacted(self) -> dict[str, object]:
        """Safe-to-log view of the config.

        The key is reported as `set`/`unset` only. An earlier version appended
        its last four characters as a debugging aid, but `GET /health` returns
        this dict to anyone who can reach the service, and a key tail is still
        key material: it confirms a guess and narrows a search. Local scripts
        that genuinely want a mask use `llm_keys.masked_key()`.
        """
        return {
            "llm_provider": self.llm_provider,
            "glm_model": self.glm_model,
            "glm_thinking": self.glm_thinking,
            "glm_max_concurrency": self.glm_max_concurrency,
            "glm_min_interval": self.glm_min_interval,
            "glm_api_key": "<set>" if self.glm_api_key else "<unset>",
            "ollama_model": self.ollama_model,
            "confidence_threshold": self.confidence_threshold,
            "gdelt_min_interval": self.gdelt_min_interval,
            "gdelt_timeout": self.gdelt_timeout,
            "data_dir": str(self.data_dir),
        }


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings.from_env()
    return _settings


# ---------------------------------------------------------------------------
# Geographic reference data
# ---------------------------------------------------------------------------
# Small hand-curated table: (canonical name, adcode, parent city name).
# The spec explicitly forbids trying to cover all of China - 20~30 entries is
# enough for the demo, and anything unmatched degrades to text aggregation.
REGION_TABLE: list[tuple[str, str, str]] = [
    ("深圳", "440300", "深圳市"),
    # District rows repeat their own name in the display column on purpose: an
    # adcode of 440305 must render as "南山区", not "深圳市", otherwise every
    # district collapses into the same label in the report header and on the map.
    ("南山区", "440305", "南山区"),
    ("福田区", "440304", "福田区"),
    ("罗湖区", "440303", "罗湖区"),
    ("宝安区", "440306", "宝安区"),
    ("龙岗区", "440307", "龙岗区"),
    ("龙华区", "440309", "龙华区"),
    ("坪山区", "440310", "坪山区"),
    ("光明区", "440311", "光明区"),
    ("盐田区", "440308", "盐田区"),
    ("广州", "440100", "广州市"),
    ("上海", "310000", "上海市"),
    ("北京", "110000", "北京市"),
    ("杭州", "330100", "杭州市"),
    ("苏州", "320500", "苏州市"),
    ("南京", "320100", "南京市"),
    ("武汉", "420100", "武汉市"),
    ("成都", "510100", "成都市"),
    ("合肥", "340100", "合肥市"),
    ("西安", "610100", "西安市"),
    ("常州", "320400", "常州市"),
    ("无锡", "320200", "无锡市"),
    ("宁波", "330200", "宁波市"),
    ("东莞", "441900", "东莞市"),
    ("佛山", "440600", "佛山市"),
    ("青岛", "370200", "青岛市"),
    ("长沙", "430100", "长沙市"),
    ("郑州", "410100", "郑州市"),
    ("天津", "120000", "天津市"),
    ("重庆", "500000", "重庆市"),
    ("珠海", "440400", "珠海市"),
    ("厦门", "350200", "厦门市"),
]

# name -> adcode, with the "市" suffix and common variants normalised away.
REGION_TO_ADCODE: dict[str, str] = {}
REGION_DISPLAY: dict[str, str] = {}
ADCODE_TO_REGION: dict[str, str] = {}
for _name, _adcode, _display in REGION_TABLE:
    REGION_TO_ADCODE[_name] = _adcode
    REGION_DISPLAY[_adcode] = _display
    ADCODE_TO_REGION[_adcode] = _name
    if _name.endswith(("市", "区")):
        REGION_TO_ADCODE.setdefault(_name[:-1], _adcode)

DEFAULT_REGION = "深圳"
DEFAULT_ADCODE = REGION_TO_ADCODE[DEFAULT_REGION]


def match_region(text: str | None) -> str | None:
    """Return the adcode of the most specific region name found in `text`.

    Single source of truth for free-text region matching. Both
    `parse_intent.resolve_region` and `aggregate.infer_adcode` delegate here so
    the two copies can never drift apart again.

    Ranking is (administrative level, key length), highest wins:
    a district always beats a city - "南山" beats "深圳" in
    "深圳南山储能项目开工" - and a longer key beats a shorter one at the same
    level, so "南山区" beats "南山".

    The level comes from the adcode itself: its last two digits are "00" for
    cities (440300) and non-zero for districts (440305), so there is no extra
    column to keep in sync by hand.

    Returns None when nothing matches, letting callers apply their own default.
    """
    if not text:
        return None

    best_score: tuple[int, int] | None = None
    best_adcode: str | None = None
    for key, adcode in REGION_TO_ADCODE.items():
        if not key or key not in text:
            continue
        is_district = 0 if adcode.endswith("00") else 1
        score = (is_district, len(key))
        if best_score is None or score > best_score:
            best_score = score
            best_adcode = adcode
    return best_adcode
