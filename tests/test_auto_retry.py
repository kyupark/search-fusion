import httpx
import pytest

from test_turn_routing import bind, provider


@pytest.mark.parametrize("failure", ["error", "429", "timeout", "empty"])
def test_later_batch_failure_retries_fusion(plugin, failure):
    p = provider(plugin)
    bind(plugin)
    assert p.search("first")["success"]
    calls = []
    def failing(q, l, t):
        calls.append(q)
        if failure == "empty":
            return []
        if failure == "429":
            req = httpx.Request("GET", "https://example.org")
            raise httpx.HTTPStatusError("secret", request=req, response=httpx.Response(429, request=req))
        if failure == "timeout":
            raise httpx.ReadTimeout("secret")
        raise RuntimeError("secret")
    p._searchers["serper"] = failing
    bind(plugin, batch="b")
    result = p.search("recover", limit=3)
    meta = result["metadata"]
    assert result["success"]
    assert meta["initial_search_route"] == "serper"
    assert meta["search_route"] == "fusion"
    assert meta["retry_count"] == 1
    assert [r["route"] for r in meta["search_rounds"]] == ["serper", "fusion"]
    assert "tinyfish" in meta["providers_used"]
    assert "secret" not in str(result)
    # Throttled provider is NOT bypassed on Fusion fallback.
    assert len(calls) == (1 if failure == "429" else 2)


def test_first_batch_failure_retries_fusion_once(plugin):
    p = provider(plugin)
    counts = {"tinyfish": 0, "serper": 0}
    def searcher(name):
        def call(q, l, t):
            counts[name] += 1
            return [] if counts[name] == 1 else [{"url": "https://example.org", "title": q}]
        return call
    p._searchers = {name: searcher(name) for name in counts}
    bind(plugin)
    result = p.search("recover")
    assert result["success"]
    assert counts == {"tinyfish": 2, "serper": 2}
    assert result["metadata"]["retry_count"] == 1
    assert [r["route"] for r in result["metadata"]["search_rounds"]] == ["fusion", "fusion"]


def test_repeated_empty_stops_after_one_retry(plugin):
    p = provider(plugin)
    calls = []
    p._searchers = {"serper": lambda *args: calls.append(args) or []}
    bind(plugin)
    result = p.search("empty")
    assert not result["success"]
    assert len(calls) == 2
    assert result["metadata"]["retry_count"] == 1


@pytest.mark.parametrize("setting", ["turn_routing_enabled", "auto_retry_enabled"])
def test_retry_kill_switch(plugin, setting):
    p = provider(plugin, **{setting: False})
    calls = []
    p._searchers = {"serper": lambda *args: calls.append(args) or []}
    bind(plugin)
    result = p.search("empty")
    assert not result["success"]
    assert len(calls) == 1
    assert result["metadata"]["retry_count"] == 0


def test_success_with_one_failed_index_does_not_retry(plugin):
    p = provider(plugin)
    p._searchers["serper"] = lambda *args: []
    bind(plugin)
    result = p.search("partial success")
    assert result["success"]
    assert result["metadata"]["retry_count"] == 0


def test_missing_serper_falls_back(plugin):
    p = provider(plugin)
    bind(plugin)
    p.search("first")
    p._searchers.pop("serper")
    bind(plugin, batch="b")
    result = p.search("missing serper")
    assert result["success"]
    assert result["metadata"]["retry_count"] == 1
    assert result["metadata"]["providers_used"] == ["tinyfish"]


def test_raised_timeout_sanitized_and_retried_once(plugin, monkeypatch):
    p = provider(plugin)
    calls = []
    def fail(*args):
        calls.append(args)
        raise httpx.ReadTimeout("credential-secret")
    monkeypatch.setattr(p, "_search_once", fail)
    result = p.search("timeout")
    assert len(calls) == 2
    assert not result["success"]
    assert "credential-secret" not in str(result)
    assert result["metadata"]["retry_count"] == 1


def test_retry_respects_shared_budget(plugin, monkeypatch):
    f = plugin.fusion
    monkeypatch.setattr(f, "_MONTHLY_BUDGET", {"serper": 1})
    p = provider(plugin)
    calls = []
    p._searchers = {"serper": lambda *args: calls.append(args) or []}
    result = p.search("empty")
    assert not result["success"]
    assert result["metadata"]["retry_count"] == 1
    assert len(calls) == 1
    assert f._BUDGET_CACHE["counts"]["serper"] == 1
    assert any(a["status"] == "quota_exhausted" for a in result["metadata"]["fusion_attempts"])


def test_retry_env_switch(plugin, monkeypatch):
    monkeypatch.setenv("SEARCH_FUSION_AUTO_RETRY", "0")
    assert not plugin.fusion.load_fusion_settings().auto_retry_enabled
