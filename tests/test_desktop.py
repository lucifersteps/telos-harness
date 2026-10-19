"""Use the official Desktop Host, native authenticated routes and the real archive."""
import asyncio
import yaml
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import http.client

import pytest
from test_package import Services,ARCHIVE,run_async
from support import Model,call,draft
from settings import read_config,write_config

DESKTOP=Path(os.environ['TELOS_TEST_DESKTOP_APP']) if os.environ.get('TELOS_TEST_DESKTOP_APP') else None

class Desktop:
    def __init__(self,path):
        self.path=path;self.home=path/'desktop-home';self.profile=self.home/'profiles/desktop';self.profile.mkdir(parents=True)
        (self.profile/'package.json').write_text(json.dumps({'name':'dsh-profile-desktop','private':True,'dsh':{'profile':{'bundles':['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app']}}}))
        for name in ['cordis.yml','cordis.patch.yml']:(self.profile/name).write_text('[]\n')
        # Exercise the same native plugin manager used by the desktop UI, without UI timing.
        probe=path/'plugin-lifecycle.mjs'
        probe.write_text("import {createUserMessage} from "+json.dumps((DESKTOP/'Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-llm/lib/index.js').as_uri())+";\n"+"""
export const inject=['connection','pluginManager','agents','agentDefaultModel','tools'];
export function apply(ctx){ctx.connection.fetch.register({path:'/api/test/plugin-lifecycle',methods:['POST'],requestBody:'buffered',fetch:async req=>{try {
const {action}=await req.json();
if(action==='independent'){
 const handle=await ctx.agents.create({sessionId:'independent-host-check',agentOptions:ctx.agentDefaultModel.currentSelection()});
 try {handle.agent.steer(createUserMessage({content:[{type:'text',text:'这是宿主独立会话测试，仅回复收到。'}],source:{kind:'user'}}));await handle.agent.whenIdle();return Response.json({model:ctx.agentDefaultModel.currentSelection(),businessTool:!!ctx.tools.get('personalPlan',handle.agent)});}
 finally {await handle.dispose();}
}
const result=action==='remove'?await ctx.pluginManager.removeBundle('telos-harness'):await ctx.pluginManager.setBundleEnabled('telos-harness',action==='enable');
return Response.json({result,bundles:await ctx.pluginManager.listBundles()});}catch(error){return Response.json({error:error.stack});}}});}
""")
        self.env={k:v for k,v in os.environ.items() if not k.startswith(('TELOS_','DEEPSEEK_','BAIDU_','DSH_','PYTHON'))}
        self.env.update(DSH_AGENTS_HOME=str(self.home/'agents'),DSH_HOME=str(self.home),ELECTRON_RUN_AS_NODE='1')
        self.res=DESKTOP/'Contents/Resources';self.runtime=self.res/'app.asar/dsh';self.exe=DESKTOP/'Contents/MacOS/DeepSeek Harness';self.proc=None
        self.services=Services();self.model=Model([call('bad',{'action':'batch','operations':[]}),call('draft',draft()),'请确认。','已保存。','无待确认操作。'])
        result=subprocess.run([str(self.res/'runtime/cli/bin/dsh'),'plugin','--profile','desktop','add','telos-harness@file:'+str(ARCHIVE),'--offline'],env=self.env,capture_output=True,text=True,timeout=60)
        assert result.returncode==0,result.stdout+result.stderr
        self.env.update(DEEPSEEK_API_KEY='desktop-model-secret',BAIDU_MAP_AUTH_TOKEN='desktop-map-secret')
        (self.profile/'cordis.patch.yml').write_text(json.dumps([
            {'id':'session-title-llm','disabled':True},
            {'id':'llm-deepseek','config':{'baseURL':self.model.url,'thinking':'disabled'}},
            {'id':'agent-default-model','config':{'provider':'deepseek-official','model':'deepseek-flash','reasoningEffort':'off'}},
            {'insert':[{'id':'test-desktop-lifecycle','name':probe.as_uri()}]},
        ]))
    async def start(self):
        self.logpath=self.path/f'host-{time.time_ns()}.log';self.log=self.logpath.open('w')
        self.proc=subprocess.Popen([str(self.exe),'--expose-internals',str(self.runtime/'node_modules/@deepseek-ai/dsh-desktop-host/lib/index.js'),str(self.runtime),str(self.profile),str(self.res/'runtime/primary-runtime'),str(self.res/'runtime/primary-runtime/dependencies/pnpm/bin/pnpm.mjs'),str(self.res/'runtime/bin')],env=self.env,cwd=self.profile,stdout=self.log,stderr=self.log)
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if self.proc.poll() is not None:raise AssertionError(self.safe_log())
            match=re.search(r'dsh web: (http://\S+)',self.logpath.read_text())
            if match:
                url=match[1];self.url=url.split('/?')[0]
                self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                await asyncio.to_thread(lambda:self.client.open(url,timeout=10).read())
                # The native Host can publish its URL before plugin onReady routes finish mounting.
                try:self.get();return
                except urllib.error.HTTPError as e:
                    if e.code!=404:raise
            await asyncio.sleep(.05)
        raise AssertionError('Desktop Host did not become ready')
    def get(self,path='status'):
        return json.loads(self.client.open(self.url+'/api/telos/'+path,timeout=20).read())
    def safe_log(self):
        return re.sub(r'([?&]token=)[^\s&]+',r'\1<redacted>',self.logpath.read_text())
    def post(self,action,**data):
        request=urllib.request.Request(self.url+'/api/telos/action',data=json.dumps({'action':action,**data}).encode(),headers={'Content-Type':'application/json'})
        return json.loads(self.client.open(request,timeout=20).read())
    def manage(self,action):
        request=urllib.request.Request(self.url+'/api/test/plugin-lifecycle',data=json.dumps({'action':action}).encode(),headers={'Content-Type':'application/json'})
        try:return json.loads(self.client.open(request,timeout=90).read())
        except urllib.error.HTTPError as error:raise AssertionError(error.read().decode()) from error
    async def wait(self,predicate,timeout=25):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if self.proc.poll() is not None:raise AssertionError(self.safe_log())
            if predicate():return
            await asyncio.sleep(.1)
        raise AssertionError('Desktop operation timed out: '+str(self.get()))
    async def action(self,action,**data):
        self.post(action,**data)
        await self.wait(lambda:not self.get()['busy'])
        assert not self.get()['error'],self.get()
    def stop(self):
        if self.proc:
            self.proc.terminate();self.proc.wait(timeout=20);self.proc=None;self.log.close()
    async def close(self):
        self.stop();await asyncio.to_thread(self.services.close);await asyncio.to_thread(self.model.close)

@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='须提供官方桌面应用和插件归档')
@run_async
async def test_desktop_first_setup_auth_login_repair_restart_and_confirm(tmp_path):
    d=Desktop(tmp_path)
    try:
        credentials=d.home/'.credentials.yaml'
        saved=json.dumps({'version':1,'refs':{'DEEPSEEK_API_KEY':'desktop-model-secret','BAIDU_MAP_AUTH_TOKEN':'desktop-map-secret'}})
        credentials.write_text(saved);credentials.chmod(0o600)
        d.env.pop('DEEPSEEK_API_KEY');d.env.pop('BAIDU_MAP_AUTH_TOKEN')
        await d.start();saved=credentials.read_text();s=d.get();assert not s['configuration'] and not s['running'] and s['model']['model']=='deepseek-flash'
        with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(d.url+'/api/telos/status')
        assert error.value.code==401
        assert [p['id'] for p in s['personas']]==['default','liman','custom']
        await d.action('save',max_steps=24,persona='liman')
        assert d.get()['config']['persona']=='liman'
        assert 'credentials' not in d.get()
        assert 'desktop-model-secret' not in json.dumps(d.get()) and 'desktop-model-secret' not in d.logpath.read_text()
        assert yaml.safe_load(credentials.read_text()).get('refs')==yaml.safe_load(saved).get('refs')
        helper=tmp_path/'fake-apple';helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\nr=urllib.request.urlopen(urllib.request.Request({d.services.url+"/apple"!r},data=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode(),headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n');helper.chmod(0o700)
        root=d.home/'telos';config=read_config(root);config['apple_helper']=str(helper);config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(root,config)
        await d.action('authorize');assert d.services.authorizations==1
        d.services.login_wait=True;d.post('login');await d.wait(lambda:bool(d.get()['qr']))
        assert d.get()['qr'].startswith('data:image/png;base64,')
        with pytest.raises(urllib.error.HTTPError) as conflict:d.post('start')
        assert conflict.value.code==409
        d.services.login_wait=False;await d.wait(lambda:not d.get()['busy']);assert d.get()['wechat_login'] and not d.get()['error']
        # Start saves the selected form even if the user did not click Save first.
        await d.action('save',max_steps=24,persona='default')
        await d.action('start',max_steps=24,persona='liman',persona_file='')
        assert d.get()['config']['persona']=='liman'
        await d.wait(lambda:d.services.polls>0)
        d.services.push('request','创建测试会议');await d.wait(lambda:len(d.services.sent)>0)
        assert '你叫黎曼' in json.dumps(d.model.requests[0],ensure_ascii=False)
        assert not d.services.apple.writes
        import sqlite3
        with sqlite3.connect(root/'business.sqlite') as db:
            pending=json.loads(db.execute('SELECT value FROM pending').fetchone()[0])
            assert pending['kind']=='approval'
            reply=db.execute('SELECT reply FROM messages ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            assert reply==pending['text']
        assert list((d.home/'sessions').glob('*/*/session.v*.jsonl.zstd'))
        assert {x['name'] for x in d.model.requests[0]['tools']}=={'personalPlan','ask_user','baidu_maps','todo_write','skill','user_memory'}
        d.stop();await d.start();await d.wait(lambda:d.get()['running'])
        d.services.push('confirm','1');await d.wait(lambda:len(d.services.apple.writes)==1 and len(d.services.sent)>1)
        assert '你叫黎曼' in json.dumps(d.model.requests[-1],ensure_ascii=False)
        d.services.push('confirm','1');d.services.push('confirm-again','1');await d.wait(lambda:len(d.services.sent)>2)
        assert len(d.services.apple.writes)==1
        d.model.script=[call('question',{'text':'请问是哪座城市？'},name='ask_user'),'你说的是哪个城市呢？']
        count=len(d.services.sent)
        d.services.push('question-check','还要安排另一个会议，先问城市')
        await d.wait(lambda:len(d.services.sent)>count)
        with sqlite3.connect(root/'business.sqlite') as db:
            assert db.execute('SELECT reply FROM messages ORDER BY rowid DESC LIMIT 1').fetchone()[0]=='请问是哪座城市？'
        d.model.script=[call('memory-read',{'action':'read'},name='user_memory'),
                        call('memory-write',{'action':'write','content':'PROFILE_PACKAGE_TEST: concise answers','expected_content':''},name='user_memory'),'已保存偏好。']
        count=len(d.services.sent);d.services.push('memory-save','记住我的回答偏好')
        await d.wait(lambda:len(d.services.sent)>count)
        profile=next((root/'users').glob('*/user.md'))
        assert profile.read_text()=='PROFILE_PACKAGE_TEST: concise answers'
        d.stop();await d.start();await d.wait(lambda:d.get()['running'])
        d.model.script=['偏好已恢复。\n\n会继续按你的习惯来。'];count=len(d.services.sent)
        d.services.push('memory-resume','我的偏好是什么')
        await d.wait(lambda:len(d.services.sent)>=count+2)
        assert [message['msg']['item_list'][0]['text_item']['text'] for message in d.services.sent[count:]]==['偏好已恢复。','会继续按你的习惯来。']
        assert 'PROFILE_PACKAGE_TEST' in json.dumps(d.model.requests[-1])
        assert 'PROFILE_PACKAGE_TEST' not in str(d.model.requests[-1].get('system'))
        await d.action('stop');assert not d.get()['running']
        original=(root/'config.json').read_bytes()
        d.post('save',max_steps=24,persona='unknown');await d.wait(lambda:not d.get()['busy'])
        assert d.get()['error'] and (root/'config.json').read_bytes()==original
        d.post('save',max_steps=0);await d.wait(lambda:not d.get()['busy'])
        assert d.get()['error'] and (root/'config.json').read_bytes()==original
        await d.action('save',max_steps=24)
        assert d.get()['model']['model']=='deepseek-flash'
        await d.action('start');assert d.get()['running']
        # A separate native session must not inherit Telos tools, prompt or business hooks.
        independent=await asyncio.to_thread(d.manage,'independent')
        assert 'error' not in independent,independent
        assert not independent['businessTool']
        unrelated=json.dumps(d.model.requests[-1],ensure_ascii=False)
        assert '个人 GTD 助手' not in unrelated and 'telos:business' not in unrelated and '你叫黎曼' not in unrelated
        assert yaml.safe_load(credentials.read_text()).get('refs')==yaml.safe_load(saved).get('refs')
        disabled=await asyncio.to_thread(d.manage,'disable');assert not disabled['result'].get('error'),disabled
        with pytest.raises(urllib.error.HTTPError) as missing:d.get()
        assert missing.value.code==404
        enabled=await asyncio.to_thread(d.manage,'enable');assert not enabled['result'].get('error'),enabled
        await d.wait(lambda:d.get()['running']);assert len(d.services.apple.writes)==1
        removed=await asyncio.to_thread(d.manage,'remove');assert not removed['result'].get('error'),removed
        assert all(b['name']!='telos-harness' for b in removed['bundles'])
        assert (root/'business.sqlite').exists() and d.proc.poll() is None
        assert yaml.safe_load(credentials.read_text()).get('refs')==yaml.safe_load(saved).get('refs')
    finally:await d.close()


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='须提供官方桌面应用和插件归档')
@run_async
async def test_desktop_bad_configuration_keeps_host_available(tmp_path):
    d=Desktop(tmp_path)
    try:
        root=d.home/'telos';root.mkdir();path=root/'config.json'
        original=json.dumps({'version':99,'wechat':{'enabled':True}});path.write_text(original)
        await d.start()
        assert not d.get()['running'] and '配置版本' in d.get()['error']
        d.post('save',max_steps=24)
        await d.wait(lambda:not d.get()['busy'])
        assert d.get()['error'] and path.read_text()==original and d.proc.poll() is None
        # Telos rejects generic settings instead of saving credentials or overriding DSH.
        path.unlink();await d.action('save',max_steps=24)
        before=path.read_bytes();d.post('save',max_steps=24,apiKey='must-not-save')
        await d.wait(lambda:not d.get()['busy'])
        assert 'DSH' in d.get()['error'] and path.read_bytes()==before
        assert 'must-not-save' not in (d.home/'.credentials.yaml').read_text()
        await d.action('start');assert d.get()['running']
    finally:await d.close()


def test_mock_disconnected_poll_keeps_messages_until_cursor_acknowledgment():
    service=Services()
    try:
        parsed=urllib.parse.urlparse(service.url)
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port)
        connection.request('POST','/getupdates',body=json.dumps({'get_updates_buf':''}),headers={'Content-Type':'application/json'})
        end=time.monotonic()+2
        while not service.polls and time.monotonic()<end:time.sleep(.005)
        connection.close();service.push('confirm','1')
        time.sleep(.08) # deterministically let the abandoned response finish first
        def poll(cursor):
            r=urllib.request.Request(service.url+'/getupdates',data=json.dumps({'get_updates_buf':cursor}).encode(),headers={'Content-Type':'application/json'})
            return json.loads(urllib.request.urlopen(r).read())
        a=poll('');assert [m['message_id'] for m in a['msgs']]==['confirm']
        assert poll('')['msgs']==a['msgs']
        assert poll(a['get_updates_buf'])['msgs']==[]
    finally:service.close()

@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='须提供官方桌面应用和插件归档')
@run_async
async def test_native_models_tool_switches_apply_and_restart(tmp_path):
    d=Desktop(tmp_path)
    def model_action(action,**data):
        req=urllib.request.Request(d.url+'/api/telos/models',data=json.dumps({'action':action,**data}).encode(),headers={'Content-Type':'application/json'})
        try:return json.loads(d.client.open(req,timeout=40).read())
        except urllib.error.HTTPError as error:raise AssertionError(error.read().decode()) from error
    try:
        d.model.script=['OK','工具已关闭。','继续聊天。']
        await d.start()
        catalog=d.get('models')
        assert catalog['customAvailable'],catalog
        official=next(p for p in catalog['providers'] if p['id']=='deepseek-official')
        assert official['credentialRef']=='DEEPSEEK_API_KEY' and official['models']
        assert 'desktop-model-secret' not in json.dumps(catalog)
        original_default=d.get()['defaultModel']
        added=model_action('add',config={'name':'测试 API','baseURL':d.model.url,'model':'test-model','api':'openai-completions'})
        assert added['provider'].startswith('telos-api-')
        d.env[added['credentialRef']]='native-test-api-key'
        d.stop();await d.start()
        selection={'provider':added['provider'],'model':added['model']}
        assert '连接成功' in model_action('test',selection=selection)['message']
        await d.action('save',model_selection=selection,tools={'apple':False,'maps':False})
        assert d.get()['apple']['status']=='disabled'
        root=d.home/'telos';config=read_config(root)
        config['apple_helper']='/nonexistent/forbidden-when-disabled'
        config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(root,config)
        await d.action('login');await d.action('start')
        d.services.push('disabled-tools','今天你好，请继续聊天。')
        await d.wait(lambda:len(d.services.sent)>0)
        names={t['function']['name'] if 'function' in t else t['name'] for t in d.model.requests[-1]['tools']}
        assert names=={'ask_user','skill','todo_write','user_memory'}
        assert all('baidu-ai-map' not in block for block in re.findall(r'<available_skills>(.*?)</available_skills>',json.dumps(d.model.requests[-1],ensure_ascii=False),re.S))
        assert d.model.requests[-1]['model']=='test-model' and d.get()['defaultModel']==original_default
        before=(root/'config.json').read_bytes()
        d.post('apply',model_selection={'provider':'missing-route','model':'unknown'})
        await d.wait(lambda:not d.get()['busy'])
        assert d.get()['error'] and d.get()['running'] and (root/'config.json').read_bytes()==before
        await d.action('apply',model_selection=selection,tools={'apple':False,'maps':True},persona='liman')
        assert d.get()['running'] and d.get()['config']['tools']['maps']
        d.stop();await d.start();await d.wait(lambda:d.get()['running'])
        d.services.push('restored-tools','继续，不要查询。')
        await d.wait(lambda:len(d.services.sent)>1)
        names={t['function']['name'] if 'function' in t else t['name'] for t in d.model.requests[-1]['tools']}
        assert 'baidu_maps' in names and 'personalPlan' not in names
        assert '你叫黎曼' in json.dumps(d.model.requests[-1],ensure_ascii=False)
        assert d.model.requests[-1]['model']=='test-model' and d.get()['defaultModel']==original_default
        assert 'native-test-api-key' not in (root/'config.json').read_text()
        assert not d.services.apple.writes
    finally:await d.close()


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='须提供官方桌面应用和插件归档')
@run_async
async def test_desktop_native_compaction_and_restart(tmp_path):
    from compression import zstd
    d=Desktop(tmp_path)
    def initial(request):
        d.model.input_tokens=800000
        return 'A'*600000
    def summarize(request):
        d.model.input_tokens=10
        return 'Retained fact: the user supplied a long test record.'
    d.model.script=[initial,summarize,'压缩后继续。','重启后继续。']
    try:
        await d.start()
        await d.action('save',tools={'apple':False,'maps':False})
        root=d.home/'telos';config=read_config(root)
        config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(root,config)
        await d.action('login');await d.action('start')
        d.services.push('large-input','B'*600000)
        await d.wait(lambda:bool(d.services.sent),timeout=40)
        d.services.push('compact-now','继续处理')
        await d.wait(lambda:any('压缩后继续。' in str(x) for x in d.services.sent),timeout=40)
        await d.action('stop')
        logs=list((d.home/'sessions').glob('*/wechat-*/session.v4.jsonl.zstd'))
        assert len(logs)==1
        events=[json.loads(line) for line in zstd.decompress(logs[0].read_bytes()).decode().splitlines()]
        assert sum(e['type']=='compaction/start' for e in events)==1
        assert sum(e['type']=='compaction/summary' for e in events)==1
        ends=[e for e in events if e['type']=='compaction/end']
        assert len(ends)==1 and not ends[0]['data'].get('error')
        d.stop();await d.start()
        await d.wait(lambda:d.get()['running'])
        d.services.push('after-restart','重启后继续')
        await d.wait(lambda:any('重启后继续。' in str(x) for x in d.services.sent),timeout=40)
        assert 'Retained fact' in json.dumps(d.model.requests[-1])
        assert not d.services.apple.writes
        # A native request failure must not become a synthetic WeChat utterance.
        sent=len(d.services.sent)
        d.model.script=[{'tool_calls':[{'id':'broken','type':'function',
            'function':{'name':'ask_user','arguments':'{"text":'}}]}]
        d.services.push('native-error','模拟无回答的错误')
        def consumed():
            import sqlite3
            with sqlite3.connect(root/'business.sqlite') as db:
                row=db.execute('SELECT reply,delivered FROM messages WHERE input=?',('模拟无回答的错误',)).fetchone()
            return row==('',1)
        await d.wait(consumed)
        assert len(d.services.sent)==sent
    finally:await d.close()
