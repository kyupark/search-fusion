"""Public-page extraction: guarded ATS JSON, direct HTTP, then offline Defuddle.

All local HTTP requests resolve and pin public IPs and validate every redirect hop.
Remote Firecrawl/TinyFish fetching is intentionally unavailable: these APIs do not
expose redirect hops for validation. A browser is needed for JS or authenticated pages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from agent.web_search_provider import WebSearchProvider

from .url_safety import UnsafeURL, validate_url
from .tools import _clip, _direct_http, _html_to_text, _looks_raw, _run_defuddle

logger = logging.getLogger(__name__)

NAME = "frugal-extract"
# Empty Defuddle output on a real page is usually a JS shell or a bot wall; below this the
# local result is not trusted and the caller receives browser guidance.
_MIN_LOCAL_CHARS = 200
# Definitive client errors: a missing or authenticated page needs caller intervention.
_FINAL_HTTP = {"401", "404", "410"}
_BOILERPLATE_MARKERS = (
    "enable javascript",
    "javascript is required",
    "please turn on javascript",
    "checking your browser",
    "verify you are human",
    "access denied",
    "just a moment",
)

# Match posting URLs only; board/search pages retain the generic extraction ladder.
_ATS_ADAPTERS = (
    ("ashby", r"jobs\.ashbyhq\.com",
     r"/(?P<org>[\w-]+)/(?P<id>[\w-]+)(?:/application)?/?",
     "https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"),
    ("workable", r"apply\.workable\.com",
     r"/(?P<account>[\w-]+)/j/(?P<shortcode>[\w-]+)(?:/apply)?/?",
     "https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}"),
    ("workday", r"(?P<tenant>[a-z0-9-]+)\.(?P<shard>wd\d+)\.myworkdayjobs\.com",
     r"(?:/[a-z]{2}-[A-Za-z]{2})?/(?P<site>[\w-]+)/job/(?P<path>.+?)/*",
     "https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}"),
)


def _ats_markdown(html: str) -> str:
    """Keep basic paragraph, heading, and list structure without a new dependency."""
    html = re.sub(r"(?i)<h([1-6])\b[^>]*>", lambda m: "\n\n" + "#" * int(m[1]) + " ", html)
    html = re.sub(r"(?i)</(?:h[1-6]|div|ul|ol)\s*>", "\n\n", html)
    html = re.sub(r"(?i)<li\b[^>]*>", "\n- ", html)
    html = re.sub(r"(?i)</li\s*>", "\n", html)
    return _html_to_text(html)


def _ats_extract(url: str, timeout: int, max_chars: int, attempts: list[str]) -> dict[str, Any] | None:
    try:
        validate_url(url)
    except UnsafeURL:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    for name, host_pattern, path_pattern, endpoint in _ATS_ADAPTERS:
        host = re.fullmatch(host_pattern, parsed.hostname or "")
        path = re.fullmatch(path_pattern, parsed.path)
        if not host or not path:
            continue
        method = f"ats_{name}"
        attempts.append(method)
        try:
            endpoint = endpoint.format(**host.groupdict(), **path.groupdict())
            # Parse the complete JSON before clipping the selected posting. The shared
            # transport caps this read at 10 MiB, independently of the output limit.
            response = _direct_http(endpoint, timeout, 0)
            if not response.get("ok"):
                return None
            data = json.loads(response["content"])
            if name == "ashby":
                posting_path = f'/{path["org"]}/{path["id"]}'
                # The public board API identifies postings by jobUrl, unlike
                # the authenticated API's id-based response.
                job = next(j for j in data["jobs"] if j.get("id") == path["id"] or (
                    (job_url := urlparse(j.get("jobUrl") or "")).hostname == parsed.hostname
                    and job_url.path.rstrip("/") == posting_path
                ))
                description = job.get("descriptionHtml") or job.get("descriptionPlain") or ""
                location = job.get("location") or ""
            elif name == "workable":
                job = data
                description = job.get("description") or ""
                if not _ats_markdown(description):
                    return None
                for key in ("requirements", "benefits"):
                    if job.get(key):
                        description += f"<h2>{key.title()}</h2>{job[key]}"
                loc = job.get("location") or {}
                location = ", ".join(loc[k] for k in ("city", "region", "country") if loc.get(k))
            else:
                job = data["jobPostingInfo"]
                description = job.get("jobDescription") or ""
                location = job.get("location") or ""
            title = _html_to_text(job.get("title") or "")
            description = _ats_markdown(description)
            if not title or not description:
                return None
            content = f"# {title}\n\n"
            if location:
                content += f"Location: {_html_to_text(location)}\n\n"
            content, truncated = _clip(content + description, max_chars)
            return {"ok": True, "method": method, "title": title, "content": content,
                    "final_url": url, "content_type": "text/markdown", "truncated": truncated}
        except Exception:  # noqa: BLE001 — API/network/schema failures preserve the old ladder.
            logger.debug("frugal-extract: %s adapter failed for %s", name, url, exc_info=True)
            return None
    return None


def _settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        entry = (((cfg.get("plugins") or {}).get("entries") or {}).get("search-fusion") or {})
        raw = entry.get("frugal_extract") or {}
    except Exception:  # noqa: BLE001
        raw = {}
    return {
        "timeout_s": int(raw.get("timeout_s") or 30),
        "max_chars": int(raw.get("max_chars") or 200_000),
        "min_local_chars": int(raw.get("min_local_chars") or _MIN_LOCAL_CHARS),
    }


def _looks_like_shell(content: str, min_chars: int) -> str | None:
    """Reason string when local content is not trustworthy, else ``None``."""
    text = (content or "").strip()
    if len(text) < min_chars:
        return f"local content too short ({len(text)} chars)"
    head = text[:1500].lower()
    for marker in _BOILERPLATE_MARKERS:
        if marker in head:
            return f"local content looks like a JS/bot wall ({marker!r})"
    return None


def _entry(url: str, result: dict[str, Any], method: str, attempts: list[str]) -> dict[str, Any]:
    content = result.get("content") or ""
    return {
        "url": result.get("final_url") or url,
        "title": result.get("title") or "",
        "content": content,
        "raw_content": content,
        "metadata": {
            "extract_method": method,
            "attempts": attempts,
            **{k: v for k, v in result.items() if k in ("description", "author", "published", "content_type", "truncated")},
        },
    }


def _error(url: str, error: str, attempts: list[str]) -> dict[str, Any]:
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error, "metadata": {"attempts": attempts}}


class FrugalExtractProvider(WebSearchProvider):
    """Extract-only provider: guarded ATS JSON → direct HTTP → offline Defuddle."""

    def __init__(self, settings_override: dict[str, Any] | None = None):
        self._settings_override = settings_override

    @property
    def name(self) -> str:
        return NAME

    @property
    def display_name(self) -> str:
        return "Frugal Extract (guarded local extraction)"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def _cfg(self) -> dict[str, Any]:
        return self._settings_override or _settings()

    # --- per-URL ladder -----------------------------------------------------------------

    def _local(self, url: str, cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str, bool]:
        """Run the local rungs. Returns (result or None, attempts, last_error, escalate)."""
        attempts: list[str] = []
        timeout, max_chars, min_chars = cfg["timeout_s"], cfg["max_chars"], cfg["min_local_chars"]
        ats = _ats_extract(url, timeout, max_chars, attempts)
        if ats is not None:
            return ats, attempts, "", False
        # Rung 1: plain GET. Non-HTML bodies (JSON/CSV/XML/plain text) are final here; a definitive
        # HTTP 4xx (404/410/401) is also final.
        direct = _direct_http(url, timeout, max_chars)
        attempts.append("direct_http")
        ctype = str(direct.get("content_type") or "").lower()
        if direct.get("ok") and ("html" not in ctype or _looks_raw(url)):
            return direct, attempts, "", False
        err = str(direct.get("error") or "")
        if m := re.match(r"HTTP (\d{3})", err):
            if m.group(1) in _FINAL_HTTP:
                return None, attempts, f"direct_http: {err}", False
        last_error = f"direct_http: {err}" if not direct.get("ok") else ""
        # Rung 2: Defuddle article/docs extraction (HTML only).
        defuddle = _run_defuddle(url, timeout, max_chars)
        attempts.append("defuddle")
        if defuddle.get("ok"):
            reason = _looks_like_shell(defuddle.get("content", ""), min_chars)
            if reason is None:
                return defuddle, attempts, "", False
            last_error = f"defuddle: {reason}"
        else:
            last_error = (last_error + "; " if last_error else "") + f"defuddle: {defuddle.get('error')}"
        # Rung 2b: the raw GET already fetched HTML; if it isn't a JS shell, keep it (free).
        if direct.get("ok") and _looks_like_shell(direct.get("content", ""), min_chars) is None:
            return direct, attempts, "", False
        return None, attempts, last_error, True

    async def _one(self, url: str, format: str | None, cfg: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_url(url)
        except UnsafeURL as exc:
            return _error(url, str(exc), [])
        result, attempts, last_error, _ = await asyncio.to_thread(self._local, url, cfg)
        if result is not None:
            method = result.get("method") or attempts[-1]
            logger.info("frugal-extract: %s via %s (%s)", url, method, "/".join(attempts))
            return _entry(url, result, method, attempts)
        return _error(url, last_error + "; remote extraction disabled: redirect safety cannot be enforced", attempts)

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        cfg = self._cfg()
        format = kwargs.get("format")
        return [await self._one(url, format, cfg) for url in urls]
