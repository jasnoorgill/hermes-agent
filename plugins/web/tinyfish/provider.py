"""TinyFish web search + fetch — bundled plugin form (v2026.9.7+).

Subclasses :class:`plugins.web._common.BaseWebSearchProvider`, registered via
``plugins.web.tinyfish.__init__`` → ``ctx.register_web_search_provider``.
Selected by ``web.search_backend`` / ``web.extract_backend`` / ``web.backend: tinyfish``.

Endpoints
---------
- Search  : GET  https://api.search.tinyfish.ai  (free, 30 req/min)
- Fetch   : POST https://api.fetch.tinyfish.ai  (free, 150 URLs/min)

Local free-tier rate tracker (per-minute rolling window, process-lifetime):
- Search: 28 req/min (free 30, 2-under safety cap)
- Fetch : 140 URLs/min (free 150, 10-under safety cap)
- Burst-drain: requests fire freely until window is full, then block-wait for
  the oldest slot to expire. Warns at 80% utilization. TinyFish enforces its
  own rate limits server-side (HTTP 429 / RATE_LIMIT_EXCEEDED); the local
  tracker is a free-tier safeguard to avoid burning API quota.

Config::

    web:
      search_backend: "tinyfish"
      extract_backend: "tinyfish"
      backend: "tinyfish"

Env::

    TINYFISH_API_KEY=<key from https://agent.tinyfish.ai/api-keys>
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Dict, List

from plugins.web._common import (
    BaseWebSearchProvider,
    document,
    page_error,
    provider_env,
    run_extract,
    run_search,
    setup_schema,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Free-tier rate-limit tracker (per-minute rolling window)
# ---------------------------------------------------------------------------
_WINDOW_SECS = 60  # 1-minute rolling window

# Per-endpoint config: {limit, warn_at, request_times deque}
_TRACKER: Dict[str, Dict[str, Any]] = {
    "search": {
        "limit": 28,           # free tier: 30/min, 2-under safety cap
        "warn_at": 22,         # 80% warn threshold
        "request_times": None, # deque, created lazily
    },
    "fetch": {
        "limit": 140,          # free tier: 150/min, 10-under safety cap
        "warn_at": 112,        # 80% warn threshold
        "request_times": None,
    },
}


def _check_limit(endpoint: str) -> None:
    """Burst-drain rate limiter: fill fast, block-wait for oldest to expire.

    No pre-request sleep — requests fire freely until the sliding window is
    full. When full, blocks until the oldest request ages out of the 60s
    window, then proceeds. Warn-level requests log but don't block.
    """
    slot = _TRACKER[endpoint]
    if slot["request_times"] is None:
        slot["request_times"] = deque()

    times = slot["request_times"]
    while True:
        now = time.monotonic()
        cutoff = now - _WINDOW_SECS
        while times and times[0] < cutoff:
            times.popleft()
        if len(times) < slot["limit"]:
            break
        oldest = times[0]
        wait_secs = oldest + _WINDOW_SECS - now
        logger.info(
            "TinyFish %s window full (%d/%d) — waiting %.1fs for oldest to expire",
            endpoint, len(times), slot["limit"], wait_secs,
        )
        time.sleep(wait_secs)

    if len(times) >= slot["warn_at"]:
        logger.warning(
            "TinyFish %s usage: %d/%d (%.0f%%) — approaching per-minute limit",
            endpoint, len(times), slot["limit"],
            (len(times) / slot["limit"]) * 100,
        )

    times.append(time.monotonic())


class QuotaExceededError(Exception):
    """Raised when the local free-tier per-minute quota is exhausted."""

    retry_after: float = 60.0

    def __init__(self, message: str, retry_after: float = 60.0):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
_TINYFISH_SEARCH_BASE = "https://api.search.tinyfish.ai"
_TINYFISH_FETCH_BASE = "https://api.fetch.tinyfish.ai"


def _headers() -> Dict[str, str]:
    api_key = provider_env("TINYFISH_API_KEY")
    if not api_key:
        raise ValueError(
            "TINYFISH_API_KEY environment variable not set. "
            "Get your key at https://agent.tinyfish.ai/api-keys"
        )
    return {"X-API-Key": api_key, "Content-Type": "application/json"}


def _do_get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    import httpx
    with httpx.Client(timeout=60.0) as client:
        response = client.get(url, params=params, headers=_headers())
    response.raise_for_status()
    return response.json()


def _do_post(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    import httpx
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, json=payload, headers=_headers())
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Response normalizers (legacy wire shapes — see agent/web_search_provider docstring)
# ---------------------------------------------------------------------------
def _normalize_search(response: Dict[str, Any]) -> Dict[str, Any]:
    """Map TinyFish GET / response → ``{success, data: {web: [...]}}``."""
    web: List[Dict[str, Any]] = []
    for item in response.get("results", []):
        web.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("snippet", ""),
            "position": item.get("position", 0),
        })
    return {"success": True, "data": {"web": web}}


def _normalize_fetch(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map TinyFish POST / response → list-of-docs (legacy ``raw_content`` shape)."""
    docs: List[Dict[str, Any]] = []
    error_map: Dict[str, Dict[str, str]] = {}
    for err in response.get("errors", []):
        error_map[err.get("url", "")] = err

    for result in response.get("results", []):
        url = result.get("url", "")
        err_entry = error_map.get(url, {})
        # TinyFish Fetch returns "text" (markdown) as the primary content field
        raw = (
            result.get("text", "")
            or result.get("content", "")
            or result.get("raw_content", "")
        )
        docs.append(document(
            url,
            result.get("title", ""),
            raw,
            source_url=result.get("final_url", url) or None,
        ))

    for err_url, err_entry in error_map.items():
        if not any(d["url"] == err_url for d in docs):
            docs.append(page_error(
                err_url,
                f"{err_entry.get('code', 'ERROR')}: {err_entry.get('message', '')}",
            ))

    return docs


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class TinyfishWebSearchProvider(BaseWebSearchProvider):
    """TinyFish search + extract, free-tier-aware.

    Sets the four ``BaseWebSearchProvider`` class attrs (``NAME``,
    ``DISPLAY_NAME``, ``KEY_ENV``, ``EXTRACT=True``); the base class
    provides ``is_available`` (env-var gate) and the ``name`` /
    ``display_name`` properties, so this subclass only implements the
    request methods.
    """

    NAME = "tinyfish"
    DISPLAY_NAME = "TinyFish"
    KEY_ENV = "TINYFISH_API_KEY"
    EXTRACT = True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        def _body() -> Dict[str, Any]:
            _check_limit("search")
            params: Dict[str, Any] = {"query": query}
            raw = _do_get(f"{_TINYFISH_SEARCH_BASE}/", params)
            if "error" in raw:
                code = raw["error"].get("code", "")
                if code == "RATE_LIMIT_EXCEEDED":
                    return {
                        "success": False,
                        "error": (
                            "TinyFish search rate limit reached. "
                            "Upgrade at https://agent.tinyfish.ai or try again later."
                        ),
                        "retry_after": 60.0,
                    }
                return {"success": False, "error": raw["error"].get("message", str(raw))}
            return _normalize_search(raw)

        return run_search("TinyFish", logger, _body)

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        def _body() -> List[Dict[str, Any]]:
            _check_limit("fetch")
            payload = {
                "urls": urls,
                "format": kwargs.get("format", "markdown"),
                "links": kwargs.get("links", False),
                "image_links": kwargs.get("image_links", False),
            }
            raw = _do_post(f"{_TINYFISH_FETCH_BASE}/", payload)
            if "error" in raw:
                code = raw["error"].get("code", "")
                if code == "RATE_LIMIT_EXCEEDED":
                    return [page_error(
                        u, "TinyFish fetch rate limit reached. Try again later.",
                    ) for u in urls]
                return [page_error(
                    u, f"TinyFish fetch error: {raw['error'].get('message', '')}",
                ) for u in urls]
            return _normalize_fetch(raw)

        return run_extract("TinyFish", logger, urls, _body)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "TinyFish", "free",
            "Search + fetch. Free: 28 search/min, 140 fetch URL/min (safety-capped from 30/150).",
            "TINYFISH_API_KEY", "TinyFish API key", "https://agent.tinyfish.ai/api-keys",
        )
