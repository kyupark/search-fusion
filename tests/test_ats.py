"""ATS extraction contracts; all HTTP is mocked at the transport boundary."""
import asyncio
import io
import json
import urllib.error
import urllib.request

import pytest


CASES = [
    (
        'https://jobs.ashbyhq.com/acme/wanted/application?source=test',
        'https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true',
        {'jobs': [
            {'id': 'other', 'title': 'Wrong job', 'descriptionHtml': '<p>Wrong</p>'},
            {'id': 'wanted', 'title': 'Engineer', 'location': 'Geneva',
             'descriptionHtml': '<p>Build &amp; ship.</p><ul><li>Python</li><li>Testing</li></ul>'},
        ]},
        'ats_ashby',
    ),
    (
        'https://apply.workable.com/acme/j/ABC123/?source=test',
        'https://apply.workable.com/api/v2/accounts/acme/jobs/ABC123',
        {'title': 'Engineer', 'location': {'city': 'Geneva', 'region': None, 'country': 'Switzerland'},
         'description': '<p>Build &amp; ship.</p>',
         'requirements': '<ul><li>Python</li><li>Testing</li></ul>',
         'benefits': '<p>Vacation</p>'},
        'ats_workable',
    ),
    *[
        (
            f'https://acme.wd3.myworkdayjobs.com/{locale}Careers/job/Geneva/Engineer_R123?source=test',
            'https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Careers/job/Geneva/Engineer_R123',
            {'jobPostingInfo': {'title': 'Engineer', 'location': 'Geneva',
                                'jobDescription': '<p>Build &amp; ship.</p><ul><li>Python</li><li>Testing</li></ul>'}},
            'ats_workday',
        ) for locale in ('en-US/', '')
    ],
]


@pytest.mark.parametrize('url,endpoint,payload,method', CASES)
@pytest.mark.parametrize('max_chars', [0, 45])
def test_ats_returns_posting_before_generic_extraction(plugin, monkeypatch, url, endpoint, payload, method, max_chars):
    calls = []

    def get(req, timeout):
        calls.append(req.full_url)
        assert req.full_url == endpoint and req.get_method() == 'GET' and timeout == 7
        response = io.BytesIO(json.dumps(payload).encode())
        response.headers = {'content-type': 'application/json'}
        response.geturl = lambda: endpoint
        return response

    monkeypatch.setattr(urllib.request, 'urlopen', get)
    m = plugin.frugal_extract
    monkeypatch.setattr(m, '_run_defuddle', lambda *a: pytest.fail('ATS success must skip Defuddle'))
    monkeypatch.setattr(m, '_firecrawl_provider', lambda: pytest.fail('ATS success must skip Firecrawl'))
    cfg = dict(firecrawl_hosts=set(), skip_firecrawl_hosts=set(), timeout_s=7,
               max_chars=max_chars, min_local_chars=200)
    out = asyncio.run(m.FrugalExtractProvider(cfg).extract([url]))[0]
    assert calls == [endpoint]
    assert out['url'] == url and out['title'] == 'Engineer'
    assert out['content'] == out['raw_content']
    assert out['content'].startswith('# Engineer\n\nLocation: Geneva')
    assert out['metadata']['extract_method'] == method
    assert out['metadata']['attempts'] == [method]
    assert out['metadata']['truncated'] is bool(max_chars)
    if max_chars:
        assert len(out['content']) == max_chars
    else:
        assert 'Build & ship.' in out['content'] and '<p>' not in out['content']
        assert '- Python\n- Testing' in out['content'] and 'Wrong' not in out['content']
        if method == 'ats_workable':
            assert 'Switzerland' in out['content'] and 'Vacation' in out['content']


@pytest.mark.parametrize('url,endpoint,payload,method', CASES)
@pytest.mark.parametrize('failure', ['http', 'timeout', 'json', 'shape', 'empty', 'missing', 'unrelated'])
def test_ats_failure_preserves_generic_fallback(plugin, monkeypatch, url, endpoint, payload, method, failure):
    if failure == 'unrelated':
        url = url.replace('.com/', '.com.evil/', 1)
    calls = []

    def get(req, timeout):
        calls.append(req.full_url)
        if req.full_url == endpoint:
            if failure == 'http':
                raise urllib.error.HTTPError(endpoint, 404, 'missing', {}, None)
            if failure == 'timeout':
                raise TimeoutError('timed out')
            body = {'json': 'not json', 'shape': '[]', 'empty': '{}',
                    'missing': json.dumps({'jobs': [{'id': 'other', 'title': 'Wrong job',
                                                   'descriptionHtml': '<p>Wrong</p>'}],
                                           'title': 'Title without description',
                                           'jobPostingInfo': {'title': 'Title without description'}})}[failure]
            ctype = 'application/json'
        else:
            assert req.full_url == url
            body, ctype = '<html>Enable JavaScript</html>', 'text/html'
        response = io.BytesIO(body.encode())
        response.headers = {'content-type': ctype}
        response.geturl = lambda: req.full_url
        return response

    monkeypatch.setattr(urllib.request, 'urlopen', get)
    m = plugin.frugal_extract
    generic = 'Existing generic extraction. ' * 10
    monkeypatch.setattr(m, '_run_defuddle', lambda *a: dict(ok=True, content=generic))
    monkeypatch.setattr(m, '_firecrawl_provider', lambda: pytest.fail('Generic success must skip Firecrawl'))
    cfg = dict(firecrawl_hosts=set(), skip_firecrawl_hosts=set(), timeout_s=7,
               max_chars=1234, min_local_chars=200)
    out = asyncio.run(m.FrugalExtractProvider(cfg).extract([url]))[0]
    assert out['content'] == generic and out['metadata']['extract_method'] == 'defuddle'
    assert calls == ([url] if failure == 'unrelated' else [endpoint, url])
    assert out['metadata']['attempts'] == ([] if failure == 'unrelated' else [method]) + ['direct_http', 'defuddle']
