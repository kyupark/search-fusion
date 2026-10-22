from concurrent.futures import ThreadPoolExecutor

import pytest


def provider(plugin, **settings):
    f = plugin.fusion
    return f.SearchFusionWebSearchProvider(
        settings=f.FusionSettings(**settings),
        searchers={name: lambda q, l, t: [{"url": "https://example.org", "title": q}]
                   for name in ("serper", "tinyfish")},
        coordinator=f._ProviderCoordinator(),
    )


def bind(plugin, session="s", turn="t", batch="a"):
    plugin.turn_routing.capture_call_context(session_id=session, turn_id=turn, api_request_id=batch)


def test_first_batch_all_queries_fusion_later_serper(plugin):
    p = provider(plugin)
    bind(plugin)
    for q in ("one", "two"):
        result = p.search(q)
        assert result["metadata"]["providers_used"] == ["tinyfish", "serper"]
        assert result["metadata"]["search_route"] == "fusion"
    bind(plugin, batch="b")
    result = p.search("three")
    assert result["metadata"]["providers_used"] == ["serper"]
    assert result["metadata"]["search_route"] == "serper"
    bind(plugin, turn="new", batch="c")
    assert p.search("four")["metadata"]["search_route"] == "fusion"


def test_concurrent_first_batch_and_session_isolation(plugin):
    r = plugin.turn_routing
    def route(i):
        bind(plugin, batch="first")
        return r.ROUTER.select()[0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(route, range(20))) == ["fusion"] * 20
    bind(plugin, batch="second")
    assert r.ROUTER.select()[0] == "serper"
    bind(plugin, session="other", batch="second")
    assert r.ROUTER.select()[0] == "fusion"
    # A delayed sibling from the original first batch stays Fusion.
    bind(plugin, batch="first")
    assert r.ROUTER.select()[0] == "fusion"


def test_bounded_worker_hook_bridge(plugin, monkeypatch):
    import contextvars
    import sys
    from types import SimpleNamespace
    import tools
    ac = SimpleNamespace(**{name: contextvars.ContextVar(name, default="") for name in
        ("_approval_session_id", "_approval_turn_id", "_approval_tool_call_id")})
    monkeypatch.setitem(sys.modules, "tools.approval_context", ac)
    monkeypatch.setattr(tools, "approval_context", ac, raising=False)
    r = plugin.turn_routing
    p = provider(plugin)
    for batch, expected in (("a", "fusion"), ("a", "fusion"), ("b", "serper")):
        # Hook and provider deliberately execute in different contexts.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(r.capture_call_context, session_id="s", turn_id="t",
                        tool_call_id="call", api_request_id=batch).result()
        ac._approval_session_id.set("s")
        ac._approval_turn_id.set("t")
        ac._approval_tool_call_id.set("call")
        assert p.search("query")["metadata"]["search_route"] == expected
        r.clear_call_context(session_id="s", turn_id="t", tool_call_id="call")
        assert not r._CALLS


def test_exact_turn_does_not_idle_reset(plugin):
    r = plugin.turn_routing
    now = [0.0]
    router = r.TurnRouter(clock=lambda: now[0])
    bind(plugin)
    assert router.select()[0] == "fusion"
    now[0] = 1000.0
    bind(plugin, batch="b")
    assert router.select()[0] == "serper"


def test_fallback_idle_gap_and_fixed_batch_window(plugin):
    r = plugin.turn_routing
    now = [0.0]
    router = r.TurnRouter(clock=lambda: now[0])
    bind(plugin, turn="", batch="")
    assert router.select() == ("fusion", "session_idle_gap")
    now[0] = 0.5
    assert router.select()[0] == "fusion"
    now[0] = 2.0
    assert router.select()[0] == "serper"
    now[0] = 123.0
    assert router.select()[0] == "fusion"


def test_no_identity_does_not_share_global_session(plugin, monkeypatch):
    r = plugin.turn_routing
    monkeypatch.setattr(r, "current_context", lambda: ("", "", ""))
    for _ in range(3):
        assert r.ROUTER.select() == ("fusion", "no_session")


def test_kill_switch_restores_all_fusion(plugin):
    p = provider(plugin, turn_routing_enabled=False)
    for batch in ("a", "b", "c"):
        bind(plugin, batch=batch)
        result = p.search(batch)
        assert result["metadata"]["providers_used"] == ["tinyfish", "serper"]
        assert result["metadata"]["turn_boundary"] == "disabled"


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_env_kill_switch(plugin, monkeypatch, value):
    monkeypatch.setenv("SEARCH_FUSION_TURN_ROUTING", value)
    assert not plugin.fusion.load_fusion_settings().turn_routing_enabled


def test_config_settings(plugin, monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules["hermes_cli.config"], "load_config", lambda: {
        "plugins": {"entries": {"search-fusion": {"fusion": {
            "turn_routing_enabled": False, "turn_idle_gap_s": 90, "batch_window_s": 0.5}}}}})
    settings = plugin.fusion.load_fusion_settings()
    assert not settings.turn_routing_enabled
    assert settings.turn_idle_gap_s == 90
    assert settings.batch_window_s == 0.5


def test_hooks_registered_and_context_cleared(plugin, monkeypatch):
    hooks = {}
    class Context:
        def register_hook(self, name, fn):
            hooks.setdefault(name, []).append(fn)
        def register_tool(self, **kwargs):
            pass
        def register_web_search_provider(self, p):
            pass
    monkeypatch.setattr(plugin, "_install_tinyfish_direct_tool_filter", lambda: None)
    monkeypatch.setattr(plugin, "_patch_web_tools_backend_availability", lambda: None)
    plugin.register(Context())
    for fn in hooks["pre_tool_call"]:
        fn(tool_name="web_search", session_id="s", turn_id="t", api_request_id="a")
    assert plugin.turn_routing.current_context() == ("s", "t", "a")
    for fn in hooks["post_tool_call"]:
        fn(tool_name="web_search")
    assert plugin.turn_routing._CONTEXT.get() == ("", "", "")
