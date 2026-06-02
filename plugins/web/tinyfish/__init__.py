"""TinyFish web search + fetch plugin — bundled, auto-loaded.

Mirrors the ``plugins/web/brave_free/`` layout: ``provider.py`` holds the
provider class, ``__init__.py::register(ctx)`` registers an instance.
"""

from __future__ import annotations

from plugins.web.tinyfish.provider import TinyfishWebSearchProvider


def register(ctx) -> None:
    """Register the TinyFish provider with the plugin context."""
    ctx.register_web_search_provider(TinyfishWebSearchProvider())
