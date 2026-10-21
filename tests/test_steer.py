"""原生 steer、Skill、工具隔离及微信回执的完整流程；不启动真实微信轮询。"""
import asyncio
import json
import threading
import pytest
from support import Harness,call,draft
from telos.adapters.base import SendResult


def unique_events(h,kind):
    return {e['seq']:e for e in h.events if e['type']==kind}.values()


@pytest.mark.parametrize('during',['model','tool','final'])
def test_busy_steer_enters_same_turn_without_restart_or_stale_preview(tmp_path,during):
    async def run():
        entered=threading.Event();release=threading.Event()
        revised={'action':'revise','draft_call_id':'first','request_mode':'continue',
                 'fields':{'start':'2027-02-19T16:00:00+08:00','end':'2027-02-19T17:00:00+08:00'}}
        def slow(request):
            entered.set();assert release.wait(5)
            return '旧时间请确认。' if during=='final' else call('first',draft())
        script=([call('first',draft()),slow] if during=='final' else
                [slow] if during=='model' else [call('first',draft())])
        h=Harness(tmp_path,[*script,call('revised',revised),'已按补充修改，请确认。','完成。'])
        if during=='tool':
            original=h.runtime.business.execute
            async def hold(*args):
                result=await original(*args)
                if args[1]=='first':entered.set();assert await asyncio.to_thread(release.wait,5)
                return result
            h.runtime.business.execute=hold
        try:
            first=asyncio.create_task(h.submit('创建2027年2月19日14点到15点测试会议',key='first-msg'))
            assert await asyncio.to_thread(entered.wait,10)
            second=asyncio.create_task(h.submit('改为16点到17点',key='change-msg'))
            # Observe durable native admission while the first request/tool is still held.
            for _ in range(100):
                with h.runtime.business.db() as db:accepted=db.execute('SELECT 1 FROM messages WHERE id=?',('change-msg',)).fetchone()
                if accepted:break
                await asyncio.sleep(.01)
            await asyncio.sleep(.05);release.set()
            await asyncio.gather(first,second)
            assert len(h.sent)==1 and '16:00' in h.sent[0] and '14:00' not in h.sent[0]
            assert len(list(unique_events(h,'turn/start')))==1
            assert not h.native.writes
            steer=[e for e in unique_events(h,'agent/inbox/spliced') if any(m.get('id')=='change-msg' for m in e['data'].get('inserted',[]))]
            assert steer and steer[0]['data']['target']=='next-step'
            last=json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert '14点到15点' in last and '16点到17点' in last
            await h.submit('1')
            assert len(h.native.writes)==1 and h.native.writes[0][1]['fields']['start'].startswith('2027-02-19T16:00')
        finally:release.set();await h.close()
    asyncio.run(run())


def test_steer_confirmation_cannot_approve_undelivered_preview(tmp_path):
    async def run():
        entered=threading.Event();release=threading.Event()
        def slow(request):entered.set();assert release.wait(5);return call('preview',draft())
        h=Harness(tmp_path,[slow,'尚未确认。','完成。'])
        try:
            first=asyncio.create_task(h.submit('创建测试会议'))
            assert await asyncio.to_thread(entered.wait,10)
            confirm=asyncio.create_task(h.submit('1',key='premature'))
            await asyncio.sleep(.05);release.set();await asyncio.gather(first,confirm)
            assert not h.native.writes and len(h.sent)==2 and '没有可确认' in h.sent[0]
            assert h.sent[1]==h.runtime.business.pending(h.session)['text']
            await h.submit('1',key='delivered-confirm')
            assert len(h.native.writes)==1
        finally:release.set();await h.close()
    asyncio.run(run())


def test_native_skill_and_only_expected_tools_are_available(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('read',{'name':'baidu-ai-map'},name='skill'),'已读取地图使用说明。'])
        try:
            await h.submit('先查看地图功能的使用方法')
            names={x['name'] for x in h.model.requests[0]['tools']}
            assert names=={'personalPlan','ask_user','baidu_maps','todo_write','skill','user_memory','web_search','web_fetch','session_event_search','session_event_read','schedule_create','schedule_list','schedule_update','schedule_delete','bash','read','read_image'}
            first=json.dumps(h.model.requests[0],ensure_ascii=False)
            last=json.dumps(h.model.requests[-1],ensure_ascii=False)
            assert 'baidu-ai-map' in first and '<skill_content name=' not in first
            assert '<skill_content name=' in last and 'baidu_maps(operation, parameters)' in last
            assert not h.native.writes
            # A new native process restores loaded Skill content from its own session history.
            await h.runtime.close();h.runtime=h.restart();h.model.script.append('说明仍在会话中。')
            await h.submit('刚才读了什么？')
            assert '<skill_content name=' in json.dumps(h.model.requests[-1],ensure_ascii=False)
        finally:await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('name',['missing-skill','../../private.env'])
def test_native_skill_unknown_or_path_cannot_read_arbitrary_files(tmp_path,name):
    async def run():
        h=Harness(tmp_path,[call('missing',{'name':name},name='skill'),'没有该技能。'])
        try:
            await h.submit('读取地图技能')
            result=next(iter(unique_events(h,'tool/result')))['data']['message']
            assert result.get('isError') and not h.native.writes
        finally:await h.close()
    asyncio.run(run())


def test_concurrent_duplicate_confirmations_and_failed_delivery_keep_one_write(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。','保存回执已记录。'])
        try:
            await h.submit('创建测试会议')
            send=h.adapter.send_text
            async def fail(ref,text,**kwargs):return SendResult(ok=False,error='模拟微信失败')
            h.adapter.send_text=fail
            await asyncio.gather(h.submit('1',key='confirm-a'),h.submit('1',key='confirm-b'))
            assert len(h.native.writes)==1
            h.adapter.send_text=send
            await h.runtime.close();h.runtime=h.restart()
            await h.runtime.recover(h.session,h.ref)
            assert sum('已创建成功' in text for text in h.sent)==1
            await h.submit('1',key='confirm-a');await h.submit('1',key='third')
            assert len(h.native.writes)==1
        finally:await h.close()
    asyncio.run(run())


def test_voice_transcription_is_not_confirmation(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('p',draft()),'请确认。','请用文字确认。'])
        try:
            await h.submit('创建测试会议')
            await h.submit('[Voice message received, transcribed below]\n确认')
            assert not h.native.writes
        finally:await h.close()
    asyncio.run(run())
