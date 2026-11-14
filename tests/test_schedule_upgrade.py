"""Real packaged hosts: target-based tools and persisted pre-upgrade reminders."""
import asyncio
import json
import os
from pathlib import Path
import plistlib
import shutil
import pytest
from settings import read_config,write_config
from support import call
from test_desktop import Desktop,DESKTOP
from test_package import ARCHIVE,run_async
from test_schedule import create,value
from test_history import results,tool_text

pytestmark=pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires desktop and archive')

async def start_channel(h):
    await h.start();await h.action('save',tools={'apple':False,'maps':False})
    config=read_config(h.home/'telos');config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url)
    write_config(h.home/'telos',config)
    await h.action('login');await h.action('start')

async def send(h,key,text):
    before=len(h.services.sent);h.services.push(key,text)
    await h.wait(lambda:len(h.services.sent)>before)

@run_async
async def test_native_target_tools_keep_wechat_destination_and_managed_checks(tmp_path):
    h=Desktop(tmp_path)
    try:
        await start_channel(h)
        h.model.script=['绑定。'];await send(h,'bind','绑定测试')
        if not any(t['name']=='schedule_edit' for t in h.model.requests[-1]['tools']):
            pytest.skip('Target-based tools are available in DSH 0.2.2')
        h.model.script=[create('owned',300),'已登记。'];await send(h,'create','登记当前聊天提醒')
        task=value(h.model.requests[-1]);sid=task['id']
        assert task['target']['kind']=='session'
        session=task['target']['sessionId']
        h.model.script=[call('list',{},'schedule_list'),call('view',{'id':sid},'schedule_view'),
            call('edit',{'id':sid,'action':'update','title':'修改后的提醒'},'schedule_edit'),
            call('redirect',{'id':sid,'action':'update','target':'new_once'},'schedule_edit'),'管理完成。']
        await send(h,'manage','查询并修改提醒')
        values=[json.loads(tool_text(r)) for r in results(h.model.requests[-1])[-4:]]
        assert values[0]['total']==1 and values[0]['items'][0]['id']==sid
        assert values[1]['target']['sessionId']==session
        assert values[2]['title']=='修改后的提醒'
        assert values[3]['code']=='invalid_rule' and 'CURRENT_SESSION_ONLY' in values[3]['message']
        def new_chat(request):
            result=create('forbidden',300)(request)
            args=json.loads(result['tool_calls'][0]['function']['arguments']);args['target']='new'
            result['tool_calls'][0]['function']['arguments']=json.dumps(args);return result
        h.model.script=[new_chat,call('delete',{'id':sid,'action':'delete'},'schedule_edit'),call('empty',{},'schedule_list'),'检查完成。']
        await send(h,'delete','删除当前提醒并检查隔离')
        values=[json.loads(tool_text(r))for r in results(h.model.requests[-1])[-3:]]
        assert values[0]['code']=='invalid_rule'
        assert values[1]['deleted'] is True and values[2]['total']==0
        # The separate proactive API is still the only way to edit its checks.
        from proactive import ProactiveStore,defaults
        from test_proactive import next_check
        config=read_config(h.home/'telos');p=defaults();p.update(enabled=True,session=session);p['heartbeat']['enabled']=False
        await h.action('apply',proactive=p)
        h.model.script=[lambda _:next_check('managed',300),'主动检查已登记。'];await send(h,'managed','登记主动检查')
        store=ProactiveStore(h.home/'telos');event=store.status()['pending'][0]
        row=store.event(event['id'],session)
        h.model.script=[call('protected',{'id':row['task_id'],'action':'delete'},'schedule_edit'),'不能直接删除。']
        await send(h,'protected','尝试直接删除托管任务')
        assert value(h.model.requests[-1])['code']=='internal_error'
        assert store.event(event['id'],session)['state']=='planned'
        assert not h.services.apple.writes
    finally:await h.close()

@pytest.mark.skipif(not os.environ.get('TELOS_TEST_UPGRADE_APP'),reason='Requires upgrade target')
@run_async
async def test_pending_native_reminder_survives_host_upgrade_without_duplicate(tmp_path):
    h=Desktop(tmp_path)
    try:
        await start_channel(h)
        h.model.script=[create('before-upgrade',5),'旧版已登记。'];await send(h,'schedule','登记升级前提醒')
        old_task=value(h.model.requests[-1]);assert old_task['kind']=='after'
        h.stop();await asyncio.sleep(6)
        newer=Path(os.environ['TELOS_TEST_UPGRADE_APP']);h.res=newer/'Contents/Resources'
        h.runtime=h.res/'app.asar/dsh';h.exe=newer/'Contents/MacOS/DeepSeek Harness'
        with (newer/'Contents/Info.plist').open('rb') as f:h.env['DSH_CLIENT_VERSION']=plistlib.load(f)['CFBundleShortVersionString']
        h.model.script=['新版接续了旧任务。'];await h.start()
        await h.wait(lambda:'新版接续了旧任务。' in json.dumps(h.services.sent,ensure_ascii=False))
        assert any(t['name']=='schedule_edit'for t in h.model.requests[-1]['tools'])
        h.stop();await h.start();h.model.script=['升级后可继续对话。'];await send(h,'after','继续')
        assert json.dumps(h.services.sent,ensure_ascii=False).count('新版接续了旧任务。')==1
        assert h.get()['running'] and not h.get()['error']
    finally:await h.close()

@pytest.mark.skipif(not os.environ.get('TELOS_TEST_HABIT_PACKAGE'),reason='Requires optional installed habit package')
@run_async
async def test_installed_habit_tools_coexist_with_new_host_and_current_state(tmp_path):
    h=Desktop(tmp_path)
    try:
        await start_channel(h)
        h.model.script=['已绑定。'];await send(h,'bind-habit','绑定隔离测试')
        session=h.get()['proactive']['sessions'][0]['session'];h.stop()
        package=h.profile/'node_modules/dsh-habit-coach'
        shutil.copytree(os.environ['TELOS_TEST_HABIT_PACKAGE'],package,symlinks=True)
        patch=h.profile/'cordis.patch.yml';rows=json.loads(patch.read_text())
        rows.append({'insert':[{'id':'isolated-habit','name':(package/'plugin/index.mjs').as_uri(),
            'config':{'dataDir':str(tmp_path/'isolated-habit-data'),'owner_key':'telos:'+'a'*64,
                      'native_context':{'session_ids':[session]}}}]})
        patch.write_text(json.dumps(rows))
        h.model.script=[call('diagnostics',{},'habit_diagnostics'),call('context',{},'habit_context'),'习惯工具正常。']
        await h.start();await send(h,'habit','读取隔离习惯上下文')
        values=[json.loads(tool_text(r))for r in results(h.model.requests[-1])[-2:]]
        assert values[0]['enabled'] and values[0]['owner_authorized']
        assert all(not r.get('is_error')for r in results(h.model.requests[-1])[-2:])
        from test_current_state import states
        state=states(h.model.requests[-1]);assert len(state)==1
        assert 'habit-coach:current' in state[0]['current']
        assert not h.services.apple.writes
    finally:await h.close()
