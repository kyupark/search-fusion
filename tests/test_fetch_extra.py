"""Regression cases for observable extraction metadata and warning diagnostics."""
import asyncio
import json
from types import SimpleNamespace
import pytest


def test_defuddle_metadata_preserved(plugin, monkeypatch):
    m = plugin.tools
    payload = dict(contentMarkdown='content', title='Title', description='Description',
                   author='Writer', published='2026-10-17', language='ko', wordCount=42)
    monkeypatch.setattr(m.shutil, 'which', lambda name: '/fake/defuddle')
    monkeypatch.setattr(m.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=''))
    out = m._run_defuddle('https://example.com', 8, 99)
    assert {key: out[key] for key in ['title', 'description', 'author', 'published', 'language']} == {
        key: payload[key] for key in ['title', 'description', 'author', 'published', 'language']}
    assert out['word_count'] == 42


@pytest.mark.parametrize('mode,ok,error,warning', [
    ('rendered', True, None, 'rendered_mode_requested: trying TinyFish Fetch after successful Defuddle'),
    ('rendered', False, 'denied', 'defuddle_failed: denied'),
    ('article', False, 'denied', 'defuddle_failed: denied'),
    ('auto', False, None, 'defuddle_failed: empty extraction'),
])
def test_exact_fetch_warning(plugin, monkeypatch, mode, ok, error, warning):
    m = plugin.tools
    monkeypatch.setattr(m, '_run_defuddle', lambda *args:
                        dict(ok=ok, content='article' if ok else '', method='defuddle', error=error))
    monkeypatch.setattr(m, '_tinyfish_fetch', lambda *args:
                        dict(ok=True, content='rendered', method='tinyfish_fetch'))
    out = json.loads(m.web_fetch(dict(url='https://example.com/article', mode=mode)))
    assert out['warnings'] == [warning]
    assert out['method'] == 'tinyfish_fetch'


@pytest.mark.parametrize('explicit,expected', [(None, 'direct_http'), ('custom_transport', 'custom_transport')])
def test_frugal_method_fallback_preserves_explicit(plugin, monkeypatch, explicit, expected):
    m = plugin.frugal_extract
    cfg = dict(firecrawl_hosts=set(), skip_firecrawl_hosts=set(), firecrawl_only_hosts=set(),
               timeout_s=7, max_chars=1234, min_local_chars=200)
    provider = m.FrugalExtractProvider(cfg)
    result = dict(content='body', method=explicit)
    monkeypatch.setattr(provider, '_local', lambda *args: (result, ['direct_http'], '', False))
    out = asyncio.run(provider.extract(['https://example.com']))[0]
    assert out['metadata']['extract_method'] == expected
    assert out['metadata']['attempts'] == ['direct_http']


@pytest.mark.parametrize('limit,expected_read,body,expected_ok', [
    (400000, 1600000, b'raw\xff', True),
    (0, 10485760, b'   ', False),
])
def test_direct_transport_boundaries(plugin, monkeypatch, limit, expected_read, body, expected_ok):
    m = plugin.tools
    reads = []
    class Response:
        headers = {'content-type': 'application/octet-stream'}
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def geturl(self): return 'https://example.com/final'
        def read(self, size):
            reads.append(size)
            return body
    def open_url(req, timeout):
        assert timeout == 13
        assert req.get_header('User-agent') == m.USER_AGENT
        assert req.get_header('Accept') == '*/*'
        return Response()
    monkeypatch.setattr(m.urllib.request, 'urlopen', open_url)
    out = m._direct_http('https://example.com', 13, limit)
    assert reads == [expected_read]
    assert out['ok'] is expected_ok
    assert out['content'] == body.decode('utf-8', errors='replace')
    assert out['truncated'] is False


def test_defuddle_markdown_wins_over_html(plugin,monkeypatch):
    import json
    from types import SimpleNamespace
    m=plugin.tools
    monkeypatch.setattr(m.shutil,'which',lambda name:'/mock/defuddle')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=json.dumps({'contentMarkdown':'chosen markdown','content':'<p>discard HTML</p>'}),stderr=''))
    result=m._run_defuddle('https://example.com',1,100)
    assert result['content']=='chosen markdown'
