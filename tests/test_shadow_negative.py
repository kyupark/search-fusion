"""Additional negative-path shadow routing checks."""
import importlib
import json


def test_first_and_recovered_batches_do_not_shadow(plugin, monkeypatch):
    f = plugin.fusion
    submitted = []
    monkeypatch.setattr(f.SHADOW, 'submit', lambda *a: submitted.append(a))
    p = f.SearchFusionWebSearchProvider(settings=f.FusionSettings(shadow_enabled=True), searchers={
        'serper': lambda *a: [],
        'deepseek': lambda *a: [{'url': 'https://aux.example'}]})
    for batch in ('first', 'later'):
        plugin.turn_routing.capture_call_context(session_id='s', turn_id='t', api_request_id=batch)
        result = p.search('q')
        assert result['metadata']['search_route'] == 'fusion'
    assert not submitted


def test_queued_job_honors_kill_switch(plugin, monkeypatch, tmp_path):
    f = plugin.fusion
    s = importlib.import_module(f.__package__ + '.shadow')
    r = s.ShadowRunner(tmp_path, autostart=False)
    calls = []
    p = f.SearchFusionWebSearchProvider(settings=f.FusionSettings(shadow_enabled=True), searchers={
        'deepseek': lambda *a: calls.append(a) or []})
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '1')
    assert r.submit(p, 'q', 4, p._settings(), {'data': {'web': []}}, {})
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '0')
    job = r.queue.get_nowait()
    r.run(job)
    r.queue.task_done()
    assert not calls and not (tmp_path / 'shadow.jsonl').exists()
    r.close()


def test_shadow_error_bodies_never_logged(plugin, monkeypatch, tmp_path):
    f = plugin.fusion
    s = importlib.import_module(f.__package__ + '.shadow')
    r = s.ShadowRunner(tmp_path, autostart=False)
    def fail(*a):
        raise RuntimeError('PRIVATE_BODY API_KEY=FAKE_SECRET')
    p = f.SearchFusionWebSearchProvider(settings=f.FusionSettings(shadow_enabled=True), searchers={'deepseek': fail})
    monkeypatch.setenv('SEARCH_FUSION_SHADOW', '1')
    assert r.submit(p, 'q', 4, p._settings(), {'data': {'web': []}}, {})
    job = r.queue.get_nowait()
    r.run(job)
    r.queue.task_done()
    text = (tmp_path / 'shadow.jsonl').read_text()
    assert 'PRIVATE_BODY' not in text and 'FAKE_SECRET' not in text
    assert any(o['status'] == 'error' for o in json.loads(text)['providers'])
    r.close()
