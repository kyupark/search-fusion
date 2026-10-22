import json
import urllib.error
from types import SimpleNamespace
import pytest

@pytest.mark.parametrize('text,limit,expected', [('abc',2,('ab',True)),('abc',3,('abc',False)),('abc',0,('abc',False)),('abc',-1,('abc',False)),('',1,('',False))])
def test_clip(plugin,text,limit,expected):
    assert plugin.tools._clip(text,limit)==expected

@pytest.mark.parametrize('url,expected',[('https://example.org/x',True),('http://a',True),('https:///x',False),('file://a',False),('a',False),('http://[',False)])
def test_http_url(plugin,url,expected):
    assert plugin.tools._is_http_url(url)==expected

@pytest.mark.parametrize('url,expected',[(f'https://a/file{ext}?x=1',True) for ext in ['.md','.txt','.json','.jsonl','.yaml','.yml','.xml','.csv','.tsv','.rss','.atom']]+[('https://RAW.GITHUBUSERCONTENT.COM/a',True),('https://gist.githubusercontent.com/a',True),('https://a/RAW/x',True),('https://a/path/raw',True),('https://a/rawish',False),('https://a/a.html',False)])
def test_raw(plugin,url,expected):
    assert plugin.tools._looks_raw(url)==expected

def test_html(plugin):
    assert plugin.tools._html_to_text('<script>x</script><STYLE>x</STYLE><noscript>x</noscript><p>Hello &amp;\t world</p><p>A<br/>B</p>')=='Hello & world\nA\nB'

@pytest.mark.parametrize('mode,raw,states,order,success,method',[
    ('auto',False,(True,True,True),['defuddle'],True,'defuddle'),
    ('article',True,(True,True,True),['defuddle'],True,'defuddle'),
    ('raw',False,(True,True,True),['direct_http'],True,'direct_http'),
    ('raw',False,(False,True,True),['direct_http'],False,None),
    ('auto',True,(False,True,True),['direct_http','defuddle'],True,'defuddle'),
    ('auto',False,(False,False,True),['defuddle','tinyfish_fetch'],True,'tinyfish_fetch'),
    ('rendered',False,(False,True,True),['defuddle','tinyfish_fetch'],True,'tinyfish_fetch'),
    ('rendered',False,(False,True,False),['defuddle','tinyfish_fetch'],True,'defuddle'),
    ('auto',False,(False,False,False),['defuddle','tinyfish_fetch'],False,None),
])
def test_ladder(plugin,monkeypatch,mode,raw,states,order,success,method):
    m=plugin.tools; calls=[]
    monkeypatch.setattr(m,'_looks_raw',lambda u:raw)
    for name,ok in zip(['direct_http','defuddle','tinyfish_fetch'],states):
        def fake(*args,_name=name,_ok=ok):
            calls.append(_name)
            return dict(ok=_ok,method=_name,content='body' if _ok else '',error='failed',title='Title',description='Desc',truncated=True,author='Author')
        monkeypatch.setattr(m,{'direct_http':'_direct_http','defuddle':'_run_defuddle','tinyfish_fetch':'_tinyfish_fetch'}[name],fake)
    out=json.loads(m.web_fetch({'url':' https://a/page ','mode':mode}))
    assert calls==order and out['success'] is success and out['method']==method
    if success:
        assert out['content']=='body' and out['title']=='Title' and out['description']=='Desc'
        assert out['truncated'] is True and out['metadata']['author']=='Author'
        assert out['fallbacks_attempted']==order
        assert 'content' not in out['metadata'] and 'error' not in out['metadata']
    elif mode!='raw': assert out['needs_browser'] is True

@pytest.mark.parametrize('args,error',[({'url':'file:///a'},'absolute'),({'url':'https://a','mode':'bogus'},'invalid mode')])
def test_invalid(plugin,args,error):
    assert error in json.loads(plugin.tools.web_fetch(args))['error']

def test_argument_defaults(plugin,monkeypatch):
    seen=[]
    monkeypatch.setattr(plugin.tools,'_run_defuddle',lambda *a: seen.append(a) or {'ok':True,'method':'defuddle'})
    plugin.tools.web_fetch({'url':'https://a','max_chars':'oops','timeout':{},'ttl':'bad'})
    assert seen==[('https://a',30,50000)]

@pytest.mark.parametrize('data,content', [({'contentMarkdown':' first ','markdown':'second'},'first'),({'markdown':'second'},'second'),({'text':'third'},'third'),({'content':'<p>fourth</p>'},'fourth'),({},'')])
def test_defuddle_fields(plugin,monkeypatch,data,content):
    m=plugin.tools
    monkeypatch.setattr(m.shutil,'which',lambda x:'/fake')
    calls=[]
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:calls.append((a,k)) or SimpleNamespace(returncode=0,stdout=json.dumps(data),stderr=''))
    out=m._run_defuddle('https://a',7,99)
    assert out['content']==content and out['ok']==bool(content)
    assert calls[0][0][0]==['defuddle','parse','--json','https://a'] and calls[0][1]['timeout']==7

@pytest.mark.parametrize('kind', ['missing','timeout','error','nonzero','plain','empty'])
def test_defuddle_failures(plugin,monkeypatch,kind):
    m=plugin.tools
    monkeypatch.setattr(m.shutil,'which',lambda _:None if kind=='missing' else '/fake')
    def run(*a,**k):
        if kind=='timeout': raise m.subprocess.TimeoutExpired('x',3)
        if kind=='error': raise RuntimeError('boom')
        return SimpleNamespace(returncode=1 if kind=='nonzero' else 0,stderr='bad',stdout=' text ' if kind=='plain' else '')
    monkeypatch.setattr(m.subprocess,'run',run)
    out=m._run_defuddle('https://a',3,2)
    assert out['ok'] is (kind=='plain')
    if kind=='plain': assert out['content']=='te' and out['truncated'] and out['warning']=='defuddle_non_json_output'
    elif kind!='empty': assert out['error']

def test_direct_http(plugin,monkeypatch):
    m=plugin.tools; seen=[]
    class Response:
        headers={'content-type':'TEXT/HTML'}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def geturl(self):return 'https://final'
        def read(self,n):seen.append(n);return b'<p>Hello world</p>'
    monkeypatch.setattr(m.urllib.request,'urlopen',lambda req,timeout:Response())
    out=m._direct_http('https://a',7,5)
    assert out==dict(ok=True,method='direct_http',content='Hello',content_type='TEXT/HTML',final_url='https://final',truncated=True)
    assert seen==[1024*1024]
    m._direct_http('https://a',7,0)
    assert seen[-1]==10*1024*1024

@pytest.mark.parametrize('error',[urllib.error.HTTPError('https://a',404,'Gone',{},None),RuntimeError('boom')])
def test_direct_error(plugin,monkeypatch,error):
    def fail(*a,**k):raise error
    monkeypatch.setattr(plugin.tools.urllib.request,'urlopen',fail)
    out=plugin.tools._direct_http('https://a',1,1)
    assert out['ok'] is False and ('404' in out['error'] if isinstance(error,urllib.error.HTTPError) else out['error']=='boom')

@pytest.mark.parametrize('shape',[{'results':[{'content':' abc ','title':'T','url':'https://b'}]}, {'data':[{'markdown':' abc ','title':'T','url':'https://b'}]}])
def test_tinyfish(plugin,monkeypatch,shape):
    seen=[]
    monkeypatch.setattr(plugin.tools,'_HIDDEN_TINYFISH_DISPATCH',lambda a:seen.append(a) or json.dumps(shape))
    out=plugin.tools._tinyfish_fetch('https://a',3,2,60,None)
    assert out['ok'] and out['content']=='ab' and out['truncated'] and out['final_url']=='https://b' and out['title']=='T'
    assert seen==[dict(urls=['https://a'],format='markdown',links=False,image_links=False,include_html_head=False,ttl=60)]

def test_tinyfish_dispatch_fallback(plugin,monkeypatch):
    monkeypatch.setattr(plugin.tools,'_HIDDEN_TINYFISH_DISPATCH',None)
    assert not plugin.tools._tinyfish_fetch('https://a',1,1,None,None)['ok']
    names=[]
    def dispatch(name,args):
        names.append(name)
        assert 'ttl' not in args
        if len(names)<3: raise RuntimeError('missing')
        return {'results':[{'text':'ok'}]}
    out=plugin.tools._tinyfish_fetch('https://a',1,10,None,SimpleNamespace(dispatch_tool=dispatch))
    assert out['ok'] and names==['mcp_tinyfish_fetch_content','tinyfish_fetch_content','fetch_content']

@pytest.mark.parametrize('data,error',[({'results':[{'error':'blocked'}]},'blocked'),('junk','unexpected'),({},'unexpected')])
def test_tinyfish_bad_response(plugin,monkeypatch,data,error):
    monkeypatch.setattr(plugin.tools,'_HIDDEN_TINYFISH_DISPATCH',lambda a:data)
    out=plugin.tools._tinyfish_fetch('https://a',1,10,None,None)
    assert not out['ok'] and error in out['error']
