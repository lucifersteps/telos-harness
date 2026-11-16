"""Restoring a visible Desktop tab must not steal the WeChat session writer."""
import hashlib
import json
import urllib.request
import urllib.error

import pytest
from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, run_async
from settings import read_config, write_config


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires official Desktop')
@pytest.mark.parametrize('mode',['installed','delayed'])
@run_async
async def test_desktop_restore_and_wechat_share_channel_owner(tmp_path,mode):
    d=Desktop(tmp_path)
    try:
        if mode=='delayed':
            target=d.profile/'node_modules/telos-harness'
            manifest=json.loads((target/'component-manifest.json').read_text())
            name='index.mjs'
            original=(target/name).read_text()
            needle='  await delivery.replay();'
            assert needle in original
            (target/name).write_text(original.replace(needle,'  await new Promise(resolve=>setTimeout(resolve,1000));\n'+needle))
            manifest['files'][name]=hashlib.sha256((target/name).read_bytes()).hexdigest()
            (target/'component-manifest.json').write_text(json.dumps(manifest))
        probe=tmp_path/'plugin-lifecycle.mjs'
        probe.write_text(probe.read_text()+'''
export const ownerProbe = true;
''')
        # Use the same public controller as the Desktop's restored tab.
        code=probe.read_text().replace("'connection','pluginManager'","'sessionController','connection','pluginManager'")
        code=code.replace("export function apply(ctx){", """export function apply(ctx){
ctx.get('appReady').onReady(()=>ctx.connection.fetch.register({path:'/api/test/owner',methods:['POST'],requestBody:'buffered',fetch:async req=>{
 const {session,resolve}=await req.json();
 if(resolve){const result=await ctx.sessionController.resolveAgent(session);if(result.error)return Response.json({error:String(result.error)});}
 const agent=ctx.agents.get(session);
 return Response.json({live:!!agent,channelTools:!!agent&&!!ctx.tools.get('notify_user',agent)});
}}));
""")
        probe.write_text(code)
        await d.start();await d.action('save',tools={'apple':False,'maps':False})
        config=read_config(d.home/'telos');config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(d.home/'telos',config)
        await d.action('login');await d.action('start')
        d.model.script=['绑定成功。'];d.services.push('owner-bind','绑定测试')
        await d.wait(lambda:'绑定成功。' in json.dumps(d.services.sent,ensure_ascii=False))
        session=d.get()['proactive']['sessions'][0]['session']
        d.stop();await d.start()
        # Restore immediately when the Host publishes its authenticated URL,
        # without waiting for Telos polling: the real shell does the same.
        def inspect(resolve=False):
            req=urllib.request.Request(d.url+'/api/test/owner',data=json.dumps({'session':session,'resolve':resolve}).encode(),headers={'Content-Type':'application/json'})
            return json.loads(d.client.open(req,timeout=20).read())
        def gateway_ready():
            try:inspect();return True
            except urllib.error.HTTPError as error:
                if error.code==404:return False
                raise
        await d.wait(gateway_ready)
        assert inspect()=={'live':True,'channelTools':True},d.safe_log()
        assert inspect(True)=={'live':True,'channelTools':True}
        d.model.script=['恢复后正常回复。'];d.services.push('owner-next','恢复后消息')
        await d.wait(lambda:'恢复后正常回复。' in json.dumps(d.services.sent,ensure_ascii=False))
        assert 'already owned by an active write handle' not in d.safe_log()
        await d.action('stop')
        assert not d.get()['running']
        await d.action('start')
        assert inspect(True)=={'live':True,'channelTools':True}
        d.model.script=['再次启动后正常回复。'];d.services.push('owner-restart','再次启动测试')
        await d.wait(lambda:'再次启动后正常回复。' in json.dumps(d.services.sent,ensure_ascii=False))
    finally:await d.close()
