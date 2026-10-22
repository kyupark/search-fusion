import asyncio
from types import SimpleNamespace
import pytest

@pytest.fixture
def cfg():
    return dict(firecrawl_hosts=set(),skip_firecrawl_hosts=set(),firecrawl_only_hosts=set(),timeout_s=7,max_chars=1234,min_local_chars=200)

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

@pytest.mark.parametrize('force', [False, True])
def test_remote_extract_always_disabled(plugin,monkeypatch,cfg,force):
    m=plugin.frugal_extract
    if force:
        cfg['firecrawl_hosts']={'example.com'}  # Legacy configuration cannot bypass safety.
    p=m.FrugalExtractProvider(cfg)
    calls=[]
    monkeypatch.setattr(p,'_local',lambda *a:calls.append(a) or (None,['direct_http','defuddle'],'failed',True))
    out=asyncio.run(p.extract(['https://example.com'],format='markdown'))[0]
    assert calls and 'remote extraction disabled' in out['error']
    assert out['metadata']['attempts']==['direct_http','defuddle']


@pytest.mark.parametrize('kind',['local','final'])
def test_extract_outcomes(plugin,monkeypatch,cfg,kind):
    m=plugin.frugal_extract;p=m.FrugalExtractProvider(cfg)
    local_result={'content':'local','final_url':'https://final','title':'Title','description':'D','author':'A','published':'P','truncated':False,'ignored':1} if kind=='local' else None
    monkeypatch.setattr(p,'_local',lambda *a:(local_result,['direct_http'],'failure',False))
    out=asyncio.run(p.extract(['https://a']))[0]
    if kind=='local':
        assert out['url']=='https://final' and out['title']=='Title' and out['content']==out['raw_content']=='local'
        assert out['metadata']==dict(extract_method='direct_http',attempts=['direct_http'],description='D',author='A',published='P',truncated=False)
    else:
        assert out['error'].startswith('failure')

def test_provider_contract(plugin,cfg):
    p=plugin.frugal_extract.FrugalExtractProvider(cfg)
    assert p.name=='frugal-extract' and 'Frugal Extract' in p.display_name
    assert p.is_available() and p.supports_extract() and not p.supports_search()
    assert p._cfg() is cfg

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
    assert out==dict(timeout_s=30 if broken else 9,max_chars=200000 if broken else 600,min_local_chars=200 if broken else 50)
