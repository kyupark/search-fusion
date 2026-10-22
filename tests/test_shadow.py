import importlib
import json
import sqlite3
import threading
import time
from dataclasses import replace

import pytest


def setup(plugin, monkeypatch, tmp_path, **settings):
    f = plugin.fusion
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '1')
    s = importlib.import_module(f.__package__ + '.shadow')
    runner = s.ShadowRunner(tmp_path / 'shadow', autostart=False, queue_size=1)
    calls = []
    def search(name):
        def call(q, l, t):
            calls.append(name)
            return [{'url': 'https://' + name + '.example/a', 'title': name, 'description': 'x' * 500}]
        return call
    p = f.SearchFusionWebSearchProvider(settings=f.FusionSettings(**settings),
        searchers={name: search(name) for name in ('serper', 'deepseek')})
    result = {'data': {'web': [{'url': 'https://serper.example/a', 'title': 'serper', 'description': ''}]}}
    context = {'session_id': 's', 'turn_id': 't', 'batch_index': 2}
    return f, s, runner, p, result, context, calls


def run_one(runner, p, result, context):
    assert runner.submit(p, 'q', 5, p._settings(), result, context)
    job = runner.queue.get_nowait()
    try:
        runner.run(job)
    finally:
        runner.queue.task_done()


def test_reuse_schema_budget_and_secrets(plugin, monkeypatch, tmp_path):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path)
    monkeypatch.setattr(f, 'get_provider_env', lambda key: 'test-secret')
    result['data']['web'][0]['description'] = 'test-secret'
    result['data']['web'][0]['url'] += '?token=test-secret'
    original = json.dumps(result)
    run_one(r, p, result, ctx)
    record = json.loads((r.state / 'shadow.jsonl').read_text())
    assert calls == ['deepseek']  # never invokes Serper again
    assert json.dumps(result) == original
    assert f._BUDGET_CACHE['counts']['deepseek'] == 1
    assert record['batch_index'] == 2
    assert record['session_id'] == 's' and record['turn_id'] == 't'
    assert record['query'] == 'q' and isinstance(record['ts'], float)
    assert record['fusion_only_urls'] == ['https://deepseek.example/a']
    assert record['providers_used'] == ['serper', 'deepseek']
    assert all(set(p) == {'provider', 'status', 'elapsed_ms'} for p in record['providers'])
    assert all(len(row['snippet']) <= 300 for row in record['results'])
    assert 'test-secret' not in json.dumps(record)
    r.close()


@pytest.mark.parametrize('reason', ['cap', 'budget', 'switch', 'config'])
def test_skip_guards(plugin, monkeypatch, tmp_path, reason):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path)
    if reason == 'cap':
        p._settings_override = replace(p._settings(), shadow_daily_cap=0)
    elif reason == 'budget':
        f._budget_allows('deepseek', charge=False)
        f._BUDGET_CACHE['counts']['deepseek'] = 8001
    elif reason == 'switch':
        monkeypatch.setenv('SEARCH_FUSION_SHADOW', '0')
    else:
        p._settings_override = replace(p._settings(), shadow_enabled=False)
    if reason in ('switch', 'config'):
        assert not r.submit(p, 'q', 5, p._settings(), result, ctx)
    else:
        run_one(r, p, result, ctx)
    assert not calls
    assert not (r.state / 'shadow.jsonl').exists()
    r.close()


def test_queue_full_drops_without_wait(plugin, monkeypatch, tmp_path):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path)
    assert r.submit(p, 'q', 5, p._settings(), result, ctx)
    start = time.monotonic()
    assert not r.submit(p, 'q2', 5, p._settings(), result, ctx)
    assert time.monotonic() - start < .05
    r.queue.get_nowait()
    r.queue.task_done()
    r.close()


def test_busy_slots_reserve_last_deepseek_and_brave_pacing(plugin, monkeypatch, tmp_path):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path)
    leases = [p._claim('deepseek', p._settings())[0] for _ in range(3)]
    run_one(r, p, result, ctx)
    assert not calls
    assert p._claim('deepseek', p._settings())[0] is not None  # real gets reserved slot
    c = f._ProviderCoordinator()
    c._next_allowed['brave-free'] = time.monotonic() + .1
    assert c.try_claim_shadow('brave-free', p._settings())[1] == 'rate_paced'
    with_lock = threading.Event()
    release = threading.Event()
    def hold():
        with c._lock:
            with_lock.set()
            release.wait(2)
    thread = threading.Thread(target=hold)
    thread.start()
    assert with_lock.wait(1)
    assert c.try_claim_shadow('deepseek', p._settings())[1] == 'busy'
    release.set()
    thread.join()
    r.close()


def test_never_waits_or_changes_foreground_and_no_recall(plugin, monkeypatch, tmp_path):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path)
    r.close()
    # Use actual background queue, blocking only the auxiliary index.
    runner = s.ShadowRunner(tmp_path / 'async')
    entered, release = threading.Event(), threading.Event()
    def slow(q, l, t):
        entered.set()
        release.wait(2)
        return [{'url': 'https://shadow.example', 'title': 'aux'}]
    p._searchers['deepseek'] = slow
    monkeypatch.setattr(f, 'SHADOW', runner)
    routing = plugin.turn_routing
    routing.capture_call_context(session_id='s', turn_id='t', api_request_id='a')
    routing.ROUTER.select()  # first-batch identity only; no fake transport
    routing.capture_call_context(session_id='s', turn_id='t', api_request_id='b')
    try:
        start = time.monotonic()
        actual = p.search('q')
        assert time.monotonic() - start < .3
        assert entered.wait(1)
        frozen = json.dumps(actual)
        assert actual['metadata']['providers_used'] == ['serper']
        assert actual['metadata']['search_route'] == 'serper'
        assert calls == ['serper']
    finally:
        release.set()
        runner.queue.join()
    assert json.dumps(actual) == frozen
    assert json.loads((runner.state / 'shadow.jsonl').read_text())['batch_index'] == 2
    runner.close()
    # Restore so fixture closes its original singleton, not an already closed runner.
    monkeypatch.setattr(f, 'SHADOW', s.SHADOW)


def test_deadline_and_held_leases(plugin, monkeypatch, tmp_path):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path, shadow_timeout_s=.05)
    release = threading.Event()
    p._searchers['deepseek'] = lambda *a: (release.wait(2) or [])
    try:
        start = time.monotonic()
        run_one(r, p, result, ctx)
        assert time.monotonic() - start < .3
        record = json.loads((r.state / 'shadow.jsonl').read_text())
        assert any(o['provider'] == 'deepseek' and o['status'] == 'hard_deadline' for o in record['providers'])
        assert p._coordinator._inflight['deepseek'] == 1
    finally:
        release.set()
        r.close()
    assert not p._coordinator._inflight


def test_daily_persistence_rotation_and_worker_saturation(plugin, monkeypatch, tmp_path):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path, shadow_daily_cap=1)
    assert all(r.slots.acquire(False) for _ in range(6))
    run_one(r, p, result, ctx)
    assert not calls
    assert any(o['status'] == 'worker_busy' for o in json.loads((r.state / 'shadow.jsonl').read_text())['providers'])
    assert not r._daily_claim(1)
    r2 = s.ShadowRunner(r.state, autostart=False)
    assert not r2._daily_claim(1)
    r.max_log_bytes = 200
    r._append({'a': 'x' * 80})
    r._append({'a': 'x' * 80})
    assert (r.state / 'shadow.jsonl.1').exists()
    assert (r.state / 'shadow.jsonl').stat().st_size <= 200
    r.close()
    r2.close()


def test_submission_exception_is_invisible(plugin, monkeypatch, tmp_path):
    f, s, r, p, result, ctx, calls = setup(plugin, monkeypatch, tmp_path)
    def fail(*a):
        raise RuntimeError('secret')
    monkeypatch.setattr(f.SHADOW, 'submit', fail)
    monkeypatch.setattr(f.ROUTER, 'select', lambda **kw: ('serper', 'hook_ids'))
    actual = p.search('q')
    assert actual['success'] and actual['metadata']['providers_used'] == ['serper']
    r.close()


def test_report_readonly_and_unknown_session(plugin, tmp_path):
    module = importlib.import_module(plugin.fusion.__package__ + '.shadow_report')
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE messages (session_id TEXT, timestamp REAL, tool_calls TEXT)')
        conn.execute('INSERT INTO messages VALUES (?, ?, ?)', ('s', 2, json.dumps({'url': 'https://unique.example/a?x=1'})))
    path = tmp_path / 'shadow.jsonl'
    record = {'ts': 1, 'session_id': 's', 'fusion_urls': ['https://unique.example/a'], 'fusion_only_urls': ['https://unique.example/a']}
    path.write_text(json.dumps(record) + '\n')
    before = db.read_bytes()
    report = module.report([path], db)
    assert report['runs'] == 1 and report['fusion_only_url_rate'] == 1
    assert report['later_tool_calls']['fusion_only_urls_seen_later'] == 1
    assert db.read_bytes() == before
    assert 'not obtainable' in module.report([path], tmp_path / 'missing')['later_tool_calls']


def test_shadow_configuration(plugin, monkeypatch):
    import sys
    f = plugin.fusion
    assert f.FusionSettings().shadow_enabled
    assert f.FusionSettings().shadow_daily_cap == 300
    monkeypatch.setattr(sys.modules['hermes_cli.config'], 'load_config', lambda: {
        'plugins': {'entries': {'search-fusion': {'fusion': {
            'shadow_enabled': False, 'shadow_daily_cap': 12, 'shadow_timeout_s': 1.2}}}}})
    settings = f.load_fusion_settings()
    assert not settings.shadow_enabled
    assert settings.shadow_daily_cap == 12 and settings.shadow_timeout_s == 1.2
