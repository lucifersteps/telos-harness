"""Real Desktop preset/loaders with mock WeChat/model/Apple; restart projection."""
import json
import sqlite3
import sys
import pytest
from test_desktop import Desktop,DESKTOP
from test_package import ARCHIVE,run_async
from settings import read_config,write_config
from support import call

@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires official Desktop and package')
@run_async
async def test_desktop_retention_and_single_profile_loader(tmp_path):
    h=Desktop(tmp_path)
    try:
        await h.start();await h.action('save',direct_write=True)
        helper=tmp_path/'fake-apple'
        helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\nr=urllib.request.urlopen(urllib.request.Request({h.services.url+"/apple"!r},data=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode(),headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n');helper.chmod(0o700)
        config=read_config(h.home/'telos');config['apple_helper']=str(helper)
        config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(h.home/'telos',config)
        await h.action('login');await h.action('start')
        async def send(key,text):
            h.services.push(key,text)
            def delivered():
                with sqlite3.connect(h.home/'telos/business.sqlite') as db:
                    return db.execute('SELECT delivered FROM messages WHERE input=?',(text,)).fetchone()==(1,)
            await h.wait(delivered)
        h.services.apple.items[0]['title']='QUERY_SENTINEL_UNIQUE_4291'
        h.model.script=[call('query',{'action':'query','kind':'reminder'}),call('memory',{'action':'write','expected_content':'','content':'PROFILE_SENTINEL_7391'},'user_memory'),'第一条正常回复。']
        await send('first','请查询提醒并记住偏好')
        assert 'QUERY_SENTINEL_UNIQUE_4291' in json.dumps(h.model.requests[-1])
        h.model.script=[call('memory-read',{'action':'read'},'user_memory'),'第二条正常回复。']
        start=len(h.model.requests)
        await send('second','继续聊聊天')
        for request in h.model.requests[start:]:
            wire=json.dumps(request,ensure_ascii=False)
            assert 'QUERY_SENTINEL_UNIQUE_4291' not in wire
            assert '请查询提醒并记住偏好' in wire and '第一条正常回复。' in wire
        # Repeated profile paragraphs must not alternate between AGENTS.md and user.md.
        wire=json.dumps(h.model.requests[start],ensure_ascii=False)
        assert wire.count('PROFILE_SENTINEL_7391')==1,wire[-5000:]
        h.stop();await h.start();await h.wait(lambda:h.get()['running'])
        h.model.script=['重启后正常回复。'];await send('third','重启后继续')
        wire=json.dumps(h.model.requests[-1],ensure_ascii=False)
        assert 'QUERY_SENTINEL_UNIQUE_4291' not in wire
        assert wire.count('PROFILE_SENTINEL_7391')==1
        assert '继续聊聊天' in wire and '第二条正常回复。' in wire
        assert len(h.services.sent)==3
        assert 'history retention skipped' not in h.safe_log()
        # Prompt below the host's 80% window still receives early native compaction.
        def response(request):
            return 'Earlier conversation summarized; no pending actions.' if request.get('max_tokens')==8192 else '正常回复。'
        start=len(h.model.requests)
        h.model.script=[response]*20
        for i in range(5):await send('large-'+str(i),str(i)+'B'*150000)
        await send('after-large','大段聊天后继续')
        summaries=[r for r in h.model.requests[start:] if r.get('max_tokens')==8192]
        assert summaries, 'The early native compaction did not run: '+h.safe_log()
        assert len(json.dumps(h.model.requests[-1],ensure_ascii=False))<550000
        assert 'early compaction deferred' not in h.safe_log()
    finally:await h.close()


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires official Desktop and package')
@run_async
async def test_partial_read_and_corrected_notify_retire_after_delivery_and_restart(tmp_path):
    from proactive import ProactiveStore,defaults
    from support import activity_read
    from test_proactive import next_check
    from test_proactive_hosts import isolate_activity
    from test_history import results,tool_text
    h=Desktop(tmp_path)
    try:
        await h.start();await h.action('save',direct_write=True)
        helper=tmp_path/'fake-apple'
        helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\nr=urllib.request.urlopen(urllib.request.Request({h.services.url+"/apple"!r},data=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode(),headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n');helper.chmod(0o700)
        root=h.home/'telos';config=read_config(root);config['apple_helper']=str(helper)
        config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(root,config)
        isolate_activity(root,h.services)
        await h.action('login');await h.action('start')
        h.model.script=['绑定成功。'];h.services.push('bind-retention','绑定清理测试')
        sent=lambda:json.dumps(h.services.sent,ensure_ascii=False)
        await h.wait(lambda:'绑定成功。' in sent())
        store=ProactiveStore(root);session=store.status()['sessions'][0]['session']
        p=defaults();p.update(enabled=True,session=session);p['heartbeat']['enabled']=False
        await h.action('apply',proactive=p)
        h.services.apple.items[0]['title']='SETTLED_QUERY_SHOULD_RETIRE_5347'
        start=len(h.model.requests)
        h.model.script=[lambda _:next_check('retention-check',2),'已登记。',
            call('notify-too-early',{'message':'不得发出这条。'},'notify_user'),
            activity_read('partial-read'),call('notify-after-read',{'message':'数据不完整，提醒仍已正常发送。'},'notify_user'),'内部完成。']
        h.services.push('start-retention','稍后检查')
        await h.wait(lambda:'数据不完整，提醒仍已正常发送。' in sent())
        await h.wait(lambda:len(h.model.requests)>=start+6)
        prior=[r for request in h.model.requests[start:] for r in results(request)]
        assert any(r.get('tool_use_id')=='notify-too-early' and r.get('is_error') for r in prior)
        assert any(r.get('tool_use_id')=='partial-read' and '"status":"partial"' in tool_text(r).replace(' ','') for r in prior)
        assert '不得发出这条。' not in sent()
        # An ordinary delivered chat wakes retention after the scheduled turn settles.
        h.model.script=['聊天仍正常。'];h.services.push('after-retention','继续聊天')
        await h.wait(lambda:'聊天仍正常。' in sent())
        wire=json.dumps(h.model.requests[-1],ensure_ascii=False)
        assert 'SETTLED_QUERY_SHOULD_RETIRE_5347' not in wire
        retired={'notify-too-early','partial-read','partial-read-reminders','partial-read-calendar','notify-after-read'}
        assert not any(r.get('tool_use_id') in retired for r in results(h.model.requests[-1]))
        assert '数据不完整，提醒仍已正常发送。' in wire  # Bounded delivered-check summary.
        h.stop();await h.start();await h.wait(lambda:h.get()['running'])
        h.model.script=['重启后聊天正常。'];h.services.push('restart-retention','重启后继续')
        await h.wait(lambda:'重启后聊天正常。' in sent())
        assert 'SETTLED_QUERY_SHOULD_RETIRE_5347' not in json.dumps(h.model.requests[-1],ensure_ascii=False)
        assert sent().count('数据不完整，提醒仍已正常发送。')==1
        assert not h.services.apple.writes
        assert 'history retention skipped' not in h.safe_log()
    finally:await h.close()
