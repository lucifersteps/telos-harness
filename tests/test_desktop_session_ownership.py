"""Restoring a visible Desktop tab must not steal the WeChat session writer."""
import asyncio
import hashlib
import json
import shutil
import urllib.request
import urllib.error

import pytest
from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, run_async
from settings import read_config, write_config
from runtime import ROOT


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires official Desktop')
@pytest.mark.parametrize('mode',['legacy','eager','ready'])
@run_async
async def test_desktop_restore_and_wechat_share_channel_owner(tmp_path,mode):
    patched=mode=='ready'
    d=Desktop(tmp_path)
    try:
        if mode!='legacy':
            target=d.profile/'node_modules/telos-harness'
            manifest=json.loads((target/'component-manifest.json').read_text())
            for name in ['bootstrap.mjs','index.mjs','proactive.mjs','runtime-context.mjs','proactive-policies/goal-execution/index.mjs','proactive-policies/goal-execution/activity-check.mjs']:
                if name=='bootstrap.mjs' and mode=='eager':continue
                shutil.copy2(ROOT/'plugin'/name,target/name)
                if name=='index.mjs':
                    # Existing large history/other startup work makes the race
                    # visible; readiness must cover it regardless of duration.
                    code=(target/name).read_text().replace('  await delivery.replay();','  await new Promise(resolve=>setTimeout(resolve,1000));\n  await delivery.replay();')
                    (target/name).write_text(code)
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
        d.stop();polls=d.services.polls;await d.start()
        # Restore immediately when the Host publishes its authenticated URL,
        # without waiting for Telos polling: the real shell does the same.
        if mode=='legacy':await d.wait(lambda:d.services.polls>polls)
        def inspect(resolve=False):
            req=urllib.request.Request(d.url+'/api/test/owner',data=json.dumps({'session':session,'resolve':resolve}).encode(),headers={'Content-Type':'application/json'})
            return json.loads(d.client.open(req,timeout=20).read())
        def gateway_ready():
            try:inspect();return True
            except urllib.error.HTTPError as error:
                if error.code==404:return False
                raise
        await d.wait(gateway_ready)
        assert inspect()=={'live':patched,'channelTools':patched},d.safe_log()
        assert inspect(True)=={'live':True,'channelTools':patched}
        if mode=='eager':
            # The page has acquired the writer while channel startup is still
            # in flight. No user message can reach Telos until startup finishes.
            await asyncio.sleep(1.2)
            assert d.services.polls==polls
            assert inspect()=={'live':True,'channelTools':False}
            return
        d.model.script=['恢复后正常回复。'];d.services.push('owner-next','恢复后消息')
        if patched:
            await d.wait(lambda:'恢复后正常回复。' in json.dumps(d.services.sent,ensure_ascii=False))
            assert 'already owned by an active write handle' not in d.safe_log()
            await d.action('stop')
            assert not d.get()['running']
            await d.action('start')
            assert inspect(True)=={'live':True,'channelTools':True}
            d.model.script=['再次启动后正常回复。'];d.services.push('owner-restart','再次启动测试')
            await d.wait(lambda:'再次启动后正常回复。' in json.dumps(d.services.sent,ensure_ascii=False))
        else:
            await d.wait(lambda:'already owned by an active write handle' in d.safe_log())
            assert '恢复后正常回复。' not in json.dumps(d.services.sent,ensure_ascii=False)
    finally:await d.close()
