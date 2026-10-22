"""Regression cases for observable extraction metadata and warning diagnostics."""
import asyncio
import json
from types import SimpleNamespace
import pytest


def test_defuddle_metadata_preserved(plugin, monkeypatch):
    m = plugin.tools
    monkeypatch.setattr(m,'guarded_fetch',lambda *a:(b'<html>body</html>',{},'https://a'))
    payload = dict(contentMarkdown='content', title='Title', description='Description',
                   author='Writer', published='2026-10-17', language='ko', wordCount=42)
    monkeypatch.setattr(m.shutil, 'which', lambda name: '/fake/defuddle')
    monkeypatch.setattr(m.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=''))
    out = m._run_defuddle('https://example.com', 8, 99)
    assert {key: out[key] for key in ['title', 'description', 'author', 'published', 'language']} == {
        key: payload[key] for key in ['title', 'description', 'author', 'published', 'language']}
    assert out['word_count'] == 42


@pytest.mark.parametrize('ok,error,warning', [
    (False, 'denied', 'defuddle_failed: denied'),
    (False, None, 'defuddle_failed: empty extraction'),
])
def test_exact_fetch_warning(plugin, monkeypatch, ok, error, warning):
    m = plugin.tools
    monkeypatch.setattr(m, '_run_defuddle', lambda *args:
                        dict(ok=ok, content='', method='defuddle', error=error))
    monkeypatch.setattr(m, '_direct_http', lambda *args:dict(ok=False,error='denied',method='direct_http'))
    out = json.loads(m.web_fetch(dict(url='https://example.com/article')))
    assert out['warnings'] == [warning, 'direct_http_failed: denied']
    assert not out['success'] and out['needs_browser']


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
    def fetch(url, timeout, size, headers):
        assert timeout == 13
        assert headers['User-Agent'] == m.USER_AGENT
        assert headers['Accept'] == '*/*'
        reads.append(size)
        return body, {'content-type':'application/octet-stream'}, 'https://example.com/final'
    monkeypatch.setattr(m, 'guarded_fetch', fetch)
    out = m._direct_http('https://example.com', 13, limit)
    assert reads == [expected_read]
    assert out['ok'] is expected_ok
    assert out['content'] == body.decode('utf-8', errors='replace')
    assert out['truncated'] is False


def test_defuddle_markdown_wins_over_html(plugin,monkeypatch):
    import json
    from types import SimpleNamespace
    m=plugin.tools
    monkeypatch.setattr(m,'guarded_fetch',lambda *a:(b'<html>body</html>',{},'https://a'))
    monkeypatch.setattr(m.shutil,'which',lambda name:'/mock/defuddle')
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout=json.dumps({'contentMarkdown':'chosen markdown','content':'<p>discard HTML</p>'}),stderr=''))
    result=m._run_defuddle('https://example.com',1,100)
    assert result['content']=='chosen markdown'
