"""Real host: plan prerequisites reject premature/mis-scoped reads and recover."""
import asyncio
import json
import time
import pytest
from support import Harness, activity_read, call
from test_proactive import configured
from settings import read_config, write_config
from test_schedule import wait
from test_history import results, tool_text

@pytest.mark.parametrize('failure',[False,True])
def test_plan_prerequisites_in_real_host(tmp_path,failure):
    async def run():
        h=Harness(tmp_path,['已绑定。'])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        config=read_config(tmp_path/'data');config['proactive']['goal_list_ids']=['list-001'];write_config(tmp_path/'data',config)
        try:
            await h.submit('绑定会话');await h.runtime.close()
            _,revision,_=store.sync()
            with store.db() as db:event=store._insert(db,h.session,revision,'heartbeat',time.time()+1,'诊断并通知。')
            from datetime import datetime,timedelta
            from zoneinfo import ZoneInfo
            day=datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0)
            def last_reads(request):
                batch=call('all-reminders',{'action':'query','kind':'reminder','include_completed':True})
                batch['tool_calls']+=call('full-calendar',{'action':'query','kind':'event','start':day.isoformat(),'end':(day+timedelta(days=1)).isoformat()})['tool_calls']
                batch['tool_calls']+=call('same-step',{'message':'不能预写通知。'},'notify_user')['tool_calls']
                return batch
            h.model.script=[activity_read(with_plan=False),
                call('no-plan',{'message':'不应发送。'},'notify_user'),
                call('goal-only',{'action':'query','kind':'reminder','calendar_ids':['list-001'],'include_completed':True}),
                call('still-no-plan',{'message':'不应发送。'},'notify_user'),last_reads,
                call('send',{'message':'本地测试结果','assessment':'unknown' if failure else 'plan_missing'},'notify_user'),'结束。']
            h.native.fail=failure
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:store.event(event,h.session)['state']=='sent')
            await wait(lambda:len(h.model.requests)==8)
            final=results(h.model.requests[-1]);errors=[tool_text(r) for r in final if r.get('is_error')]
            assert sum('TELOS_PLAN_REQUIRED' in e for e in errors)>=3
            assert h.sent==['已绑定。','本地测试结果'] and not h.native.writes
            assert store.event(event,h.session)['message']=='本地测试结果'
        finally:await h.close()
    asyncio.run(run())


def test_query_requirements_are_trusted_and_local_day_handles_dst(tmp_path):
    from datetime import datetime
    from proactive import validate
    now=datetime.fromisoformat('2026-11-01T12:00:00-05:00').timestamp()
    store=configured(tmp_path,now=lambda:now)
    config=read_config(tmp_path);config['proactive'].update(timezone='America/New_York',goal_list_ids=['goals'],action_list_ids=['work'])
    write_config(tmp_path,config)
    req=store.context('wechat-test',[])['policy']['context']['query_requirements']
    assert req=={'goal_list_ids':['goals'],'action_list_ids':['work'],'day_start':'2026-11-01T00:00:00-04:00','day_end':'2026-11-02T00:00:00-05:00'}
    assert datetime.fromisoformat(req['day_end']).timestamp()-datetime.fromisoformat(req['day_start']).timestamp()==25*3600
    for invalid in [None,'goals',[''],['same','same'],[1]]:
        with pytest.raises(ValueError):validate({'goal_list_ids':invalid})
