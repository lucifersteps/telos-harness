import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from business import Business
from proactive_channel import ProactiveChannel
from scheduled_delivery import ScheduledDelivery
from test_proactive import configured
from test_proactive_observation import enable
from telos.adapters.base import ConversationRef, SendResult
from telos.adapters.wechat.adapter import WeChatAdapter
from wechat_budget import view, totals, NOTICE_PREFIX


class Fixture(ProactiveChannel, ScheduledDelivery):
    def __init__(self, root, used=0):
        self.root=root;self.business=Business(root,SimpleNamespace())
        self.delivery_locks={};self.refs={};self.sent=[];self.reject=False
        async def send(ref,text):
            self.sent.append(text)
            return SendResult(ok=not self.reject,retryable=self.reject)
        self.adapter=WeChatAdapter(send_text_fn=send)
        self.init_scheduled_delivery()
        self.proactive=configured(root,heartbeat=False,fixed=False);enable(root)
        self.session='wechat-test'
        self.bind_ref(self.session,ConversationRef(platform='wechat',chat_id='fixture',user_id='fixture'))
        if self.business.accept(self.session,'inbound','你好'):
            with self.business.db() as db:
                db.execute("UPDATE messages SET reply='reply',delivered=1 WHERE id='inbound'")
                db.execute('INSERT INTO text_outbox VALUES(?,?,?,?,?)',('inbound','hash',json.dumps(['part']*used),used,'complete'))

    def event(self):
        _,rev,_=self.proactive.sync()
        with self.business.db() as db:
            eid=self.proactive._insert(db,self.session,rev,'heartbeat',time.time()-1,'normal check')
            db.execute("UPDATE proactive_events SET task_id=?,input_id=?,state='running' WHERE id=?",('task-'+eid,'input-'+eid,eid))
        return eid

    async def admit(self,eid):
        return await self.proactive_route('/proactive/admit',{'session':self.session,'event_id':eid,'task_id':'task-'+eid,'input_id':'input-'+eid})


@pytest.mark.parametrize('used',[7,8,9,10])
def test_reserves_notice_before_two_bubble_pair(tmp_path,used):
    async def run():
        f=Fixture(tmp_path,used);eid=f.event()
        admitted=await f.admit(eid)
        if used==10:
            assert not admitted['allowed'];assert f.sent==[];return
        context=await f.proactive_route('/proactive/context',{'session':f.session,'inputs':['input-'+eid]})
        notice=used>=8
        assert bool(context['events'][0].get('budget_notice'))==notice
        if notice:
            assert context['policy']['enabled'] is False
            assert 'observation_window' not in context['events'][0]
            with pytest.raises(ValueError):
                await f.notify_proactive(f.session,eid,'input-'+eid,'normal','observation')
        await f.notify_proactive(f.session,eid,'input-'+eid,'微信快把我的小喇叭静音啦🥺\n回我一句，我就能继续陪你～' if notice else '建议',None if notice else '观察')
        await f.drain_scheduled()
        assert len(f.sent)==(1 if notice else 2)
        assert f.proactive.event(eid,f.session)['state']=='sent'
        if notice:
            assert not (await f.admit(f.event()))['allowed']
        else:
            assert (await f.admit(f.event()))['event']['budget_notice']
    asyncio.run(run())


def test_notice_rejection_restart_duplicate_inbound_and_recovery(tmp_path):
    async def run():
        f=Fixture(tmp_path,8);eid=f.event();await f.admit(eid)
        notice='微信的小喇叭快没电啦，回我一句就能续上🥺 这条之后先安静等你～'
        await f.notify_proactive(f.session,eid,'input-'+eid,notice)
        f.reject=True;await f.drain_scheduled()
        assert f.proactive.event(eid,f.session)['state']=='queued'
        f=Fixture(tmp_path,8) # A duplicate inbound/restart cannot replenish quota.
        assert (await f.notify_proactive(f.session,eid,'input-'+eid,'different'))['status']=='already_queued'
        await f.drain_scheduled();await f.drain_scheduled()
        assert f.sent==[notice]
        assert not (await f.admit(f.event()))['allowed']
        f.business.accept(f.session,'new-inbound','我回来啦')
        eid2=f.event();assert (await f.admit(eid2))['allowed']
        with f.business.db() as db:assert view(db,f.session,eid2)['remaining']==10
        await f.notify_proactive(f.session,eid2,'input-'+eid2,'新建议','新观察')
        await f.drain_scheduled();assert len(f.sent)==3
    asyncio.run(run())


def test_new_inbound_cancels_queued_notice(tmp_path):
    async def run():
        f=Fixture(tmp_path,8);eid=f.event();await f.admit(eid)
        await f.notify_proactive(f.session,eid,'input-'+eid,'回我一句就能继续啦')
        f.business.accept(f.session,'new','回来了')
        await f.drain_scheduled()
        assert f.sent==[] and f.proactive.event(eid,f.session)['state']=='skipped'
    asyncio.run(run())


def test_budget_rechecked_before_transport_and_notice_text_varies(tmp_path):
    async def run():
        f=Fixture(tmp_path,7);eid=f.event();await f.admit(eid)
        await f.notify_proactive(f.session,eid,'input-'+eid,'建议','观察')
        # Another confirmed output consumes the normal pair's spare bubble.
        with f.business.db() as db:
            db.execute("UPDATE text_outbox SET parts=?,next_part=8 WHERE key='inbound'",(json.dumps(['part']*8),))
        await f.drain_scheduled();assert not f.sent
        assert f.proactive.event(eid,f.session)['state']=='skipped'
        eid=f.event();await f.admit(eid)
        notice='微信额度快用完啦，回我一句就能继续陪你～'
        await f.notify_proactive(f.session,eid,'input-'+eid,notice);await f.drain_scheduled()
        f.business.accept(f.session,'new','你好')
        with f.business.db() as db:
            db.execute('INSERT INTO text_outbox VALUES(?,?,?,?,?)',('new','hash',json.dumps(['part']*8),8,'complete'))
        next_id=f.event();await f.admit(next_id)
        context=await f.proactive_route('/proactive/context',{'session':f.session,'inputs':['input-'+next_id]})
        assert context['recent_budget_notices']==[notice]
        with pytest.raises(ValueError,match='近期已经发过'):
            await f.notify_proactive(f.session,next_id,'input-'+next_id,notice)
        await f.notify_proactive(f.session,next_id,'input-'+next_id,'微信快把我按静音啦🥺 先乖乖等你，回我一句就能继续聊～')
        await f.drain_scheduled();assert len(f.sent)==2 and f.sent[0]!=f.sent[1]
    asyncio.run(run())


def test_partial_pair_keeps_reserved_second_bubble_before_notice(tmp_path):
    async def run():
        f=Fixture(tmp_path,7);eid=f.event();await f.admit(eid)
        await f.notify_proactive(f.session,eid,'input-'+eid,'建议','观察')
        calls=[]
        async def send(ref,text):
            calls.append(text)
            return SendResult(ok=len(calls)!=2,retryable=len(calls)==2)
        f.adapter=WeChatAdapter(send_text_fn=send)
        await f.drain_scheduled()
        notice=f.event();assert (await f.admit(notice))['event']['budget_notice']
        await f.notify_proactive(f.session,notice,'input-'+notice,'微信额度快用完了，回我一句再继续～')
        await f.drain_scheduled()
        assert len(calls)==4 and calls[1]==calls[2]=='建议'
        assert f.proactive.event(eid,f.session)['state']==f.proactive.event(notice,f.session)['state']=='sent'
    asyncio.run(run())


def test_reservations_and_images_count_once_and_unknown_is_conservative(tmp_path):
    f=Fixture(tmp_path,5)
    with f.business.db() as db:
        db.execute("CREATE TABLE image_receipts(id TEXT PRIMARY KEY,fingerprint TEXT,state TEXT,result TEXT)")
        db.execute('INSERT INTO image_receipts VALUES(?,?,?,?)',('image','hash','caption_unknown',json.dumps({'_session':f.session,'_parts':2,'image':'api_accepted','caption':'unknown'})))
        db.execute("INSERT INTO messages VALUES('partial',?,'','partial',0)",(f.session,))
        db.execute('INSERT INTO text_outbox VALUES(?,?,?,?,?)',('partial','hash',json.dumps(['first','second']),1,'unknown'))
        db.execute('INSERT INTO schedule_outbox(session,seq,key,parts,proactive_id) VALUES(?,?,?,?,?)',(f.session,-1,'partial',json.dumps(['first','second']),'event'))
        assert totals(db,f.session)==(9,0)
        assert view(db,f.session)['remaining']==1
        assert view(db,'other') is None
    f.business.accept(f.session,'new','回来了')
    with f.business.db() as db:assert view(db,f.session)['remaining']==10


def test_native_notice_bypasses_observation_and_plan_queries(tmp_path):
    from support import Harness,call
    from test_schedule import wait
    async def run():
        h=Harness(tmp_path,['\n'.join('普通回复'+str(i) for i in range(8))])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False);enable(tmp_path/'data')
        try:
            await h.submit('绑定测试');assert len(h.sent)==8
            await h.runtime.close();_,rev,_=store.sync()
            with store.db() as db:eid=store._insert(db,h.session,rev,'heartbeat',time.time()+1,'normal check')
            notice='小喇叭快被微信按静音啦🥺 回我一句就能继续聊，这条之后先乖乖等你～'
            h.model.script=[call('notice',{'message':notice},'notify_user'),'内部结束']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:store.event(eid,h.session)['state']=='sent')
            assert h.sent[8:]==[notice]
            wire=json.dumps(h.model.requests[-2],ensure_ascii=False)
            assert 'budget_notice' in wire and '不传 observation' in wire
            assert not h.native.writes
        finally:await h.close()
    asyncio.run(run())
