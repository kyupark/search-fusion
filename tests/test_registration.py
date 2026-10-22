from types import SimpleNamespace
import sys

import pytest

class Registry:
    def __init__(self): self.entries={}; self.calls=[]
    def register(self,name,handler=None,**kwargs):
        self.calls.append(name)
        self.entries[name]=SimpleNamespace(handler=handler,is_async=False)
        return 'registered'
    def get_entry(self,name): return self.entries.get(name)
    def deregister(self,name): self.entries.pop(name,None)

@pytest.mark.parametrize('name,blocked', [('mcp_tinyfish_search',True),('tinyfish_search',True),('mcp_tinyfish_fetch_content',True),('tinyfish_fetch_content',True),('fetch_content',True),('web_search',False),('web_fetch',False),('other',False)])
def test_block_hook(plugin,name,blocked):
    result=plugin._block_direct_tinyfish_tools(name, {'url':'https://x'})
    if blocked:
        assert result['action']=='block'
        assert ('web_fetch' if 'fetch' in name else 'web_search') in result['message']
    else: assert result is None


def test_register_and_refresh(plugin,monkeypatch):
    reg=Registry(); monkeypatch.setattr(plugin,'registry',reg)
    reg.register('mcp_tinyfish_fetch_content',handler=lambda args,**kw: (args,kw))
    reg.register('tinyfish_search',handler=lambda args: 'search')
    reg.register('normal')
    providers=[]; tools=[]; hooks=[]
    monkeypatch.setattr(plugin,'_patch_web_tools_backend_availability',lambda:None)
    ctx=SimpleNamespace(register_web_search_provider=providers.append,register_tool=lambda **kw:tools.append(kw),register_hook=lambda *a:hooks.append(a))
    plugin.register(ctx)
    assert [p.name for p in providers]==['search-fusion','frugal-extract']
    assert set(reg.entries)=={'normal'}
    assert set(plugin._HIDDEN_TINYFISH_ENTRIES)=={'mcp_tinyfish_fetch_content'}
    filt=reg.register; plugin._install_tinyfish_direct_tool_filter(); assert reg.register is filt
    assert reg.register('ordinary')=='registered'
    reg.register(name='fetch_content',handler=lambda args:'internal')
    reg.register('mcp_tinyfish_search')
    assert set(reg.entries)=={'normal','ordinary'}
    assert plugin._hidden_tinyfish_dispatch('fetch_content',{})=='internal'
    assert plugin._hidden_tinyfish_dispatch('mcp_tinyfish_fetch_content',{'a':1}, flag=True)==({'a':1},{'flag':True})
    with pytest.raises(RuntimeError): plugin._hidden_tinyfish_dispatch('missing',{})
    assert plugin.tools._HIDDEN_TINYFISH_DISPATCH is plugin._hidden_tinyfish_dispatch
    tool=tools[0]
    assert tool['name']=='web_fetch' and tool['toolset']=='web'
    assert tool['schema'] is plugin.WEB_FETCH_SCHEMA and tool['check_fn']()
    monkeypatch.setattr(plugin,'web_fetch',lambda args,**kw:(args,kw))
    assert tool['handler']({'x':1},extra=2)==({'x':1},{'plugin_ctx':ctx,'extra':2})
    assert hooks==[('pre_tool_call',plugin._block_direct_tinyfish_tools),
                   ('pre_tool_call',plugin.capture_call_context),
                   ('post_tool_call',plugin.clear_call_context)]


def test_backend_patch(plugin,monkeypatch):
    fake=SimpleNamespace(_is_backend_available=lambda name:name=='other')
    monkeypatch.setitem(sys.modules,'tools.web_tools',fake)
    import tools
    monkeypatch.setattr(tools,'web_tools',fake,raising=False)
    plugin._patch_web_tools_backend_availability()
    patched=fake._is_backend_available
    assert patched(' SEARCH-FUSION ') and patched('other') and not patched('missing') and not patched(None)
    plugin._patch_web_tools_backend_availability()
    assert fake._is_backend_available is patched


def test_schema(plugin):
    s=plugin.WEB_FETCH_SCHEMA
    assert s['name']=='web_fetch'
    p=s['parameters']; assert p['type']=='object' and p['required']==['url']
    props=p['properties']
    assert set(props)=={'url','mode','max_chars','timeout','ttl'}
    assert props['url']['type']=='string'
    assert props['mode']['enum']==['auto','article','raw','rendered']
    assert props['mode']['default']=='auto'
    assert props['max_chars']['default']==50000 and props['timeout']['default']==30
    assert all(props[x]['type']=='integer' for x in ['max_chars','timeout','ttl'])
