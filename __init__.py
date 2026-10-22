"""Combined web routing plugin: bounded fusion search + Defuddle-first web_fetch.

Update-safe local architecture guardrails:
- Register a self-contained ``search-fusion`` provider that fuses Exa,
  TinyFish, and locally paced Brave under strict deadlines, with Firecrawl as
  an availability fallback when the primary fusion returns no useful result.
- Hide direct TinyFish MCP Search from the model surface. TinyFish Search is a
  discovery backend candidate, not an agent-facing tool.
- Hide direct TinyFish MCP Fetch from the model surface while preserving it as
  the internal ``web_fetch`` fallback.
"""

from __future__ import annotations

import logging
from typing import Any

from tools.registry import registry

from .frugal_extract import FrugalExtractProvider
from .fusion import SearchFusionWebSearchProvider
from .turn_routing import capture_call_context, clear_call_context
from .schemas import WEB_FETCH_SCHEMA
from .tools import set_hidden_tinyfish_dispatch, web_fetch

logger = logging.getLogger(__name__)

_TINYFISH_FETCH_TOOL_NAMES = {"mcp_tinyfish_fetch_content", "tinyfish_fetch_content", "fetch_content"}
_TINYFISH_SEARCH_TOOL_NAMES = {"mcp_tinyfish_search", "tinyfish_search"}
_TINYFISH_DIRECT_TOOL_NAMES = _TINYFISH_FETCH_TOOL_NAMES | _TINYFISH_SEARCH_TOOL_NAMES
_HIDDEN_TINYFISH_ENTRIES: dict[str, Any] = {}
_REGISTER_PATCHED = False
_ORIGINAL_REGISTER = None
_WEB_TOOLS_PATCHED = False


def _check_requirements() -> bool:
    # Always register the router. Individual routes return structured diagnostics
    # if Defuddle, TinyFish, or an underlying search provider is unavailable.
    return True


def _capture_existing_tinyfish_direct_tools() -> None:
    """Remove direct TinyFish Search/Fetch tools from model surface.

    Fetch entries are retained internally so ``web_fetch`` can use them as the
    final rendered-content fallback. Search entries are intentionally hidden and
    unused; discovery should go through ``web_search`` / ``search-fusion``.
    """
    for name in _TINYFISH_DIRECT_TOOL_NAMES:
        entry = registry.get_entry(name)
        if entry is None:
            continue
        if name in _TINYFISH_FETCH_TOOL_NAMES:
            _HIDDEN_TINYFISH_ENTRIES[name] = entry
        registry.deregister(name)
        logger.info("search-fusion: hid direct TinyFish MCP tool from model surface: %s", name)


def _install_tinyfish_direct_tool_filter() -> None:
    """Patch registry.register so future MCP refreshes keep TinyFish direct tools hidden."""
    global _REGISTER_PATCHED, _ORIGINAL_REGISTER
    if _REGISTER_PATCHED:
        return

    _ORIGINAL_REGISTER = registry.register

    def _register_filter(*args, **kwargs):
        name = kwargs.get("name") if "name" in kwargs else (args[0] if args else None)
        if name in _TINYFISH_DIRECT_TOOL_NAMES:
            # Reconstruct enough of ToolEntry by temporarily registering then capturing.
            _ORIGINAL_REGISTER(*args, **kwargs)
            entry = registry.get_entry(name)
            if entry is not None:
                if name in _TINYFISH_FETCH_TOOL_NAMES:
                    _HIDDEN_TINYFISH_ENTRIES[name] = entry
                    logger.info("search-fusion: captured TinyFish Fetch fallback tool without exposing it: %s", name)
                else:
                    logger.info("search-fusion: suppressed TinyFish Search MCP tool: %s", name)
                registry.deregister(name)
            return
        return _ORIGINAL_REGISTER(*args, **kwargs)

    registry.register = _register_filter
    _REGISTER_PATCHED = True


def _hidden_tinyfish_dispatch(tool_name: str, args: dict, **kwargs) -> str:
    entry = _HIDDEN_TINYFISH_ENTRIES.get(tool_name)
    if entry is None:
        raise RuntimeError(f"TinyFish fallback tool is not registered internally: {tool_name}")
    if entry.is_async:
        from model_tools import _run_async

        return _run_async(entry.handler(args, **kwargs))
    return entry.handler(args, **kwargs)


def _block_direct_tinyfish_tools(tool_name: str, args: dict | None = None, **kwargs):
    """Keep TinyFish MCP tools behind web_search/web_fetch routing."""
    if tool_name in _TINYFISH_FETCH_TOOL_NAMES:
        return {
            "action": "block",
            "message": (
                "Use web_fetch first for known URLs. the plugin's routing policy is "
                "Defuddle first, with TinyFish Fetch only as the web_fetch fallback."
            ),
        }
    if tool_name in _TINYFISH_SEARCH_TOOL_NAMES:
        return {
            "action": "block",
            "message": "Use web_search. TinyFish Search is not exposed directly in this Hermes setup.",
        }
    return None


def _patch_web_tools_backend_availability() -> None:
    """Teach older Hermes checkouts that ``search-fusion`` is a valid web backend.

    Newer upstream builds know this natively. This monkeypatch is narrow and
    harmless there; it only intercepts the single backend name this plugin owns.
    """
    global _WEB_TOOLS_PATCHED
    if _WEB_TOOLS_PATCHED:
        return
    try:
        import tools.web_tools as web_tools
    except Exception as exc:  # noqa: BLE001
        logger.debug("search-fusion: could not patch web_tools backend availability: %s", exc)
        return

    original = getattr(web_tools, "_is_backend_available", None)
    if original is None:
        return

    def _is_backend_available_patched(backend: str) -> bool:
        if str(backend or "").strip().lower() == "search-fusion":
            return True
        return original(backend)

    web_tools._is_backend_available = _is_backend_available_patched
    _WEB_TOOLS_PATCHED = True
    logger.info("search-fusion: patched web_tools availability for search-fusion")


def _register_search_fusion(ctx) -> None:
    """Register the update-safe, locally owned fusion provider."""
    ctx.register_web_search_provider(SearchFusionWebSearchProvider())
    logger.info("search-fusion: registered local bounded search-fusion provider")
    ctx.register_web_search_provider(FrugalExtractProvider())
    logger.info("search-fusion: registered frugal-extract provider (local-first, Firecrawl last resort)")
    _patch_web_tools_backend_availability()


def register(ctx) -> None:
    """Register search routing and the fetch router tool."""
    _install_tinyfish_direct_tool_filter()
    _capture_existing_tinyfish_direct_tools()
    set_hidden_tinyfish_dispatch(_hidden_tinyfish_dispatch)

    _register_search_fusion(ctx)

    def _handler(args: dict, **kwargs) -> str:
        return web_fetch(args, plugin_ctx=ctx, **kwargs)

    ctx.register_tool(
        name="web_fetch",
        toolset="web",
        schema=WEB_FETCH_SCHEMA,
        handler=_handler,
        check_fn=_check_requirements,
        description="Defuddle-first known-URL fetch/extraction router with TinyFish fallback.",
        emoji="🌐",
    )
    ctx.register_hook("pre_tool_call", _block_direct_tinyfish_tools)
    ctx.register_hook("pre_tool_call", capture_call_context)
    ctx.register_hook("post_tool_call", clear_call_context)
    logger.info("search-fusion: turn routing hooks active (session/turn/api_request IDs; idle-gap fallback)")
