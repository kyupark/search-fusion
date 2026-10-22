from __future__ import annotations

import importlib.util
import concurrent.futures
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent
PACKAGE = "kyu_search_fusion_testpkg"
MODULE = f"{PACKAGE}.fusion"
if MODULE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
    spec = importlib.util.spec_from_file_location(MODULE, ROOT / "fusion.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = module
    spec.loader.exec_module(module)

fusion = sys.modules[MODULE]
SearchFusionWebSearchProvider = fusion.SearchFusionWebSearchProvider
FusionSettings = fusion.FusionSettings


def setup_function():
    # Production shares one coordinator process-wide. Each test gets an
    # isolated process state so tardy workers cannot contaminate later cases.
    coordinator_type = getattr(fusion, "_ProviderCoordinator")
    setattr(fusion, "_PROCESS_COORDINATOR", coordinator_type())


def settings(**overrides):
    values = {
        "provider_timeout_s": 0.1,
        "global_timeout_s": 0.15,
        "first_result_timeout_s": 0.25,
        "post_first_grace_s": 0.02,
        "max_inflight_per_provider": 4,
        "circuit_failures": 2,
        "circuit_cooldown_s": 10.0,
        # Keep the consensus floor on (production default) but short, so tests
        # that assert latency invariants are not dominated by quorum waiting.
        # Quorum-specific tests override this explicitly.
        "quorum_timeout_s": 0.12,
        "rrf_k": 60,
    }
    values.update(overrides)
    return FusionSettings(**values)


def test_fuses_and_deduplicates_canonical_urls():
    def exa(query, limit, timeout):
        return [
            {
                "title": "Official result",
                "url": "https://www.example.com/story/?utm_source=exa",
                "description": "short",
            },
            {"title": "Exa only", "url": "https://exa.example/item", "description": "exa"},
        ]

    def brave(query, limit, timeout):
        return [
            {
                "title": "Same result",
                "url": "https://example.com/story",
                "description": "the longer description should win",
            },
            {"title": "Brave only", "url": "https://brave.example/item", "description": "brave"},
        ]

    router = SearchFusionWebSearchProvider(
        settings=settings(),
        searchers={"exa": exa, "brave-free": brave},
    )
    result = router.search("query", limit=5)

    assert result["success"] is True
    rows = result["data"]["web"]
    assert len(rows) == 3
    assert fusion._canonical_url(rows[0]["url"]) == fusion._canonical_url("https://example.com/story")
    assert set(rows[0]["source_providers"]) == {"exa", "brave-free"}
    assert rows[0]["description"] == "the longer description should win"
    assert [row["position"] for row in rows] == [1, 2, 3]
    assert result["metadata"]["fusion"] is True
    assert set(result["metadata"]["providers_used"]) == {"exa", "brave-free"}


def test_global_deadline_returns_fast_provider_without_waiting_for_slow_one():
    def slow(query, limit, timeout):
        time.sleep(0.30)
        return [{"title": "late", "url": "https://late.example", "description": "late"}]

    def fast(query, limit, timeout):
        return [{"title": "fast", "url": "https://fast.example", "description": "fast"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(provider_timeout_s=0.05, global_timeout_s=0.07),
        searchers={"exa": slow, "brave-free": fast},
    )
    started = time.monotonic()
    result = router.search("query", limit=5)
    elapsed = time.monotonic() - started

    # The invariant is that the router does not join the 300ms slow worker.
    # Allow scheduler overhead on loaded macOS runners.
    assert elapsed < 0.28
    assert result["success"] is True
    assert [row["title"] for row in result["data"]["web"]] == ["fast"]
    assert result["metadata"]["providers_used"] == ["brave-free"]
    attempts = {item["provider"]: item["status"] for item in result["metadata"]["fusion_attempts"]}
    # The slow provider is now abandoned at the quorum deadline rather than the
    # soft one: the router briefly seeks a second opinion, then gives up.
    assert attempts["exa"] == "quorum_deadline"


def test_transport_timeout_falls_back_to_other_provider():
    def timeout(query, limit, deadline):
        raise TimeoutError("simulated provider timeout")

    def good(query, limit, deadline):
        return [{"title": "good", "url": "https://good.example", "description": "ok"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(),
        searchers={"exa": timeout, "brave-free": good},
    )
    result = router.search("query", limit=5)

    assert result["success"] is True
    assert result["data"]["web"][0]["title"] == "good"
    attempts = {item["provider"]: item["status"] for item in result["metadata"]["fusion_attempts"]}
    assert attempts["exa"] == "timeout"
    assert attempts["brave-free"] == "ok"


def test_query_domain_affinity_lifts_likely_first_party_result():
    def exa(query, limit, deadline):
        return [{"title": "Secondary", "url": "https://news.example/cursor", "description": "secondary"}]

    def brave(query, limit, deadline):
        rows = [
            {
                "title": f"Secondary {n}",
                "url": f"https://secondary{n}.example/item",
                "description": "secondary",
            }
            for n in range(1, 5)
        ]
        rows.append({"title": "Official", "url": "https://cursor.com/blog/router", "description": "first party"})
        return rows

    router = SearchFusionWebSearchProvider(
        settings=settings(),
        searchers={"exa": exa, "brave-free": brave},
    )
    result = router.search("Cursor Router model routing", limit=5)

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://cursor.com/blog/router"


def test_circuit_breaker_skips_repeatedly_failing_provider():
    calls = {"exa": 0}

    def broken(query, limit, deadline):
        calls["exa"] += 1
        raise TimeoutError("still slow")

    def good(query, limit, deadline):
        return [{"title": "good", "url": "https://good.example", "description": "ok"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(circuit_failures=2, circuit_cooldown_s=30),
        searchers={"exa": broken, "tinyfish": good},
    )
    assert router.search("one")["success"] is True
    assert router.search("two")["success"] is True
    third = router.search("three")

    assert third["success"] is True
    assert calls["exa"] == 2
    attempts = {item["provider"]: item["status"] for item in third["metadata"]["fusion_attempts"]}
    assert attempts["exa"] == "circuit_open"


def test_failure_is_structured_when_every_provider_misses_deadline():
    def timeout(query, limit, deadline):
        raise TimeoutError("no result")

    router = SearchFusionWebSearchProvider(
        settings=settings(),
        searchers={"exa": timeout, "brave-free": timeout},
    )
    result = router.search("query")

    assert result["success"] is False
    assert "before the hard deadline" in result["error"]
    assert result["metadata"]["fusion"] is True
    assert result["metadata"]["providers_used"] == []


@pytest.mark.parametrize(
    ("url_a", "url_b"),
    [
        ("https://www.example.com/a/?utm_medium=x", "http://example.com/a"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2#fragment"),
    ],
)
def test_canonical_url_ignores_scheme_tracking_and_query_order(url_a, url_b):
    assert fusion._canonical_url(url_a) == fusion._canonical_url(url_b)


def test_waits_past_soft_window_for_first_success():
    def late_success(query, limit, deadline):
        time.sleep(0.11)
        return [{"title": "late success", "url": "https://late.example/ok", "description": "ok"}]

    def fast_failure(query, limit, deadline):
        raise TimeoutError("fast failure")

    router = SearchFusionWebSearchProvider(
        settings=settings(
            provider_timeout_s=0.20,
            global_timeout_s=0.04,
            first_result_timeout_s=0.22,
            post_first_grace_s=0.01,
        ),
        searchers={"exa": late_success, "brave-free": fast_failure},
    )
    started = time.monotonic()
    result = router.search("query")
    elapsed = time.monotonic() - started

    assert 0.09 <= elapsed < 0.35
    assert result["success"] is True
    assert result["metadata"]["providers_used"] == ["exa"]
    assert result["data"]["web"][0]["title"] == "late success"


def test_late_first_success_collects_nearby_provider_during_grace(monkeypatch):
    allow_nearby = threading.Event()
    real_wait = concurrent.futures.wait
    wait_calls = 0

    def controlled_wait(futures, timeout=None, return_when=None):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            # Deterministically end the soft collection window with no result.
            return set(), set(futures)
        if wait_calls == 3:
            # The third wait is the post-first grace collection.
            allow_nearby.set()
        return real_wait(futures, timeout=timeout, return_when=return_when)

    def first(query, limit, deadline):
        return [{"title": "shared", "url": "https://shared.example", "description": "first"}]

    def nearby(query, limit, deadline):
        assert allow_nearby.wait(timeout=0.5)
        return [{"title": "shared", "url": "https://shared.example", "description": "nearby longer"}]

    monkeypatch.setattr(fusion.concurrent.futures, "wait", controlled_wait)
    router = SearchFusionWebSearchProvider(
        settings=settings(
            provider_timeout_s=0.50,
            global_timeout_s=0.10,
            first_result_timeout_s=0.50,
            post_first_grace_s=0.20,
        ),
        searchers={"exa": first, "tinyfish": nearby},
    )
    result = router.search("query")

    assert result["success"] is True
    assert result["metadata"]["providers_used"] == ["exa", "tinyfish"]
    assert set(result["data"]["web"][0]["source_providers"]) == {"exa", "tinyfish"}


def test_hard_deadline_bounds_all_slow_providers():
    def too_slow(query, limit, deadline):
        time.sleep(0.30)
        return [{"title": "too late", "url": "https://late.example", "description": "late"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(
            provider_timeout_s=0.25,
            global_timeout_s=0.03,
            first_result_timeout_s=0.08,
            post_first_grace_s=0,
        ),
        searchers={"exa": too_slow, "tinyfish": too_slow},
    )
    started = time.monotonic()
    result = router.search("query")
    elapsed = time.monotonic() - started

    # macOS CI thread scheduling can overshoot very short waits substantially;
    # the key invariant is that search returns before joining 300ms workers.
    assert elapsed < 0.28
    assert result["success"] is False
    attempts = {item["provider"]: item["status"] for item in result["metadata"]["fusion_attempts"]}
    assert attempts["exa"] == "hard_deadline"
    assert attempts["tinyfish"] == "hard_deadline"


def test_fusion_uses_all_three_free_providers():
    def searcher(name):
        def run(query, limit, deadline):
            return [
                {"title": "shared", "url": "https://shared.example/item", "description": name},
                {"title": name, "url": f"https://{name}.example/item", "description": name},
            ]
        return run

    router = SearchFusionWebSearchProvider(
        settings=settings(),
        searchers={name: searcher(name) for name in ["exa", "tinyfish", "brave-free"]},
    )
    result = router.search("query", limit=5)

    assert result["success"] is True
    assert result["metadata"]["providers_used"] == ["exa", "tinyfish", "brave-free"]
    assert result["data"]["web"][0]["url"] == "https://shared.example/item"
    assert len(result["data"]["web"][0]["source_providers"]) == 3


def test_router_never_contacts_a_metered_provider():
    """Firecrawl bills 2 credits per 10 results; it must not be reachable."""

    router = SearchFusionWebSearchProvider()

    assert "firecrawl" not in router._searchers
    assert "firecrawl" not in fusion._PROVIDER_ORDER
    assert not hasattr(fusion, "_firecrawl_search")
    assert not hasattr(fusion, "_FIRECRAWL_ENDPOINT")
    assert "firecrawl" not in fusion.__dict__.get("_PROVIDER_WEIGHTS", {})
    assert router._configured("firecrawl") is False


def test_surviving_provider_answers_when_others_fail():
    def failed(query, limit, deadline):
        raise TimeoutError("provider unavailable")

    def ok(query, limit, deadline):
        return [{"title": "live", "url": "https://live.example", "description": "ok"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(),
        searchers={"exa": failed, "tinyfish": failed, "brave-free": ok},
    )
    result = router.search("query")

    assert result["success"] is True
    assert result["metadata"]["providers_used"] == ["brave-free"]


def test_quorum_waits_for_a_second_provider_past_the_soft_window():
    def fast(query, limit, deadline):
        return [{"title": "fast", "url": "https://fast.example/a", "description": "fast"}]

    def slow(query, limit, deadline):
        time.sleep(0.20)
        return [{"title": "slow", "url": "https://fast.example/a", "description": "slow"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(
            global_timeout_s=0.05,
            # The quorum window is clamped by the hard deadline, so this must
            # exceed it or the test races the 0.20s provider on a loaded box.
            first_result_timeout_s=1.5,
            quorum_timeout_s=1.0,
        ),
        searchers={"exa": fast, "tinyfish": slow},
    )
    result = router.search("query")

    # Without the quorum floor the 50ms soft window would have returned exa alone.
    assert result["success"] is True
    assert result["metadata"]["quorum_target"] == 2
    assert result["metadata"]["quorum_met"] is True
    assert sorted(result["metadata"]["providers_used"]) == ["exa", "tinyfish"]
    assert len(result["data"]["web"][0]["source_providers"]) == 2


def test_quorum_wait_is_bounded_and_still_returns_one_provider():
    def fast(query, limit, deadline):
        return [{"title": "fast", "url": "https://fast.example/a", "description": "fast"}]

    def never(query, limit, deadline):
        time.sleep(5.0)
        raise AssertionError("must not be awaited")

    router = SearchFusionWebSearchProvider(
        settings=settings(global_timeout_s=0.05, quorum_timeout_s=0.25),
        searchers={"exa": fast, "tinyfish": never},
    )
    started = time.monotonic()
    result = router.search("query")
    elapsed = time.monotonic() - started

    assert result["success"] is True
    assert elapsed < 0.9
    assert result["metadata"]["providers_used"] == ["exa"]
    assert result["metadata"]["quorum_met"] is False
    attempts = {item["provider"]: item["status"] for item in result["metadata"]["fusion_attempts"]}
    assert attempts["tinyfish"] == "quorum_deadline"


def test_quorum_target_never_exceeds_dispatched_providers():
    """A blocked provider must not make the router wait for an impossible quorum."""

    def fast(query, limit, deadline):
        return [{"title": "fast", "url": "https://fast.example/a", "description": "fast"}]

    configured = settings(global_timeout_s=0.05, quorum_timeout_s=5.0, min_providers=3)
    router = SearchFusionWebSearchProvider(
        settings=configured,
        searchers={"exa": fast},
    )
    started = time.monotonic()
    result = router.search("query")
    elapsed = time.monotonic() - started

    assert result["success"] is True
    assert result["metadata"]["quorum_target"] == 1
    assert result["metadata"]["quorum_met"] is True
    assert elapsed < 0.5


def test_brave_admission_allows_queued_turns_instead_of_rejecting_them():
    """Brave's limit is pacing, not concurrency; queued turns must be admitted."""

    configured = settings(brave_min_interval_s=1.0, brave_max_wait_s=1.2)
    coordinator = fusion._ProviderCoordinator()

    first, blocked = coordinator.claim("brave-free", configured)
    assert blocked is None and first is not None

    # Previously this returned concurrency_limited because in-flight was capped
    # at 1, so a legitimately paced turn was dropped and fusion lost Brave.
    second, blocked = coordinator.claim("brave-free", configured)
    assert blocked is None and second is not None
    assert second.start_after_s > 0


def test_ddgs_is_a_keyless_fourth_provider():
    """DuckDuckGo needs no API key, so availability is package importability."""

    router = SearchFusionWebSearchProvider()
    assert "ddgs" in fusion._PROVIDER_ORDER
    assert "ddgs" in router._searchers
    # Keyless: must not be gated on any *_API_KEY lookup.
    assert router._configured("ddgs") is fusion._ddgs_available()


def test_ddgs_normalizes_href_and_body_fields():
    """ddgs returns href/body, not url/description; the adapter must map them."""

    captured = {}

    class FakeDDGS:
        def __init__(self, timeout=None):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results=None):
            captured["query"] = query
            captured["max_results"] = max_results
            yield {"title": "First", "href": "https://ddg.example/a", "body": "from ddg"}
            yield {"title": "Second", "href": "https://ddg.example/b", "body": "second"}
            yield {"title": "Overflow", "href": "https://ddg.example/c", "body": "extra"}

    module = types.ModuleType("ddgs")
    module.DDGS = FakeDDGS
    with mock.patch.dict(sys.modules, {"ddgs": module}):
        rows = fusion._ddgs_search("query", 2, 7.0)

    assert captured["query"] == "query"
    assert captured["timeout"] == 7
    # Hard cap: never trust the library to honour max_results alone.
    assert len(rows) == 2
    assert rows[0] == {
        "title": "First",
        "url": "https://ddg.example/a",
        "description": "from ddg",
    }


def test_ddgs_participates_in_fusion_and_is_attributed():
    def exa(query, limit, deadline):
        return [{"title": "Shared", "url": "https://shared.example/x", "description": "exa"}]

    def ddg(query, limit, deadline):
        return [{"title": "Shared", "url": "https://shared.example/x", "description": "ddg"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(first_result_timeout_s=1.5, quorum_timeout_s=1.0),
        searchers={"exa": exa, "ddgs": ddg},
    )
    result = router.search("query")

    assert result["success"] is True
    assert sorted(result["metadata"]["providers_used"]) == ["ddgs", "exa"]
    # DuckDuckGo must be able to satisfy the consensus floor.
    assert result["metadata"]["quorum_met"] is True
    assert set(result["data"]["web"][0]["source_providers"]) == {"exa", "ddgs"}


def test_ddgs_failure_does_not_break_the_other_providers():
    def exa(query, limit, deadline):
        return [{"title": "Good", "url": "https://good.example/x", "description": "exa"}]

    def ddg(query, limit, deadline):
        raise RuntimeError("DuckDuckGo rate limited")

    router = SearchFusionWebSearchProvider(
        settings=settings(quorum_timeout_s=0.3),
        searchers={"exa": exa, "ddgs": ddg},
    )
    result = router.search("query")

    assert result["success"] is True
    assert result["metadata"]["providers_used"] == ["exa"]
    attempts = {item["provider"]: item["status"] for item in result["metadata"]["fusion_attempts"]}
    assert attempts["ddgs"] == "error"


def test_router_uses_in_process_ddgs_not_the_ddgr_cli():
    """Guard the ddgr-vs-ddgs decision.

    Measured head to head, the ddgr CLI returned "HTTP Error 202: Accepted"
    after two queries yet still exited 0 with an empty result array — a silent
    wrong answer the router cannot distinguish from "no results". The ddgs
    package survived the same burst. Shelling out must not creep back in.
    """

    source = Path(fusion.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "ddgr" not in source.replace("ddgr CLI", "").replace("``ddgr``", "")


def test_mwmbl_flattens_segmented_title_and_extract():
    """mwmbl returns highlight segments, not strings.

    Storing ``str(value)`` here would put a Python list repr into the title,
    so the adapter must join the ``value`` fields.
    """

    payload = [
        {
            "url": "https://mwmbl.example/a",
            "title": [
                {"value": "Managing ", "is_bold": False},
                {"value": "index", "is_bold": True},
                {"value": " partitions", "is_bold": False},
            ],
            "extract": [
                {"value": "Postgres ", "is_bold": False},
                {"value": "bloat", "is_bold": True},
            ],
        },
        {"url": "https://mwmbl.example/b", "title": [], "extract": []},
    ]

    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return FakeResponse()

    with mock.patch.object(fusion._HTTP_CLIENT, "get", fake_get):
        rows = fusion._mwmbl_search("postgres bloat", 1, 4.0)

    assert captured["params"] == {"s": "postgres bloat"}
    # limit is honoured even though the payload had two rows.
    assert rows == [
        {
            "title": "Managing index partitions",
            "url": "https://mwmbl.example/a",
            "description": "Postgres bloat",
        }
    ]


def test_mwmbl_tolerates_a_wrapped_object_payload():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"url": "https://mwmbl.example/x", "title": "Plain", "extract": "s"}]}

    with mock.patch.object(fusion._HTTP_CLIENT, "get", lambda url, **kw: FakeResponse()):
        rows = fusion._mwmbl_search("query", 5, 4.0)

    # Plain strings must survive the flattener untouched.
    assert rows == [
        {"title": "Plain", "url": "https://mwmbl.example/x", "description": "s"}
    ]


def test_mwmbl_is_keyless_and_lowest_weighted():
    router = SearchFusionWebSearchProvider()
    assert "mwmbl" in fusion._PROVIDER_ORDER
    # Keyless: a public JSON endpoint with no optional dependency.
    assert router._configured("mwmbl") is True
    # A small volunteer index must never outrank a major one.
    assert fusion._PROVIDER_WEIGHTS["mwmbl"] < min(
        fusion._PROVIDER_WEIGHTS[name]
        for name in ("exa", "tinyfish", "brave-free", "ddgs")
    )


def test_serper_parses_organic_rows():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "organic": [
                    {
                        "title": "The State of Async Rust",
                        "link": "https://corrode.dev/blog/async/",
                        "snippet": "Tokio stands as Rust's canonical async runtime.",
                        "position": 1,
                    }
                ],
                "credits": 1,
            }

    with mock.patch.object(fusion, "get_provider_env", lambda _name: "key"):
        with mock.patch.object(fusion._HTTP_CLIENT, "post", lambda url, **kw: FakeResponse()):
            rows = fusion._serper_search("query", 5, 4.0)

    assert rows == [
        {
            "title": "The State of Async Rust",
            "url": "https://corrode.dev/blog/async/",
            "description": "Tokio stands as Rust's canonical async runtime.",
        }
    ]


def test_tavily_parses_results_and_requests_basic_depth():
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "url": "https://wyeworks.com/blog/async",
                        "title": "Async Rust",
                        "content": "A comprehensive guide.",
                        "score": 0.9,
                    }
                ]
            }

    def fake_post(url, **kw):
        captured.update(kw.get("json") or {})
        return FakeResponse()

    with mock.patch.object(fusion, "get_provider_env", lambda _name: "key"):
        with mock.patch.object(fusion._HTTP_CLIENT, "post", fake_post):
            rows = fusion._tavily_search("query", 5, 4.0)

    assert rows == [
        {
            "title": "Async Rust",
            "url": "https://wyeworks.com/blog/async",
            "description": "A comprehensive guide.",
        }
    ]
    # An advanced search costs 2 credits against a 1,000/month free grant.
    assert captured["search_depth"] == "basic"


def test_metered_providers_stop_at_their_monthly_budget(tmp_path):
    state = tmp_path / "budget.json"
    with mock.patch.object(fusion, "_BUDGET_STATE_PATH", state):
        with mock.patch.dict(fusion._MONTHLY_BUDGET, {"tavily": 3}, clear=False):
            fusion._BUDGET_CACHE.clear()
            allowed = [fusion._budget_allows("tavily") for _ in range(5)]

    # Exactly the budget is granted, then the provider is refused for the month.
    assert allowed == [True, True, True, False, False]
    # Keyless providers are never metered.
    fusion._BUDGET_CACHE.clear()
    assert fusion._budget_allows("mwmbl") is True


def test_budget_survives_restart_via_persisted_state(tmp_path):
    state = tmp_path / "budget.json"
    with mock.patch.object(fusion, "_BUDGET_STATE_PATH", state):
        with mock.patch.dict(fusion._MONTHLY_BUDGET, {"tavily": 2}, clear=False):
            fusion._BUDGET_CACHE.clear()
            assert fusion._budget_allows("tavily") is True
            assert fusion._budget_allows("tavily") is True
            # Simulate a gateway restart: in-process cache is lost, file remains.
            fusion._BUDGET_CACHE.clear()
            assert fusion._budget_allows("tavily") is False


def test_budget_exhaustion_refuses_admission_without_calling_provider():
    calls = []

    def tavily(query, limit, deadline):
        calls.append(query)
        return [{"title": "t", "url": "https://t.example/x", "description": "d"}]

    def exa(query, limit, deadline):
        return [{"title": "e", "url": "https://e.example/x", "description": "d"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(first_result_timeout_s=1.5, quorum_timeout_s=1.0),
        searchers={"exa": exa, "tavily": tavily},
    )
    with mock.patch.object(fusion, "_budget_allows", lambda name: name != "tavily"):
        result = router.search("query")

    # A provider over budget must never issue a billable HTTP call.
    assert calls == []
    assert result["success"] is True


def test_mwmbl_can_satisfy_quorum_with_a_keyed_provider():
    def exa(query, limit, deadline):
        return [{"title": "Shared", "url": "https://shared.example/x", "description": "exa"}]

    def mwmbl(query, limit, deadline):
        return [{"title": "Shared", "url": "https://shared.example/x", "description": "mwmbl"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(first_result_timeout_s=1.5, quorum_timeout_s=1.0),
        searchers={"exa": exa, "mwmbl": mwmbl},
    )
    result = router.search("query")

    assert result["metadata"]["quorum_met"] is True
    assert set(result["data"]["web"][0]["source_providers"]) == {"exa", "mwmbl"}


def test_brave_pacing_spaces_calls_instead_of_bursting():
    """The invariant is spacing, not a call count.

    The original router dropped every paced caller, so a burst produced one
    Brave call and three searches silently missing a provider. Queued turns are
    now admitted, so more calls may land over time — but consecutive calls must
    never be closer together than brave_min_interval_s, which is what actually
    triggered the upstream 429 storm.
    """

    call_times: list[float] = []
    calls_lock = threading.Lock()

    def exa(query, limit, deadline):
        return [{"title": query, "url": f"https://exa.example/{query}", "description": query}]

    def brave(query, limit, deadline):
        with calls_lock:
            call_times.append(time.monotonic())
        return [{"title": query, "url": f"https://brave.example/{query}", "description": query}]

    configured = settings(
        brave_min_interval_s=1.0,
        brave_max_wait_s=1.2,
        global_timeout_s=2.0,
        first_result_timeout_s=3.0,
        quorum_timeout_s=3.0,
    )
    routers = [
        SearchFusionWebSearchProvider(
            settings=configured,
            searchers={"exa": exa, "brave-free": brave},
        )
        for _ in range(2)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(routers[index % 2].search, query)
            for index, query in enumerate(["one", "two", "three", "four"])
        ]
        results = [future.result() for future in futures]

    assert all(result["success"] is True for result in results)
    with calls_lock:
        ordered = sorted(call_times)
    # No burst: every consecutive pair respects the pacing interval.
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    assert all(gap >= 0.95 for gap in gaps), gaps
    statuses = [
        item["status"]
        for result in results
        for item in result["metadata"]["fusion_attempts"]
        if item["provider"] == "brave-free"
    ]
    assert len(statuses) == 4
    assert statuses.count("ok") == len(ordered)


def test_upstream_throttle_is_sanitized_and_opens_provider_cooldown(caplog):
    request = fusion.httpx.Request(
        "GET",
        "https://api.search.brave.com/res/v1/web/search?q=private+query",
    )
    response = fusion.httpx.Response(
        429,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"},
        request=request,
    )

    def exa(query, limit, deadline):
        return [{"title": "good", "url": "https://good.example", "description": "ok"}]

    def brave(query, limit, deadline):
        raise fusion.httpx.HTTPStatusError("raw error with private query", request=request, response=response)

    router = SearchFusionWebSearchProvider(
        settings=settings(circuit_cooldown_s=2.0),
        searchers={"exa": exa, "brave-free": brave},
    )
    first = router.search("private query")
    first_attempts = {
        item["provider"]: item
        for item in first["metadata"]["fusion_attempts"]
    }

    assert first["success"] is True
    assert first_attempts["brave-free"]["status"] == "provider_cooldown"
    assert "private query" not in first_attempts["brave-free"]["error"]
    assert "private query" not in caplog.text
    assert "429" not in caplog.text

    second = router.search("another query")
    second_attempts = {
        item["provider"]: item["status"]
        for item in second["metadata"]["fusion_attempts"]
    }
    assert second["success"] is True
    assert second_attempts["brave-free"] == "cooldown"


def test_brave_success_headers_prevent_call_after_long_quota_exhaustion():
    brave_calls = 0

    def exa(query, limit, deadline):
        return [{"title": "good", "url": "https://good.example", "description": "ok"}]

    def brave(query, limit, deadline):
        nonlocal brave_calls
        brave_calls += 1
        return fusion._ProviderRows(
            [{"title": "brave", "url": "https://brave.example", "description": "ok"}],
            rate_headers={
                "X-RateLimit-Remaining": "0, 0",
                "X-RateLimit-Reset": "1, 3600",
            },
        )

    router = SearchFusionWebSearchProvider(
        settings=settings(brave_min_interval_s=1.0),
        searchers={"exa": exa, "brave-free": brave},
    )
    first = router.search("first")
    second = router.search("second")
    second_attempts = {
        item["provider"]: item["status"]
        for item in second["metadata"]["fusion_attempts"]
    }

    assert first["success"] is True
    assert second["success"] is True
    assert brave_calls == 1
    assert second_attempts["brave-free"] == "cooldown"


def test_brave_pacing_queues_within_budget_and_drops_beyond_it():
    now = [100.0]
    coordinator = fusion._ProviderCoordinator(clock=lambda: now[0])
    configured = settings(brave_min_interval_s=1.0, brave_max_wait_s=1.2)

    first, blocked = coordinator.claim("brave-free", configured)
    assert blocked is None and first is not None
    assert first.start_after_s == 0.0
    first.finish("ok", configured)

    # Within the wait budget the turn is queued, not discarded.
    second, blocked = coordinator.claim("brave-free", configured)
    assert blocked is None and second is not None
    assert second.start_after_s == pytest.approx(1.0)
    second.finish("ok", configured)

    # Each queued turn pushes the next one out; beyond the budget it is dropped
    # so a caller never blocks past its own deadline.
    third, blocked = coordinator.claim("brave-free", configured)
    assert third is None and blocked == "rate_paced"

    now[0] = 102.01
    fourth, blocked = coordinator.claim("brave-free", configured)
    assert blocked is None and fourth is not None
    fourth.finish("cancelled", configured)


def test_brave_pacing_budget_of_zero_restores_drop_behavior():
    now = [100.0]
    coordinator = fusion._ProviderCoordinator(clock=lambda: now[0])
    configured = settings(brave_min_interval_s=1.0, brave_max_wait_s=0.0)

    first, blocked = coordinator.claim("brave-free", configured)
    assert blocked is None and first is not None
    first.finish("ok", configured)

    second, blocked = coordinator.claim("brave-free", configured)
    assert second is None and blocked == "rate_paced"


def test_rate_limit_headers_select_only_exhausted_windows():
    request = fusion.httpx.Request("GET", "https://api.search.brave.com/res/v1/web/search")

    def cooldown(headers):
        response = fusion.httpx.Response(429, headers=headers, request=request)
        exc = fusion.httpx.HTTPStatusError("throttled", request=request, response=response)
        return fusion._rate_limit_cooldown(exc, 10.0)

    short = cooldown({
        "X-RateLimit-Remaining": "0, 14523",
        "X-RateLimit-Reset": "1, 1419704",
    })
    long = cooldown({
        "X-RateLimit-Remaining": "0, 0",
        "X-RateLimit-Reset": "1, 1419704",
    })

    assert 1.0 <= short < 2.0
    assert long > 1_000_000


def test_empty_response_resets_transport_failure_streak():
    now = [10.0]
    coordinator = fusion._ProviderCoordinator(clock=lambda: now[0])
    configured = settings(circuit_failures=2, max_inflight_per_provider=1)

    first, _ = coordinator.claim("exa", configured)
    assert first is not None
    first.finish("error", configured)

    healthy_empty, _ = coordinator.claim("exa", configured)
    assert healthy_empty is not None
    healthy_empty.finish("empty", configured)

    second_failure, _ = coordinator.claim("exa", configured)
    assert second_failure is not None
    second_failure.finish("error", configured)

    still_available, blocked = coordinator.claim("exa", configured)
    assert blocked is None and still_available is not None
    still_available.finish("cancelled", configured)


def test_stale_success_cannot_clear_newer_circuit():
    now = [10.0]
    coordinator = fusion._ProviderCoordinator(clock=lambda: now[0])
    configured = settings(circuit_failures=1, max_inflight_per_provider=4)

    old_success, _ = coordinator.claim("exa", configured)
    assert old_success is not None
    now[0] = 11.0
    newer_failure, _ = coordinator.claim("exa", configured)
    assert newer_failure is not None
    now[0] = 12.0
    newer_failure.finish("error", configured)
    now[0] = 13.0
    old_success.finish("ok", configured)

    blocked_lease, blocked = coordinator.claim("exa", configured)
    assert blocked_lease is None and blocked == "circuit_open"


def test_submit_failure_releases_admission_lease(monkeypatch):
    class FailingExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def submit(self, *args, **kwargs):
            raise RuntimeError("submit failed")

        def shutdown(self, *args, **kwargs):
            pass

    coordinator = fusion._ProviderCoordinator()
    configured = settings(max_inflight_per_provider=1)
    monkeypatch.setattr(fusion.concurrent.futures, "ThreadPoolExecutor", FailingExecutor)
    router = SearchFusionWebSearchProvider(
        settings=configured,
        searchers={"exa": lambda query, limit, deadline: []},
        coordinator=coordinator,
        executor=FailingExecutor(),
    )

    result = router.search("query")
    assert result["success"] is False
    lease, blocked = coordinator.claim("exa", configured)
    assert blocked is None and lease is not None
    lease.finish("cancelled", configured)


def test_queued_searcher_does_not_start_after_search_returns(monkeypatch):
    real_executor = concurrent.futures.ThreadPoolExecutor
    brave_calls = 0

    class SingleWorkerExecutor:
        def __init__(self, *args, **kwargs):
            self.inner = real_executor(max_workers=1, thread_name_prefix="fusion-test")

        def submit(self, *args, **kwargs):
            return self.inner.submit(*args, **kwargs)

        def shutdown(self, *args, **kwargs):
            return self.inner.shutdown(*args, **kwargs)

    release_exa = threading.Event()
    exa_finished = threading.Event()

    def slow_exa(query, limit, deadline):
        assert release_exa.wait(timeout=1.0)
        exa_finished.set()
        return [{"title": "late", "url": "https://late.example", "description": "late"}]

    def brave(query, limit, deadline):
        nonlocal brave_calls
        brave_calls += 1
        return [{"title": "unexpected", "url": "https://brave.example", "description": "late"}]

    router = SearchFusionWebSearchProvider(
        settings=settings(
            provider_timeout_s=0.2,
            global_timeout_s=0.01,
            first_result_timeout_s=0.03,
        ),
        searchers={"exa": slow_exa, "brave-free": brave},
        executor=SingleWorkerExecutor(),
    )

    result = router.search("query")
    assert result["success"] is False
    release_exa.set()
    assert exa_finished.wait(timeout=0.5)
    assert brave_calls == 0


def test_concurrent_queries_do_not_block_each_other():
    def searcher(query, limit, deadline):
        time.sleep(0.03)
        return [{"title": query, "url": f"https://example.com/{query}", "description": query}]

    router = SearchFusionWebSearchProvider(
        settings=settings(max_inflight_per_provider=2),
        searchers={"exa": searcher},
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(router.search, ["one", "two"]))

    assert all(result["success"] is True for result in results)
    assert {result["data"]["web"][0]["title"] for result in results} == {"one", "two"}
