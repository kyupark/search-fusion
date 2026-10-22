"""Offline SSRF regressions; synthetic DNS, sockets, HTTP and CLI boundaries only."""
import asyncio
import json
import socket
import sys
from types import SimpleNamespace

import pytest


def safety(plugin):
    return sys.modules[plugin.__name__ + '.url_safety']


def addresses(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '',
             (ip, 443, 0, 0) if ':' in ip else (ip, 443)) for ip in ips]


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'ftp://example.com/a', 'gopher://example.com',
    'https://user:password@example.com', 'https://user@example.com',
    'http://localhost', 'http://LOCALHOST.', 'http://x.localhost',
    'http://metadata.google.internal', 'http://metadata.goog',
    'http://127.0.0.1', 'http://127.2.3.4', 'http://10.1.2.3', 'http://172.16.0.1',
    'http://192.168.0.1', 'http://169.254.169.254/latest/meta-data', 'http://169.254.170.2',
    'http://100.100.100.200', 'http://100.64.0.1', 'http://224.0.0.1', 'http://240.0.0.1',
    'http://0.0.0.0', 'http://192.0.2.1', 'http://[::1]', 'http://[::]',
    'http://[fc00::1]', 'http://[fe80::1]', 'http://[ff02::1]',
    'http://[fd00:ec2::254]', 'http://[::ffff:127.0.0.1]',
    'http://[::ffff:0:127.0.0.1]', 'http://[2002:7f00:1::]',
    'http://[64:ff9b::7f00:1]', 'http://[64:ff9b:1::a00:1]',
    'http://[fe80::1%25en0]', 'http://[', 'http://example.com:bad',
    'https://example.com\\@localhost', 'https://example.com/\r\nInjected: x',
])
def test_unsafe_url_rejected_before_any_fetch(plugin, monkeypatch, url):
    m = safety(plugin)
    monkeypatch.setattr(m, '_connection', lambda *a: pytest.fail('must not connect'))
    with pytest.raises(m.UnsafeURL):
        m.guarded_fetch(url, 1, 100, {})


@pytest.mark.parametrize('ips', [
    ('10.0.0.1',), ('93.184.216.34', '127.0.0.1'),
    ('93.184.216.34', '100.64.0.1'), ('2606:4700:4700::1111', 'fd00::1'),
    ('93.184.216.34', 'garbage'), (),
])
def test_dns_all_answers_must_be_public(plugin, monkeypatch, ips):
    m = safety(plugin)
    monkeypatch.setattr(m.socket, 'getaddrinfo', lambda *a: addresses(*ips))
    with pytest.raises(m.UnsafeURL):
        m.validate_url('https://public-name.example/a')


def test_dns_failure_closed_even_when_host_helper_or_proxy_allows(plugin, monkeypatch):
    m = safety(plugin)
    host = SimpleNamespace(is_safe_url=lambda url: True)
    monkeypatch.setitem(sys.modules, 'tools.url_safety', host)
    monkeypatch.setattr(host, 'is_safe_url', lambda url: True)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example:8080')
    def fail(*a):
        raise socket.gaierror('no DNS')
    monkeypatch.setattr(m.socket, 'getaddrinfo', fail)
    with pytest.raises(m.UnsafeURL):
        m.validate_url('https://missing.example')


@pytest.mark.parametrize('allowed', [False, True])
def test_host_helper_is_extra_veto_and_own_checks_survive_host_optout(plugin, monkeypatch, allowed):
    host = SimpleNamespace(is_safe_url=lambda url: True)
    monkeypatch.setitem(sys.modules, 'tools.url_safety', host)
    m = safety(plugin)
    seen = []
    monkeypatch.setattr(host, 'is_safe_url', lambda url: seen.append(url) or allowed)
    if allowed:
        assert m.validate_url('https://example.com')[1]
        monkeypatch.setattr(m.socket, 'getaddrinfo', lambda *a: addresses('10.0.0.1'))
        with pytest.raises(m.UnsafeURL):
            m.validate_url('https://private-name.example')
    else:
        with pytest.raises(m.UnsafeURL):
            m.validate_url('https://example.com')
    assert seen == ['https://example.com']


def test_fallback_works_without_host_module(plugin, monkeypatch):
    m = safety(plugin)
    monkeypatch.setitem(sys.modules, 'tools.url_safety', None)
    assert m.validate_url('https://example.com')[1]
    with pytest.raises(m.UnsafeURL):
        m.validate_url('http://127.0.0.1')


class Response:
    def __init__(self, status=200, location=None):
        self.status, self.reason = status, 'status'
        self.location = location
    def getheaders(self):
        return [('Content-Type', 'text/plain')] + ([('Location', self.location)] if self.location is not None else [])
    def read(self, count):
        assert count == 100
        return b'body'


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('location', ['http://127.0.0.1/private', '//private.example/secret',
                                     'file:///etc/passwd', 'https://u:p@example.com'])
def test_redirect_targets_checked_before_dial(plugin, monkeypatch, status, location):
    m = safety(plugin)
    host = SimpleNamespace(is_safe_url=lambda url: True)
    monkeypatch.setitem(sys.modules, 'tools.url_safety', host)
    monkeypatch.setattr(host, 'is_safe_url', lambda url: True)
    monkeypatch.setattr(m.socket, 'getaddrinfo', lambda host, *a:
                        addresses('10.0.0.1' if host == 'private.example' else '93.184.216.34'))
    calls, closed = [], []
    def connection(parsed, answers, timeout):
        calls.append(parsed.hostname)
        return SimpleNamespace(request=lambda *a, **k: None,
                               getresponse=lambda: Response(status, location),
                               close=lambda: closed.append(True))
    monkeypatch.setattr(m, '_connection', connection)
    with pytest.raises(m.UnsafeURL):
        m.guarded_fetch('https://example.com/start', 2, 100, {})
    assert calls == ['example.com'] and len(closed) == 1


def test_relative_and_cross_host_redirects_and_body_limit(plugin, monkeypatch):
    m = safety(plugin)
    calls = []
    responses = iter([Response(302, '/middle'), Response(307, 'https://other.example/end'), Response()])
    def connection(parsed, answers, timeout):
        return SimpleNamespace(request=lambda method, path, headers: calls.append((parsed.hostname, method, path)),
                               getresponse=lambda: next(responses), close=lambda: None)
    monkeypatch.setattr(m, '_connection', connection)
    assert m.guarded_fetch('https://example.com/start', 2, 100, {}) == (
        b'body', {'content-type':'text/plain'}, 'https://other.example/end')
    assert calls == [('example.com','GET','/start'), ('example.com','GET','/middle'), ('other.example','GET','/end')]


@pytest.mark.parametrize('location', [None, '/again'])
def test_malformed_or_looping_redirects_stop(plugin, monkeypatch, location):
    m = safety(plugin)
    calls=[]
    def connection(*args):
        calls.append(True)
        return SimpleNamespace(request=lambda *a,**kw:None,
                               getresponse=lambda:Response(302, location),close=lambda:None)
    monkeypatch.setattr(m,'_connection',connection)
    with pytest.raises(m.UnsafeURL):
        m.guarded_fetch('https://example.com/start',2,100,{})
    assert len(calls) == (1 if location is None else 11)


def test_dns_answers_pinned_preserving_tls_sni_and_host(plugin, monkeypatch):
    m = safety(plugin)
    host = SimpleNamespace(is_safe_url=lambda url: True)
    monkeypatch.setitem(sys.modules, 'tools.url_safety', host)
    monkeypatch.setattr(host, 'is_safe_url', lambda url: True)
    dns=[]
    def resolve(*args):
        dns.append(args)
        return addresses('93.184.216.34') if len(dns)==1 else addresses('127.0.0.1')
    monkeypatch.setattr(m.socket,'getaddrinfo',resolve)
    parsed, answers=m.validate_url('https://example.com/a')
    connected=[]
    fake=SimpleNamespace(settimeout=lambda t:None,setsockopt=lambda *a:None,connect=lambda address:connected.append(address),
                         close=lambda:None)
    monkeypatch.setattr(m.socket,'socket',lambda *a:fake)
    conn=m._connection(parsed,answers,3)
    sni=[]
    conn._context=SimpleNamespace(wrap_socket=lambda sock,server_hostname:sni.append(server_hostname) or sock)
    conn.connect()
    assert dns == [('example.com',443,socket.AF_UNSPEC,socket.SOCK_STREAM)]
    assert connected == [('93.184.216.34',443)] and sni == ['example.com']
    assert conn.host == 'example.com'
    # Standard-library request formatting retains the original Host, never the pinned IP.
    wire=[]
    conn.sock=None
    conn.send=lambda value:wire.append(value)
    conn.request('GET','/a')
    assert b'Host: example.com\r\n' in b''.join(wire)


@pytest.mark.parametrize('route',['web_raw','web_article','web_rendered','direct','defuddle','frugal','ats'])
def test_each_entry_rejects_unsafe_target_without_dispatch(plugin, monkeypatch, route):
    url='http://u:p@127.0.0.1/private'
    monkeypatch.setattr(plugin.tools.shutil,'which',lambda *a:'/fake/defuddle')
    if route.startswith('web_'):
        out=json.loads(plugin.tools.web_fetch({'url':url,'mode':route[4:]}))
        assert not out['success'] and 'credentials' in out['error']
    elif route=='direct':
        assert not plugin.tools._direct_http(url,1,100)['ok']
    elif route=='defuddle':
        assert not plugin.tools._run_defuddle(url,1,100)['ok']
    elif route=='frugal':
        out=asyncio.run(plugin.frugal_extract.FrugalExtractProvider().extract([url]))[0]
        assert 'credentials' in out['error'] and out['metadata']['attempts']==[]
    else:
        assert plugin.frugal_extract._ats_extract(url,1,100,[]) is None


def test_ats_generated_endpoint_is_guarded(plugin, monkeypatch):
    m=safety(plugin)
    monkeypatch.setattr(m.socket,'getaddrinfo',lambda host,*a:
                        addresses('10.0.0.1' if host=='api.ashbyhq.com' else '93.184.216.34'))
    monkeypatch.setattr(m,'_connection',lambda *a:pytest.fail('unsafe generated endpoint must not dial'))
    attempts=[]
    assert plugin.frugal_extract._ats_extract('https://jobs.ashbyhq.com/acme/posting',1,100,attempts) is None
    assert attempts==['ats_ashby']


def test_defuddle_consumes_guarded_local_html_with_network_preload(plugin, monkeypatch):
    m=plugin.tools
    monkeypatch.setattr(m.shutil,'which',lambda *a:'/fake/defuddle')
    calls=[]
    monkeypatch.setattr(m,'guarded_fetch',lambda *a:calls.append(a) or (b'<p>original HTML</p>',{},'https://example.com/final'))
    def run(command, **kwargs):
        from pathlib import Path
        assert command[:3]==['defuddle','parse','--json']
        assert Path(command[3]).read_bytes()==b'<p>original HTML</p>'
        assert Path(command[3]).suffix=='.html'
        assert 'defuddle_offline.cjs' in kwargs['env']['NODE_OPTIONS']
        return SimpleNamespace(returncode=0,stdout='{"contentMarkdown":"safe text"}',stderr='')
    monkeypatch.setattr(m.subprocess,'run',run)
    assert m._run_defuddle('https://example.com/start',3,100)['content']=='safe text'
    assert calls[0][0]=='https://example.com/start'
