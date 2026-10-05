"""Prompt file loader.

Prompts live in `prompts/*.md` as versioned deliverables, so the pipeline reads
them from disk rather than embedding strings in code. Each file exposes a
`## System` block and a `## User template` block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_VERSION_RE = re.compile(r"\*\*Version:\*\*\s*`?([^`\n]+)`?")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    system: str
    user_template: str
    path: Path

    def render(self, **values: object) -> str:
        """Fill the user template. Unknown placeholders are left untouched."""

        class _Safe(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        return self.user_template.format_map(_Safe(values))


def _split_sections(text: str) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip().lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[title] = text[start:end].strip()
    return sections


@lru_cache(maxsize=32)
def load_prompt(name: str) -> PromptTemplate:
    """Load `prompts/{name}.md`. Raises FileNotFoundError when missing."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt file not found: {path}")
    text = path.read_text(encoding="utf-8")
    version_match = _VERSION_RE.search(text)
    sections = _split_sections(text)

    system = sections.get("system", "")
    user_template = ""
    for key, value in sections.items():
        if key.startswith("user template"):
            user_template = value
            break

    if not system:
        raise ValueError(f"prompt {name} has no '## System' section")

    return PromptTemplate(
        name=name,
        version=(version_match.group(1).strip() if version_match else "unversioned"),
        system=system,
        user_template=user_template,
        path=path,
    )


def prompt_version(name: str) -> str:
    """Cheap version lookup for tracing (never raises)."""
    try:
        return load_prompt(name).version
    except Exception:
        return "unknown"
