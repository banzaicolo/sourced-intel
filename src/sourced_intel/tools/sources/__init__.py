"""Source adapter registry.

Adding a source means implementing `SourceAdapter.fetch` and registering it here;
no node code needs to change.
"""

from __future__ import annotations

from .base import SourceAdapter
from .china5e import China5eAdapter
from .gdelt import GdeltAdapter

ADAPTER_REGISTRY: dict[str, type[SourceAdapter]] = {
    GdeltAdapter.name: GdeltAdapter,
    China5eAdapter.name: China5eAdapter,
}

#: Order in which adapters are tried; the first is the breadth source.
DEFAULT_SOURCE_ORDER: tuple[str, ...] = ("gdelt", "china5e")


def build_adapters(names: tuple[str, ...] | list[str] | None = None) -> list[SourceAdapter]:
    selected = names or DEFAULT_SOURCE_ORDER
    adapters: list[SourceAdapter] = []
    for name in selected:
        factory = ADAPTER_REGISTRY.get(name)
        if factory is None:
            raise KeyError(f"unknown source adapter: {name}")
        adapters.append(factory())
    return adapters


__all__ = [
    "ADAPTER_REGISTRY",
    "DEFAULT_SOURCE_ORDER",
    "China5eAdapter",
    "GdeltAdapter",
    "SourceAdapter",
    "build_adapters",
]
