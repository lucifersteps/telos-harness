import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import pytest
from telos.adapters.base import SendResult
from runtime import ROOT
from support import Harness,call,draft

def test_query_preview_confirm(tmp_path):
    async def run():
        def answer(request):
            return '提醒事项有买菜。\n\n'+h.runtime.business.pending(h.session)['text']
        h=Harness(tmp_path,[call('p',draft()),call('q',{'action':'query','kind':'reminder'}),answer,'已完成。'])
        try:
            await h.submit('创建测试会议，同时告诉我提醒事项')
            assert h.runtime.business.pending(h.session)['kind']=='approval'
            assert h.sent==['提醒事项有买菜。',h.runtime.business.pending(h.session)['text']] and not h.native.writes
            await h.submit('1',key='confirmed')
            assert len(h.native.writes)==1 and '已创建成功' in h.sent[-1]
            await h.submit('1',key='confirmed')
            await h.submit('1',key='again')
            assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())

@pytest.mark.parametrize('bad',[
    call('bad',{'action':'traffic'}),
    call('bad',{'action':'batch','operations':[]}),
    call('bad',{'action':'create','kind':'event','fields':{'title':'测试会议','flagged':True,'start':'bad'}}),
    call('bad',{},name='unknown_tool'),
])
def test_parameter_repair_preserves_original_requirements(tmp_path,bad):
    async def run():
        good=draft();good['fields'].update(location='测试楼',notes='带材料',alarm_minutes_before=[30])
        h=Harness(tmp_path,[bad,call('fixed',good),'请确认。','完成。'])
        try:
            await h.submit('测试会议在测试楼，带材料，提前30分钟提醒')
            pending=h.runtime.business.pending(h.session)
            assert pending and pending['args']==good and not h.native.writes
            assert len(h.model.requests)==3
            await h.submit('1')
            assert len(h.native.writes)==1
            fields=h.native.writes[0][1]['fields']
            assert fields['location']=='测试楼' and fields['notes']=='带材料' and fields['alarm_minutes_before']==[30]
        finally:await h.close()
    asyncio.run(run())

def test_revision_failure_does_not_lock_request_and_saved_lineage_cannot_replay(tmp_path):
    async def run():
        initial=draft();initial['fields'].update(location='测试楼',notes='带材料',alarm_minutes_before=[30])
        change={'action':'revise','draft_call_id':'p','fields':{'end':'2027-02-19T15:15:00+08:00'}}
        h=Harness(tmp_path,[call('p',initial),'请确认。',call('wrong-scope',change),
            call('revised',{**change,'request_mode':'continue'}),'已修改预览。','完成。',
            call('replay',{**change,'request_mode':'continue'}),'已保存的对象需要重新读取后修改。'])
        try:
            await h.submit('添加测试会议，保留地点、备注和提前提醒')
            await h.submit('1小时15分就够了')
            assert '15:15' in h.sent[-1] and not h.native.writes
            await h.submit('1')
            assert len(h.native.writes)==1 and h.native.writes[0][1]['fields']['notes']=='带材料'
            await h.submit('继续修改')
            assert len(h.native.writes)==1 and h.runtime.business.pending(h.session) is None
            assert '已保存' in json.dumps(h.model.requests[-1],ensure_ascii=False)
        finally:await h.close()
    asyncio.run(run())

def test_incomplete_draft_question_numeric_answer_then_confirmation(tmp_path):
    async def run():
        partial=draft();partial['fields'].pop('end')
        h=Harness(tmp_path,[call('partial',partial),call('ask',{'text':'持续几小时？'},name='ask_user'),'持续几小时？',
            call('fixed',{'action':'revise','draft_call_id':'partial','request_mode':'continue',
                          'fields':{'end':'2027-02-19T15:00:00+08:00'}}),'请确认。','完成。'])
        try:
            await h.submit('添加会议，14点开始')
            assert h.runtime.business.pending(h.session)['kind']=='question'
            await h.submit('1')
            assert h.runtime.business.pending(h.session)['kind']=='approval' and not h.native.writes
            await h.submit('1')
            assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())

@pytest.mark.parametrize('count',[1,2,4])
def test_batch_preview_confirm_exactly_once(tmp_path,count):
    async def run():
        operations=[draft('测试会议'+str(i)) for i in range(count)]
        h=Harness(tmp_path,[call('batch',{'action':'batch','operations':operations}),'请确认。','完成。'])
        try:
            await h.submit(f'添加{count}个日程')
            assert len(h.runtime.business.pending(h.session)['preview']['operations'])==count
            assert not h.native.writes
            await h.submit('1',key='confirm')
            await h.submit('1',key='confirm')
            await h.submit('1',key='double')
            assert len(h.native.writes)==count
        finally:await h.close()
    asyncio.run(run())

def test_partial_batch_failure_then_query_never_replays(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('batch',{'action':'batch','operations':[draft(str(i)) for i in range(4)]}),
            '请确认。','第二项结果未知，其他未执行。',call('query',{'action':'query'}),'已核实当前状态。'])
        h.native.fail_at=2
        try:
            await h.submit('添加四个日程');await h.submit('1')
            assert len(h.native.writes)==2 and '已完成 1 项' in h.sent[-1]
            await h.submit('1');await h.submit('查询现在的结果')
            assert len(h.native.writes)==2 and h.apple.status()['uncertain_writes']==1
        finally:await h.close()
    asyncio.run(run())

@pytest.mark.parametrize('control',['2','取消','拒绝'])
def test_cancel_retire_approval(tmp_path,control):
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。'])
        try:
            await h.submit('添加测试会议');await h.submit(control);await h.submit('1')
            assert not h.native.writes and '没有可确认' in h.sent[-1]
        finally:await h.close()
    asyncio.run(run())

def test_expired_approval_and_changed_object_are_not_written(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。'])
        try:
            await h.submit('添加会议')
            p=h.runtime.business.pending(h.session);p['expires_at']=0;h.runtime.business.set_pending(h.session,p)
            await h.submit('1');assert not h.native.writes and '过期' in h.sent[-1]
            item=h.apple.detail('reminder','reminder-001')
            h.model.script += [call('update',{'action':'update','kind':'reminder','id':item['id'],
                'expected_revision':item['revision'],'fields':{'title':'买水果'}}),'请确认。']
            await h.submit('把买菜改为买水果')
            h.native.items[0]['title']='手机上已修改'
            await h.submit('1')
            assert not h.native.writes and '重新核实' in h.sent[-1]
        finally:await h.close()
    asyncio.run(run())

@pytest.mark.parametrize('stage',['preview','saved-undelivered'])
def test_native_session_restart_keeps_history_and_does_not_duplicate_write(tmp_path,stage):
    async def run():
        h=Harness(tmp_path,[call('p',draft('跨进程测试会议')),'请确认。记住虚构口令青色树叶。','完成。',
            call('query',{'action':'query'}),'查询完成。'])
        try:
            await h.submit('创建跨进程测试会议')
            send=h.adapter.send_text
            if stage=='saved-undelivered':
                async def fail(ref,text,**kwargs):return SendResult(ok=False,error='模拟失败')
                h.adapter.send_text=fail;await h.submit('1',key='confirm');assert len(h.native.writes)==1
            oldpid=h.runtime.proc.pid
            await h.runtime.close();h.runtime=h.restart();h.adapter.send_text=send
            await h.runtime.recover(h.session,h.ref)
            if stage=='preview':await h.submit('1',key='confirm')
            await h.submit('1',key='confirm')
            await h.submit('刚刚的会议现在怎么样？')
            assert len(h.native.writes)==1
            assert h.runtime.proc.pid!=oldpid
            assert '跨进程测试会议' in json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert '虚构口令青色树叶' in json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert any('已创建成功' in x for x in h.sent)
            assert not list((h.path/'data').rglob('context.sqlite'))
            assert not list((h.path/'data').rglob('runs.sqlite'))
        finally:await h.close()
    asyncio.run(run())

def test_killed_harness_after_business_write_preserves_receipt(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。'])
        try:
            await h.submit('添加会议')
            h.native.on_write=lambda:os.kill(h.runtime.proc.pid,signal.SIGKILL)
            await h.submit('1',key='confirm')
            assert len(h.native.writes)==1 and '已创建成功' in h.sent[-1]
            await h.runtime.close();h.runtime=h.restart()
            await h.runtime.recover(h.session,h.ref);await h.submit('1',key='confirm');await h.submit('1')
            assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())

def test_preview_delivery_failure_cannot_authorize_write(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。'])
        try:
            send=h.adapter.send_text
            async def fail(ref,text,**kwargs):return SendResult(ok=False,error='模拟失败')
            h.adapter.send_text=fail
            await h.submit('添加会议',key='initial');await h.submit('1')
            assert not h.native.writes
            h.adapter.send_text=send;await h.runtime.recover(h.session,h.ref)
            h.model.script.append('完成。');await h.submit('1')
            assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())

def test_native_todo_is_not_user_reply_and_new_turn_clears_current_projection(tmp_path):
    async def run():
        plan={'todos':[{'content':'查询提醒事项','status':'in_progress'},{'content':'创建日程','status':'pending'}]}
        h=Harness(tmp_path,[call('todo',plan,name='todo_write'),call('q',{'action':'query'}),call('p',draft()),
            '提醒事项有买菜，请确认会议。','你好。'])
        try:
            await h.submit('查提醒事项并创建会议')
            assert any(e['type']=='todo/write' for e in h.events)
            assert 'in_progress' not in h.sent[-1] and '☐' not in h.sent[-1]
            await h.submit('你好')
            assert h.sent[-1]=='你好。'
            with h.runtime.business.db() as db:
                names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                assert not any('todo' in n or 'run' in n or 'context' in n for n in names)
        finally:await h.close()
    asyncio.run(run())

def test_no_legacy_execution_imports_and_no_parallel_history_database():
    code="""import channel,sys
blocked=('telos.agent.loop','telos.agent.turn_flow','telos.agent.context','telos.apps.personal_runtime','telos.apps.personal_tasks','telos.providers','telos.session','telos.runtime','telos.conversation.repository','telos.apps.personal_skills','telos.agent.skills','deepseek_harness')
assert not [n for n in sys.modules if any(n==b or n.startswith(b+'.') for b in blocked)]
"""
    env={**os.environ,'PYTHONPATH':str(ROOT)+':'+str(ROOT/'checkout/src')}
    subprocess.run([sys.executable,'-c',code],env=env,check=True,capture_output=True)


def test_malformed_response_uses_native_stop_then_user_can_continue(tmp_path):
    async def run():
        bad={'tool_calls':[{'id':'bad','type':'function','function':{'name':'personalPlan','arguments':'{"action":'}}]}
        h=Harness(tmp_path,[bad,call('fixed',draft()),'请确认。','完成。'])
        try:
            await h.submit('创建测试会议，2027年2月19日14点到15点')
            assert len(h.model.requests)==1 and not h.native.writes
            assert not h.sent and not h.runtime.business.pending(h.session)
            assert any(e['type']=='turn/end' and e['data']['reason']['kind']=='error' for e in h.events)
            await h.submit('继续刚才的要求')
            assert h.runtime.business.pending(h.session)['kind']=='approval'
            assert '2027年2月19日' in json.dumps(h.model.requests[-1],ensure_ascii=False)
            await h.submit('1')
            assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())


def test_failed_draft_can_be_revised_without_retyping_original_fields(tmp_path):
    async def run():
        original=draft();original['fields']['end']='bad-date';original['fields']['notes']='带材料'
        h=Harness(tmp_path,[call('bad',original),call('fixed',{'action':'revise','draft_call_id':'bad',
            'fields':{'end':'2027-02-19T15:00:00+08:00'}}),'请确认。','完成。'])
        try:
            await h.submit('创建测试会议，带材料')
            assert h.runtime.business.pending(h.session)
            await h.submit('1')
            assert len(h.native.writes)==1 and h.native.writes[0][1]['fields']['notes']=='带材料'
        finally:await h.close()
    asyncio.run(run())


def test_execution_step_budget_stops_without_writing(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('bad'+str(i),{'action':'traffic'}) for i in range(30)])
        try:
            await h.submit('查询交通')
            assert len(h.model.requests)==24 and not h.native.writes
            assert not h.sent
            assert any(e['type']=='turn/end' and e['data']['reason']['kind']=='error' for e in h.events)
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('point',['before-receipt','after-receipt'])
def test_business_host_crash_never_replays_external_write(tmp_path,point):
    class Crash(BaseException):pass
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。'])
        try:
            await h.submit('创建测试会议')
            def crash(*args):raise Crash()
            if point=='before-receipt':h.native.on_write=crash
            else:h.runtime.business.settle_drafts=crash
            route=h.runtime.route
            async def crashed_host(path,data):
                try:return await route(path,data)
                except Crash:
                    h.runtime.proc.kill()
                    raise RuntimeError('模拟业务宿主在保存边界退出') from None
            h.runtime.route=crashed_host
            await h.submit('1',key='confirm')
            assert len(h.native.writes)==1
            assert h.runtime.business.pending(h.session)['executing']
            await h.runtime.close();h.runtime=h.restart();h.native.on_write=None
            await h.runtime.recover(h.session,h.ref)
            await h.submit('1',key='confirm');await h.submit('1')
            assert len(h.native.writes)==1
            assert h.apple.status()['uncertain_writes']==(1 if point=='before-receipt' else 0)
            assert any('已核对上次操作' in r for r in h.sent)
        finally:await h.close()
    asyncio.run(run())


def test_harness_checkpoint_restores_unknown_outcome_without_replaying_tool(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('q',{'action':'query'}),'上次查询被中断，请核实。'])
        invocations=[]
        try:
            original=h.runtime.business.execute
            async def interrupted(*args):
                result=await original(*args);invocations.append(args[1])
                os.kill(h.runtime.proc.pid,signal.SIGKILL)
                return result
            h.runtime.business.execute=interrupted
            await h.submit('查询提醒事项')
            assert invocations==['q'] and not h.native.writes
            await h.runtime.close();h.runtime=h.restart()
            await h.submit('刚才发生了什么？')
            history=json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert 'TOOL_OUTCOME_UNKNOWN' in history or 'outcome is unknown' in history or 'outcome of this tool call is unknown' in history
            assert len(h.model.requests)==2 and not h.native.writes
        finally:await h.close()
    asyncio.run(run())


def test_native_parallel_preview_calls_cannot_overwrite_confirmation(tmp_path):
    async def run():
        calls={'tool_calls':call('first',draft('第一项'))['tool_calls']+call('second',draft('第二项'))['tool_calls']}
        h=Harness(tmp_path,[calls,'先确认第一项。','已保存。'])
        try:
            await h.submit('创建第一项日程')
            pending=h.runtime.business.pending(h.session)
            title=pending['args']['fields']['title']
            assert title in {'第一项','第二项'} and not h.native.writes
            assert '不能覆盖' in json.dumps(h.model.requests[-1],ensure_ascii=False)
            await h.submit('1')
            assert len(h.native.writes)==1 and h.native.writes[0][1]['fields']['title']==title
        finally:await h.close()
    asyncio.run(run())


def test_confirmation_receipt_is_current_before_model_and_reply_is_only_result(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。',
            '收到确认，正在提交，保存结果稍后告诉你。','只回答这次的新问题。'])
        try:
            await h.submit('创建测试会议')
            await h.submit('1')
            assert h.sent[-1]=='已创建成功。'
            assert len(h.native.writes)==1
            # Check the latest runtime context, not an older receipt in history.
            def latest_context(request):
                blocks=[]
                for message in request['messages']:
                    content=message.get('content',[])
                    if isinstance(content,str):content=[{'type':'text','text':content}]
                    blocks.extend(b.get('text','') for b in content if isinstance(b,dict) and any(marker in b.get('text','') for marker in ('{"pending"','{"direct_write"')))
                assert blocks
                text=blocks[-1]
                start=min(text.index(marker) for marker in ('{"pending"','{"direct_write"') if marker in text)
                return json.JSONDecoder().raw_decode(text[start:])[0]
            confirmed=latest_context(h.model.requests[-1])
            assert confirmed['receipt']['text']=='已创建成功。' and confirmed['pending'] is None
            await h.submit('现在帮我回答一个新问题')
            current=latest_context(h.model.requests[-1])
            assert current['receipt'] is None
            assert h.sent[-1]=='只回答这次的新问题。' and len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())
