import asyncio
from types import SimpleNamespace
import pytest

@pytest.fixture
def cfg():
    return dict(firecrawl_hosts=set(),skip_firecrawl_hosts=set(),firecrawl_only_hosts=set(),timeout_s=7,max_chars=1234,min_local_chars=200)

@pytest.mark.parametrize('host,patterns,expected',[('EXAMPLE.COM',{'example.com'},True),('sub.example.com',{'example.com'},True),('evil-example.com',{'example.com'},False),('example.com.evil',{'example.com'},False),('a',set(),False)])
def test_host(plugin,host,patterns,expected):
    assert plugin.frugal_extract._host_matches(host,patterns) is expected

@pytest.mark.parametrize('content,limit,shell',[('x'*199,200,True),('x'*200,200,False),('  x  ',2,True),('x'*1500+'enable javascript',200,False)]+[(marker.upper()+'x'*200,200,True) for marker in ['enable javascript','javascript is required','please turn on javascript','checking your browser','verify you are human','access denied','just a moment']])
def test_shell(plugin,content,limit,shell):
    assert (plugin.frugal_extract._looks_like_shell(content,limit) is not None)==shell

@pytest.mark.parametrize('direct,defuddle,method,attempts,escalate',[
    ({'ok':True,'content':'{}','content_type':'application/json'},None,'direct_http',['direct_http'],False),
    ({'ok':False,'error':'HTTP 404: missing'},None,None,['direct_http'],False),
    ({'ok':False,'error':'HTTP 401: auth'},None,None,['direct_http'],False),
    ({'ok':False,'error':'HTTP 410: gone'},None,None,['direct_http'],False),
    ({'ok':False,'error':'HTTP 403: blocked'},{'ok':True,'content':'x'*200},'defuddle',['direct_http','defuddle'],False),
    ({'ok':True,'content':'x'*200,'content_type':'text/html'},{'ok':False,'error':'bad'},'direct_http',['direct_http','defuddle'],False),
    ({'ok':True,'content':'tiny','content_type':'text/html'},{'ok':True,'content':'tiny'},None,['direct_http','defuddle'],True),
    ({'ok':False,'error':'network'},{'ok':False,'error':'bad'},None,['direct_http','defuddle'],True),
])
def test_local(plugin,monkeypatch,cfg,direct,defuddle,method,attempts,escalate):
    m=plugin.frugal_extract; calls=[]
    def fake(which,result):
        def invoke(url,timeout,max_chars):
            calls.append(which); assert (timeout,max_chars)==(7,1234)
            assert result is not None
            return dict(result,method=which)
        return invoke
    monkeypatch.setattr(m,'_direct_http',fake('direct_http',direct))
    monkeypatch.setattr(m,'_run_defuddle',fake('defuddle',defuddle))
    result,actual,error,esc=m.FrugalExtractProvider(cfg)._local('https://a/page',cfg)
    assert actual==attempts==calls and esc is escalate
    if method: assert result['method']==method and error==''
    else: assert result is None and error

def test_raw_html_accepted(plugin,monkeypatch,cfg):
    monkeypatch.setattr(plugin.frugal_extract,'_direct_http',lambda *a:dict(ok=True,content='tiny',content_type='text/html'))
    out=plugin.frugal_extract.FrugalExtractProvider(cfg)._local('https://a/a.json',cfg)
    assert out[0]['content']=='tiny' and out[1]==['direct_http'] and out[3] is False

@pytest.mark.parametrize('force,skip,only,host,expect',[
    (set(),set(),set(),'example.com','firecrawl'),
    ({'example.com'},set(),set(),'sub.example.com','forced'),
    ({'example.com'},{'example.com'},set(),'example.com','disabled'),
    (set(),set(),{'allowed.com'},'example.com','disabled'),
    (set(),set(),{'example.com'},'sub.example.com','firecrawl'),
])
def test_host_ladder(plugin,monkeypatch,cfg,force,skip,only,host,expect):
    m=plugin.frugal_extract; local=[]; remote=[]
    cfg.update(firecrawl_hosts=force,skip_firecrawl_hosts=skip,firecrawl_only_hosts=only)
    p=m.FrugalExtractProvider(cfg)
    monkeypatch.setattr(p,'_local',lambda *a:local.append(a) or (None,['direct_http','defuddle'],'failed',True))
    def extract(urls,format):
        remote.append((urls,format));return [{'url':urls[0],'content':'remote','metadata':{'preserved':True}}]
    monkeypatch.setattr(m,'_firecrawl_provider',lambda:SimpleNamespace(extract=extract))
    out=asyncio.run(p.extract(['https://'+host],format='markdown'))[0]
    if expect=='disabled': assert not remote and 'disabled' in out['error'] and local
    else:
        assert remote==[(['https://'+host],'markdown')]
        assert out['metadata']['preserved'] and out['metadata']['extract_method']=='firecrawl'
        assert out['metadata']['attempts']==(['firecrawl'] if expect=='forced' else ['direct_http','defuddle','firecrawl'])
        assert bool(local)==(expect!='forced')
        assert out['metadata']['escalation_reason']==('forced by firecrawl_hosts' if expect=='forced' else 'failed')

@pytest.mark.parametrize('kind',['local','final','unavailable','empty','async'])
def test_extract_outcomes(plugin,monkeypatch,cfg,kind):
    m=plugin.frugal_extract;p=m.FrugalExtractProvider(cfg)
    local_result={'content':'local','final_url':'https://final','title':'Title','description':'D','author':'A','published':'P','truncated':False,'ignored':1} if kind=='local' else None
    monkeypatch.setattr(p,'_local',lambda *a:(local_result,['direct_http'],'failure',kind!='final'))
    async def remote(*a,**k):return [{'content':'async','metadata':'invalid'}]
    def get_provider():
        assert kind not in {'local','final'}
        return None if kind=='unavailable' else SimpleNamespace(extract=remote if kind=='async' else lambda *a,**k:[])
    monkeypatch.setattr(m,'_firecrawl_provider',get_provider)
    out=asyncio.run(p.extract(['https://a']))[0]
    if kind=='local':
        assert out['url']=='https://final' and out['title']=='Title' and out['content']==out['raw_content']=='local'
        assert out['metadata']==dict(extract_method='direct_http',attempts=['direct_http'],description='D',author='A',published='P',truncated=False)
    elif kind=='final':assert out['error']=='failure'
    elif kind=='unavailable':assert 'unavailable' in out['error']
    elif kind=='empty':assert out['error']=='Firecrawl returned no result' and out['metadata']['extract_method']=='firecrawl'
    else:assert out['content']=='async' and out['metadata']['extract_method']=='firecrawl'

def test_provider_contract(plugin,cfg):
    p=plugin.frugal_extract.FrugalExtractProvider(cfg)
    assert p.name=='frugal-extract' and 'Frugal Extract' in p.display_name
    assert p.is_available() and p.supports_extract() and not p.supports_search()
    assert p._cfg() is cfg

@pytest.mark.parametrize('kind',['missing','noextract','unavailable','raises','available'])
def test_firecrawl_selection(plugin,monkeypatch,kind):
    import agent.web_search_registry as registry
    def available():
        if kind=='raises':raise RuntimeError('bad')
        return kind=='available'
    provider=None if kind=='missing' else SimpleNamespace(supports_extract=lambda:kind!='noextract',is_available=available)
    monkeypatch.setattr(registry,'get_provider',lambda name:provider)
    assert plugin.frugal_extract._firecrawl_provider() is (provider if kind=='available' else None)

@pytest.mark.parametrize('broken',[False,True])
def test_settings(plugin,monkeypatch,broken):
    import sys
    from types import ModuleType
    fake=ModuleType('hermes_cli.config')
    def load():
        if broken: raise ValueError('invalid')
        return {'plugins':{'entries':{'search-fusion':{'frugal_extract':{'firecrawl_hosts':[' Example.COM ',None],'skip_firecrawl_hosts':['SKIP.COM'],'firecrawl_only_hosts':['ONLY.COM'],'timeout_s':'9','max_chars':'600','min_local_chars':'50'}}}}}
    fake.load_config=load
    monkeypatch.setitem(sys.modules,'hermes_cli.config',fake)
    out=plugin.frugal_extract._settings()
    assert out==dict(firecrawl_hosts=set() if broken else {'example.com'},skip_firecrawl_hosts=set() if broken else {'skip.com'},firecrawl_only_hosts=set() if broken else {'only.com'},timeout_s=30 if broken else 9,max_chars=200000 if broken else 600,min_local_chars=200 if broken else 50)
