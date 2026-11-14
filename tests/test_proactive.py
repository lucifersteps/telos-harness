"""Proactive scheduling policy plus real DSH execution using only fake external services."""
import asyncio
from datetime import datetime, timezone
import json
import time
import pytest

from proactive import ProactiveStore, defaults, validate
from settings import read_config, write_config
from support import Harness, activity_read, call, draft
from test_schedule import wait, value
from test_history import results, tool_text


def stamp(text):
    return datetime.fromisoformat(text).timestamp()


def configured(root, *, now=None, heartbeat=True, fixed=True):
    config=read_config(root); proactive=defaults()
    proactive.update(enabled=True,session='wechat-test')
    proactive['heartbeat']['enabled']=heartbeat
    for item in proactive['fixed']:item['enabled']=fixed
    config['proactive']=proactive;write_config(root,config)
    return ProactiveStore(root, now=now or time.time)


def test_default_and_validation():
    assert defaults()['heartbeat']['interval_minutes']==30
    assert not defaults()['enabled']
    assert [r['time'] for r in defaults()['fixed']]==['00:00','08:00','12:00','18:00']
    for value in [{'enabled':True},{'heartbeat':{'interval_minutes':0}}, {'heartbeat':{'interval_minutes':True}},
                  {'timezone':'not/a-zone'}, {'heartbeat':{'start':'25:00'}}, {'fixed':[{}]}]:
        with pytest.raises(ValueError):validate(value)


def test_required_heartbeat_instructions_reach_model_across_tools_and_restart(tmp_path):
    from test_current_state import states
    async def run():
        h=Harness(tmp_path,['旧的检查曾选择不打扰。'])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        prompt='本轮必须读取最新目标，并通过 notify_user 发送简短检查结果。'
        try:
            await h.submit('绑定会话');await h.runtime.close()
            config=read_config(tmp_path/'data');config['proactive']['heartbeat']['prompt']=prompt
            write_config(tmp_path/'data',config);_,revision,_=store.sync()
            with store.db() as db:
                event=store._insert(db,h.session,revision,'heartbeat',time.time()+1,prompt)
            h.model.script=[call('read-goal',{'action':'query','kind':'reminder'}),
                activity_read(),
                call('required-notify',{'message':'已核对最新事项，本次没有新增进展证据。'},'notify_user'),'内部完成。']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:store.event(event,h.session)['state']=='sent')
            await wait(lambda:len(h.model.requests)==5)
            for request in h.model.requests[1:]:
                current=states(request)
                assert len(current)==1
                active=current[0]['current']['telos:proactive-context']['proactive']['events'][0]
                assert active['notification_required'] is True and active['prompt']==prompt
                wire=json.dumps(request,ensure_ascii=False)
                assert 'notification_required=true' in wire
                assert 'Heartbeat/followup checks may remain silent' not in wire
                assert '消息有价值时调用 notify_user' not in wire
                assert '[TELOS CURRENT CHECK]' in wire
                assert '不能用“先不打扰”、普通最终文本或 schedule_next_check 代替本次 notify_user' in wire
            assert h.sent==['旧的检查曾选择不打扰。','已核对最新事项，本次没有新增进展证据。']
            assert not h.native.writes
            h.model.script=['普通聊天正常回复。'];await h.submit('你好')
            context=states(h.model.requests[-1])[0]['current']['telos:proactive-context']
            assert 'proactive_settings' in context and 'proactive' not in context
            assert h.sent[-1]=='普通聊天正常回复。'
        finally:await h.close()
    asyncio.run(run())


def test_mentor_context_survives_quiet_checks_and_restart(tmp_path):
    now=stamp('2026-11-05T00:00:05+08:00')
    store=configured(tmp_path,now=lambda:now)
    _,revision,_=store.sync()
    with store.db() as db:
        sent=store._insert(db,'wechat-test',revision,'fixed',now-36000,'已经问过',state='sent')
        db.execute('UPDATE proactive_events SET message=? WHERE id=?',('上午盘点得怎样？',sent))
        for n in range(15):store._insert(db,'wechat-test',revision,'heartbeat',now-1800*(n+1),'检查',state='quiet')
        # Another chat's notification must not enter this chat's context.
        other=store._insert(db,'other',revision,'fixed',now-1,'secret',state='sent')
        db.execute('UPDATE proactive_events SET message=? WHERE id=?',('secret',other))
        for kind,offset in [('fixed',0),('heartbeat',1),('followup',2)]:
            key=store._insert(db,'wechat-test',revision,kind,now-5+offset,'检查',state='running')
            db.execute('UPDATE proactive_events SET input_id=? WHERE id=?',(kind,key))
    store.plan()
    restored=ProactiveStore(tmp_path,now=lambda:now)
    result=restored.context('wechat-test',['fixed','heartbeat','followup'])
    assert all(r.get('message')!='上午盘点得怎样？' for r in result['recent'])
    assert [r['message'] for r in result['recent_notifications']]==['上午盘点得怎样？']
    assert {r['kind']:r['habit_reminder_allowed'] for r in result['events']}=={'fixed':True,'heartbeat':False,'followup':False}
    assert result['pending_checks'] and all(r['kind'] in ('fixed','followup') for r in result['pending_checks'])
    assert 'secret' not in json.dumps(result)


def test_midnight_permission_uses_configured_timezone(tmp_path):
    now=stamp('2026-11-05T00:00:00+08:00')
    store=configured(tmp_path,now=lambda:now)
    config=read_config(tmp_path);config['proactive']['timezone']='UTC';write_config(tmp_path,config)
    _,revision,_=store.sync()
    with store.db() as db:
        key=store._insert(db,'wechat-test',revision,'fixed',now,'检查',state='running')
        db.execute('UPDATE proactive_events SET input_id=? WHERE id=?',('input',key))
    assert store.context('wechat-test',['input'])['events'][0]['habit_reminder_allowed'] is False


def test_exact_collision_only_and_restart_idempotency(tmp_path):
    now=[stamp('2026-11-04T07:59:00+08:00')]
    store=configured(tmp_path,now=lambda:now[0]);events=store.plan()
    eight=stamp('2026-11-04T08:00:00+08:00')
    assert [e['kind'] for e in events if e['due']==eight]==['fixed']
    assert [e['kind'] for e in events if e['due']==eight+1800]==['heartbeat']
    before=store.followup('wechat-test','near', '2026-11-04T08:00:01+08:00','检查刚刚的任务')
    same=store.followup('wechat-test','same', '2026-11-04T08:00:00+08:00','独立的检查')
    again=ProactiveStore(tmp_path,now=lambda:now[0]);planned=again.plan()
    assert len({e['id'] for e in planned})==len(planned)==len(events)+2
    assert {before['event_id'],same['event_id']} <= {e['id'] for e in planned}
    with store.db() as db:
        assert db.execute("SELECT state FROM proactive_events WHERE due=? AND kind='heartbeat'",(eight,)).fetchone()[0]=='covered'


def test_disabled_fixed_does_not_suppress_heartbeat_and_midnight_window(tmp_path):
    now=stamp('2026-11-04T23:59:00+08:00')
    store=configured(tmp_path,now=lambda:now,fixed=False)
    times={datetime.fromtimestamp(e['due'],timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).strftime('%H:%M') for e in store.plan()}
    assert '00:00' in times and '00:30' in times and '01:00' not in times and '08:00' in times


def test_weekdays_timezone_and_dst(tmp_path):
    config=read_config(tmp_path);p=defaults();p.update(enabled=True,session='wechat-test',timezone='America/New_York')
    p['heartbeat']['enabled']=False
    p['fixed']=[{'id':'fold','enabled':True,'time':'01:30','prompt':'早安','days':[6]}]
    config['proactive']=p;write_config(tmp_path,config)
    store=ProactiveStore(tmp_path,now=lambda:stamp('2026-11-01T00:00:00-04:00'))
    due=[e['due'] for e in store.plan()]
    assert stamp('2026-11-01T01:30:00-04:00') in due and stamp('2026-11-01T01:30:00-05:00') in due
    p['fixed'][0]['time']='02:30';write_config(tmp_path,config)
    store=ProactiveStore(tmp_path,now=lambda:stamp('2027-03-14T00:00:00-05:00'))
    assert not store.plan()


def test_followup_pause_cancel_ownership_and_expiry(tmp_path):
    now=[stamp('2026-11-04T10:00:00+08:00')];store=configured(tmp_path,now=lambda:now[0],heartbeat=False,fixed=False)
    a=store.followup('wechat-test','call-1','2026-11-04T10:00:01+08:00','一秒后看情况')
    assert store.followup('wechat-test','call-1','2026-11-04T11:00:00+08:00','重复调用')==a
    with pytest.raises(ValueError):store.followup('other','call-2','2026-11-04T11:00:00+08:00','跨会话')
    with pytest.raises(ValueError):store.control('other','cancel',a['event_id'])
    with pytest.raises(ValueError):store.followup('wechat-test','bad','2026-11-04T11:00:00','缺少时区')
    store.bind_task(a['event_id'],'wechat-test','native-1')
    assert not store.admit(a['event_id'],'wechat-test','native-1','input-1')['allowed']
    now[0]+=1
    assert store.admit(a['event_id'],'wechat-test','native-1','input-1')['allowed']
    assert not store.admit(a['event_id'],'wechat-test','native-1','duplicate-input')['allowed']
    store.control('wechat-test','pause');assert not store.eligible(store.event(a['event_id'],'wechat-test'))
    store.control('wechat-test','resume');assert not store.plan()
    b=store.followup('wechat-test','later','2026-11-04T11:00:00+08:00','稍后')
    now[0]=stamp('2026-11-04T11:31:00+08:00');store.plan()
    assert store.event(b['event_id'],'wechat-test')['state']=='expired'


def next_check(cid, seconds=2):
    return call(cid,{'at':datetime.fromtimestamp(time.time()+seconds,timezone.utc).isoformat(),'reason':'查询待办，决定是否联系；需要时发送简短消息。'},'schedule_next_check')


def test_native_followup_context_notify_once_and_silent_output(tmp_path):
    async def run():
        h=Harness(tmp_path,[])
        configured(tmp_path/'data',heartbeat=False,fixed=False)
        h.model.script=[lambda _:next_check('register'), '已登记。',call('query',{'action':'query','kind':'reminder'}),
                        activity_read(),
                        call('notify',{'message':'该去买菜了。'},'notify_user'),'这段后台总结不应该发出去。']
        try:
            await h.submit('帮我稍后看看待办')
            record=value(h.model.requests[1]);assert record['status']=='registered'
            await wait(lambda:'该去买菜了。' in h.sent)
            await wait(lambda:len(h.model.requests)==6)
            assert h.sent==['已登记。','该去买菜了。']
            request=json.dumps(h.model.requests[2],ensure_ascii=False)
            assert '查询待办，决定是否联系' in request and 'proactive' in request
            assert not results(h.model.requests[3])[-1].get('is_error')
            assert value(h.model.requests[5])['status']=='queued'
            await h.runtime.close();h.runtime=h.restart();await h.runtime.start();await asyncio.sleep(.4)
            assert h.sent==['已登记。','该去买菜了。'] and len(h.model.requests)==6
            assert h.runtime.proactive.event(record['event_id'],h.session)['state']=='sent'
        finally:await h.close()
    asyncio.run(run())


def test_native_quiet_readonly_guard_and_followup_after_delivery(tmp_path):
    async def run():
        h=Harness(tmp_path,[]);configured(tmp_path/'data',heartbeat=False,fixed=False)
        def slow_followup(request):
            time.sleep(2.5)  # Give the sender time to mark the current notification sent.
            return next_check('another',1)
        h.model.script=[lambda _:next_check('register'), '已登记。',call('write',draft()),
            activity_read(),
            call('notify',{'message':'一次主动消息。'},'notify_user'),slow_followup,'内部完成。','暂时没有需要联系的内容。']
        try:
            await h.submit('稍后主动检查')
            await wait(lambda:len(h.model.requests)==8)
            await wait(lambda:any(e['state']=='quiet' for e in h.runtime.proactive.status()['recent']))
            assert not h.native.writes
            assert results(h.model.requests[3])[-1].get('is_error')
            assert value(h.model.requests[6])['status']=='registered'
            assert h.sent==['已登记。','一次主动消息。']
        finally:await h.close()
    asyncio.run(run())


def test_native_two_close_checks_are_separate_turns(tmp_path):
    async def run():
        h=Harness(tmp_path,[]);configured(tmp_path/'data',heartbeat=False,fixed=False)
        h.model.script=[lambda _:next_check('first',2),lambda _:next_check('second',2),'两次检查已登记。','第一次安静。','第二次安静。']
        try:
            await h.submit('登记两次相近检查')
            await wait(lambda:len(h.model.requests)==5)
            await wait(lambda:sum(e['state']=='quiet' for e in h.runtime.proactive.status()['recent'])==2)
            assert h.sent==['两次检查已登记。']
        finally:await h.close()
    asyncio.run(run())


def test_paused_queued_message_never_sends_and_notify_is_idempotent(tmp_path):
    async def run():
        h=Harness(tmp_path,[]);store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            h.runtime.bind_ref(h.session,h.ref)
            r=store.followup(h.session,'q',datetime.fromtimestamp(time.time()+1,timezone.utc).isoformat(),'检查')
            store.bind_task(r['event_id'],h.session,'native')
            with store.db() as db:db.execute('UPDATE proactive_events SET due=?,state=?,input_id=? WHERE id=?',(time.time()-1,'running','input',r['event_id']))
            data={'session':h.session,'event_id':r['event_id'],'input_id':'input','message':'测试消息'}
            assert (await h.runtime.route('/proactive/notify',data))['status']=='queued'
            assert (await h.runtime.route('/proactive/notify',{**data,'message':'重复内容'}))['status']=='already_queued'
            store.control(h.session,'pause');await h.runtime.drain_scheduled()
            assert h.sent==[]
        finally:await h.close()
    asyncio.run(run())


def test_native_overdue_unregistered_fixed_and_cancel_before_wakeup(tmp_path):
    async def run():
        h=Harness(tmp_path,['普通聊天正常。']);configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            await h.submit('记录一下我的偏好')
            await h.runtime.close()
            store=h.runtime.proactive;_,revision,_=store.sync()
            with store.db() as db:
                event=store._insert(db,h.session,revision,'fixed',time.time()-3,'FIXED_CUSTOM_PROMPT：查询日历后提醒我出门。')
            h.model.script=[activity_read(),call('fixed',{'message':'记得出门。'},'notify_user'),'后台结束。'];h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:'记得出门。' in h.sent)
            assert 'FIXED_CUSTOM_PROMPT' in json.dumps(h.model.requests[1],ensure_ascii=False)
            assert h.runtime.proactive.event(event,h.session)['state']=='sent'
            h.model.script=[lambda _:next_check('cancel-me',3),call('pause',{'action':'pause'},'proactive_control'),'已暂停。']
            await h.submit('安排检查然后暂停主动消息')
            count=len(h.model.requests);await asyncio.sleep(4)
            assert len(h.model.requests)==count and h.sent[-1]=='已暂停。'
        finally:await h.close()
    asyncio.run(run())


def test_native_registered_check_survives_restart_and_stays_silent(tmp_path):
    async def run():
        h=Harness(tmp_path,[]);configured(tmp_path/'data',heartbeat=False,fixed=False)
        h.model.script=[lambda _:next_check('restart',4),'已登记。']
        try:
            await h.submit('稍后再检查')
            task=value(h.model.requests[-1]);await h.runtime.close();await asyncio.sleep(4)
            h.model.script=['这次不联系。'];h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:h.runtime.proactive.event(task['event_id'],h.session)['state']=='quiet')
            assert len(h.model.requests)==3 and h.sent==['已登记。']
            await h.runtime.close();h.runtime=h.restart();await h.runtime.start();await asyncio.sleep(.4)
            assert len(h.model.requests)==3
        finally:await h.close()
    asyncio.run(run())


def test_resume_preserves_future_checks_outside_timed_pause(tmp_path):
    now=stamp('2026-11-04T10:00:00+08:00');store=configured(tmp_path,now=lambda:now,heartbeat=False,fixed=False)
    before=store.followup('wechat-test','before','2026-11-04T10:30:00+08:00','暂停期间')
    after=store.followup('wechat-test','after','2026-11-04T12:00:00+08:00','暂停之后')
    store.control('wechat-test','pause',until='2026-11-04T11:00:00+08:00')
    store.control('wechat-test','resume');store.control('wechat-test','resume')
    assert store.event(before['event_id'],'wechat-test')['state']=='cancelled'
    assert store.eligible(store.event(after['event_id'],'wechat-test'))


def test_proactive_receipt_unknown_and_partial_rejection(tmp_path):
    from telos.adapters.base import SendResult
    from dataclasses import replace
    async def run():
        h=Harness(tmp_path,[]);store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            h.runtime.bind_ref(h.session,h.ref)
            h.adapter.config=replace(h.adapter.config,max_text_chars=10)
            r=store.followup(h.session,'q',datetime.fromtimestamp(time.time()+1,timezone.utc).isoformat(),'检查')
            store.bind_task(r['event_id'],h.session,'native')
            with store.db() as db:db.execute('UPDATE proactive_events SET due=?,state=?,input_id=? WHERE id=?',(time.time()-1,'running','input',r['event_id']))
            attempts=[]
            async def send(ref,text,**kwargs):
                attempts.append(text)
                return SendResult(ok=False,error='unknown') if len(attempts)==2 else SendResult(ok=True)
            h.adapter.send_text=send
            await h.runtime.route('/proactive/notify',{'session':h.session,'event_id':r['event_id'],'input_id':'input','message':'这是超过单条长度限制的主动消息，需要拆成多个发送段。'})
            await h.runtime.drain_scheduled();assert len(attempts)==2
            assert store.event(r['event_id'],h.session)['state']=='unknown'
            await h.runtime.close();h.runtime=h.restart();await h.runtime.drain_scheduled()
            assert len(attempts)==2
        finally:await h.close()
    asyncio.run(run())


def test_native_exact_same_time_fixed_and_followup_both_execute(tmp_path):
    async def run():
        h=Harness(tmp_path,['收到。']);configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            await h.submit('建立会话');await h.runtime.close()
            store=h.runtime.proactive;_,revision,_=store.sync();due=time.time()+2
            with store.db() as db:
                first=store._insert(db,h.session,revision,'fixed',due,'固定提醒的内容')
                second=store._insert(db,h.session,revision,'followup',due,'独立后续检查的内容')
            h.model.script=[activity_read('read-first'),call('first',{'message':'第一条主动消息。'},'notify_user'),'内部完成1',
                            call('missing-second',{'message':'上一轮查询不能代替本轮。'},'notify_user'),
                            activity_read('read-second'),call('second',{'message':'第二条主动消息。'},'notify_user'),'内部完成2']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:len(h.sent)==3)
            assert h.sent==['收到。','第一条主动消息。','第二条主动消息。']
            assert all(store.event(e,h.session)['state']=='sent' for e in (first,second))
        finally:await h.close()
    asyncio.run(run())


def test_pause_during_model_turn_stops_continuation_without_normal_reply(tmp_path):
    async def run():
        h=Harness(tmp_path,[]);configured(tmp_path/'data',heartbeat=False,fixed=False)
        def paused(request):
            h.runtime.proactive.control(h.session,'pause')
            return call('query-after-pause',{'action':'query','kind':'reminder'})
        h.model.script=[lambda _:next_check('pause-in-flight',1),'已登记。',paused,'不能作为普通聊天发出。']
        try:
            await h.submit('稍后检查')
            await wait(lambda:len(h.model.requests)==3)
            await wait(lambda:bool(h.runtime.proactive.status()['recent']))
            await asyncio.sleep(.5)
            assert len(h.model.requests)==3 and h.sent==['已登记。']
            assert all(e['state']=='cancelled' for e in h.runtime.proactive.status()['recent'])
        finally:await h.close()
    asyncio.run(run())


def test_native_scheduler_receives_heartbeat_and_fixed_plan_without_covered_ticks(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('catalog',{},'schedule_list'),'已列出。'])
        store=configured(tmp_path/'data');h.runtime.bind_ref(h.session,h.ref)
        try:
            await h.submit('列出当前已登记的任务')
            records=value(h.model.requests[-1])
            planned=store.plan()
            assert {e['kind'] for e in planned}=={'heartbeat','fixed'}
            assert all(e['task_id'] for e in planned)
            assert {e['task_id'] for e in planned}<={r['id'] for r in records}
            with store.db() as db:
                covered=db.execute("SELECT task_id FROM proactive_events WHERE state='covered'").fetchall()
            assert covered and all(r['task_id'] is None for r in covered)
        finally:await h.close()
    asyncio.run(run())


def test_native_proactive_can_read_apple_detail(tmp_path):
    async def run():
        h=Harness(tmp_path,[]);configured(tmp_path/'data',heartbeat=False,fixed=False)
        h.model.script=[lambda _:next_check('details',1),'已登记。',
                        call('detail',{'action':'detail','kind':'reminder','id':'reminder-001'}),'保持安静。']
        try:
            await h.submit('稍后看看这条待办详情')
            await wait(lambda:len(h.model.requests)==4)
            assert not results(h.model.requests[-1])[-1].get('is_error')
            assert '买菜' in tool_text(results(h.model.requests[-1])[-1])
            assert h.sent==['已登记。'] and not h.native.writes
        finally:await h.close()
    asyncio.run(run())
