"""Low-latency adaptive multi-index fusion search provider.

The provider owns the HTTP calls instead of delegating to Hermes' bundled
providers because their transport timeouts are too long for a strict fusion
SLA. Calls are bounded at the HTTP layer, run concurrently, and fused within
a soft window. TinyFish, Serper, paced Brave, Tavily, DeepSeek, and optional
DDGS form the active roster. Exa and MWMBL clients are inactive. If no provider
succeeds in the soft window, collection continues until the first useful result
or a bounded hard deadline. Search never uses Firecrawl.
"""

from __future__ import annotations

import concurrent.futures
from contextvars import copy_context
from functools import partial
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from agent.web_search_provider import WebSearchProvider, get_provider_env

try:
    from .turn_routing import ROUTER, decision_context
    from .shadow import SHADOW
    from .paths import hermes_home
except ImportError:  # standalone provider smoke / scripts
    from turn_routing import ROUTER, decision_context
    from shadow import SHADOW
    from paths import hermes_home

logger = logging.getLogger(__name__)

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TINYFISH_ENDPOINT = "https://api.search.tinyfish.ai"
_EXA_ENDPOINT = "https://api.exa.ai/search"
_MWMBL_ENDPOINT = "https://api.mwmbl.org/api/v1/search/"
_SERPER_ENDPOINT = "https://google.serper.dev/search"
_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
# Dispatch order doubles as priority: providers are claimed in this order, and
# under contention the later ones are the first to be refused admission. Fast
# JSON APIs come first; ddgs is last because it is CPU-heavy HTML scraping that
# would otherwise starve them (see _PROVIDER_MAX_INFLIGHT["ddgs"]).
# Exa and MWMBL clients remain experimental/inactive and are not dispatched.
_PROVIDER_ORDER = ("tinyfish", "serper", "brave-free", "tavily", "deepseek", "ddgs")
_PROVIDER_MAX_INFLIGHT = {
    "exa": 8,
    "tinyfish": 4,
    # Brave's free tier allows ~1 query/second, but that is a *pacing* limit,
    # not a concurrency limit. Admission slots only need to cover the waiters
    # the pacer can still serve inside brave_max_wait_s; those queue rather
    # than run in parallel, and anything beyond the budget is refused as
    # rate_paced. Capping this at 1 rejected legitimate queued turns as
    # concurrency_limited and silently degraded fusion to two providers.
    "brave-free": 3,
    # Keep CPU-heavy HTML scraping below the keyed JSON providers' concurrency
    # so excess callers do not spawn work that collection will not wait for.
    "ddgs": 2,
    # Inactive community index: retain a modest experimental concurrency cap.
    "mwmbl": 4,
    # Metered providers bound burst spending as well as monthly attempts.
    "serper": 4,
    "tavily": 3,
    "deepseek": 4,
}
# Bound auxiliary work without extending the existing collection deadlines.
_PROVIDER_TIMEOUT_CAP_S: dict[str, float] = {"deepseek": 2.0}

# Admission refusals that clear on their own within a search window; anything
# else (circuit_open, quota_exhausted, cooldown, unavailable) will not.
_TRANSIENT_ADMISSION = frozenset({"concurrency_limited", "rate_paced"})
_ADMISSION_RETRY_S = 0.2
_PROVIDER_WEIGHTS = {
    "exa": 1.05,
    "tinyfish": 1.0,
    "brave-free": 1.0,
    # Slightly discounted: DuckDuckGo aggregates other indexes, so it is
    # valuable as an independent tie-breaker but should not outrank a
    # first-party index on its own.
    "ddgs": 0.95,
    # Lowest weight: a small volunteer index with real coverage gaps. Valuable
    # as an independent corroborating signal, but it must never outrank a
    # major index on its own.
    "mwmbl": 0.75,
    # Favor the Serper ranking signal while retaining consensus contributions.
    "serper": 1.40,
    # Tavily ranks for LLM-answer relevance rather than raw web authority,
    # which makes it a useful but slightly different signal. Held at parity.
    "tavily": 1.0,
    # New independent index: auxiliary until evaluated on clicked-URL recall.
    "deepseek": 0.70,
}
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "source",
}
_GENERIC_QUERY_TERMS = {
    "about",
    "cost",
    "documentation",
    "free",
    "guide",
    "lower",
    "model",
    "official",
    "percent",
    "performance",
    "plugin",
    "provider",
    "routing",
    "router",
    "search",
    "the",
    "web",
    "with",
}

SearchFn = Callable[[str, int, float], list[dict[str, Any]]]


class _ProviderRows(list[dict[str, Any]]):
    """Rows plus safe transport metadata needed by the router."""

    def __init__(self, rows: list[dict[str, Any]], *, rate_headers: dict[str, str] | None = None):
        super().__init__(rows)
        self.rate_headers = rate_headers or {}


@dataclass(frozen=True)
class FusionSettings:
    """Behavioral settings for bounded fusion search."""

    provider_timeout_s: float = 4.5
    global_timeout_s: float = 1.8
    first_result_timeout_s: float = 5.0
    post_first_grace_s: float = 0.25
    max_inflight_per_provider: int = 4
    circuit_failures: int = 2
    circuit_cooldown_s: float = 60.0
    brave_min_interval_s: float = 1.10
    brave_max_wait_s: float = 1.20
    min_providers: int = 2
    quorum_timeout_s: float = 2.6
    rrf_k: int = 60
    turn_routing_enabled: bool = True
    auto_retry_enabled: bool = True
    shadow_enabled: bool = False
    shadow_daily_cap: int = 300
    shadow_timeout_s: float = 3.0
    turn_idle_gap_s: float = 120.0
    batch_window_s: float = 1.0


def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def load_fusion_settings() -> FusionSettings:
    """Load optional plugin settings from config.yaml with safe bounds."""

    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        entry = (((cfg.get("plugins") or {}).get("entries") or {}).get("search-fusion") or {})
        raw = entry.get("fusion") or {}
    except Exception:  # noqa: BLE001
        raw = {}

    provider_timeout = _bounded_float(raw.get("provider_timeout_s"), 4.5, 0.2, 10.0)
    global_timeout = _bounded_float(raw.get("global_timeout_s"), 1.8, 0.2, 12.0)
    first_result_timeout = _bounded_float(raw.get("first_result_timeout_s"), 5.0, 0.5, 15.0)
    return FusionSettings(
        provider_timeout_s=provider_timeout,
        global_timeout_s=global_timeout,
        first_result_timeout_s=max(global_timeout, first_result_timeout),
        post_first_grace_s=_bounded_float(raw.get("post_first_grace_s"), 0.25, 0.0, 2.0),
        max_inflight_per_provider=_bounded_int(raw.get("max_inflight_per_provider"), 4, 1, 16),
        circuit_failures=_bounded_int(raw.get("circuit_failures"), 2, 1, 10),
        circuit_cooldown_s=_bounded_float(raw.get("circuit_cooldown_s"), 60.0, 1.0, 900.0),
        brave_min_interval_s=_bounded_float(raw.get("brave_min_interval_s"), 1.10, 1.0, 10.0),
        brave_max_wait_s=_bounded_float(raw.get("brave_max_wait_s"), 1.20, 0.0, 10.0),
        min_providers=_bounded_int(raw.get("min_providers"), 2, 1, len(_PROVIDER_ORDER)),
        quorum_timeout_s=max(
            global_timeout,
            _bounded_float(raw.get("quorum_timeout_s"), 2.6, 0.2, 15.0),
        ),
        rrf_k=_bounded_int(raw.get("rrf_k"), 60, 1, 1000),
        turn_routing_enabled=str(os.getenv("SEARCH_FUSION_TURN_ROUTING", raw.get("turn_routing_enabled", True))).lower() not in {"0", "false", "off", "no"},
        auto_retry_enabled=str(os.getenv("SEARCH_FUSION_AUTO_RETRY", raw.get("auto_retry_enabled", True))).lower() not in {"0", "false", "off", "no"},
        shadow_enabled=str(os.getenv("SEARCH_FUSION_SHADOW", raw.get("shadow_enabled", False))).lower() not in {"0", "false", "off", "no"},
        shadow_daily_cap=_bounded_int(raw.get("shadow_daily_cap"), 300, 0, 10000),
        shadow_timeout_s=_bounded_float(raw.get("shadow_timeout_s"), 3.0, 0.1, 10.0),
        turn_idle_gap_s=_bounded_float(raw.get("turn_idle_gap_s"), 120.0, 1.0, 3600.0),
        batch_window_s=_bounded_float(raw.get("batch_window_s"), 1.0, 0.0, 10.0),
    )


def _httpx_timeout(seconds: float):
    # Bound every socket phase. The outer global deadline separately bounds how
    # long the router waits for the complete provider call.
    return httpx.Timeout(seconds, connect=min(seconds, 0.75))


# Shared connection pool. Every provider call previously went through the
# module-level httpx.get/post helpers, which build and discard a client per
# call and so pay a fresh TCP+TLS handshake every time. Measured against
# Serper: 875ms median per call unpooled vs 693ms pooled, a ~182ms saving on
# every provider request. Keep-alives are capped so idle sockets to seven
# providers cannot accumulate.
_HTTP_CLIENT = httpx.Client(
    limits=httpx.Limits(max_keepalive_connections=16, max_connections=32),
    follow_redirects=True,
    headers={"User-Agent": "Hermes-search-fusion/0.4"},
)


def _brave_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    key = get_provider_env("BRAVE_SEARCH_API_KEY")
    if not key:
        raise RuntimeError("BRAVE_SEARCH_API_KEY is not configured")
    response = _HTTP_CLIENT.get(
        _BRAVE_ENDPOINT,
        params={"q": query, "count": max(1, min(int(limit), 20))},
        headers={
            "X-Subscription-Token": key,
            "Accept": "application/json",
            "User-Agent": "Hermes-search-fusion/0.4",
        },
        timeout=_httpx_timeout(timeout_s),
    )
    response.raise_for_status()
    rate_headers = {
        name: value
        for name in (
            "X-RateLimit-Limit",
            "X-RateLimit-Policy",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
        )
        if (value := response.headers.get(name)) is not None
    }
    return _ProviderRows(
        list(((response.json().get("web") or {}).get("results") or [])),
        rate_headers=rate_headers,
    )


def _tinyfish_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    key = get_provider_env("TINYFISH_API_KEY")
    if not key:
        raise RuntimeError("TINYFISH_API_KEY is not configured")
    response = _HTTP_CLIENT.get(
        _TINYFISH_ENDPOINT,
        params={"query": query, "location": "US", "language": "en"},
        headers={
            "X-API-Key": key,
            "Accept": "application/json",
            "User-Agent": "Hermes-search-fusion/0.4",
        },
        timeout=_httpx_timeout(timeout_s),
        follow_redirects=True,
    )
    response.raise_for_status()
    return list(response.json().get("results") or [])[: max(1, int(limit))]


def _exa_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    key = get_provider_env("EXA_API_KEY")
    if not key:
        raise RuntimeError("EXA_API_KEY is not configured")
    response = _HTTP_CLIENT.post(
        _EXA_ENDPOINT,
        json={
            "query": query,
            "numResults": max(1, min(int(limit), 20)),
            "type": "fast",
            "contents": {"highlights": True},
        },
        headers={
            "x-api-key": key,
            "Accept": "application/json",
            "User-Agent": "Hermes-search-fusion/0.4",
        },
        timeout=_httpx_timeout(timeout_s),
        follow_redirects=True,
    )
    response.raise_for_status()
    return list(response.json().get("results") or [])


def _serper_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    """Google SERP via Serper; metered requests share the local monthly cap."""

    key = get_provider_env("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY is not configured")
    response = _HTTP_CLIENT.post(
        _SERPER_ENDPOINT,
        json={"q": query, "num": max(1, min(int(limit), 20))},
        headers={
            "X-API-KEY": key,
            "Content-Type": "application/json",
            "User-Agent": "Hermes-search-fusion/0.4",
        },
        timeout=_httpx_timeout(timeout_s),
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    results: list[dict[str, Any]] = []
    for row in list(payload.get("organic") or [])[: max(1, int(limit))]:
        if not isinstance(row, dict):
            continue
        results.append(
            {
                "title": row.get("title"),
                "url": row.get("link"),
                "description": row.get("snippet"),
            }
        )
    return results


def _tavily_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    """Tavily basic search; omit answer generation and enforce a local cap."""

    key = get_provider_env("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not configured")
    response = _HTTP_CLIENT.post(
        _TAVILY_ENDPOINT,
        json={
            "query": query,
            "max_results": max(1, min(int(limit), 20)),
            "search_depth": "basic",
        },
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-search-fusion/0.4",
        },
        timeout=_httpx_timeout(timeout_s),
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    results: list[dict[str, Any]] = []
    for row in list(payload.get("results") or [])[: max(1, int(limit))]:
        if not isinstance(row, dict):
            continue
        results.append(
            {
                "title": row.get("title"),
                "url": row.get("url"),
                "description": row.get("content"),
            }
        )
    return results


def _deepseek_query_valid(query: str) -> bool:
    return isinstance(query, str) and 1 <= len(query) <= 128 and bool(query.strip())


def _deepseek_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    """Raw Search only; preserve operators, never truncate an overlong query."""
    if not _deepseek_query_valid(query):
        raise ValueError("DeepSeek query must contain 1..128 Unicode characters and not be blank")
    key = get_provider_env("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    count = max(1, min(int(limit), 50))
    response = _HTTP_CLIENT.post(
        _DEEPSEEK_ENDPOINT,
        json={"query": query, "count": count, "need_content": False},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=_httpx_timeout(timeout_s),
        follow_redirects=False,
    )
    response.raise_for_status()
    return [
        {"title": row.get("title"), "url": row.get("url"),
         "description": row.get("snippet") or row.get("summary") or ""}
        for row in (response.json().get("results") or [])[:count]
        if isinstance(row, dict)
    ]


def _mwmbl_flatten(value: Any) -> str:
    """Join mwmbl's segmented text into a plain string.

    mwmbl returns ``title``/``extract`` as a list of ``{"value", "is_bold"}``
    highlight segments rather than a string, so naive ``str()`` would store a
    Python repr in the result row.
    """

    if isinstance(value, list):
        return "".join(
            str(segment.get("value", ""))
            for segment in value
            if isinstance(segment, dict)
        ).strip()
    return str(value or "").strip()


def _mwmbl_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    """Inactive experimental client for the independent, community-run MWMBL index."""

    response = _HTTP_CLIENT.get(
        _MWMBL_ENDPOINT,
        params={"s": query},
        headers={
            "Accept": "application/json",
            "User-Agent": "Hermes-search-fusion/0.4",
        },
        timeout=_httpx_timeout(timeout_s),
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    # The endpoint returns a bare JSON array; tolerate a wrapped object too.
    rows = payload if isinstance(payload, list) else payload.get("results") or []
    results: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        results.append(
            {
                "title": _mwmbl_flatten(row.get("title")),
                "url": row.get("url"),
                "description": _mwmbl_flatten(row.get("extract")),
            }
        )
    return results


@lru_cache(maxsize=1)
def _ddgs_available() -> bool:
    """Return True when the optional ``ddgs`` package is importable.

    Imports eagerly at module load rather than on first search: ``ddgs`` pulls
    in a sizeable dependency tree, and paying that inside the first provider
    worker thread stalled the whole first fused search by ~2s (every other
    provider then hit post_first_grace and quorum fell to 2/5). Warm, the same
    five-provider search completes in under a second.
    """

    try:
        import ddgs  # noqa: F401
    except Exception:  # noqa: BLE001 — a broken install must not break routing
        return False
    return True


def _ddgs_search(query: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
    """DuckDuckGo via the ``ddgs`` package. No API key.

    Run in-process to distinguish provider failures from empty CLI output.

    ``ddgs`` has no overall wall-clock cap of its own (its multi-engine retry
    loop can run long past the per-request timeout), so the router's own
    deadline is what bounds this call; we additionally pass the per-request
    timeout down.
    """

    from ddgs import DDGS  # imported lazily: optional dependency

    rows: list[dict[str, Any]] = []
    with DDGS(timeout=max(1, int(timeout_s))) as client:
        for index, hit in enumerate(client.text(query, max_results=limit)):
            if index >= limit:
                break
            if not isinstance(hit, dict):
                continue
            rows.append(
                {
                    "title": hit.get("title"),
                    "url": hit.get("href") or hit.get("url"),
                    "description": hit.get("body") or hit.get("description"),
                }
            )
    return rows


def _canonical_url(url: str) -> str:
    """Return a stable deduplication key without destroying meaningful query args."""

    try:
        parsed = urlsplit(url.strip())
    except Exception:  # noqa: BLE001
        return url.strip()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    kept_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in _TRACKING_QUERY_KEYS:
            continue
        kept_query.append((key, value))
    return urlunsplit(("", host + port, path, urlencode(sorted(kept_query)), ""))


def _normalized_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    url = str(row.get("url") or "").strip()
    if not url:
        return None
    description = (
        row.get("description")
        or row.get("snippet")
        or row.get("content")
        or row.get("highlights")
        or ""
    )
    if isinstance(description, list):
        description = " ".join(str(item) for item in description)
    return {
        "title": str(row.get("title") or "").strip(),
        "url": url,
        "description": str(description).strip(),
    }


def _domain_affinity(url: str, query: str) -> float:
    """Give a modest deterministic lift to likely first-party domains."""

    host = (urlsplit(url).hostname or "").lower()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", query.lower())
        if len(token) >= 4 and token not in _GENERIC_QUERY_TERMS
    }
    if any(token in host for token in tokens):
        return 1.25
    if host.startswith("docs.") or host.endswith(".gov") or host.endswith(".edu"):
        return 1.10
    return 1.0


def _header_seconds(value: str | None) -> float | None:
    """Parse Retry-After or rate-reset values as seconds without raising."""

    if not value:
        return None
    text = value.strip()
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        # Some APIs emit an epoch timestamp; Brave normally emits a duration.
        if numeric > 1_000_000_000:
            return max(0.0, numeric - time.time())
        return max(0.0, numeric)
    try:
        parsed = parsedate_to_datetime(text)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _exhausted_window_cooldown(headers: Any) -> float | None:
    """Return the longest reset among exhausted provider quota windows."""

    candidates: list[float] = []
    remaining_values = [
        item.strip()
        for item in headers.get("X-RateLimit-Remaining", "").split(",")
        if item.strip()
    ]
    reset_values = [
        item.strip()
        for item in headers.get("X-RateLimit-Reset", "").split(",")
        if item.strip()
    ]
    if remaining_values and reset_values:
        for remaining, reset in zip(remaining_values, reset_values):
            try:
                exhausted = float(remaining) <= 0
            except ValueError:
                exhausted = False
            if exhausted:
                parsed = _header_seconds(reset)
                if parsed is not None:
                    candidates.append(parsed)
    elif reset_values:
        parsed = _header_seconds(reset_values[0])
        if parsed is not None:
            candidates.append(parsed)
    return max(candidates) if candidates else None


def _rate_limit_cooldown(exc: Exception, default_s: float) -> float:
    """Honor upstream reset headers, including Brave's paired quota windows."""

    response = getattr(exc, "response", None)
    if response is None:
        return default_s
    headers = response.headers
    candidates: list[float] = []
    retry_after = _header_seconds(headers.get("Retry-After"))
    if retry_after is not None:
        candidates.append(retry_after)
    exhausted_window = _exhausted_window_cooldown(headers)
    if exhausted_window is not None:
        candidates.append(exhausted_window)
    if not candidates:
        return default_s
    # Add the same safety margin used by local pacing. Bound corrupted headers
    # while allowing a depleted monthly window to remain closed until reset.
    return max(1.0, min(max(candidates) + 0.1, 31 * 24 * 60 * 60.0))


def _classify_provider_exception(
    exc: Exception,
    settings: FusionSettings,
) -> tuple[str, str, float | None]:
    """Return a safe status/message/cooldown tuple without leaking query URLs."""

    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout", "provider request timed out", None
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 429:
            cooldown = _rate_limit_cooldown(exc, settings.circuit_cooldown_s)
            return "provider_cooldown", "upstream throttle activated provider cooldown", cooldown
        if status_code in {401, 403}:
            return "auth_error", f"provider authentication failed (HTTP {status_code})", 900.0
        if status_code == 402:
            # Payment-required responses need account intervention; use the
            # authentication circuit instead of repeatedly occupying slots.
            return "auth_error", "provider account out of credit (HTTP 402); top up or disable it", 900.0
        return "error", f"provider returned HTTP {status_code}", None
    return "error", f"provider request failed ({type(exc).__name__})", None


@dataclass
class _AdmissionLease:
    coordinator: Any
    name: str
    admitted_at: float
    start_after_s: float = 0.0
    released: bool = False

    def finish(
        self,
        status: str,
        settings: FusionSettings,
        cooldown_s: float | None = None,
    ) -> None:
        self.coordinator.finish(self, status, settings, cooldown_s)


class _ProviderCoordinator:
    """Process-wide provider admission, pacing, and circuit state."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._inflight: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._next_allowed: dict[str, float] = {}
        self._rate_open_until: dict[str, float] = {}
        self._circuit_open_until: dict[str, float] = {}
        self._circuit_opened_at: dict[str, float] = {}

    def claim(
        self,
        name: str,
        settings: FusionSettings,
    ) -> tuple[_AdmissionLease | None, str | None]:
        now = self._clock()
        with self._lock:
            if self._rate_open_until.get(name, 0.0) > now:
                return None, "cooldown"
            if self._circuit_open_until.get(name, 0.0) > now:
                return None, "circuit_open"
            paced_wait = 0.0
            interval = settings.brave_min_interval_s if name == "brave-free" else 0.0
            max_wait = settings.brave_max_wait_s
            if interval:
                paced_wait = max(0.0, self._next_allowed.get(name, 0.0) - now)
                if paced_wait > max_wait:
                    return None, "rate_paced"
            current = self._inflight.get(name, 0)
            provider_limit = min(
                settings.max_inflight_per_provider,
                _PROVIDER_MAX_INFLIGHT.get(name, settings.max_inflight_per_provider),
            )
            if current >= provider_limit:
                return None, "concurrency_limited"
            # Check eligibility here; queued/cancelled work must not spend credits.
            if not _budget_allows(name, charge=False):
                return None, "quota_exhausted"
            self._inflight[name] = current + 1
            if interval:
                # Reserve the next slot before HTTP so concurrent searches queue
                # behind this one instead of all firing into a 429.
                self._next_allowed[name] = now + paced_wait + interval
            return _AdmissionLease(self, name, now, start_after_s=paced_wait), None

    def try_claim_shadow(self, name, settings):
        if not self._lock.acquire(blocking=False):
            return None, "busy"
        try:
            limit = min(settings.max_inflight_per_provider,
                        _PROVIDER_MAX_INFLIGHT.get(name, settings.max_inflight_per_provider))
            reserve = 1 if name == "deepseek" and limit > 1 else 0
            if self._inflight.get(name, 0) >= limit - reserve:
                return None, "concurrency_limited"
            if name == "brave-free" and self._next_allowed.get(name, 0) > self._clock():
                return None, "rate_paced"
            return self.claim(name, settings)
        finally:
            self._lock.release()

    def finish(
        self,
        lease: _AdmissionLease,
        status: str,
        settings: FusionSettings,
        cooldown_s: float | None = None,
    ) -> None:
        with self._lock:
            if lease.released:
                return
            lease.released = True
            name = lease.name
            current = self._inflight.get(name, 0)
            if current <= 1:
                self._inflight.pop(name, None)
            else:
                self._inflight[name] = current - 1

            now = self._clock()
            if status in {"ok", "empty"}:
                # Empty is a healthy transport response and breaks a consecutive
                # transport-failure streak just like a non-empty success.
                self._failures[name] = 0
                if cooldown_s is not None:
                    self._rate_open_until[name] = max(
                        self._rate_open_until.get(name, 0.0),
                        now + cooldown_s,
                    )
                # Never let an older success clear a newer generic circuit.
                if self._circuit_opened_at.get(name, 0.0) <= lease.admitted_at:
                    self._circuit_open_until.pop(name, None)
                    self._circuit_opened_at.pop(name, None)
                return

            if status == "provider_cooldown":
                cooldown = cooldown_s or settings.circuit_cooldown_s
                self._rate_open_until[name] = max(
                    self._rate_open_until.get(name, 0.0),
                    now + cooldown,
                )
                return
            if status == "auth_error":
                cooldown = cooldown_s or settings.circuit_cooldown_s
                self._circuit_open_until[name] = max(
                    self._circuit_open_until.get(name, 0.0),
                    now + cooldown,
                )
                self._circuit_opened_at[name] = now
                return
            if status not in {"timeout", "error"}:
                return
            failures = self._failures.get(name, 0) + 1
            self._failures[name] = failures
            if failures >= settings.circuit_failures:
                self._circuit_open_until[name] = now + settings.circuit_cooldown_s
                self._circuit_opened_at[name] = now


_PROCESS_COORDINATOR = _ProviderCoordinator()

# One bounded shared pool amortises thread creation and limits concurrent work
# across searches. Tardy provider calls keep their slots until completion.
_SHARED_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(8, len(_PROVIDER_ORDER) * 3),
    thread_name_prefix="search-fusion",
)

# Conservative local monthly attempted-request ceilings. These are cost controls,
# not account balances or guarantees about current upstream pricing/allowances.
# Failed and empty requests count locally too; shadow uses the same allowance.
_MONTHLY_BUDGET = {"tavily": 950, "serper": 6000, "deepseek": 10000}
# Optional override for isolated consumers/tests; normal paths resolve at call time.
_BUDGET_STATE_PATH: Path | None = None
_BUDGET_CACHE_PATH: Path | None = None


def _budget_state_path() -> Path:
    return _BUDGET_STATE_PATH if _BUDGET_STATE_PATH is not None else hermes_home() / "state/fusion-provider-budget.json"

_BUDGET_LOCK = threading.Lock()
_BUDGET_CACHE: dict[str, Any] = {}


def _budget_period() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def _persist_budget() -> None:
    """Persist while the caller holds _BUDGET_LOCK; retain in-memory fallback."""
    try:
        _budget_state_path().parent.mkdir(parents=True, exist_ok=True)
        _budget_state_path().write_text(json.dumps(_BUDGET_CACHE))
    except Exception:  # noqa: BLE001 — never fail a search on bookkeeping
        logger.debug("fusion: could not persist provider budget", exc_info=True)


def _budget_allows(
    name: str, *, charge: bool = True, cancelled: threading.Event | None = None,
) -> bool:
    """Return False once a metered provider has spent its monthly allowance.

    Counts are persisted so the ceiling survives a gateway restart; an unwritable
    state file degrades to in-process counting rather than failing the search.
    Unmetered providers are always allowed.
    """

    global _BUDGET_CACHE_PATH
    limit = _MONTHLY_BUDGET.get(name)
    if limit is None:
        return True

    period = _budget_period()
    with _BUDGET_LOCK:
        path = _budget_state_path()
        if _BUDGET_CACHE_PATH is not None and _BUDGET_CACHE_PATH != path:
            _BUDGET_CACHE.clear()
        _BUDGET_CACHE_PATH = path
        if _BUDGET_CACHE.get("period") != period:
            loaded: dict[str, Any] = {}
            try:
                loaded = json.loads(path.read_text())
            except Exception:  # noqa: BLE001 — missing/corrupt state starts fresh
                loaded = {}
            if loaded.get("period") != period:
                loaded = {"period": period, "counts": {}}
            _BUDGET_CACHE.clear()
            _BUDGET_CACHE.update(loaded)
            _BUDGET_CACHE.setdefault("counts", {})

        if cancelled is not None and cancelled.is_set():
            return False
        counts = _BUDGET_CACHE["counts"]
        used = int(counts.get(name, 0))
        if used >= limit:
            # Once per period: the provider silently drops out of fusion here,
            # and ranking quality degrades without any other visible signal.
            if not _BUDGET_CACHE.get("exhausted_logged", {}).get(name):
                _BUDGET_CACHE.setdefault("exhausted_logged", {})[name] = True
                logger.warning("search-fusion: budget exhausted provider=%s used=%d/%d period=%s", name, used, limit, period)
            return False
        if not charge:
            return True
        counts[name] = used + 1
        if used + 1 == int(limit * 0.8):
            logger.warning("search-fusion: budget 80%% provider=%s used=%d/%d period=%s", name, used + 1, limit, period)

        _persist_budget()
        # Persistence can block too. Roll back before releasing the lock so
        # cancelled work neither starts HTTP nor consumes a credit.
        if cancelled is not None and cancelled.is_set():
            counts[name] = used
            _persist_budget()
            return False
        return True

# Pay the ddgs import cost at module load, not inside the first provider
# worker; availability is cached for later dispatch.
_ddgs_available()


class SearchFusionWebSearchProvider(WebSearchProvider):
    """Fuse several independent search indexes under a bounded latency budget."""

    def __init__(
        self,
        *,
        settings: FusionSettings | None = None,
        searchers: dict[str, SearchFn] | None = None,
        coordinator: _ProviderCoordinator | None = None,
        executor: concurrent.futures.Executor | None = None,
    ) -> None:
        self._settings_override = settings
        self._executor = executor
        self._searchers = searchers or {
            "exa": _exa_search,
            "tinyfish": _tinyfish_search,
            "brave-free": _brave_search,
            "serper": _serper_search,
            "tavily": _tavily_search,
            "deepseek": _deepseek_search,
            "ddgs": _ddgs_search,
            "mwmbl": _mwmbl_search,
        }
        self._coordinator = coordinator or _PROCESS_COORDINATOR

    @property
    def name(self) -> str:
        return "search-fusion"

    @property
    def display_name(self) -> str:
        return "Search Fusion"

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def _settings(self) -> FusionSettings:
        return self._settings_override or load_fusion_settings()

    def _configured(self, name: str) -> bool:
        if self._settings_override is not None and name in self._searchers:
            # Injected test searchers are intentionally independent of real keys.
            return True
        if name == "brave-free":
            return bool(get_provider_env("BRAVE_SEARCH_API_KEY"))
        if name == "tinyfish":
            return bool(get_provider_env("TINYFISH_API_KEY"))
        if name == "exa":
            return bool(get_provider_env("EXA_API_KEY"))
        if name == "serper":
            return bool(get_provider_env("SERPER_API_KEY"))
        if name == "tavily":
            return bool(get_provider_env("TAVILY_API_KEY"))
        if name == "deepseek":
            return bool(get_provider_env("DEEPSEEK_API_KEY"))
        if name == "ddgs":
            # Keyless: availability is the optional package being importable.
            # Probe once and cache — this runs on every search dispatch, and
            # a failed import is comparatively expensive to repeat.
            return _ddgs_available()
        if name == "mwmbl":
            # Keyless and dependency-free: a plain public JSON endpoint.
            return True
        return False

    def is_available(self) -> bool:
        return any(name in self._searchers and self._configured(name) for name in _PROVIDER_ORDER)

    def _claim(
        self,
        name: str,
        settings: FusionSettings,
    ) -> tuple[_AdmissionLease | None, str | None]:
        return self._coordinator.claim(name, settings)

    def _invoke(
        self,
        name: str,
        searcher: SearchFn,
        query: str,
        limit: int,
        settings: FusionSettings,
        lease: _AdmissionLease,
        cancelled: threading.Event,
    ) -> dict[str, Any]:
        started = time.monotonic()
        status = "error"
        cooldown_s: float | None = None
        try:
            if lease.start_after_s > 0:
                # Hold a paced provider's turn instead of dropping it. The wait
                # is bounded at admission, and cancellation still wins.
                if cancelled.wait(lease.start_after_s):
                    status = "cancelled"
                    return {
                        "provider": name,
                        "status": status,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "rows": [],
                    }
            if cancelled.is_set():
                status = "cancelled"
                return {
                    "provider": name,
                    "status": status,
                    "elapsed_ms": 0,
                    "rows": [],
                }
            # Recheck and charge atomically at the transport boundary, since
            # other admitted workers may have spent the remaining allowance.
            if not _budget_allows(name, cancelled=cancelled):
                status = "cancelled" if cancelled.is_set() else "quota_exhausted"
                return {"provider": name, "status": status, "elapsed_ms": 0, "rows": []}
            rows = searcher(
                query,
                limit,
                min(settings.provider_timeout_s, _PROVIDER_TIMEOUT_CAP_S.get(name, settings.provider_timeout_s)),
            )
            if name == "brave-free" and isinstance(rows, _ProviderRows):
                exhausted_window = _exhausted_window_cooldown(rows.rate_headers)
                if exhausted_window is not None and exhausted_window > settings.brave_min_interval_s:
                    # A successful response can report that a longer quota
                    # window is now exhausted. Stop before the next call, so
                    # quota exhaustion does not have to be learned through 429.
                    cooldown_s = exhausted_window
            normalized = [item for row in rows if (item := _normalized_row(row)) is not None]
            status = "ok" if normalized else "empty"
            return {
                "provider": name,
                "status": status,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "rows": normalized,
            }
        except Exception as exc:  # noqa: BLE001
            status, safe_error, cooldown_s = _classify_provider_exception(exc, settings)
            logger.warning(
                "search-fusion: provider=%s status=%s elapsed_ms=%d cooldown_s=%s detail=%s",
                name,
                status,
                round((time.monotonic() - started) * 1000),
                "-" if cooldown_s is None else round(cooldown_s, 1),
                safe_error,
            )
            return {
                "provider": name,
                "status": status,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "rows": [],
                "error": safe_error,
            }
        finally:
            lease.finish(status, settings, cooldown_s)

    @staticmethod
    def _merge(
        outcomes: list[dict[str, Any]],
        query: str,
        limit: int,
        rrf_k: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for outcome in outcomes:
            if outcome.get("status") != "ok":
                continue
            provider = str(outcome.get("provider") or "unknown")
            for rank, row in enumerate(outcome.get("rows") or [], start=1):
                key = _canonical_url(row["url"])
                entry = merged.setdefault(
                    key,
                    {
                        "title": row["title"],
                        "url": row["url"],
                        "description": row["description"],
                        "fusion_score": 0.0,
                        "source_providers": [],
                        # Per-provider original rank. Without it the logs
                        # cannot show whether a clicked URL was rank 1 at a
                        # provider and got buried by fusion, which is what a
                        # weight re-evaluation needs.
                        "provider_ranks": {},
                        "best_rank": rank,
                    },
                )
                if provider not in entry["source_providers"]:
                    weight = _PROVIDER_WEIGHTS.get(provider, 0.85)
                    entry["fusion_score"] += weight / (rrf_k + rank)
                    entry["source_providers"].append(provider)
                    entry["provider_ranks"][provider] = rank
                entry["best_rank"] = min(entry["best_rank"], rank)
                if len(row["description"]) > len(entry["description"]):
                    entry["description"] = row["description"]
                if not entry["title"] and row["title"]:
                    entry["title"] = row["title"]

        for entry in merged.values():
            entry["fusion_score"] *= _domain_affinity(entry["url"], query)

        ranked = sorted(
            merged.values(),
            key=lambda item: (
                -item["fusion_score"],
                -len(item["source_providers"]),
                item["best_rank"],
                item["url"],
            ),
        )[: max(1, int(limit))]
        for position, row in enumerate(ranked, start=1):
            row["position"] = position
            row["fusion_score"] = round(row["fusion_score"], 8)
            row.pop("best_rank", None)
        return ranked

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        settings = self._settings()
        route, boundary = ROUTER.select(
            enabled=settings.turn_routing_enabled,
            idle_gap_s=settings.turn_idle_gap_s,
            batch_window_s=settings.batch_window_s,
        )
        started = time.monotonic()
        rounds = []
        result: dict[str, Any] = {}
        actual_route = route
        # Non-recursive: at most one recovery round, even if Fusion itself fails.
        max_rounds = 2 if settings.turn_routing_enabled and settings.auto_retry_enabled else 1
        for index in range(max_rounds):
            actual_route = route if index == 0 else "fusion"
            try:
                result = self._search_once(query, limit, settings,
                                          _PROVIDER_ORDER if actual_route == "fusion" else ("serper",))
            except Exception as exc:  # safe diagnostics; never echo provider URLs/keys
                status, error, _ = _classify_provider_exception(exc, settings)
                result = {"success": False, "error": error,
                          "metadata": {"fusion_attempts": [{"status": status}]}}
            failed = (result.get("success") is False or bool(result.get("error"))
                      or not (result.get("data") or {}).get("web"))
            rounds.append({"route": actual_route, "success": not failed,
                           "providers_used": result.get("metadata", {}).get("providers_used", []),
                           "fusion_attempts": result.get("metadata", {}).get("fusion_attempts", [])})
            if not failed:
                break
        metadata = result.setdefault("metadata", {})
        metadata.update({"search_route": actual_route, "initial_search_route": route,
                         "turn_boundary": boundary, "retry_count": len(rounds) - 1,
                         "search_rounds": rounds,
                         "routing_elapsed_ms": round((time.monotonic() - started) * 1000)})
        logger.info("search-fusion: route=%s initial=%s boundary=%s retries=%d elapsed_ms=%d",
                    actual_route, route, boundary, len(rounds) - 1, metadata["routing_elapsed_ms"])
        if actual_route == "serper" and not failed:
            try:
                SHADOW.submit(self, query, limit, settings, result, decision_context())
            except Exception:
                # Observability must never change the foreground result.
                logger.debug("search-fusion: shadow submission skipped")
        return result

    def _search_once(self, query: str, limit: int, settings: FusionSettings,
                     provider_order: tuple[str, ...]) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        available: dict[str, SearchFn] = {}

        for name in provider_order:
            searcher = self._searchers.get(name)
            if searcher is None or not self._configured(name):
                attempts.append({"provider": name, "status": "unavailable"})
                continue
            if name == "deepseek" and not _deepseek_query_valid(query):
                # Skip only this index before admission/billing. Other indexes
                # receive the original query, including every search operator.
                attempts.append({"provider": name, "status": "unsupported_query",
                                 "error": "DeepSeek requires 1..128 Unicode characters, not blank"})
                continue
            available[name] = searcher

        if not available:
            return {
                "success": False,
                "error": "search-fusion has no configured provider",
                "metadata": {"fusion": True, "fusion_attempts": attempts},
            }

        collection_started = time.monotonic()
        soft_deadline = collection_started + settings.global_timeout_s
        hard_deadline = collection_started + settings.first_result_timeout_s
        executor = self._executor or _SHARED_EXECUTOR
        future_names: dict[concurrent.futures.Future, str] = {}
        future_leases: dict[concurrent.futures.Future, _AdmissionLease] = {}
        pending: set[concurrent.futures.Future] = set()
        outcomes: list[dict[str, Any]] = []
        cancelled = threading.Event()

        def submit(name: str, searcher: SearchFn, lease: _AdmissionLease) -> None:
            try:
                future = executor.submit(
                    partial(copy_context().run, self._invoke),
                    name,
                    searcher,
                    query,
                    limit,
                    settings,
                    lease,
                    cancelled,
                )
            except Exception:  # pragma: no cover - executor failures are rare
                lease.finish("cancelled", settings)
                attempts.append({"provider": name, "status": "worker_submit_failed"})
                return
            future_names[future] = name
            future_leases[future] = lease
            pending.add(future)

        def dispatch_round(names: tuple[str, ...] | list[str]) -> list[str]:
            """Claim a slot on every idle provider; return the transient refusals."""
            transient: list[str] = []
            for name in names:
                if time.monotonic() >= hard_deadline:
                    break
                searcher = available.get(name)
                if searcher is None or name in dispatched:
                    continue
                lease, blocked = self._claim(name, settings)
                if blocked:
                    attempts.append({"provider": name, "status": blocked})
                    if blocked in _TRANSIENT_ADMISSION:
                        transient.append(name)
                    continue
                assert lease is not None
                if time.monotonic() >= hard_deadline:
                    lease.finish("cancelled", settings)
                    attempts.append({"provider": name, "status": "hard_deadline"})
                    break
                dispatched.add(name)
                submit(name, searcher, lease)
            return transient

        dispatched: set[str] = set()
        transient_refusals = dispatch_round(provider_order)

        # Busy slots can clear during a parallel batch. With no results,
        # admission gets the full first-result window; the shorter quorum
        # window only bounds waiting for corroboration.
        admission_polls = 0
        while not pending and transient_refusals:
            remaining = hard_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_ADMISSION_RETRY_S, remaining))
            if time.monotonic() >= hard_deadline:
                break
            admission_polls += 1
            transient_refusals = dispatch_round(transient_refusals)
        if admission_polls:
            logger.log(
                logging.INFO if pending else logging.WARNING,
                "search-fusion: admission polls=%d wait_ms=%d dispatched=%s",
                admission_polls,
                round((time.monotonic() - collection_started) * 1000),
                ",".join(sorted(dispatched)) or "-",
            )

        def consume(done: set[concurrent.futures.Future]) -> None:
            for future in sorted(done, key=lambda item: _PROVIDER_ORDER.index(future_names[item])):
                try:
                    outcome = future.result()
                except Exception:  # pragma: no cover - _invoke contains provider failures
                    name = future_names[future]
                    outcome = {
                        "provider": name,
                        "status": "error",
                        "rows": [],
                        "error": "provider worker failed",
                    }
                outcomes.append(outcome)
                attempts.append({
                    key: value
                    for key, value in outcome.items()
                    if key in {"provider", "status", "elapsed_ms", "error"}
                })

        def has_success() -> bool:
            return any(outcome.get("status") == "ok" for outcome in outcomes)

        def success_count() -> int:
            return sum(1 for outcome in outcomes if outcome.get("status") == "ok")

        # Quorum can never exceed the providers actually dispatched, otherwise a
        # circuit-open or rate-paced provider would make the router wait for a
        # result that can never arrive.
        quorum_target = min(settings.min_providers, len(pending) + success_count())

        def quorum_met() -> bool:
            return success_count() >= quorum_target

        # Normal path: collect every provider that finishes inside the soft
        # fusion window. A fast success does not immediately cut off consensus.
        while pending:
            remaining = soft_deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending,
                timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                break
            consume(done)

        # Consensus floor: one ranking is a single index's opinion, and the log
        # showed Brave frequently missing, leaving unverified single-provider
        # answers. Keep waiting for a second agreeing provider, bounded by
        # quorum_timeout_s so a slow index cannot stall the caller. The hard
        # deadline still wins: quorum may never extend total collection time.
        quorum_deadline = min(
            hard_deadline,
            collection_started + settings.quorum_timeout_s,
        )
        while pending and has_success() and not quorum_met():
            remaining = quorum_deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = concurrent.futures.wait(
                pending,
                timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                break
            consume(done)

        late_first = False
        # Availability floor: keep in-flight calls and transiently refused
        # providers eligible until the first success or the hard deadline.
        while (pending or transient_refusals) and not has_success():
            remaining = hard_deadline - time.monotonic()
            if remaining <= 0:
                break
            if transient_refusals:
                transient_refusals = dispatch_round(transient_refusals)
                quorum_target = min(settings.min_providers, len(future_names))
                remaining = hard_deadline - time.monotonic()
                if remaining <= 0:
                    break
            if transient_refusals:
                remaining = min(remaining, _ADMISSION_RETRY_S)
            if not pending:
                if not transient_refusals:
                    break
                time.sleep(remaining)
                continue
            done, pending = concurrent.futures.wait(
                pending,
                timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                if transient_refusals:
                    continue
                break
            consume(done)
            if has_success():
                late_first = True
                break

        # FIRST_COMPLETED wakes on one future, but another can complete before
        # this thread is scheduled again. Drain those ready futures so they are
        # not mislabeled as deadline misses.
        ready = {future for future in pending if future.done()}
        if ready:
            pending.difference_update(ready)
            consume(ready)

        # Once a late first result arrives, give nearby providers a small grace
        # period so a nearly-complete second ranking can still join the fusion.
        if late_first and pending and settings.post_first_grace_s > 0:
            grace_deadline = min(
                hard_deadline,
                time.monotonic() + settings.post_first_grace_s,
            )
            remaining = grace_deadline - time.monotonic()
            if remaining > 0:
                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=remaining,
                    return_when=concurrent.futures.ALL_COMPLETED,
                )
                consume(done)

        ready = {future for future in pending if future.done()}
        if ready:
            pending.difference_update(ready)
            consume(ready)

        # Stop any admitted worker that has not crossed the pre-network check.
        # Running HTTP calls retain their own strict transport timeout.
        cancelled.set()

        if pending:
            if late_first:
                close_status = "post_first_grace"
            elif not has_success():
                close_status = "hard_deadline"
            elif not quorum_met():
                close_status = "quorum_deadline"
            else:
                close_status = "soft_deadline"
            elapsed_ms = round((time.monotonic() - collection_started) * 1000)
            for future in pending:
                name = future_names[future]
                if future.cancel():
                    # A task canceled before _invoke cannot release its lease in
                    # the worker's finally block, so release idempotently here.
                    future_leases[future].finish("cancelled", settings)
                attempts.append({
                    "provider": name,
                    "status": close_status,
                    "elapsed_ms": elapsed_ms,
                })

        # The executor is process-wide and must outlive this search, so it is
        # never shut down here. Abandoning the futures is what honours the
        # collection deadline: each worker has its own bounded HTTP timeout and
        # releases its in-flight slot in _invoke.finally, so a tardy provider
        # drains on its own without the caller waiting for it.
        for future in pending:
            future.cancel()

        rows = self._merge(outcomes, query, limit, settings.rrf_k)
        providers_used = [
            name
            for name in _PROVIDER_ORDER
            if any(
                outcome.get("provider") == name and outcome.get("status") == "ok"
                for outcome in outcomes
            )
        ]
        metadata = {
            "fusion": True,
            "routed_provider": "search-fusion",
            "providers_used": providers_used,
            "provider_timeout_ms": round(settings.provider_timeout_s * 1000),
            "soft_timeout_ms": round(settings.global_timeout_s * 1000),
            "first_result_timeout_ms": round(settings.first_result_timeout_s * 1000),
            "post_first_grace_ms": round(settings.post_first_grace_s * 1000),
            "brave_min_interval_ms": round(settings.brave_min_interval_s * 1000),
            "quorum_target": quorum_target,
            "quorum_met": quorum_met(),
            "quorum_timeout_ms": round(settings.quorum_timeout_s * 1000),
            "collection_elapsed_ms": round((time.monotonic() - collection_started) * 1000),
            "fusion_attempts": attempts,
        }
        # One greppable line per search so quorum health can be audited from
        # agent.log; per-provider failures alone cannot show consensus outcomes.
        logger.log(
            logging.INFO if quorum_met() else logging.WARNING,
            "search-fusion: quorum=%d/%d ok=%s elapsed_ms=%d rows=%d",
            len(providers_used),
            quorum_target,
            ",".join(providers_used) or "-",
            metadata["collection_elapsed_ms"],
            len(rows),
        )
        if rows:
            return {"success": True, "data": {"web": rows}, "metadata": metadata}
        return {
            "success": False,
            "error": "search-fusion found no non-empty result before the hard deadline",
            "metadata": metadata,
        }

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Search Fusion",
            "badge": "fusion",
            "tag": "Fuses Exa + TinyFish + Brave + DuckDuckGo with a two-provider consensus floor.",
            "env_vars": [],
        }
