import json
import threading
import time
from dataclasses import replace

import httpx
import pytest


def row(url='https://example.com/a', title='Title', description='text'):
    return dict(url=url, title=title, description=description)

@pytest.mark.parametrize('url,expected', [
    ('https://WWW.Example.COM/a/?utm_a=x&b=2&a=1#f', '//example.com/a?a=1&b=2'),
    ('http://example.com:8080/', '//example.com:8080/'),
    ('https://a.test?ref=x&x=&SOURCE=y&gclid=z', '//a.test/?x='),
    ('http://a.test/a//', '//a.test/a'),
])
def test_canonical(plugin, url, expected):
    assert plugin.fusion._canonical_url(url) == expected

@pytest.mark.parametrize('value', [None, [], 'bad', {}, {'url':'  '}])
def test_bad_rows(plugin, value):
    assert plugin.fusion._normalized_row(value) is None

@pytest.mark.parametrize('field', ['description','snippet','content','highlights'])
def test_row_fallbacks(plugin, field):
    assert plugin.fusion._normalized_row({'url':' https://x ', 'title':' T ',field:['a',3]}) == row('https://x','T','a 3')

@pytest.mark.parametrize('url,query,score', [
    ('https://docs.python.org','python guide',1.25),
    ('https://docs.x.com','the documentation',1.1),
    ('https://x.gov','x',1.1),('https://x.edu','x',1.1),
    ('https://example.com','web search official',1),
    ('https://abc.com','abc',1),('https://ABCD.com','ABCD',1.25),
])
def test_affinity(plugin,url,query,score):
    assert plugin.fusion._domain_affinity(url,query) == score


def test_rrf(plugin):
    f=plugin.fusion
    outcomes=[dict(provider='serper',status='ok',rows=[row(title=''),row(description='long text'),row('https://b.test')]),
              dict(provider='tinyfish',status='ok',rows=[row('http://www.example.com/a/?utm_x=y',title='Filled')]),
              dict(provider='ddgs',status='error',rows=[row('https://ignored.test')])]
    result=f.SearchFusionWebSearchProvider._merge(outcomes,'unrelated',10,60)
    assert len(result)==2
    first=result[0]
    assert first['fusion_score']==round(1.4/61+1/61,8)
    assert first['provider_ranks']=={'serper':1,'tinyfish':1}
    assert first['source_providers']==['serper','tinyfish']
    assert first['description']=='long text' and first['title']=='Title'
    assert [x['position'] for x in result]==[1,2]
    assert 'best_rank' not in first
    assert len(f.SearchFusionWebSearchProvider._merge(outcomes,'unrelated',0,1))==1

@pytest.mark.parametrize('value,want', [(None,None),('',None),('bad',None),('-2',0),('0',0),('2.5',2.5),('1000000010',10),('Sun, 09 Sep 2001 01:46:50 GMT',10)])
def test_header_seconds(plugin,monkeypatch,value,want):
    monkeypatch.setattr(plugin.fusion.time,'time',lambda:1000000000)
    assert plugin.fusion._header_seconds(value)==want

@pytest.mark.parametrize('remaining,reset,want', [('0, 5','4, 90',4),('0,0','4,90',90),('1','50',None),('bad','40',None),('','20',20),('0','bad',None),('-1','0',0)])
def test_windows(plugin,remaining,reset,want):
    assert plugin.fusion._exhausted_window_cooldown({'X-RateLimit-Remaining':remaining,'X-RateLimit-Reset':reset})==want

@pytest.mark.parametrize('code,status,cooldown', [(429,'provider_cooldown',12.1),(401,'auth_error',900),(403,'auth_error',900),(402,'auth_error',900),(500,'error',None)])
def test_classification(plugin,code,status,cooldown):
    response=httpx.Response(code,headers={'Retry-After':'12'},request=httpx.Request('GET','https://secret.test/?key=secret'))
    exc=httpx.HTTPStatusError('secret',request=response.request,response=response)
    result=plugin.fusion._classify_provider_exception(exc,plugin.fusion.FusionSettings())
    assert result[0]==status and result[2]==cooldown
    assert 'secret' not in result[1]


def test_circuit_and_release(plugin):
    f=plugin.fusion; now=[10.]; c=f._ProviderCoordinator(clock=lambda:now[0]); s=f.FusionSettings(circuit_cooldown_s=5)
    a,_=c.claim('tinyfish',s); b,_=c.claim('tinyfish',s)
    a.finish('error',s); a.finish('error',s)
    assert c._inflight['tinyfish']==1 and c._failures['tinyfish']==1
    b.finish('timeout',s)
    assert c.claim('tinyfish',s)[1]=='circuit_open'
    now[0]=15
    a,reason=c.claim('tinyfish',s)
    assert reason is None
    a.finish('empty',s)
    assert c._failures['tinyfish']==0 and not c._inflight


def test_admission_pacing_and_caps(plugin):
    f=plugin.fusion; c=f._ProviderCoordinator(clock=lambda:10); s=f.FusionSettings()
    a,_=c.claim('brave-free',s); b,_=c.claim('brave-free',s)
    assert a.start_after_s==0 and b.start_after_s==pytest.approx(1.1)
    assert c.claim('brave-free',s)[1]=='rate_paced'
    for _ in range(2): assert c.claim('ddgs',s)[1] is None
    assert c.claim('ddgs',s)[1]=='concurrency_limited'
    a.finish('ok',s,20)
    assert c.claim('brave-free',s)[1]=='cooldown'


def test_budget(plugin,monkeypatch):
    f=plugin.fusion
    monkeypatch.setattr(f,'_MONTHLY_BUDGET',{'tavily':2})
    monkeypatch.setattr(f,'_budget_period',lambda:'2026-01')
    assert f._budget_allows('free')
    assert not f._BUDGET_STATE_PATH.exists()
    assert f._budget_allows('tavily')
    f._BUDGET_CACHE.clear()
    assert f._budget_allows('tavily')
    assert not f._budget_allows('tavily')
    assert f._ProviderCoordinator().claim('tavily',f.FusionSettings())[1]=='quota_exhausted'
    monkeypatch.setattr(f,'_budget_period',lambda:'2026-02')
    assert f._budget_allows('tavily')
    assert json.loads(f._BUDGET_STATE_PATH.read_bytes())['counts']=={'tavily':1}

@pytest.mark.parametrize('delays,soft,hard,quorum,expected,status', [
    ((0,.6),.2,3,2,2,None),
    ((0,2),.2,1.2,.6,1,'quorum_deadline'),
    ((.7,.8),.2,3,2,2,None),
    ((2,2),.2,.6,.4,0,'hard_deadline'),
])
def test_search_deadlines(plugin,delays,soft,hard,quorum,expected,status):
    f=plugin.fusion
    def searcher(delay):
        def run(q,l,t):
            time.sleep(delay)
            return [row()]
        return run
    s=f.FusionSettings(global_timeout_s=soft,first_result_timeout_s=hard,quorum_timeout_s=quorum,post_first_grace_s=.5)
    p=f.SearchFusionWebSearchProvider(settings=s,searchers=dict(zip(('tinyfish','ddgs'),map(searcher,delays))))
    started=time.monotonic(); result=p.search('test')
    assert time.monotonic()-started < 3.5
    assert len(result['metadata']['providers_used'])==expected
    assert result['success']==bool(expected)
    assert result['metadata']['quorum_met']==(expected==2)
    if status: assert status in [a['status'] for a in result['metadata']['fusion_attempts']]


def test_empty_and_no_provider(plugin):
    f=plugin.fusion
    p=f.SearchFusionWebSearchProvider(settings=f.FusionSettings(),searchers={'unknown':lambda *a: []})
    assert not p.is_available() and not p.search('x')['success']
    p=f.SearchFusionWebSearchProvider(settings=f.FusionSettings(),searchers={'tinyfish':lambda *a: []})
    result=p.search('x')
    assert not result['success'] and result['metadata']['quorum_target']==1
    assert result['metadata']['fusion_attempts'][-1]['status']=='empty'


def test_settings(plugin,monkeypatch):
    import sys
    f=plugin.fusion
    monkeypatch.setattr(sys.modules['hermes_cli.config'],'load_config',lambda:{'plugins':{'entries':{'search-fusion':{'fusion':{'global_timeout_s':99,'first_result_timeout_s':1,'rrf_k':0,'min_providers':99,'provider_timeout_s':'bad'}}}}})
    s=f.load_fusion_settings()
    assert s.global_timeout_s==12 and s.first_result_timeout_s==12
    assert s.rrf_k==1 and s.min_providers==len(f._PROVIDER_ORDER) and s.provider_timeout_s==4.5
    assert s.quorum_timeout_s==12
    t=f._httpx_timeout(2)
    assert t.connect==.75 and t.read==2
