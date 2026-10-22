"""DeepSeek Search contract and shared admission guards; no live calls."""
import concurrent.futures
import json
import threading
from unittest.mock import Mock

import httpx
import pytest


def transport(plugin, monkeypatch, payload=None, status=200, headers=None):
    f = plugin.fusion
    monkeypatch.setattr(f, 'get_provider_env', lambda name: 'fake-secret' if name == 'DEEPSEEK_API_KEY' else '')
    response = httpx.Response(status, json=payload or {}, headers=headers,
                              request=httpx.Request('POST', f._DEEPSEEK_ENDPOINT))
    post = Mock(return_value=response)
    monkeypatch.setattr(f._HTTP_CLIENT, 'post', post)
    return f, post


def test_dispatch_availability_weight_and_normalization(plugin, monkeypatch):
    f, post = transport(plugin, monkeypatch, {'results': [
        {'url': 'https://example.org/a', 'title': ' A ', 'snippet': 'short', 'summary': 'long'},
        {'url': 'https://example.org/b', 'summary': 'fallback'},
        {'title': 'no URL'}, None,
    ]})
    router = f.SearchFusionWebSearchProvider()
    assert router._searchers['deepseek'] is f._deepseek_search
    assert router._configured('deepseek')
    assert router.is_available()
    monkeypatch.setattr(f, '_PROVIDER_ORDER', ('deepseek',))
    result = router.search('site:example.org "quoted phrase"', 4)
    assert result['success']
    assert result['metadata']['providers_used'] == ['deepseek']
    rows = result['data']['web']
    assert [r['description'] for r in rows] == ['short', 'fallback']
    assert rows[0]['title'] == 'A'
    assert rows[0]['source_providers'] == ['deepseek']
    assert rows[0]['provider_ranks'] == {'deepseek': 1}
    assert f._PROVIDER_WEIGHTS['deepseek'] < f._PROVIDER_WEIGHTS['serper']
    assert post.call_args.args == (f._DEEPSEEK_ENDPOINT,)
    args = post.call_args.kwargs
    assert args['json'] == {'query': 'site:example.org "quoted phrase"', 'count': 4, 'need_content': False}
    assert args['headers']['Authorization'] == 'Bearer fake-secret'
    assert args['follow_redirects'] is False
    assert args['timeout'].read == 2.0
    assert f._BUDGET_CACHE['counts']['deepseek'] == 1
    monkeypatch.setattr(f, 'get_provider_env', lambda name: '')
    assert not router._configured('deepseek')
    assert not router.is_available()


@pytest.mark.parametrize('limit,expected', [(-1, 1), (0, 1), (1, 1), (50, 50), (100, 50)])
def test_count_bounds(plugin, monkeypatch, limit, expected):
    f, post = transport(plugin, monkeypatch)
    assert f._deepseek_search('q', limit, 1) == []
    assert post.call_args.kwargs['json']['count'] == expected


@pytest.mark.parametrize('query', ['', ' \t\n', 'x' * 129, '한' * 129])
def test_invalid_query_never_sent_or_charged(plugin, monkeypatch, query):
    f, post = transport(plugin, monkeypatch)
    with pytest.raises(ValueError, match='1..128'):
        f._deepseek_search(query, 5, 1)
    seen = []
    def other(q, *args):
        seen.append(q)
        return [{'url': 'https://example.org'}]
    router = f.SearchFusionWebSearchProvider(settings=f.FusionSettings(),
        searchers={'deepseek': f._deepseek_search, 'tinyfish': other})
    result = router.search(query)
    assert seen == [query]
    assert {'provider': 'deepseek', 'status': 'unsupported_query',
            'error': 'DeepSeek requires 1..128 Unicode characters, not blank'} in result['metadata']['fusion_attempts']
    assert not f._BUDGET_CACHE
    post.assert_not_called()


def test_unicode_boundary_preserved(plugin, monkeypatch):
    f, post = transport(plugin, monkeypatch)
    query = '한' * 128
    f._deepseek_search(query, 5, 1)
    assert post.call_args.kwargs['json']['query'] == query


@pytest.mark.parametrize('status', [401, 402, 403, 429])
def test_http_account_errors_cooldown_and_safe_logs(plugin, monkeypatch, caplog, status):
    f, post = transport(plugin, monkeypatch, {'error': {'message': 'fake-secret PRIVATE_BODY'}},
                        status, {'Retry-After': '12'})
    monkeypatch.setattr(f, '_PROVIDER_ORDER', ('deepseek',))
    router = f.SearchFusionWebSearchProvider()
    result = router.search('q')
    attempt = result['metadata']['search_rounds'][0]['fusion_attempts'][0]
    assert attempt['status'] == ('provider_cooldown' if status == 429 else 'auth_error')
    assert not result['success']
    again = router.search('q')
    assert again['metadata']['fusion_attempts'][0]['status'] == ('cooldown' if status == 429 else 'circuit_open')
    assert post.call_count == 1
    assert 'fake-secret' not in caplog.text + json.dumps(result)
    assert 'PRIVATE_BODY' not in caplog.text + json.dumps(result)
    if status == 429:
        assert f._PROCESS_COORDINATOR._rate_open_until['deepseek'] > f.time.monotonic() + 11
    else:
        assert f._PROCESS_COORDINATOR._circuit_open_until['deepseek'] > f.time.monotonic() + 899


def test_empty_is_healthy(plugin, monkeypatch):
    f, post = transport(plugin, monkeypatch, {'results': []})
    monkeypatch.setattr(f, '_PROVIDER_ORDER', ('deepseek',))
    f._PROCESS_COORDINATOR._failures['deepseek'] = 1
    result = f.SearchFusionWebSearchProvider().search('q')
    assert result['metadata']['fusion_attempts'][0]['status'] == 'empty'
    assert f._PROCESS_COORDINATOR._failures['deepseek'] == 0
    assert post.call_count == 2  # empty query is retried once; empty is still healthy


def test_shared_pacing_is_separate_from_concurrency(plugin, monkeypatch):
    f = plugin.fusion
    clock = [100.0]
    c = f._ProviderCoordinator(clock=lambda: clock[0])
    settings = f.FusionSettings()
    routers = [f.SearchFusionWebSearchProvider(coordinator=c) for _ in range(2)]
    a, _ = routers[0]._claim('deepseek', settings)
    b, _ = routers[1]._claim('deepseek', settings)
    assert a.start_after_s == 0
    assert b.start_after_s == 0
    extra = [c.claim('deepseek', settings)[0] for _ in range(2)]
    assert all(lease and lease.start_after_s == 0 for lease in extra)
    assert c.claim('deepseek', settings)[1] == 'concurrency_limited'
    for lease in [a, b, *extra]:
        lease.finish('ok', settings)
    assert 'deepseek' not in c._next_allowed
    e, _ = c.claim('deepseek', settings)
    assert e.start_after_s == 0
    e.finish('ok', settings)
    assert not f._BUDGET_CACHE.get('counts', {}).get('deepseek')


def test_budget_atomic_boundary_persistence_cancel_and_rollover(plugin, monkeypatch):
    f = plugin.fusion
    assert f._MONTHLY_BUDGET['deepseek'] == 10000
    monkeypatch.setitem(f._MONTHLY_BUDGET, 'deepseek', 1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(lambda _: f._budget_allows('deepseek'), range(8)))
    assert sum(allowed) == 1
    f._BUDGET_CACHE.clear()
    assert not f._budget_allows('deepseek')
    assert f._PROCESS_COORDINATOR.claim('deepseek', f.FusionSettings())[1] == 'quota_exhausted'
    monkeypatch.setattr(f, '_budget_period', lambda: '2099-01')
    cancel = threading.Event()
    cancel.set()
    assert not f._budget_allows('deepseek', cancelled=cancel)
    assert f._budget_allows('deepseek')
    assert f._BUDGET_CACHE['counts']['deepseek'] == 1


def test_no_http_after_budget_exhausted(plugin, monkeypatch):
    f, post = transport(plugin, monkeypatch)
    monkeypatch.setitem(f._MONTHLY_BUDGET, 'deepseek', 0)
    monkeypatch.setattr(f, '_PROVIDER_ORDER', ('deepseek',))
    result = f.SearchFusionWebSearchProvider().search('q')
    assert result['metadata']['fusion_attempts'][0]['status'] == 'quota_exhausted'
    post.assert_not_called()


def test_rrf_auxiliary_keeps_serper_priority(plugin):
    f = plugin.fusion
    outcomes = [{'provider': name, 'status': 'ok', 'rows': [
        {'url': url, 'title': name, 'description': name}]} for name, url in [
            ('deepseek', 'https://aux.example'), ('serper', 'https://primary.example')]]
    rows = f.SearchFusionWebSearchProvider._merge(outcomes, 'q', 5, 60)
    assert rows[0]['source_providers'] == ['serper']
    assert rows[1]['provider_ranks'] == {'deepseek': 1}
