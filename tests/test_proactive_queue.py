"""Exercise failure recovery in the real host loop, with fake model/Apple/WeChat."""
import asyncio
import json
import time

from support import Harness, call
from test_proactive import configured
from test_schedule import wait


def test_model_failure_drains_valid_check_past_expired_head_without_next_tick(tmp_path):
    async def run():
        h=Harness(tmp_path,['绑定成功。'])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            await h.submit('绑定测试会话')
            await h.runtime.close()
            _,revision,_=store.sync();due=time.time()+1
            with store.db() as db:
                failed=store._insert(db,h.session,revision,'heartbeat',due,'FIRST_MODEL_FAILURE')
                expired=store._insert(db,h.session,revision,'followup',due+.1,'EXPIRED_MUST_NOT_REACH_MODEL')
                valid=store._insert(db,h.session,revision,'heartbeat',due+.2,'VALID_AFTER_FAILURE')
                db.execute('UPDATE proactive_events SET expires=? WHERE id=?',(due+1,expired))
            # The first request stays in flight while the other two are queued.
            def fail(_request):
                time.sleep(1.2)
                return {'_http_status':503}
            h.model.script=[fail]*6+[
                call('read-after-error',{'action':'query','kind':'reminder'}),
                call('notify-after-error',{'message':'失败后仍有效的检查已恢复。'},'notify_user'),
                '后台总结不能发送。']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:'失败后仍有效的检查已恢复。' in h.sent,timeout=45)
            await wait(lambda:store.event(valid,h.session)['state']=='sent')
            assert store.event(failed,h.session)['state']=='failed'
            assert store.event(expired,h.session)['state']=='expired'
            assert h.sent==['绑定成功。','失败后仍有效的检查已恢复。']
            assert not h.native.writes
            # Exactly six failed attempts and three successful continuations.
            # The transport-only wake marker never causes another model call.
            await asyncio.sleep(.3)
            assert len(h.model.requests)==10
            assert 'VALID_AFTER_FAILURE' in json.dumps(h.model.requests[-1])
            leaks=[]
            for i,r in enumerate(h.model.requests):
                wire=json.dumps(r['messages']);pos=wire.find('telos-proactive-queue-wake')
                if pos>=0:leaks.append((i,wire[max(0,pos-200):pos+200]))
            assert not leaks,leaks
            await h.runtime.close();h.runtime=h.restart();await h.runtime.start()
            await asyncio.sleep(.3)
            assert len(h.model.requests)==10 and h.sent.count('失败后仍有效的检查已恢复。')==1
        finally:await h.close()
    asyncio.run(run())


def test_expiry_during_tool_continuation_does_not_poison_next_check(tmp_path):
    async def run():
        h=Harness(tmp_path,['绑定成功。']);store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            await h.submit('绑定');await h.runtime.close()
            _,revision,_=store.sync();due=time.time()+1
            with store.db() as db:
                first=store._insert(db,h.session,revision,'followup',due,'EXPIRE_DURING_TOOL')
                second=store._insert(db,h.session,revision,'heartbeat',due+.1,'NEXT_VALID_CHECK')
            def slow(_request):
                time.sleep(.5)
                with store.db() as db:db.execute('UPDATE proactive_events SET expires=? WHERE id=?',(time.time()-1,first))
                return call('read-before-expiry',{'action':'query','kind':'reminder'})
            h.model.script=[slow,call('next-notify',{'message':'下一条正常运行。'},'notify_user'),'内部结束。']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:'下一条正常运行。' in h.sent)
            await wait(lambda:store.event(second,h.session)['state']=='sent')
            assert store.event(first,h.session)['state']=='expired'
            assert h.sent==['绑定成功。','下一条正常运行。'] and not h.native.writes
        finally:await h.close()
    asyncio.run(run())


def test_recovery_respects_pause_and_does_not_retry_failed_message(tmp_path):
    async def run():
        h=Harness(tmp_path,['绑定成功。']);store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            await h.submit('绑定');await h.runtime.close()
            _,revision,_=store.sync();due=time.time()+1
            with store.db() as db:
                first=store._insert(db,h.session,revision,'heartbeat',due,'FAIL_AND_PAUSE')
                second=store._insert(db,h.session,revision,'followup',due+.1,'MUST_NOT_SEND_WHILE_PAUSED')
            def fail_and_pause(_request):
                time.sleep(.5);store.control(h.session,'pause')
                return {'_http_status':401}
            h.model.script=[fail_and_pause,call('forbidden-notify',{'message':'不能发。'},'notify_user')]
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:store.event(second,h.session)['state']=='cancelled')
            await asyncio.sleep(1)
            assert len(h.model.requests)==2 and h.sent==['绑定成功。']
            assert store.event(first,h.session)['state']=='cancelled'
        finally:await h.close()
    asyncio.run(run())
