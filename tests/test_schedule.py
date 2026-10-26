"""Native timers and persistent WeChat delivery, with local model/network fixtures."""
import asyncio
import json
import threading
import pytest
from support import Harness,call,draft
from test_history import results,tool_text
from test_desktop import Desktop,DESKTOP
from test_package import ARCHIVE,Installed,run_async
from settings import read_config,write_config
from telos.adapters.base import SendResult

NAMES={'schedule_create','schedule_list','schedule_update','schedule_delete'}

async def wait(predicate,timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():await asyncio.sleep(.05)

def create(cid,seconds=3):
    return call(cid,{'title':cid,'prompt':'请发送测试提醒 '+cid,'after_seconds':seconds},'schedule_create')

def value(request):return json.loads(tool_text(results(request)[-1]))

def assert_telos(request):
    names={t.get('name') or t['function']['name'] for t in request['tools']}
    assert NAMES|{'personalPlan','user_memory','session_event_read','web_fetch','bash','read','read_image'}<=names
    assert not names&{'write','subagent'}

def test_cli_timer_restart_overdue_and_output_replay(tmp_path):
    async def run():
        h=Harness(tmp_path,[create('warm'),'已设置。',call('query',{'action':'query','kind':'reminder'}),'定时提醒第一条\n定时提醒第二条'])
        try:
            await h.submit('三秒后查询提醒事项')
            await wait(lambda:len(h.sent)==3)
            assert h.sent==['已设置。','定时提醒第一条','定时提醒第二条']
            assert_telos(h.model.requests[-1])
            with h.runtime.business.db() as db:
                seq=db.execute('SELECT seq FROM schedule_outbox').fetchone()[0]
            await h.runtime.route('/scheduled-output',{'session':h.session,'seq':seq,'text':'错误重复'})
            await h.runtime.drain_scheduled()
            assert len(h.sent)==3
            h.model.script=[create('cold',4),'重启任务已设置。']
            await h.submit('四秒后再次提醒')
            await h.runtime.close()
            await asyncio.sleep(5)
            h.runtime=h.restart();h.model.script=['重启后的提醒。']
            await h.runtime.start()
            await wait(lambda:'重启后的提醒。' in h.sent)
            assert_telos(h.model.requests[-1])
            assert len(h.model.requests)==7
            await h.runtime.close();h.runtime=h.restart();await h.runtime.start()
            await asyncio.sleep(2.5)
            assert h.sent.count('重启后的提醒。')==1
            assert len(h.model.requests)==7
            with pytest.raises(ValueError):await h.runtime.route('/scheduled-output',{'session':'unknown','seq':1,'text':'no'})
        finally:await h.close()
    asyncio.run(run())

def test_native_schedule_crud_and_session_isolation(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('create',{'title':'daily','prompt':'喝水','daily':{'time':'15:00:00','time_zone':'Asia/Shanghai'}},'schedule_create'),'已设置。'])
        try:
            await h.submit('每天15点提醒喝水')
            record=value(h.model.requests[-1]);sid=record['id']
            assert record['kind']=='daily' and record['timeZone']=='Asia/Shanghai'
            h.session='other';h.model.script=[call('list',{},'schedule_list'),'没有任务。']
            await h.submit('列出任务');assert value(h.model.requests[-1])==[]
            h.model.script=[call('delete-other',{'id':sid},'schedule_delete'),'不能删除其他会话任务。']
            await h.submit('尝试删除');assert value(h.model.requests[-1])['deleted'] is False
            h.model.script=[call('update-other',{'id':sid,'prompt':'changed'},'schedule_update'),'不能修改。']
            await h.submit('尝试修改');assert value(h.model.requests[-1])['code']=='schedule_not_found'
            h.session='wechat-test';h.model.script=[call('update-own',{'id':sid,'prompt':'喝温水','every_seconds':60},'schedule_update'),'已更新。']
            await h.submit('改成每分钟');record=value(h.model.requests[-1])
            assert record['kind']=='every' and record['prompt']=='喝温水'
            h.model.script=[call('delete-own',{'id':sid},'schedule_delete'),call('list-own',{},'schedule_list'),'已删除。']
            await h.submit('取消');assert value(h.model.requests[-1])==[]
        finally:await h.close()
    asyncio.run(run())

def test_scheduled_output_retry_resumes_after_last_acknowledged_part(tmp_path):
    async def run():
        h=Harness(tmp_path,[])
        try:
            h.runtime.bind_ref(h.session,h.ref)
            attempts=[];fail=True
            async def send(ref,text,**kwargs):
                attempts.append(text)
                return SendResult(ok=not(fail and text=='第二条'))
            h.adapter.send_text=send
            payload={'session':h.session,'seq':42,'text':'第一条\n第二条'}
            await h.runtime.route('/scheduled-output',payload);await h.runtime.drain_scheduled()
            assert attempts==['第一条','第二条']
            await h.runtime.close();h.runtime=h.restart();fail=False
            await h.runtime.route('/scheduled-output',payload);await h.runtime.drain_scheduled()
            assert attempts==['第一条','第二条','第二条']
            await h.runtime.drain_scheduled();assert len(attempts)==3
        finally:await h.close()
    asyncio.run(run())

def test_scheduled_list_is_staged_and_delivered_as_one_bubble(tmp_path):
    async def run():
        h=Harness(tmp_path,[])
        listing='**答辩笔记**\n- 研究演讲\n- 金字塔原理'
        try:
            h.runtime.bind_ref(h.session,h.ref)
            await h.runtime.stage_scheduled({'session':h.session,'seq':17,'text':'查询完成。\n\n'+listing,'inputs':[]})
            with h.runtime.business.db() as db:
                parts=json.loads(db.execute('SELECT parts FROM schedule_outbox').fetchone()[0])
            assert parts==['查询完成。',listing]
            await h.runtime.drain_scheduled()
            assert h.sent==parts
        finally:await h.close()
    asyncio.run(run())


def test_completed_output_recovers_from_native_log_without_model_reexecution(tmp_path):
    async def run():
        h=Harness(tmp_path,[create('lost-output',1),'已设置。','待恢复的定时回复。'])
        route=h.runtime.route
        async def unavailable(path,data):
            if path=='/scheduled-output':raise RuntimeError('simulated local delivery interruption')
            return await route(path,data)
        h.runtime.route=unavailable
        try:
            await h.submit('一秒后提醒')
            await wait(lambda:len(h.model.requests)==3)
            await asyncio.sleep(.3)
            assert h.sent==['已设置。']
            await h.runtime.close();h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:'待恢复的定时回复。' in h.sent)
            assert len(h.model.requests)==3
            assert 'schedule recovery:' not in (tmp_path/'data/harness.log').read_text()
        finally:await h.close()
    asyncio.run(run())

def test_account_change_blocks_stored_schedule_recipient(tmp_path):
    async def run():
        from dataclasses import replace
        from sidecar import WeChatChannel
        from telos.adapters.wechat.adapter import WeChatAdapter
        from telos.adapters.wechat.config import WeChatConfig
        h=Harness(tmp_path,[])
        adapter=WeChatAdapter(config=WeChatConfig(account_id='old-bot',allowed_users=['test']))
        channel=WeChatChannel(tmp_path/'restricted',h.apple,adapter)
        try:
            channel.bind_ref('owned',replace(h.ref,tenant_id='old-bot'))
            assert channel.known_ref('owned').user_id=='test'
            adapter.config=replace(adapter.config,account_id='new-bot')
            with pytest.raises(ValueError):await channel.route('/memory-profile',{'session':'owned'})
            with pytest.raises(ValueError):await channel.route('/scheduled-output',{'session':'owned','seq':1,'text':'blocked'})
            assert await channel.route('/sessions',{})=={'sessions':[]}
        finally:await channel.close();await h.close()
    asyncio.run(run())

def test_scheduled_prompt_cannot_confirm_human_approval(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('draft',draft()),'请确认。'])
        try:
            await h.submit('创建会议');pending=h.runtime.business.pending(h.session)
            assert pending['delivered']
            await h.runtime.prepare(h.session,[{'id':'scheduled-confirm','scheduled':True,'text':'1','hasMedia':False}])
            assert not h.native.writes and h.runtime.business.pending(h.session)==pending
            await h.runtime.route('/scheduled-output',{'session':h.session,'seq':99,'text':'喝水时间到了。','inputs':['scheduled-confirm']})
            await h.runtime.drain_scheduled();assert h.sent[-1]=='喝水时间到了。'
        finally:await h.close()
    asyncio.run(run())

def test_scheduled_preview_requires_later_human_confirmation(tmp_path):
    async def run():
        h=Harness(tmp_path,[create('preview',1),'已安排。',call('scheduled-draft',draft()),'请确认会议。','已保存。'])
        try:
            await h.submit('一秒后准备会议预览')
            await wait(lambda:bool(h.runtime.business.pending(h.session)))
            await wait(lambda:h.runtime.business.pending(h.session).get('delivered'))
            assert not h.native.writes
            assert h.sent[-1]==h.runtime.business.pending(h.session)['text']
            await h.submit('1');assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())

def test_timer_queued_during_user_turn_has_separate_reply(tmp_path):
    async def run():
        entered=threading.Event();release=threading.Event()
        def slow(request):entered.set();assert release.wait(10);return '用户问题的回答。'
        h=Harness(tmp_path,[create('overlap',2),'已设置。',slow,'独立定时回复。'])
        try:
            await h.submit('两秒后提醒')
            task=asyncio.create_task(h.submit('同时问个问题'))
            assert await asyncio.to_thread(entered.wait,5)
            await asyncio.sleep(3);release.set();await task
            await wait(lambda:'独立定时回复。' in h.sent)
            assert h.sent.count('用户问题的回答。')==h.sent.count('独立定时回复。')==1
        finally:release.set();await h.close()
    asyncio.run(run())

@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires desktop and archive')
@run_async
async def test_desktop_schedule_stop_restart_and_host_isolation(tmp_path):
    d=Desktop(tmp_path)
    try:
        await d.start();await d.action('save')
        config=read_config(d.home/'telos');config['wechat'].update(base_url=d.services.url,cdn_base_url=d.services.url);write_config(d.home/'telos',config)
        await d.action('login');await d.action('start')
        d.model.script=[create('desktop',3),'已设置桌面任务。','桌面定时回复。']
        d.services.push('schedule','三秒后提醒')
        await d.wait(lambda:'桌面定时回复。' in json.dumps(d.services.sent,ensure_ascii=False))
        assert_telos(d.model.requests[-1])
        d.model.script=[create('restart',4),'已设置重启任务。']
        d.services.push('restart','四秒后提醒')
        await d.wait(lambda:'已设置重启任务。' in json.dumps(d.services.sent,ensure_ascii=False))
        await d.action('stop');count=len(d.model.requests)
        await asyncio.sleep(5);assert len(d.model.requests)==count
        d.stop();d.model.script=['桌面恢复后提醒。'];await d.start()
        await d.wait(lambda:'桌面恢复后提醒。' in json.dumps(d.services.sent,ensure_ascii=False))
        assert_telos(d.model.requests[-1])
        d.model.script=['宿主会话正常。'];independent=d.manage('independent')
        assert not independent['businessTool'] and independent['scheduleCount']==0
        assert not d.get()['error']
    finally:await d.close()


@pytest.mark.skipif(not ARCHIVE.exists(),reason='Requires built archive')
@run_async
async def test_installed_cli_schedule_and_restart(tmp_path):
    h=Installed(tmp_path,[create('installed',2),'已设置。','CLI 安装包定时回复。'])
    try:
        h.run('--telos-login');h.start();await h.wait(lambda:h.services.polls>0)
        h.services.push('installed','两秒后提醒')
        await h.wait(lambda:'CLI 安装包定时回复。' in json.dumps(h.services.sent,ensure_ascii=False))
        assert_telos(h.model.requests[-1])
        h.model.script=[create('installed-cold',4),'重启前已设置。']
        h.services.push('installed-cold','四秒后提醒')
        await h.wait(lambda:'重启前已设置。' in json.dumps(h.services.sent,ensure_ascii=False))
        h.stop();await asyncio.sleep(5)
        h.model.script=['CLI 安装包恢复后提醒。'];h.start()
        await h.wait(lambda:'CLI 安装包恢复后提醒。' in json.dumps(h.services.sent,ensure_ascii=False))
        assert_telos(h.model.requests[-1])
        assert len(h.model.requests)==6
    finally:await h.close()
