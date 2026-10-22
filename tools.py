"""Defuddle-first web fetch router tool implementation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
from html import unescape
from typing import Any

from .url_safety import UnsafeURL, guarded_fetch, validate_url

RAW_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".tsv",
    ".rss",
    ".atom",
}
RAW_HOSTS = {"raw.githubusercontent.com", "gist.githubusercontent.com"}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_HIDDEN_TINYFISH_DISPATCH = None


def set_hidden_tinyfish_dispatch(dispatcher) -> None:
    """Set internal TinyFish Fetch dispatcher supplied by the plugin loader."""
    global _HIDDEN_TINYFISH_DISPATCH
    _HIDDEN_TINYFISH_DISPATCH = dispatcher


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _is_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_raw(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host in RAW_HOSTS:
        return True
    if "/raw/" in path or path.endswith("/raw"):
        return True
    return any(path.endswith(ext) for ext in RAW_EXTENSIONS)


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</p\s*>", "\n\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _direct_http(url: str, timeout: int, max_chars: int) -> dict[str, Any]:
    try:
        raw, headers, final_url = guarded_fetch(
            url, timeout, max(max_chars * 4, 1024 * 1024) if max_chars > 0 else 10 * 1024 * 1024,
            {"User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        content_type = headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}", "method": "direct_http"}
    except Exception as e:
        return {"ok": False, "error": str(e), "method": "direct_http"}

    text = raw.decode("utf-8", errors="replace")
    if "html" in content_type.lower():
        text = _html_to_text(text)
    text, truncated = _clip(text, max_chars)
    return {
        "ok": bool(text.strip()),
        "method": "direct_http",
        "content": text,
        "content_type": content_type,
        "final_url": final_url,
        "truncated": truncated,
    }


def _run_defuddle(url: str, timeout: int, max_chars: int) -> dict[str, Any]:
    if not shutil.which("defuddle"):
        return {"ok": False, "method": "defuddle", "error": "defuddle binary not found on PATH"}
    try:
        raw, _, final_url = guarded_fetch(
            url, timeout, 10 * 1024 * 1024,
            {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        )
        # The CLI must never receive a URL: it otherwise follows redirects internally.
        # A preload also blocks secondary HTTP/TLS/socket requests from async extractors.
        env = dict(os.environ)
        env["NODE_OPTIONS"] = "--require " + json.dumps(str(Path(__file__).with_name("defuddle_offline.cjs")))
        with tempfile.TemporaryDirectory(prefix="search-fusion-html-") as temp:
            source = Path(temp) / "page.html"
            source.write_bytes(raw)
            proc = subprocess.run(
                ["defuddle", "parse", "--json", str(source)],
                check=False, capture_output=True, text=True, timeout=timeout, env=env,
            )
    except subprocess.TimeoutExpired:
        return {"ok": False, "method": "defuddle", "error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ok": False, "method": "defuddle", "error": str(e)}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "method": "defuddle", "error": err[:1000] or f"exit {proc.returncode}"}

    try:
        data = json.loads(proc.stdout)
    except Exception:
        # Older/odd Defuddle output: fall back to markdown stdout if non-empty.
        text = (proc.stdout or "").strip()
        text, truncated = _clip(text, max_chars)
        return {
            "ok": bool(text),
            "method": "defuddle",
            "content": text,
            "truncated": truncated,
            "warning": "defuddle_non_json_output",
            "final_url": final_url,
        }

    content = (data.get("contentMarkdown") or data.get("markdown") or data.get("text") or "").strip()
    if not content and data.get("content"):
        content = _html_to_text(str(data.get("content") or ""))
    content, truncated = _clip(content, max_chars)
    return {
        "ok": bool(content.strip()),
        "method": "defuddle",
        "content": content,
        "title": data.get("title") or "",
        "description": data.get("description") or "",
        "author": data.get("author") or "",
        "published": data.get("published") or "",
        "language": data.get("language") or "",
        "word_count": data.get("wordCount"),
        "final_url": final_url,
        "truncated": truncated,
    }


def _tinyfish_fetch(url: str, timeout: int, max_chars: int, ttl: int | None, plugin_ctx: Any) -> dict[str, Any]:
    # A remote fetch service cannot expose/validate every redirect or pin our DNS answers.
    return {"ok": False, "method": "tinyfish_fetch",
            "error": "remote fetching disabled: redirect destinations cannot be validated"}


def _success(url: str, result: dict[str, Any], attempts: list[dict[str, Any]], warnings: list[str]) -> str:
    return _json(
        {
            "success": True,
            "url": url,
            "method": result.get("method"),
            "fallbacks_attempted": [a["method"] for a in attempts],
            "title": result.get("title") or "",
            "description": result.get("description") or "",
            "content": result.get("content") or "",
            "truncated": bool(result.get("truncated")),
            "metadata": {k: v for k, v in result.items() if k not in {"ok", "method", "content", "title", "description", "error"}},
            "warnings": warnings,
        }
    )


def web_fetch(args: dict[str, Any], *, plugin_ctx: Any = None, **_: Any) -> str:
    """Fetch a known URL through the Defuddle-first routing ladder."""
    url = str(args.get("url") or "").strip()
    mode = str(args.get("mode") or "auto").strip().lower()
    try:
        max_chars = int(args.get("max_chars") or 50000)
    except Exception:
        max_chars = 50000
    try:
        timeout = int(args.get("timeout") or 30)
    except Exception:
        timeout = 30

    if not _is_http_url(url):
        return _json({"success": False, "error": "web_fetch requires an absolute http(s) URL", "url": url})
    try:
        validate_url(url)
    except UnsafeURL as exc:
        return _json({"success": False, "error": str(exc), "url": url})
    if mode not in {"auto", "article", "raw", "rendered"}:
        return _json({"success": False, "error": f"invalid mode: {mode}", "url": url})

    if mode == "rendered":
        return _json({"success": False, "url": url, "needs_browser": True,
                      "error": "rendered fetching unavailable: remote redirect safety cannot be enforced"})
    attempts: list[dict[str, Any]] = []
    warnings: list[str] = []

    raw_first = mode == "raw" or (mode == "auto" and _looks_raw(url))
    if raw_first:
        direct = _direct_http(url, timeout, max_chars)
        attempts.append({"method": "direct_http", "ok": direct.get("ok"), "error": direct.get("error")})
        if direct.get("ok"):
            return _success(url, direct, attempts, warnings)
        warnings.append(f"direct_http_failed: {direct.get('error')}")
        if mode == "raw":
            return _json({"success": False, "url": url, "method": None, "fallbacks_attempted": attempts, "warnings": warnings})

    # Policy: Defuddle first for known normal URLs/articles/docs.
    defuddle = _run_defuddle(url, timeout, max_chars)
    attempts.append({"method": "defuddle", "ok": defuddle.get("ok"), "error": defuddle.get("error")})
    if defuddle.get("ok"):
        return _success(url, defuddle, attempts, warnings)
    warnings.append(f"defuddle_failed: {defuddle.get('error') or 'empty extraction'}")

    # A plain guarded GET remains useful when Defuddle is absent or cannot extract a page.
    if not raw_first:
        direct = _direct_http(url, timeout, max_chars)
        attempts.append({"method": "direct_http", "ok": direct.get("ok"), "error": direct.get("error")})
        if direct.get("ok"):
            return _success(url, direct, attempts, warnings)
        warnings.append(f"direct_http_failed: {direct.get('error')}")

    return _json(
        {
            "success": False,
            "url": url,
            "method": None,
            "fallbacks_attempted": attempts,
            "warnings": warnings,
            "needs_browser": True,
            "suggested_next": "Use local browser tools for JS/login/interaction, or Wayback for blocked/deleted pages.",
        }
    )
