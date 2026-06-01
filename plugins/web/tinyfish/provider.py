"""TinyFish web search + fetch — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Provides:
  - ``supports_search()`` → True  (TinyFish Search API — GET /)
  - ``supports_extract()`` → True (TinyFish Fetch API — POST /)

Free-tier local guard (per-minute rolling window, Hermes-process lifetime):
  - Search  : 28 requests / minute  (free tier: 30 req/min, 2-under safety cap)
  - Fetch   : 140 URLs   / minute  (free tier: 150 URLs/min, 10-under safety cap)
  - Warns at 80%, hard-blocks at limit, sleeps between requests to smooth bursts.

TinyFish enforces its own rate limits server-side (HTTP 429 / RATE_LIMIT_EXCEEDED).
The local tracker is a free-tier safeguard to avoid burning API quota.

Config keys this provider responds to::

    web:
      search_backend: "tinyfish"
      extract_backend: "tinyfish"
      backend: "tinyfish"

Env vars::

    TINYFISH_API_KEY=***    # from https://agent.tinyfish.ai/api-keys
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Free-tier rate-limit tracker (per-minute rolling window)
# ---------------------------------------------------------------------------
_WINDOW_SECS = 60  # 1-minute rolling window

# Per-endpoint config: {limit, warn_at, request_times deque}
_TRACKER: Dict[str, Dict[str, Any]] = {
    "search": {
        "limit":         28,   # free tier: 30/min, 2-under safety cap
        "warn_at":       22,   # 80% warn threshold
        "request_times": None,  # deque, created lazily
    },
    "fetch": {
        "limit":         140,  # free tier: 150/min, 10-under safety cap
        "warn_at":       112,  # 80% warn threshold
        "request_times": None,
    },
}


def _check_limit(endpoint: str) -> None:
    """Burst-drain rate limiter: fill fast, block-wait for oldest to expire.

    No pre-request sleep — requests fire freely until the sliding window is full.
    When full, blocks until the oldest request ages out of the 60s window, then
    proceeds. Warn-level requests log but don't block.
    """
    slot = _TRACKER[endpoint]

    # Lazy-init deque
    if slot["request_times"] is None:
        slot["request_times"] = deque()

    times = slot["request_times"]

    while True:
        now = time.monotonic()

        # Sliding window: expire requests older than _WINDOW_SECS
        cutoff = now - _WINDOW_SECS
        while times and times[0] < cutoff:
            times.popleft()

        if len(times) < slot["limit"]:
            break  # window has room

        # Window full — block until oldest expires
        oldest = times[0]
        wait_secs = oldest + _WINDOW_SECS - now
        logger.info(
            "TinyFish %s window full (%d/%d) — waiting %.1fs for oldest to expire",
            endpoint, len(times), slot["limit"], wait_secs,
        )
        time.sleep(wait_secs)
        # Loop back, re-check, oldest will now be expired and removed

    # Warn at 80%
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
# API helpers
# ---------------------------------------------------------------------------

_TINYFISH_SEARCH_BASE = "https://api.search.tinyfish.ai"
_TINYFISH_FETCH_BASE  = "https://api.fetch.tinyfish.ai"


def _headers() -> Dict[str, str]:
    api_key = os.getenv("TINYFISH_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "TINYFISH_API_KEY environment variable not set. "
            "Get your key at https://agent.tinyfish.ai/api-keys"
        )
    return {
        "X-API-Key":    api_key,
        "Content-Type": "application/json",
    }


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
# Response normalizers
# ---------------------------------------------------------------------------

def _normalize_search(response: Dict[str, Any]) -> Dict[str, Any]:
    """Map TinyFish GET / response → {success, data: {web: [...]}}."""
    web: List[Dict[str, Any]] = []
    for item in response.get("results", []):
        web.append({
            "title":       item.get("title", ""),
            "url":         item.get("url", ""),
            "description": item.get("snippet", ""),
            "position":    item.get("position", 0),
        })
    return {"success": True, "data": {"web": web}}


def _normalize_fetch(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map TinyFish POST / response → list-of-docs shape.

    Per-URL errors appear in response["errors"]. We include them as doc
    entries with an "error" field so the caller knows which URLs failed.
    """
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
        docs.append({
            "url":         url,
            "title":       result.get("title", ""),
            "content":     raw,
            "raw_content": raw,
            "metadata": {
                "sourceURL":   url,
                "final_url":   result.get("final_url", url),
                "description": result.get("description", ""),
                "language":    result.get("language", ""),
            },
        })

    # URLs that errored with no result
    for err_url, err_entry in error_map.items():
        if not any(d["url"] == err_url for d in docs):
            docs.append({
                "url":         err_url,
                "title":       "",
                "content":     "",
                "raw_content": "",
                "error":       f"{err_entry.get('code', 'ERROR')}: {err_entry.get('message', '')}",
                "metadata":    {"sourceURL": err_url},
            })

    return docs


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class TinyfishWebSearchProvider(WebSearchProvider):
    """TinyFish search + extract with free-tier per-minute guard."""

    @property
    def name(self) -> str:
        return "tinyfish"

    @property
    def display_name(self) -> str:
        return "TinyFish"

    def is_available(self) -> bool:
        return bool(os.getenv("TINYFISH_API_KEY", "").strip())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                return {"success": False, "error": "Interrupted"}

            _check_limit("search")

            params: Dict[str, Any] = {"query": query}
            # TinyFish supports up to 20 results
            raw = _do_get(f"{_TINYFISH_SEARCH_BASE}/", params)

            # TinyFish-side errors
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

        except QuotaExceededError as exc:
            return {
                "success": False,
                "error": str(exc),
                "retry_after": exc.retry_after,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("TinyFish search error: %s", exc)
            return {"success": False, "error": f"TinyFish search failed: {exc}"}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        try:
            from tools.interrupt import is_interrupted
            if is_interrupted():
                return [{"url": u, "error": "Interrupted", "title": "", "content": ""} for u in urls]

            _check_limit("fetch")

            payload = {
                "urls":        urls,
                "format":      kwargs.get("format", "markdown"),
                "links":       kwargs.get("links", False),
                "image_links": kwargs.get("image_links", False),
            }
            raw = _do_post(f"{_TINYFISH_FETCH_BASE}/", payload)

            # Top-level errors
            if "error" in raw:
                code = raw["error"].get("code", "")
                if code == "RATE_LIMIT_EXCEEDED":
                    return [
                        {
                            "url":     u,
                            "title":   "",
                            "content": "",
                            "error":   "TinyFish fetch rate limit reached. Try again later.",
                            "retry_after": 60.0,
                        }
                        for u in urls
                    ]
                return [
                    {
                        "url":     u,
                        "title":   "",
                        "content": "",
                        "error":   f"TinyFish fetch error: {raw['error'].get('message', '')}",
                    }
                    for u in urls
                ]

            return _normalize_fetch(raw)

        except QuotaExceededError as exc:
            return [
                {"url": u, "title": "", "content": "", "error": str(exc),
                 "retry_after": exc.retry_after}
                for u in urls
            ]
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except Exception as exc:  # noqa: BLE001
            logger.warning("TinyFish fetch error: %s", exc)
            return [
                {"url": u, "title": "", "content": "", "error": f"TinyFish fetch failed: {exc}"}
                for u in urls
            ]

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name":  "TinyFish",
            "badge": "free",
            "tag":   "Search + fetch. Free: 28 search/min, 140 fetch URL/min (safety-capped from 30/150).",
            "env_vars": [
                {
                    "key":    "TINYFISH_API_KEY",
                    "prompt": "TinyFish API key",
                    "url":    "https://agent.tinyfish.ai/api-keys",
                },
            ],
        }
