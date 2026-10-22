"""Defuddle-first web fetch router tool implementation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from typing import Any

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
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max(max_chars * 4, 1024 * 1024) if max_chars > 0 else 10 * 1024 * 1024)
            content_type = resp.headers.get("content-type", "")
            final_url = resp.geturl()
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
        proc = subprocess.run(
            ["defuddle", "parse", "--json", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
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
        "truncated": truncated,
    }


def _tinyfish_fetch(url: str, timeout: int, max_chars: int, ttl: int | None, plugin_ctx: Any) -> dict[str, Any]:
    dispatchers = []
    if _HIDDEN_TINYFISH_DISPATCH is not None:
        dispatchers.append(("hidden_tinyfish_fetch", _HIDDEN_TINYFISH_DISPATCH))
    if plugin_ctx is not None and hasattr(plugin_ctx, "dispatch_tool"):
        dispatchers.extend(
            (tool_name, lambda call_args, _tool_name=tool_name: plugin_ctx.dispatch_tool(_tool_name, call_args))
            for tool_name in ("mcp_tinyfish_fetch_content", "tinyfish_fetch_content", "fetch_content")
        )
    if not dispatchers:
        return {"ok": False, "method": "tinyfish_fetch", "error": "plugin dispatch context unavailable"}

    args: dict[str, Any] = {
        "urls": [url],
        "format": "markdown",
        "links": False,
        "image_links": False,
        "include_html_head": False,
    }
    if ttl is not None:
        args["ttl"] = ttl

    for tool_name, dispatcher in dispatchers:
        try:
            raw = dispatcher(args)
        except Exception as e:
            last_error = str(e)
            continue
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            data = {"raw": raw}

        # Expected TinyFish shape: {"results": [{"url", "title", "content", "error"}], "errors": ...}
        result = None
        if isinstance(data, dict):
            results = data.get("results") or data.get("data") or []
            if isinstance(results, list) and results:
                result = results[0]
        if isinstance(result, dict):
            if result.get("error"):
                return {"ok": False, "method": "tinyfish_fetch", "error": str(result.get("error"))}
            content = str(result.get("content") or result.get("markdown") or result.get("text") or "").strip()
            content, truncated = _clip(content, max_chars)
            return {
                "ok": bool(content),
                "method": "tinyfish_fetch",
                "content": content,
                "title": result.get("title") or "",
                "final_url": result.get("url") or url,
                "truncated": truncated,
                "tool_name": tool_name,
            }

        return {
            "ok": False,
            "method": "tinyfish_fetch",
            "error": "unexpected TinyFish response shape",
            "tool_name": tool_name,
        }

    return {"ok": False, "method": "tinyfish_fetch", "error": locals().get("last_error", "TinyFish fetch tool not registered")}


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
    ttl = args.get("ttl")
    try:
        ttl = int(ttl) if ttl is not None else None
    except Exception:
        ttl = None

    if not _is_http_url(url):
        return _json({"success": False, "error": "web_fetch requires an absolute http(s) URL", "url": url})
    if mode not in {"auto", "article", "raw", "rendered"}:
        return _json({"success": False, "error": f"invalid mode: {mode}", "url": url})

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
    if defuddle.get("ok") and mode != "rendered":
        return _success(url, defuddle, attempts, warnings)
    if defuddle.get("ok") and mode == "rendered":
        warnings.append("rendered_mode_requested: trying TinyFish Fetch after successful Defuddle")
    else:
        warnings.append(f"defuddle_failed: {defuddle.get('error') or 'empty extraction'}")

    tinyfish = _tinyfish_fetch(url, timeout, max_chars, ttl, plugin_ctx)
    attempts.append({"method": "tinyfish_fetch", "ok": tinyfish.get("ok"), "error": tinyfish.get("error")})
    if tinyfish.get("ok"):
        return _success(url, tinyfish, attempts, warnings)
    warnings.append(f"tinyfish_fetch_failed_or_unavailable: {tinyfish.get('error')}")

    fallback_content = defuddle.get("content") if defuddle.get("ok") else ""
    if fallback_content:
        warnings.append("returning_defuddle_content_after_rendered_fallback_failed")
        return _success(url, defuddle, attempts, warnings)

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
