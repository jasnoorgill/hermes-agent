"""TinyFish web search + fetch + browser provider for hermes-agent.

Auto-loaded by the plugin loader (kind: backend, name: web-tinyfish).
Registers :class:`TinyfishWebSearchProvider` with the web_search_registry,
which is consumed by ``tools.web_tools`` via ``get_active_search_provider``
/ ``get_active_extract_provider`` / ``is_available`` chokepoints.
"""
from __future__ import annotations

from plugins.web.tinyfish.provider import TinyfishWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(TinyfishWebSearchProvider())
