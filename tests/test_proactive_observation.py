import asyncio
from datetime import datetime
import json
import time

import pytest

from proactive_channel import observation_bubble
from proactive import ProactiveStore
from settings import read_config,write_config
from support import Harness,activity_read,call
from test_proactive import configured,stamp
from test_schedule import wait
from telos.adapters.base import SendResult


def enable(root):
    cfg=read_config(root)
    cfg['proactive']['policy']['configs']['goal_execution']['observation_enabled']=True
    write_config(root,cfg)


def test_window_uses_last_delivery_not_quiet_failed_or_other_session(tmp_path):
    now=stamp('2026-11-06T12:00:00+08:00')
    store=configured(tmp_path,now=lambda:now);enable(tmp_path);_,rev,_=store.sync()
    with store.db() as db:
        previous=store._insert(db,'wechat-test',rev,'heartbeat',now-7200,'old',state='sent')
        db.execute('UPDATE proactive_events SET message=?,observation=?,sent_at=? WHERE id=?',('提醒','观察',now-6500,previous))
        for n,state in enumerate(['quiet','failed','unknown','queued']):
            store._insert(db,'wechat-test',rev,'heartbeat',now-600+n,'ignored',state=state)
        other=store._insert(db,'other',rev,'fixed',now-5,'other',state='sent')
        db.execute('UPDATE proactive_events SET message=?,sent_at=? WHERE id=?',('other',now-1,other))
        current=store._insert(db,'wechat-test',rev,'heartbeat',now,'check')
    store.bind_task(current,'wechat-test','task');assert store.admit(current,'wechat-test','task','input')['allowed']
    context=store.context('wechat-test',['input']);event=context['events'][0]
    assert datetime.fromisoformat(event['observation_window']['start']).timestamp()==now-6500
    assert event['observation_window']['basis']=='last_delivered'
    assert event['health_mentions']['sleep_allowed'] is False
    assert context['recent_notifications'][0]['observation']=='观察'
    now+=60
    assert store.context('wechat-test',['input'])['events'][0]['observation_window']==event['observation_window']


def test_first_window_and_sleep_mention_context(tmp_path):
    now=stamp('2026-11-06T08:00:00+08:00')
    store=configured(tmp_path,now=lambda:now);_,rev,_=store.sync()
    with store.db() as db:
        current=store._insert(db,'wechat-test',rev,'fixed',now,'morning',state='running')
        db.execute('UPDATE proactive_events SET input_id=?,checked_at=? WHERE id=?',('input',now,current))
    event=store.context('wechat-test',['input'])['events'][0]
    assert event['observation_window']['basis']=='first_check_last_hour'
    assert event['health_mentions']['sleep_context']=='first_morning'
    with store.db() as db:
        prior=store._insert(db,'wechat-test',rev,'heartbeat',now-60,'prior',state='sent')
        db.execute('UPDATE proactive_events SET message=?,sent_at=? WHERE id=?',('先前提醒',now-50,prior))
    assert not store.context('wechat-test',['input'])['events'][0]['health_mentions']['sleep_allowed']


def test_two_bubbles_survive_rejection_restart_and_duplicate_call(tmp_path):
    async def run():
        h=Harness(tmp_path,[]);attempts=[]
        try:
            store=configured(tmp_path/'data',heartbeat=False,fixed=False);enable(tmp_path/'data')
            h.runtime.bind_ref(h.session,h.ref);_,rev,_=store.sync()
            with store.db() as db:
                event=store._insert(db,h.session,rev,'heartbeat',time.time()-1,'check',state='running')
                db.execute('UPDATE proactive_events SET input_id=? WHERE id=?',('input',event))
            data={'session':h.session,'event_id':event,'input_id':'input','message':'先推进论文的一项修改。'}
            with pytest.raises(ValueError):await h.runtime.route('/proactive/notify',data)
            observation='这段时间主要在调整 Telos。\n中间查看了视频。'
            queued=await h.runtime.route('/proactive/notify',{**data,'observation':observation})
            assert queued['observation'].endswith('\n'+observation)
            observation=queued['observation']
            async def send(ref,text):
                attempts.append(text)
                return SendResult(ok=False,retryable=True) if len(attempts)==2 else SendResult(ok=True)
            # Adapter injection is the same supported test boundary as Harness.
            from telos.adapters.wechat.adapter import WeChatAdapter
            h.runtime.adapter=WeChatAdapter(send_text_fn=send)
            await h.runtime.drain_scheduled()
            assert attempts==[observation,data['message']]
            assert store.event(event,h.session)['sent_at'] is None
            await h.runtime.close();h.runtime=h.restart();h.runtime.adapter=WeChatAdapter(send_text_fn=send)
            result=await h.runtime.route('/proactive/notify',{**data,'observation':'不能替换已登记的观察'})
            assert result['status']=='already_queued'
            await h.runtime.drain_scheduled();await h.runtime.drain_scheduled()
            assert attempts==[observation,data['message'],data['message']]
            assert store.event(event,h.session)['sent_at'] is not None
        finally:await h.close()
    asyncio.run(run())


def test_native_loop_requires_observation_and_delivers_two_local_bubbles(tmp_path):
    async def run():
        h=Harness(tmp_path,['绑定完成。'])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False);enable(tmp_path/'data')
        try:
            await h.submit('绑定测试');await h.runtime.close();_,rev,_=store.sync()
            with store.db() as db:event=store._insert(db,h.session,rev,'heartbeat',time.time()+1,'读取观察区间后给出观察和判断')
            def read(request):
                result=activity_read()(request)
                args=json.loads(result['tool_calls'][0]['function']['arguments']);args['start']=datetime.fromtimestamp(time.time()-3700).astimezone().isoformat();args['sources']=['aw','health']
                result['tool_calls'][0]['function']['arguments']=json.dumps(args)
                return result
            def screen_read(request):
                result=read(request)
                args=json.loads(result['tool_calls'][0]['function']['arguments']);args['sources']=['screen_time']
                result['tool_calls'][0]['function']['arguments']=json.dumps(args)
                return result
            h.model.script=[read,call('missing-source',{'observation':'不能缺来源。','message':'应拦截。'},'notify_user'),screen_read,
                call('missing',{'message':'缺少观察应拦截'},'notify_user'),
                call('health-supplement',{'view':'detail','event_id':'health:sleep:test'},'activity_history'),
                call('send',{'observation':'这段时间的行为记录不可用，暂时无法核实。','message':'先明确接下来的一项行动。'},'notify_user'),'内部结束']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:store.event(event,h.session)['state']=='sent')
            assert len(h.sent)==3 and h.sent[1].endswith('\n这段时间的行为记录不可用，暂时无法核实。')
            assert h.sent[2]=='先明确接下来的一项行动。'
            assert store.event(event,h.session)['observation']==h.sent[1]
        finally:await h.close()
    asyncio.run(run())


def test_missed_delivery_recovers_using_tool_supplied_window(tmp_path):
    """Replay the short-window failure after a day without successful delivery."""
    async def run():
        from test_history import results, tool_text
        h=Harness(tmp_path,['绑定完成。'])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False);enable(tmp_path/'data')
        try:
            await h.submit('绑定测试');await h.runtime.close();_,rev,_=store.sync()
            last=time.time()-35*3600
            with store.db() as db:
                prior=store._insert(db,h.session,rev,'heartbeat',last,'previous',state='sent')
                db.execute('UPDATE proactive_events SET message=?,sent_at=? WHERE id=?',('previous',last,prior))
                event=store._insert(db,h.session,rev,'heartbeat',time.time()+1,'完成当前检查')
            def recover(request):
                error=tool_text(results(request)[-1])
                payload=json.loads(error.split('TELOS_ACTIVITY_REQUIRED: ',1)[1].split('\n',1)[0])
                recovery=payload['next_call']
                assert abs(datetime.fromisoformat(recovery['arguments']['start']).timestamp()-last)<0.00001
                return call('recover',recovery['arguments'],recovery['name'])
            h.model.script=[activity_read('too-short'),
                call('blocked',{'observation':'不能只覆盖一小时','message':'不能发送'},'notify_user'),recover,
                call('send',{'observation':'完整区间的行为来源不可用，无法核实。','message':'先确定今天的一项行动。'},'notify_user'),'内部结束']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:store.event(event,h.session)['state']=='sent')
            assert len(h.sent)==3 and h.sent[1].endswith('\n完整区间的行为来源不可用，无法核实。')
            assert h.sent[2]=='先确定今天的一项行动。'
            await h.runtime.close();h.runtime=h.restart();await h.runtime.start()
            await asyncio.sleep(.2)
            assert len(h.sent)==3
        finally:await h.close()
    asyncio.run(run())


def test_real_record_range_headers():
    assert observation_bubble({'start':'2026-11-09T19:30:35+08:00','end':'2026-11-10T12:30:00+08:00','basis':'last_delivered'},'观察') == '11月09日 19:30—11月10日 12:30\n观察'
    assert observation_bubble({'start':'2026-11-10T14:00:57+08:00','end':'2026-11-10T14:30:00+08:00'},'观察') == '14:00—14:30\n观察'
    assert '近似' in observation_bubble({'start':'2026-11-09T19:30:35+08:00','end':'2026-11-10T12:30:00+08:00','basis':'legacy_schedule_time','truncated':True},'观察')
