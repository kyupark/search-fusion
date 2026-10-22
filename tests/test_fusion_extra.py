import concurrent.futures
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest


def test_tavily_transport(plugin,monkeypatch):
    f=plugin.fusion; calls=[]
    with pytest.raises(RuntimeError,match='TAVILY'): f._tavily_search('query',5,2)
    monkeypatch.setattr(f,'get_provider_env',lambda key:'fake-key')
    def post(url,**kwargs):
        calls.append((url,kwargs))
        return httpx.Response(200,json={'results':[{'url':'https://a','title':'T','content':'C'},None]},request=httpx.Request('POST',url))
    monkeypatch.setattr(f._HTTP_CLIENT,'post',post)
    assert f._tavily_search('query',99,2)==[{'url':'https://a','title':'T','description':'C'}]
    assert calls[0][1]['json']=={'query':'query','max_results':20,'search_depth':'basic'}
    assert calls[0][1]['headers']['Authorization']=='Bearer fake-key'


def test_ddgs_optional_and_rows(plugin,monkeypatch):
    f=plugin.fusion
    f._ddgs_available.cache_clear()
    monkeypatch.setitem(sys.modules,'ddgs',None)
    assert f._ddgs_available() is False
    calls=[]
    class DDGS:
        def __init__(self,**kw):calls.append(kw)
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def text(self,q,**kw):
            calls.append((q,kw))
            return [None,{'title':'T','href':'https://a','body':'B'},{'title':'U','url':'https://b','description':'D'},{'href':'https://ignored'}]
    monkeypatch.setitem(sys.modules,'ddgs',SimpleNamespace(DDGS=DDGS))
    f._ddgs_available.cache_clear()
    assert f._ddgs_available() is True
    assert f._ddgs_search('q',3,.1)==[dict(title='T',url='https://a',description='B'),dict(title='U',url='https://b',description='D')]
    assert calls==[{'timeout':1},('q',{'max_results':3})]

@pytest.mark.parametrize('name,key',[('brave-free','BRAVE_SEARCH_API_KEY'),('tinyfish','TINYFISH_API_KEY'),('exa','EXA_API_KEY'),('serper','SERPER_API_KEY'),('tavily','TAVILY_API_KEY')])
def test_configured_keys(plugin,monkeypatch,name,key):
    f=plugin.fusion;p=f.SearchFusionWebSearchProvider()
    assert not p._configured(name)
    monkeypatch.setattr(f,'get_provider_env',lambda k:'yes' if k==key else None)
    assert p._configured(name)
    assert not p._configured('unknown')
    assert p._configured('mwmbl')
    monkeypatch.setattr(f,'_ddgs_available',lambda:False)
    assert not p._configured('ddgs')
    monkeypatch.setattr(f,'_ddgs_available',lambda:True)
    assert p._configured('ddgs')

@pytest.mark.parametrize('status,cooldown,blocked', [('provider_cooldown',23,'cooldown'),('provider_cooldown',None,'cooldown'),('auth_error',23,'circuit_open'),('auth_error',None,'circuit_open'),('cancelled',None,None)])
def test_finish_statuses(plugin,status,cooldown,blocked):
    f=plugin.fusion; now=[100.]; c=f._ProviderCoordinator(clock=lambda:now[0]);s=f.FusionSettings(circuit_cooldown_s=7)
    lease,_=c.claim('tinyfish',s);lease.finish(status,s,cooldown)
    assert c.claim('tinyfish',s)[1]==blocked
    if blocked:
        until=100+(cooldown or 7)
        now[0]=until-.01
        assert c.claim('tinyfish',s)[1]==blocked
        now[0]=until
        assert c.claim('tinyfish',s)[1] is None


def test_stale_success_and_recovery(plugin):
    f=plugin.fusion; now=[10.];c=f._ProviderCoordinator(clock=lambda:now[0]);s=f.FusionSettings(circuit_failures=1,circuit_cooldown_s=5)
    old,_=c.claim('tinyfish',s);bad,_=c.claim('tinyfish',s)
    now[0]=11;bad.finish('error',s)
    now[0]=12;old.finish('ok',s)
    assert c.claim('tinyfish',s)[1]=='circuit_open'
    now[0]=16;new,_=c.claim('tinyfish',s);new.finish('ok',s)
    assert 'tinyfish' not in c._circuit_open_until
    assert 'tinyfish' not in c._circuit_opened_at


def test_success_rate_headers(plugin):
    f=plugin.fusion;c=f._ProviderCoordinator(clock=lambda:100);s=f.FusionSettings()
    p=f.SearchFusionWebSearchProvider(settings=s,coordinator=c)
    lease,_=c.claim('brave-free',s)
    response=f._ProviderRows([{'url':'https://a'}],rate_headers={'X-RateLimit-Remaining':'0, 8','X-RateLimit-Reset':'25, 100'})
    outcome=p._invoke('brave-free',lambda *a:response,'q',2,s,lease,threading.Event())
    assert outcome['status']=='ok'
    assert c.claim('brave-free',s)[1]=='cooldown'
    assert c._rate_open_until['brave-free']==125
    assert p._coordinator is c


def test_cancelled_future_releases_lease(plugin):
    f=plugin.fusion;c=f._ProviderCoordinator(); submitted=[]
    class Queue:
        def submit(self,*args):
            future=concurrent.futures.Future(); submitted.append(future);return future
    s=f.FusionSettings(global_timeout_s=.001,first_result_timeout_s=.002)
    p=f.SearchFusionWebSearchProvider(settings=s,searchers={'tinyfish':lambda *a:[]},coordinator=c,executor=Queue())
    assert not p.search('q')['success']
    assert submitted[0].cancelled() and c._inflight=={}


def test_quorum_wait_consumes_empty_then_second_success(plugin):
    f=plugin.fusion
    def search(delay,rows):
        def run(*a):time.sleep(delay);return rows
        return run
    s=f.FusionSettings(global_timeout_s=.02,first_result_timeout_s=.4,quorum_timeout_s=.3)
    p=f.SearchFusionWebSearchProvider(settings=s,searchers={'tinyfish':search(0,[{'url':'https://a'}]),'serper':search(.06,[]),'ddgs':search(.1,[{'url':'https://b'}])})
    result=p.search('q')
    assert result['metadata']['providers_used']==['tinyfish','ddgs']
    assert result['metadata']['quorum_met']


def test_no_grace_after_on_time_quorum(plugin):
    f=plugin.fusion
    # Windows are wide so a briefly descheduled worker cannot turn the
    # on-time first result into a late one.
    def slow(*a):time.sleep(.8);return [{'url':'https://b'}]
    s=f.FusionSettings(global_timeout_s=.3,first_result_timeout_s=4,quorum_timeout_s=3,min_providers=1,post_first_grace_s=2)
    p=f.SearchFusionWebSearchProvider(settings=s,searchers={'tinyfish':lambda *a:[{'url':'https://a'}],'ddgs':slow})
    result=p.search('q')
    assert result['metadata']['providers_used']==['tinyfish']
    assert result['metadata']['fusion_attempts'][-1]['status']=='soft_deadline'


def test_ready_future_is_drained(plugin,monkeypatch):
    f=plugin.fusion;now=[100.];futures=[]
    monkeypatch.setattr(f.time,'monotonic',lambda:now[0])
    class Queue:
        def submit(self,fn,*args):
            future=concurrent.futures.Future();futures.append((future,fn,args));return future
    def wait(pending,timeout,return_when):
        # Both have completed by the time the waiter wakes, but the wait API
        # reports only the first; the second must be drained via done().
        now[0]+=1
        for future,fn,args in futures:
            if not future.done():future.set_result(fn(*args))
        first=futures[0][0]
        return {first},set(pending)-{first}
    monkeypatch.setattr(f.concurrent.futures,'wait',wait)
    s=f.FusionSettings(global_timeout_s=.1,first_result_timeout_s=.2,min_providers=1)
    p=f.SearchFusionWebSearchProvider(settings=s,searchers={'tinyfish':lambda *a:[{'url':'https://a'}],'ddgs':lambda *a:[{'url':'https://b'}]},executor=Queue())
    result=p.search('q')
    assert result['metadata']['providers_used']==['tinyfish','ddgs']
    assert all(a['status'] in {'ok','unavailable'} for a in result['metadata']['fusion_attempts'])

@pytest.mark.parametrize('name,method,payload,expected', [
    ('_brave_search','get',{'web':{'results':[{'url':'https://a'}]}},[{'url':'https://a'}]),
    ('_tinyfish_search','get',{'results':[{'url':'https://a'},{'url':'https://b'}]},[{'url':'https://a'}]),
    ('_exa_search','post',{'results':[{'url':'https://a'}]},[{'url':'https://a'}]),
    ('_serper_search','post',{'organic':[{'link':'https://a','title':'T','snippet':'S'},None]},[dict(url='https://a',title='T',description='S')]),
    ('_mwmbl_search','get',[{'url':'https://a','title':[{'value':' T '}],'extract':[{'value':'body'},None]}],[dict(url='https://a',title='T',description='body')]),
])
def test_provider_transports(plugin,monkeypatch,name,method,payload,expected):
    f=plugin.fusion;calls=[]
    monkeypatch.setattr(f,'get_provider_env',lambda key:'fake-key')
    def request(url,**kw):
        calls.append((url,kw))
        return httpx.Response(200,json=payload,headers={'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'30','private-secret':'hidden'},request=httpx.Request(method.upper(),url))
    monkeypatch.setattr(f._HTTP_CLIENT,method,request)
    result=getattr(f,name)('query',1,.5)
    assert result==expected and calls[0][1]['timeout'].read==.5
    if name=='_brave_search':assert result.rate_headers=={'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'30'}

@pytest.mark.parametrize('name',['_brave_search','_tinyfish_search','_exa_search','_serper_search','_tavily_search'])
def test_missing_provider_keys(plugin,name):
    with pytest.raises(RuntimeError,match='not configured'):getattr(plugin.fusion,name)('q',1,1)


def test_budget_corruption_and_unwritable_state(plugin,monkeypatch,tmp_path):
    f=plugin.fusion
    f._BUDGET_STATE_PATH.write_bytes(b'invalid json')
    monkeypatch.setattr(f,'_MONTHLY_BUDGET',{'tavily':2})
    assert f._budget_allows('tavily')
    # A directory is unreadable as JSON and cannot be overwritten as a file.
    monkeypatch.setattr(f,'_BUDGET_STATE_PATH',tmp_path)
    f._BUDGET_CACHE.clear()
    assert f._budget_allows('tavily') and f._budget_allows('tavily')
    assert not f._budget_allows('tavily')

@pytest.mark.parametrize('value,expected',[('bad',5),(None,5),(-2,1),(99,10),(3,3)])
def test_numeric_bounds(plugin,value,expected):
    f=plugin.fusion
    assert f._bounded_float(value,5,1,10)==expected
    assert f._bounded_int(value,5,1,10)==expected


def test_rate_header_clamping_and_defaults(plugin):
    f=plugin.fusion
    assert f._rate_limit_cooldown(RuntimeError(),17)==17
    for headers,want in [({},17),({'Retry-After':'bad'},17),({'Retry-After':'0'},1),({'Retry-After':'999999999'},31*24*60*60),({'Retry-After':'2','X-RateLimit-Remaining':'0','X-RateLimit-Reset':'7'},7.1)]:
        e=SimpleNamespace(response=SimpleNamespace(headers=headers))
        assert f._rate_limit_cooldown(e,17)==want
    for error in [TimeoutError('secret'),httpx.ReadTimeout('secret')]:
        assert f._classify_provider_exception(error,f.FusionSettings())==('timeout','provider request timed out',None)
    assert f._classify_provider_exception(ValueError('secret'),f.FusionSettings())==('error','provider request failed (ValueError)',None)


def test_ready_ranking_precedes_later_grace_result(plugin,monkeypatch):
    f=plugin.fusion;now=[100.];tasks={};waits=[]
    monkeypatch.setattr(f.time,'monotonic',lambda:now[0])
    class Queue:
        def submit(self,fn,*args):
            future=concurrent.futures.Future();tasks[args[0]]=(future,fn,args);return future
    def complete(name):
        future,fn,args=tasks[name]
        if not future.done():future.set_result(fn(*args))
        return future
    def wait(pending,timeout,return_when):
        waits.append(return_when)
        if len(waits)==1:
            now[0]+=.02
            return set(),set(pending)
        if len(waits)==2:
            now[0]+=.02
            first=complete('ddgs');complete('serper')
            return {first},set(pending)-{first}
        now[0]+=.01
        complete('tinyfish')
        return set(pending),set()
    monkeypatch.setattr(f.concurrent.futures,'wait',wait)
    s=f.FusionSettings(global_timeout_s=.01,first_result_timeout_s=.5,post_first_grace_s=.1)
    p=f.SearchFusionWebSearchProvider(settings=s,executor=Queue(),searchers={
        'tinyfish':lambda *a:[{'url':'https://shared','title':'Later title'}],
        'serper':lambda *a:[{'url':'https://shared','title':'Earlier title'}],
        'ddgs':lambda *a:[{'url':'https://different','title':'First result'}]})
    result=p.search('q')
    shared=next(r for r in result['data']['web'] if r['url']=='https://shared')
    assert shared['title']=='Earlier title'
    assert shared['source_providers']==['serper','tinyfish']


@pytest.mark.parametrize("initial_providers", [False, True])
def test_first_result_retries_busy_provider_until_hard_deadline(plugin, monkeypatch, initial_providers):
    f = plugin.fusion
    now = [100.0]
    tasks = {}
    calls = []
    claims = []
    settings = f.FusionSettings(post_first_grace_s=0)
    monkeypatch.setattr(f, "_MONTHLY_BUDGET", {"tavily": 1})
    # With initial providers, the remaining slot frees just after their 4.5s
    # transport timeouts. Otherwise it frees after the 2.6s quorum window.
    available_at = 104.6 if initial_providers else 103.0
    monkeypatch.setattr(f.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(f.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    class Coordinator(f._ProviderCoordinator):
        def claim(self, name, config):
            claims.append(name)
            if name == "brave-free":
                return None, "cooldown"
            if name == "tavily":
                if now[0] < available_at:
                    self._inflight[name] = f._PROVIDER_MAX_INFLIGHT[name]
                else:
                    self._inflight.pop(name, None)
            return super().claim(name, config)

    class Queue:
        def submit(self, fn, *args):
            future = concurrent.futures.Future()
            delay = settings.provider_timeout_s if args[0] != "tavily" else .1
            tasks[future] = (now[0] + delay, fn, args)
            return future

    def wait(pending, timeout, return_when):
        now[0] = min(now[0] + timeout, min(tasks[future][0] for future in pending))
        done = {future for future in pending if tasks[future][0] <= now[0]}
        for future in done:
            _, fn, args = tasks[future]
            future.set_result(fn(*args))
        return done, set(pending) - done

    monkeypatch.setattr(f.concurrent.futures, "wait", wait)

    def timed_out(*args):
        calls.append(("timeout", args[2]))
        raise TimeoutError("mock provider timeout")

    def remaining(*args):
        calls.append(("tavily", args[2]))
        return [{"url": "https://remaining.example/result"}]

    searchers = {"brave-free": timed_out, "tavily": remaining}
    if initial_providers:
        searchers.update(tinyfish=timed_out, serper=timed_out)
    coordinator = Coordinator(clock=lambda: now[0])
    provider = f.SearchFusionWebSearchProvider(
        settings=settings, searchers=searchers, coordinator=coordinator, executor=Queue(),
    )
    result = provider.search("query")

    assert result["success"] is True
    assert result["data"]["web"][0]["url"] == "https://remaining.example/result"
    assert result["metadata"]["providers_used"] == ["tavily"]
    assert result["metadata"]["quorum_target"] == (2 if initial_providers else 1)
    assert result["metadata"]["quorum_met"] is (not initial_providers)
    assert now[0] <= 100 + settings.first_result_timeout_s
    assert calls.count(("tavily", 4.5)) == 1
    assert f._BUDGET_CACHE["counts"] == {"tavily": 1}
    assert calls.count(("timeout", 4.5)) == (2 if initial_providers else 0)
    assert claims.count("brave-free") == 1  # Never retry a quota cooldown.
    assert coordinator._inflight == {}
