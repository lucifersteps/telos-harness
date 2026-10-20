"""Real native web tools in CLI and Desktop, with deterministic provider fixtures."""
import asyncio
import json
import yaml
import pytest
from runtime import ROOT
from support import Harness, call
from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, run_async
from settings import read_config, write_config


def web_patch(path):
    provider = path / 'web-provider.mjs'
    provider.write_text('''
export const inject=['web'];
export function apply(ctx){
 ctx.web.registerSearchProvider({id:'telos-test',available:()=>true,
   search:async({query})=>({sources:[{url:'https://example.com/telos-web',title:'Test source',snippet:'SEARCH_RESULT '+query}],truncated:false})});
 ctx.web.registerFetchProvider({id:'telos-test',available:()=>true,
   fetch:async({url})=>({url,statusCode:200,body:{kind:'html',content:'<html><body><h1>FETCHRESULT</h1><p>Verified page body.</p></body></html>'},truncated:false})});
}
''')
    return [{'id':'web','config':{'searchProvider':'telos-test','fetchProvider':'telos-test'}},
            {'insert':[{'id':'test-web-provider','name':provider.as_uri()}]}]


def script():
    return [call('web-search',{'queries':['Telos integration']},'web_search'),
            call('web-fetch',{'url':'https://example.com/telos-web'},'web_fetch'),
            '网页验证完成。']


def assert_web(request):
    names=[t.get('name') or t['function']['name'] for t in request['tools']]
    assert names.count('web_search') == names.count('web_fetch') == 1
    results=[b for m in request['messages'] if isinstance(m.get('content'),list) for b in m['content'] if b.get('type')=='tool_result']
    assert len(results)>=2 and all(not b.get('is_error') for b in results[-2:])
    messages=json.dumps(results[-2:])
    assert 'SEARCH_RESULT Telos integration' in messages
    assert 'FETCHRESULT' in messages and 'Verified page body.' in messages
    assert 'https://example.com/telos-web' in messages


def test_cli_web_tools_and_resume(tmp_path):
    async def run():
        h=Harness(tmp_path, script())
        patch=tmp_path/'web.patch.yml'
        patch.write_text((ROOT/'native.patch.yml').read_text()+'\n'+yaml.safe_dump(web_patch(tmp_path)))
        try:
            h.runtime.command[-1]=str(patch)
            await h.submit('搜索并读取网页')
            assert_web(h.model.requests[-1])
            assert h.sent[-1]=='网页验证完成。'
            await h.runtime.close()
            h.runtime=h.restart();h.runtime.command[-1]=str(patch)
            h.model.script=script()
            await h.submit('恢复后再次搜索并读取网页')
            assert_web(h.model.requests[-1])
            assert len(h.model.requests)==6
        finally:await h.close()
    asyncio.run(run())


def test_missing_search_backend_returns_native_error(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('search-missing',{'queries':['Telos']},'web_search'),'搜索后端未配置。'])
        patch=tmp_path/'missing-web.patch.yml'
        patch.write_text((ROOT/'native.patch.yml').read_text()+'\n'+yaml.safe_dump([
            {'id':'web','config':{'searchProvider':'missing-test-provider'}}]))
        try:
            h.runtime.command[-1]=str(patch)
            await h.submit('搜索网页')
            request=h.model.requests[-1]
            result=request['messages'][-1]['content'][-1]
            assert result['is_error'] is True
            assert 'missing-test-provider" is not registered' in result['content'][0]['text']
            assert h.sent[-1]=='搜索后端未配置。'
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires official desktop and built archive')
@run_async
async def test_desktop_web_tools_and_resume(tmp_path):
    d=Desktop(tmp_path)
    patch=d.profile/'cordis.patch.yml'
    patch.write_text(json.dumps(json.loads(patch.read_text())+web_patch(tmp_path)))
    d.model.script=script()
    try:
        await d.start()
        await d.action('save',tools={'apple':False,'maps':False})
        root=d.home/'telos';config=read_config(root)
        config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(root,config)
        await d.action('login');await d.action('start')
        d.services.push('web-first','搜索并读取网页')
        await d.wait(lambda:bool(d.services.sent))
        assert_web(d.model.requests[-1])
        d.stop();await d.start();await d.wait(lambda:d.get()['running'])
        d.model.script=script()
        d.services.push('web-resume','恢复后再次搜索并读取网页')
        await d.wait(lambda:len(d.model.requests)>=6 and len(d.services.sent)>=2)
        assert_web(d.model.requests[-1])
        assert len(d.model.requests)==6
    finally:await d.close()
