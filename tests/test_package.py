"""从真实 tgz 安装，使用真实 Harness/本机组件/微信协议，外部服务全为模拟。"""
import asyncio
import functools
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time

import pytest
from deepseek_harness_runtime import resolve_bundled_launch_args
from support import Apple,Model,call,draft
from runtime import ROOT,launch_args
from settings import read_config,write_config

ARCHIVE=ROOT/'dist/telos-harness-0.4.6-macos-arm64.tgz'
pytestmark=pytest.mark.skipif(not ARCHIVE.exists(),reason='先构建插件归档')

def run_async(fn):
    @functools.wraps(fn)
    def run(*args,**kwargs):return asyncio.run(fn(*args,**kwargs))
    return run

class Services:
    def __init__(self):
        self.assets={};self.downloads=[];self.apple=Apple();self.messages=[];self.sent=[];self.polls=0;self.logins=0;self.authorizations=0
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def result(self,value):
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                try:self.wfile.write(json.dumps(value).encode())
                except BrokenPipeError:pass
            def do_GET(self):
                if self.path.startswith('/download?'):
                    from urllib.parse import parse_qs,urlparse
                    key=parse_qs(urlparse(self.path).query)['encrypted_query_param'][0];owner.downloads.append(key)
                    value=owner.assets.get(key,b'invalid');self.send_response(200);self.end_headers();self.wfile.write(value);return
                if 'get_bot_qrcode' in self.path:self.result({'qrcode':'isolated-qr','qrcode_img_content':'https://example.invalid/test'})
                elif getattr(owner,'login_wait',False):self.result({'status':'wait'})
                else:
                    owner.logins+=1;self.result({'status':'confirmed','ilink_bot_id':'isolated-bot','bot_token':'isolated-token','ilink_user_id':'test','baseurl':owner.url})
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path=='/apple':
                    action=data['action']
                    if action in {'status','authorize'}:
                        owner.authorizations+=action=='authorize';self.result({'events':'authorized','reminders':'authorized'})
                    else:self.result(owner.apple.request(action,data['args']))
                elif self.path.endswith('getupdates'):
                    owner.polls+=1;time.sleep(.05)
                    # Replay until the client supplies the returned cursor. A disconnected
                    # long poll must not consume messages intended for the restarted client.
                    cursor=int(data.get('get_updates_buf') or 0);messages=owner.messages[cursor:]
                    self.result({'ret':0,'msgs':messages,'get_updates_buf':str(cursor+len(messages))})
                elif self.path.endswith('sendmessage'):
                    owner.sent.append(data);self.result({'ret':0,'message_id':str(len(owner.sent))})
                else:self.result({'ret':0})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def push(self,id,text,user='test'):
        self.messages.append({'message_id':id,'from_user_id':user,'to_user_id':'isolated-bot','message_type':1,
            'context_token':'mock-context','item_list':[{'type':1,'text_item':{'text':text}}]})
    def close(self):self.server.shutdown();self.server.server_close()

class Installed:
    def __init__(self,path,script,connection=None):
        self.path=path;self.home=path/'Harness home';self.home.mkdir(parents=True)
        self.data=self.home/'telos';self.model=Model(script) if connection is None else None;self.services=Services();self.proc=None;self.logs=[]
        connection=connection or {'api_key':'package-secret-test','base_url':self.model.url,'model':'deepseek-flash'}
        self.env={k:v for k,v in os.environ.items() if not k.startswith(('TELOS_','DEEPSEEK_','BAIDU_','DSH_','PYTHON'))}
        self.env.update(DSH_AGENTS_HOME=str(self.home/'agents'),DSH_HOME=str(self.home))
        self.command=launch_args();self.launch_extra=[]
        profile=self.home/'profiles/telos';profile.mkdir(parents=True)
        (profile/'package.json').write_text(json.dumps({'name':'dsh-profile-telos','private':True,'dsh':{'profile':{'bundles':['@deepseek-ai/dsh-base']}}}))
        for name in ['cordis.yml','cordis.patch.yml']:(profile/name).write_text('[]\n')
        # Host configuration is test-owned. Telos never configures model credentials.
        self.env.update(DEEPSEEK_API_KEY=connection['api_key'],BAIDU_MAP_AUTH_TOKEN='map-secret-test')
        # A copied archive can be removed after installation; no source/symlink dependency.
        self.archive=path/'installer.tgz';shutil.copy2(ARCHIVE,self.archive)
        self.install(self.archive)
        self.archive.unlink()
        (profile/'cordis.patch.yml').write_text(json.dumps([
            {'id':'llm-deepseek','config':{'baseURL':connection['base_url'],'thinking':'disabled'}},
            {'id':'agent-default-model','config':{'provider':'deepseek-official','model':connection['model'],'reasoningEffort':'off'}},
            {'id':'session-title-llm','disabled':True},
        ]))
        self.run('--telos-setup')
        helper=path/'fake apple'
        helper.write_text(f'#!{sys.executable}\nimport json,sys,urllib.request\nb=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode()\nr=urllib.request.urlopen(urllib.request.Request({self.services.url+"/apple"!r},data=b,headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n')
        helper.chmod(0o700)
        config=read_config(self.data);config['apple_helper']=str(helper);config['wechat']['base_url']=self.services.url;config['wechat']['cdn_base_url']=self.services.url;write_config(self.data,config)
    def install(self,archive):
        result=subprocess.run([*self.command,'plugin','--profile','telos','add','telos-harness@file:'+str(archive),'--offline'],env=self.env,cwd=self.path,capture_output=True,text=True,timeout=90)
        assert result.returncode==0,result.stdout+result.stderr
    def run(self,arg,extra=None,ok=True):
        result=subprocess.run([*self.command,'--profile','telos',*self.launch_extra,*(arg if isinstance(arg,list) else [arg] if arg else [])],env={**self.env,**(extra or {})},cwd=self.path,
                              stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=35)
        self.logs.append(result.stdout+result.stderr)
        if ok:assert result.returncode==0,result.stdout+result.stderr
        return result
    def start(self):
        self.log=(self.path/f'run-{time.time_ns()}.log').open('w+');self.logs.append(self.log.name)
        self.proc=subprocess.Popen([*self.command,'--profile','telos',*self.launch_extra],env=self.env,cwd=self.path,
            stdin=subprocess.DEVNULL,stdout=self.log,stderr=self.log)
    async def wait(self,predicate,timeout=20):
        deadline=time.monotonic()+timeout
        while not predicate():
            if self.proc and self.proc.poll() is not None:
                self.log.flush();raise AssertionError(Path(self.log.name).read_text())
            if time.monotonic()>deadline:
                raise AssertionError('wait timeout\n'+(Path(self.log.name).read_text() if self.proc else ''))
            await asyncio.sleep(.05)
    def stop(self,kill=False):
        if self.proc:
            self.proc.kill() if kill else self.proc.terminate()
            self.proc.wait(timeout=20);self.log.close();self.proc=None
    async def close(self):
        self.stop()
        if self.model:await asyncio.to_thread(self.model.close)
        else:(self.home/'.credentials.yaml').unlink(missing_ok=True)
        await asyncio.to_thread(self.services.close)

@run_async
async def test_installed_first_setup_restart_upgrade_and_no_duplicate(tmp_path):
    h=Installed(tmp_path,[call('skill-read',{'name':'baidu-ai-map'},'skill'),call('draft-1',draft()),'请确认。','已保存。','没有待确认操作。','会话继续。'])
    try:
        assert not h.services.polls
        h.run('--telos-login');h.run('--telos-authorize')
        assert h.services.logins==1 and h.services.authorizations==1
        status=h.run('--telos-status').stdout
        assert '"model":"deepseek-flash"' in status and '"credentials"' not in status
        assert 'package-secret-test' not in ''.join(h.logs)
        assert (h.data/'config.json').stat().st_mode&0o777==0o600
        credentials=h.home/'.credentials.yaml';assert not credentials.exists()
        original_config=(h.data/'config.json').read_bytes()
        h.start();await h.wait(lambda:h.services.polls>0)
        h.services.push('unauthorized','恶意请求',user='other');h.services.push('request','添加测试会议')
        await h.wait(lambda:len(h.services.sent)>=1)
        assert not h.services.apple.writes
        assert {t['name'] for t in h.model.requests[0]['tools']}=={'personalPlan','ask_user','baidu_maps','todo_write','skill','user_memory'}
        assert '<skill_content name=' in json.dumps(h.model.requests[1],ensure_ascii=False)
        h.stop()
        # The business preview and official history survive a real process restart.
        assert list((h.home/'sessions').glob('*/*/session.v*.jsonl*'))
        h.start();before=len(h.services.sent);h.services.push('confirm','1')
        await h.wait(lambda:len(h.services.sent)>before)
        assert len(h.services.apple.writes)==1
        h.stop()
        # Same package content at the next version exercises the native package upgrade boundary.
        upgrade=tmp_path/'upgrade';upgrade.mkdir()
        with tarfile.open(ARCHIVE) as tar:tar.extractall(upgrade,filter='data')
        meta=upgrade/'package/package.json';value=json.loads(meta.read_text());value['version']='0.4.3';meta.write_text(json.dumps(value))
        manifest_path=upgrade/'package/component-manifest.json';manifest=json.loads(manifest_path.read_text())
        manifest['version']='0.4.3';manifest['files']['package.json']=hashlib.sha256(meta.read_bytes()).hexdigest();manifest_path.write_text(json.dumps(manifest))
        tgz=tmp_path/'upgrade.tgz'
        with tarfile.open(tgz,'w:gz') as tar:tar.add(upgrade/'package',arcname='package')
        h.install(tgz);shutil.rmtree(upgrade);tgz.unlink()
        assert (h.data/'config.json').read_bytes()==original_config and not credentials.exists()
        h.start();before=len(h.services.sent)
        h.services.push('confirm','1');h.services.push('confirm-again','1')
        await h.wait(lambda:len(h.services.sent)>before)
        assert len(h.services.apple.writes)==1
        # A second instance cannot replace or terminate the running one.
        assert h.run('--telos-status').returncode==0
        other=h.run(None,ok=False)
        assert other.returncode!=0 and '已有运行实例' in other.stderr
        assert h.proc.poll() is None
        pid=int((h.data/'instance.pid').read_text()) if (h.data/'instance.pid').exists() else None
        h.stop(kill=True)
        if pid:
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                try:os.kill(pid,0)
                except ProcessLookupError:break
                await asyncio.sleep(.1)
            else:pytest.fail('Harness 退出后本机组件仍然存活')
        before=h.services.polls;h.start();await h.wait(lambda:h.services.polls>before)
    finally:await h.close()

@run_async
async def test_installed_unknown_config_does_not_change_data(tmp_path):
    h=Installed(tmp_path,[])
    try:
        config=read_config(h.data);config['version']=999;write_config(h.data,config)
        before=(h.data/'config.json').read_bytes()
        result=h.run('--telos-status',ok=False)
        assert result.returncode!=0 and '不支持此配置版本' in result.stderr
        assert (h.data/'config.json').read_bytes()==before and not h.services.polls
    finally:await h.close()

@run_async
async def test_installed_correction_batch_pending_upgrade_then_confirm(tmp_path):
    script=[call('bad',{'action':'batch','operations':[]}),
        call('batch',{'action':'batch','operations':[draft(str(i)) for i in range(4)]}),'四项已预览。','已保存。','无待确认操作。']
    h=Installed(tmp_path,script)
    try:
        h.run('--telos-login');h.start();h.services.push('batch','添加四个测试日程')
        await h.wait(lambda:len(h.services.sent)>0)
        assert not h.services.apple.writes and len(h.model.requests)==3
        h.stop()
        # Reinstalling the published artifact is idempotent and keeps a pending preview.
        h.install(ARCHIVE)
        h.start();n=len(h.services.sent);h.services.push('approve','1')
        await h.wait(lambda:len(h.services.sent)>n)
        assert len(h.services.apple.writes)==4
        n=len(h.services.sent);h.services.push('approve-again','1')
        await h.wait(lambda:len(h.services.sent)>n)
        assert len(h.services.apple.writes)==4
    finally:await h.close()

@run_async
async def test_installed_steer_while_model_running(tmp_path):
    entered=threading.Event();release=threading.Event()
    def slow(request):
        entered.set();assert release.wait(15)
        return call('preview',draft())
    h=Installed(tmp_path,[slow,call('revise',{'action':'revise','draft_call_id':'preview',
        'request_mode':'continue','fields':{'title':'补充后的测试会议'}}),'请确认补充后的会议。','已保存。'])
    try:
        h.run('--telos-login');h.start();h.services.push('first','添加测试会议')
        await h.wait(entered.is_set)
        h.services.push('second','将标题改成补充后的测试会议')
        # Wait for durable acceptance, not an arbitrary scheduling delay.
        def accepted():
            with sqlite3.connect(h.data/'business.sqlite') as db:
                return db.execute('SELECT count(*) FROM messages').fetchone()[0]==2
        await h.wait(accepted);release.set()
        await h.wait(lambda:len(h.services.sent)>0)
        assert not h.services.apple.writes
        n=len(h.services.sent);h.services.push('confirm','1')
        await h.wait(lambda:len(h.services.sent)>n)
        assert len(h.services.apple.writes)==1 and h.services.apple.writes[0][1]['fields']['title']=='补充后的测试会议'
        assert '将标题改成补充后的测试会议' in json.dumps(h.model.requests,ensure_ascii=False)
    finally:release.set();await h.close()


def test_package_manifest_and_self_contained_components():
    with tarfile.open(ARCHIVE) as tar:
        manifest=json.load(tar.extractfile('package/component-manifest.json'))
        assert tar.extractfile('package/cordis.patch.yml').read().decode()=='- insert:\n    - id: telos-bootstrap\n      name: telos-harness\n'
        assert 'bin/telos-component' in manifest['files']
        persona=tar.extractfile('package/personas/liman.md').read().decode()
        assert '你叫黎曼' in persona and '渊栎' not in persona
        assert 'native/Telos Harness Apple.app/Contents/MacOS/telos-apple' in manifest['files']
        for name,digest in manifest['files'].items():
            assert hashlib.sha256(tar.extractfile('package/'+name).read()).hexdigest()==digest
        for item in tar.getmembers():
            assert '/.personal/' not in item.name and '/reports/' not in item.name and '/tests/' not in item.name
            if item.name.endswith(('.mjs','.json','.yml','.md')):
                text=tar.extractfile(item).read().decode()
                assert '/Users/liangyuanli/' not in text
        sidecar=tar.extractfile('package/bin/telos-component').read()
        assert sidecar[:4] in [b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf']


@run_async
async def test_host_model_ignores_legacy_telos_override_and_setup_preserves_host(tmp_path):
    h=Installed(tmp_path,['收到。'])
    try:
        config=read_config(h.data);config.update(model='obsolete-model',base_url='https://invalid.example',max_steps=24)
        write_config(h.data,config)
        profile=h.home/'profiles/telos/cordis.patch.yml';before=profile.read_bytes()
        credentials=h.home/'.credentials.yaml'
        credentials.write_text(json.dumps({'version':1,'refs':{'UNRELATED_KEY':'preserve-me'}}));credentials.chmod(0o600)
        saved=credentials.read_bytes()
        h.run('--telos-setup',extra={'DEEPSEEK_API_KEY':'must-not-store','BAIDU_MAP_AUTH_TOKEN':'must-not-store'})
        assert credentials.read_bytes()==saved and profile.read_bytes()==before
        assert read_config(h.data)==config
        h.run('--telos-login');h.start();h.services.push('plain','你好')
        await h.wait(lambda:len(h.services.sent)>0)
        assert h.model.requests[0]['model']=='deepseek-flash'
        assert credentials.read_bytes()==saved and profile.read_bytes()==before
    finally:await h.close()


@run_async
async def test_missing_host_services_fail_clearly_without_installing_fallback(tmp_path):
    h=Installed(tmp_path,[])
    try:
        profile=h.home/'profiles/telos/cordis.patch.yml'
        entries=json.loads(profile.read_text());entries.append({'id':'credentials','disabled':True});profile.write_text(json.dumps(entries))
        before=profile.read_bytes()
        result=h.run(None,ok=False)
        assert result.returncode!=0 and 'credentials' in result.stderr and '不补装' in result.stderr
        assert profile.read_bytes()==before and not h.services.polls
    finally:await h.close()


@run_async
async def test_existing_host_skill_is_loaded_by_native_dsh(tmp_path):
    h=Installed(tmp_path,[call('host-skill',{'name':'host-note'},'skill'),call('read',{'action':'query','kind':'reminder'}),'提醒事项有买菜。'])
    try:
        folder=h.home/'skills/host-note';folder.mkdir(parents=True)
        content='---\nname: host-note\ndescription: 用于虚构回归的宿主技能。\n---\nHOST_SKILL_REGRESSION_MARKER：查询结果必须向用户报告。\n'
        (folder/'SKILL.md').write_text(content)
        h.run('--telos-login');h.start();h.services.push('host-skill','使用宿主技能查询提醒事项')
        await h.wait(lambda:len(h.services.sent)>0)
        assert 'HOST_SKILL_REGRESSION_MARKER' in json.dumps(h.model.requests[1],ensure_ascii=False)
        assert not h.services.apple.writes and (folder/'SKILL.md').read_text()==content
    finally:await h.close()
