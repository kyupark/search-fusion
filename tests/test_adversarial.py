"""Behavior contracts for admission accounting, deadline and ATS boundaries."""
import asyncio
import concurrent.futures
import json
import threading

import pytest


@pytest.mark.parametrize('mode', ['queued_cancel', 'submit_failure', 'worker_cancel'])
def test_unstarted_metered_request_does_not_spend_budget(plugin, mode):
    f = plugin.fusion
    calls = []
    coordinator = f._ProviderCoordinator()
    settings = f.FusionSettings(global_timeout_s=.001, first_result_timeout_s=.002)

    class Executor:
        def submit(self, fn, *args):
            if mode == 'submit_failure':
                raise RuntimeError('executor is closed')
            future = concurrent.futures.Future()
            if mode == 'worker_cancel':
                args[-1].set()
                future.set_result(fn(*args))
            return future

    provider = f.SearchFusionWebSearchProvider(
        settings=settings, searchers={'tavily': lambda *a: calls.append(a) or []},
        coordinator=coordinator, executor=Executor(),
    )
    assert not provider.search('query')['success']
    assert calls == [] and coordinator._inflight == {}
    assert f._BUDGET_CACHE.get('counts', {}).get('tavily', 0) == 0


def test_retry_round_does_not_submit_after_claim_crosses_deadline(plugin, monkeypatch):
    f = plugin.fusion
    now = [100.0]
    calls = []
    claims = []
    settings = f.FusionSettings(global_timeout_s=.1, first_result_timeout_s=.5, auto_retry_enabled=False)
    monkeypatch.setattr(f.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(f.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))

    class Coordinator(f._ProviderCoordinator):
        def claim(self, name, cfg):
            claims.append((name, now[0]))
            if now[0] < 100.1:
                return None, 'concurrency_limited'
            # Simulate lock contention during the first retry's admission.
            now[0] += .4
            return super().claim(name, cfg)

    class Executor:
        def submit(self, fn, *args):
            calls.append((args[0], now[0]))
            future = concurrent.futures.Future()
            future.set_result(fn(*args))
            return future

    coordinator = Coordinator(clock=lambda: now[0])
    provider = f.SearchFusionWebSearchProvider(
        settings=settings, searchers={'serper': lambda *a: [], 'tavily': lambda *a: []},
        coordinator=coordinator, executor=Executor(),
    )
    assert not provider.search('query')['success']
    assert calls == []
    assert all(start < 100.5 for _, start in claims)
    assert coordinator._inflight == {}


def test_ashby_public_schema_selects_job_url_not_nonexistent_id(plugin, monkeypatch):
    m = plugin.frugal_extract
    url = 'https://jobs.ashbyhq.com/acme/wanted/application?utm_source=board'
    payload = {'apiVersion': '1', 'jobs': [
        {'jobUrl': 'https://jobs.ashbyhq.com/other/wanted', 'title': 'Wrong org',
         'descriptionHtml': '<p>Wrong org</p>'},
        {'jobUrl': 'https://jobs.ashbyhq.com/acme/other', 'title': 'Wrong posting',
         'descriptionHtml': '<p>Wrong posting</p>'},
        {'jobUrl': 'https://jobs.ashbyhq.com/acme/wanted', 'title': 'Engineer',
         'descriptionHtml': '<p>Build reliable systems.</p>'},
    ]}
    calls = []

    def get(target, *args):
        calls.append(target)
        return {'ok': True, 'content': json.dumps(payload), 'content_type': 'application/json'}

    monkeypatch.setattr(m, '_direct_http', get)
    cfg = dict(firecrawl_hosts=set(), skip_firecrawl_hosts=set(), timeout_s=7,
               max_chars=1000, min_local_chars=200)
    out = asyncio.run(m.FrugalExtractProvider(cfg).extract([url]))[0]
    assert out['metadata']['extract_method'] == 'ats_ashby'
    assert out['title'] == 'Engineer' and 'Wrong' not in out['content']
    assert calls == ['https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true']


def test_metered_workers_atomically_charge_once_at_execution(plugin, monkeypatch):
    f = plugin.fusion
    monkeypatch.setattr(f, '_MONTHLY_BUDGET', {'tavily': 1})
    settings = f.FusionSettings()
    coordinator = f._ProviderCoordinator()
    provider = f.SearchFusionWebSearchProvider(settings=settings, coordinator=coordinator)
    leases = [coordinator.claim('tavily', settings)[0] for _ in range(3)]
    calls = []
    barrier = threading.Barrier(3)

    def run(lease):
        barrier.wait(timeout=3)
        return provider._invoke('tavily', lambda *a: calls.append(a) or [{'url': 'https://result.test'}],
                                'query', 5, settings, lease, threading.Event())

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(run, leases))
    assert len(calls) == 1
    assert sorted(outcome['status'] for outcome in outcomes) == ['ok', 'quota_exhausted', 'quota_exhausted']
    assert coordinator._inflight == {}
    assert json.loads(f._BUDGET_STATE_PATH.read_text())['counts'] == {'tavily': 1}
    f._BUDGET_CACHE.clear()
    assert coordinator.claim('tavily', settings)[1] == 'quota_exhausted'


@pytest.mark.parametrize('outcome', ['ok', 'empty', 'timeout'])
def test_started_metered_attempt_is_charged_once_even_on_failure(plugin, outcome):
    f = plugin.fusion
    calls = []

    def search(*args):
        calls.append(args)
        if outcome == 'timeout':
            raise TimeoutError('transport timeout')
        return [{'url': 'https://result.test'}] if outcome == 'ok' else []

    provider = f.SearchFusionWebSearchProvider(settings=f.FusionSettings(), searchers={'tavily': search})
    result = provider.search('query')
    assert result['success'] is (outcome == 'ok')
    expected = 1 if outcome == 'ok' else 2  # one charge per transport, including recovery
    assert len(calls) == expected
    assert json.loads(f._BUDGET_STATE_PATH.read_text())['counts'] == {'tavily': expected}


@pytest.mark.parametrize('url', [
    'https://jobs.ashbyhq.com/acme?department=Engineering',
    'https://apply.workable.com/acme/?lng=fr',
    'https://acme.wd3.myworkdayjobs.com/en-US/Careers?location=Geneva',
    'https://careers.example/jobs?ashby_jid=wanted',
    'https://careers.example/embed?url=https://apply.workable.com/acme/j/ABC123',
    'https://jobs.ashbyhq.com.evil.test/acme/wanted',
    'https://jobs.ashbyhq.com@127.0.0.1/acme/wanted',
    'https://apply.workable.com.evil.test/acme/j/ABC123',
    'https://apply.workable.com@127.0.0.1/acme/j/ABC123',
    'https://acme.wd3.myworkdayjobs.com.evil.test/Careers/job/Engineer_R1',
    'https://acme.wd3.myworkdayjobs.com@127.0.0.1/Careers/job/Engineer_R1',
    'https://evilmyworkdayjobs.com/Careers/job/Engineer_R1',
])
def test_ats_host_and_page_boundaries_preserve_generic_path(plugin, monkeypatch, url):
    m = plugin.frugal_extract
    calls = []
    generic = 'Generic requested page. ' * 12

    def get(target, *args):
        calls.append(target)
        return {'ok': True, 'content': generic, 'content_type': 'text/plain'}

    monkeypatch.setattr(m, '_direct_http', get)
    cfg = dict(firecrawl_hosts=set(), skip_firecrawl_hosts=set(), timeout_s=7,
               max_chars=1000, min_local_chars=200)
    result = asyncio.run(m.FrugalExtractProvider(cfg).extract([url]))[0]
    if '@127.0.0.1' in url:
        assert calls == [] and 'embedded credentials' in result['error']
    else:
        assert calls == [url]
        assert result['metadata']['extract_method'] == 'direct_http'
        assert result['content'] == generic


@pytest.mark.parametrize('blocked_at', ['lock', 'persist'])
def test_cancellation_during_budget_io_skips_transport_and_charge(plugin, monkeypatch, blocked_at):
    f = plugin.fusion
    settings = f.FusionSettings()
    coordinator = f._ProviderCoordinator()
    provider = f.SearchFusionWebSearchProvider(settings=settings, coordinator=coordinator)
    lease, _ = coordinator.claim('tavily', settings)
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    calls = []
    outcomes = []
    if blocked_at == 'lock':
        original = f._budget_allows

        def budget(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(f, '_budget_allows', budget)
        f._BUDGET_LOCK.acquire()
    else:
        path_type = type(f._BUDGET_STATE_PATH)
        original = path_type.write_text

        def persist(path, *args, **kwargs):
            if path == f._BUDGET_STATE_PATH:
                entered.set()
                assert release.wait(3)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(path_type, 'write_text', persist)

    worker = threading.Thread(target=lambda: outcomes.append(provider._invoke(
        'tavily', lambda *a: calls.append(a) or [], 'query', 5, settings, lease, cancelled,
    )))
    worker.start()
    try:
        assert entered.wait(2)
        cancelled.set()
    finally:
        if blocked_at == 'lock':
            f._BUDGET_LOCK.release()
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert calls == []
    assert outcomes[0]['status'] == 'cancelled'
    assert f._BUDGET_CACHE.get('counts', {}).get('tavily', 0) == 0
    if f._BUDGET_STATE_PATH.exists():
        assert json.loads(f._BUDGET_STATE_PATH.read_text())['counts'].get('tavily', 0) == 0
    assert coordinator._inflight == {}
