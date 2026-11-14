"""Actual host loop: missing evidence blocks notify, completed failed reads allow it."""
import asyncio
import json
import time

import pytest

from activity_history import DESCRIPTION, PARAMETERS
from telos.agent.tools.types import ExecutableTool, ToolExecution
from support import Harness, activity_read, call
from test_history import results, tool_text
from test_proactive import configured
from test_schedule import wait


@pytest.mark.parametrize('kind,outcome',[('heartbeat','ok'),('fixed','partial'),('followup','exception')])
def test_current_check_requires_real_activity_read_and_allows_degradation(tmp_path,kind,outcome):
    async def run():
        h=Harness(tmp_path,[activity_read('old-chat-read'),'已绑定。'])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        observed=[]
        async def resolve(args,context):
            async def execute():
                observed.append(args)
                if outcome=='exception':raise RuntimeError('模拟行为来源读取失败')
                return {'status':outcome,'view':args.get('view'),
                        'sources_status':[{'source':'aw','state':'available' if outcome=='ok' else 'unavailable'}],
                        'activities':[]}
            return ToolExecution(execute=execute)
        try:
            await h.submit('绑定会话');await h.runtime.close()
            _,revision,_=store.sync()
            with store.db() as db:
                event=store._insert(db,h.session,revision,kind,time.time()+1,'读取目标、今天的计划和实际行为后发送。')
            def read_and_premature_notify(request):
                batch=activity_read('current-read')(request)
                batch['tool_calls']+=call('premature',{'message':'不能在看到结果前预写通知。'},'notify_user')['tool_calls']
                return batch
            h.model.script=[
                call('missing',{'message':'不能沿用旧的行为查询。'},'notify_user'),
                call('status-only',{'view':'status'},'activity_history'),
                call('still-missing',{'message':'不能只查状态。'},'notify_user'),
                read_and_premature_notify,
                call('goals',{'action':'query','kind':'reminder'}),
                call('plan',{'action':'query','kind':'event'}),
                call('send',{'message':'已查看目标与安排；行为证据不足，暂时无法核实实际进展。'},'notify_user'),
                '内部结束。']
            h.runtime=h.restart()
            h.runtime.business.tools['activity_history']=ExecutableTool(
                name='activity_history',description=DESCRIPTION,parameters=PARAMETERS,resolver=resolve)
            await h.runtime.start()
            await wait(lambda:store.event(event,h.session)['state']=='sent')
            await wait(lambda:len(h.model.requests)==10)
            assert h.sent==['已绑定。','已查看目标与安排；行为证据不足，暂时无法核实实际进展。']
            for index in [3,5]:
                result=results(h.model.requests[index])[-1]
                assert result.get('is_error') and 'TELOS_ACTIVITY_REQUIRED' in tool_text(result)
            assert any(r.get('is_error') and 'TELOS_ACTIVITY_REQUIRED' in tool_text(r)
                       for r in results(h.model.requests[6]))
            assert [a['view'] for a in observed]==['status','timeline']
            wire=json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert 'Every active proactive check' in wire
            assert '与当日计划是否一致' in wire and '是否支持目标' in wire
            assert not h.native.writes
            # Ordinary chat keeps its normal reply path, with no required query.
            h.model.script=['普通聊天回复。'];await h.submit('你好')
            assert h.sent[-1]=='普通聊天回复。' and len(observed)==2
        finally:await h.close()
    asyncio.run(run())
