"""Direct write, durable undo and existing approval boundaries."""
import asyncio
from copy import deepcopy
import json
import pytest
from business import Business
from settings import runtime_options, read_config, write_config
from support import Apple, draft
from telos.tools.apple.service import AppleService, operation_key, revision
from telos.tools.apple.direct import direct_allowed
from channel import Channel


def run(fn):
    def test(tmp_path):asyncio.run(fn(tmp_path))
    return test


def setup(root, config=None, native=None):
    native=native or Apple()
    b=Business(root,AppleService(native,root/'apple'),config)
    active(b,'m1')
    return b,native


def active(b,key,session='s'):
    b.accept(session,key,'用户要求新增会议')
    b.active[session]={'key':key,'text':'用户要求新增会议'}


@run
async def test_default_direct_dedup_restart_undo_and_isolation(tmp_path):
    assert runtime_options({})['direct_write'] is True
    b,n=setup(tmp_path)
    r=await b.execute('s','a','personalPlan',draft())
    assert r['saved'] and not b.pending('s') and '回复【1】撤销' in r['text']
    assert (await b.execute('s','a','personalPlan',draft()))['saved']
    assert (await b.execute('s','different-id','personalPlan',draft()))['saved']
    assert len(n.writes)==1
    other=await b.undo('other',r['undo_id'][-12:]);assert other['status']=='deny'
    restarted,_=setup(tmp_path,native=n)
    assert (await restarted.undo('s'))['undone']
    assert (await restarted.undo('s'))['already_undone']
    assert len(n.writes)==2 and len(n.items)==1


@run
async def test_update_undo_conflict_and_no_older_fallback(tmp_path):
    b,n=setup(tmp_path)
    original=deepcopy(n.items[0]);item=b.apple.detail('reminder',original['id'])
    r=await b.execute('s','a','personalPlan',{'action':'update','kind':'reminder','id':item['id'],
        'expected_revision':item['revision'],'fields':{'title':'新标题','due':'2027-01-02'}})
    assert r['saved']
    assert (await b.undo('s'))['undone']
    assert n.items[0]==original
    active(b,'m2');r=await b.execute('s','b','personalPlan',draft())
    n.items[-1]['notes']='用户从手机修改'
    assert (await b.undo('s'))['status']=='deny'
    assert n.items[-1]['notes']=='用户从手机修改'
    assert len(n.writes)==3


@run
async def test_off_explicit_confirmation_batch_delete_and_scheduled(tmp_path):
    b,n=setup(tmp_path,{'direct_write':False})
    r=await b.execute('s','a','personalPlan',draft());assert r['status']=='awaiting_approval' and not n.writes
    p=b.pending('s');p['delivered']=True;b.set_pending('s',p);active(b,'confirm')
    assert (await b.confirm('s'))['saved'];assert len(n.writes)==1
    for index,args in enumerate([{**draft(),'require_confirmation':True},
            {'action':'batch','operations':[draft()]},
            {'action':'delete','kind':'reminder','id':'reminder-001','expected_revision':b.apple.detail('reminder','reminder-001')['revision']} ]):
        b.direct_write=True;b.set_pending('s',None);active(b,'check'+str(index))
        assert (await b.execute('s','c'+str(index),'personalPlan',args))['status']=='awaiting_approval'
    b.set_pending('s',None);active(b,'timer');b.active['s']['scheduled']=True
    assert (await b.execute('s','timer','personalPlan',draft('定时内容')))['status']=='awaiting_approval'
    assert len(n.writes)==1


@run
async def test_one_direct_write_per_message_and_unknown_blocks(tmp_path):
    b,n=setup(tmp_path)
    assert (await b.execute('s','a','personalPlan',draft()))['saved']
    assert (await b.execute('s','b','personalPlan',draft('另一个会议')))['status']=='awaiting_approval'
    assert len(n.writes)==1
    b.set_pending('s',None);active(b,'m2');n.fail_at=2
    r=await b.execute('s','c','personalPlan',draft('失败'))
    assert r['status']=='failed' and not r.get('undo_id')
    assert (await b.execute('s','d','personalPlan',draft('再试')))['status']=='write_uncertain'
    assert len(n.writes)==2


@run
async def test_crash_after_native_save_reconciles_receipt_without_replay(tmp_path):
    b,n=setup(tmp_path)
    # Interrupt after a successful native write but before the business receipt commit.
    def crash(*args):raise asyncio.CancelledError()
    b.direct_receipt=crash
    with pytest.raises(asyncio.CancelledError):await b.execute('s','a','personalPlan',draft())
    pending=b.pending('s');assert pending['executing']
    b,_=setup(tmp_path,native=n)
    r=await b.reconcile('s',pending)
    assert r['saved'] and r['undo_id'] and len(n.writes)==1
    assert (await b.undo('s'))['undone']
    assert len(n.writes)==2


@run
async def test_explicit_undo_routes_without_model_and_cancel_is_truthful(tmp_path):
    write_config(tmp_path,read_config(tmp_path))
    n=Apple();c=Channel(tmp_path,AppleService(n,tmp_path/'apple'),None)
    b=c.business
    try:
        active(b,'m1');r=await b.execute('s','a','personalPlan',draft())
        active(b,'undo');b.save_reply('undo',None)
        with b.db() as db:db.execute('UPDATE messages SET input=? WHERE id=?',('撤销 '+r['undo_id'][-12:],'undo'))
        await c.prepare('s',[{'id':'undo','hasMedia':False}])
        assert b.active['s']['receipt']['control'] and len(n.writes)==2
        with pytest.raises(ValueError):await b.execute('s','bad','personalPlan',draft())
        active(b,'cancel')
        with b.db() as db:db.execute('UPDATE messages SET input=? WHERE id=?',('取消','cancel'))
        await c.prepare('s',[{'id':'cancel','hasMedia':False}])
        assert '没有待取消' in b.active['s']['receipt']['text']
    finally:await c.close()


@pytest.mark.parametrize('config',[{'direct_write':'false'},{'direct_write':1},{'direct_write':None}])
def test_invalid_mode(config):
    with pytest.raises(ValueError):runtime_options(config)


@pytest.mark.parametrize('change',[
    {'action':'delete'}, {'action':'batch'}, {'kind':'list'},
    {'fields':{'attachments':[]}}, {'fields':{'recurrence':None}},
    {'current':{'recurring':True}}, {'current':{'attendees':[{'name':'他人'}]}},
    {'current':{'subtask_ids':['child']}},
])
def test_protected_policy(change):
    assert not direct_allowed({'action':'update','kind':'reminder','fields':{'title':'改名'},**change})

@run
async def test_reused_tool_id_cannot_destroy_saved_receipt(tmp_path):
    b,n=setup(tmp_path)
    await b.execute('s','same','personalPlan',draft())
    with pytest.raises(ValueError):await b.execute('s','same','personalPlan',draft('不同请求'))
    assert b.lookup('s','same').result['undo_id']
    active(b,'another-message')
    with pytest.raises(ValueError):await b.execute('s','same','personalPlan',draft())
    assert len(n.writes)==1 and (await b.undo('s'))['undone']

@run
async def test_crash_during_undo_recovery_never_replays(tmp_path):
    b,n=setup(tmp_path)
    await b.execute('s','a','personalPlan',draft())
    active(b,'undo')
    original=b.apple.undo
    def crash(op):
        original(op)
        raise asyncio.CancelledError()
    b.apple.undo=crash
    with pytest.raises(asyncio.CancelledError):await b.undo('s')
    pending=b.pending('s');assert pending['undo_id']
    b,_=setup(tmp_path,native=n)
    r=await b.reconcile('s',pending)
    assert '撤销成功' in r['text'] and len(n.writes)==2
    assert (await b.undo('s'))['already_undone']

@run
async def test_unknown_undo_is_not_replayed(tmp_path):
    b,n=setup(tmp_path)
    await b.execute('s','a','personalPlan',draft())
    active(b,'undo');n.fail_at=2
    assert (await b.undo('s'))['status']=='deny'
    assert (await b.undo('s'))['status']=='deny'
    active(b,'new')
    assert (await b.execute('s','b','personalPlan',draft('另一个')))['status']=='write_uncertain'
    assert len(n.writes)==2

@run
async def test_direct_receipt_delivery_retry_does_not_repeat_write(tmp_path):
    from telos.adapters.base import SendResult
    b,n=setup(tmp_path)
    result=await b.execute('s','a','personalPlan',draft())
    class Adapter:
        ok=False
        texts=[]
        async def send_text(self,ref,text,**kwargs):
            self.texts.append(text)
            return SendResult(ok=self.ok)
    adapter=Adapter()
    await b.deliver('s','m1',adapter,None)
    b,_=setup(tmp_path,native=n);adapter.ok=True
    await b.deliver('s','m1',adapter,None)
    await b.deliver('s','m1',adapter,None)
    assert adapter.texts==[result['text'],result['text']] and len(n.writes)==1

@run
async def test_saved_card_is_one_bubble_and_one_undo_survives_restart(tmp_path):
    from telos.adapters.base import ConversationRef, SendResult
    from telos.adapters.wechat.outbound import WeChatOutbound
    n=Apple();c=Channel(tmp_path,AppleService(n,tmp_path/'apple'),None)
    sent=[]
    async def send(ref,text):
        sent.append(text);return SendResult(ok=True)
    from telos.adapters.wechat.config import WeChatConfig
    adapter=WeChatOutbound(config=WeChatConfig(),send_text_fn=send)
    ref=ConversationRef(platform='wechat',chat_id='user')
    try:
        b=c.business;active(b,'create')
        result=await b.execute('s','a','personalPlan',draft())
        text=b.render('s',result['text'])
        assert text==result['text'] and '**已保存**' in text and '```text' in text
        assert '回复【1】撤销；需要修改时直接回复修改内容' in text
        assert result['undo_id'][-12:] not in text
        assert not b.latest_card_undo('s')  # Never act on an unseen card.
        b.save_reply('create',text)
        await b.deliver('s','create',adapter,ref)
        assert sent==[text] and b.latest_card_undo('s')==result['undo_id']
        c.business=Business(tmp_path,AppleService(n,tmp_path/'apple'))
        b=c.business
        b.accept('s','one','1')
        await c.prepare('s',[{'id':'one','hasMedia':False}])
        assert b.active['s']['receipt']['control'] and len(n.writes)==2
        await b.deliver('s','one',adapter,ref)
        b.accept('s','again','1')
        await c.prepare('s',[{'id':'again','hasMedia':False}])
        assert b.active['s']['receipt']['status']=='deny' and len(n.writes)==2
        assert not b.latest_card_undo('other')
    finally:await c.close()

@run
async def test_update_and_query_answer_survive_render_delivery_and_restart(tmp_path):
    from telos.adapters.base import ConversationRef, SendResult
    from telos.adapters.wechat.outbound import WeChatOutbound
    from telos.adapters.wechat.config import WeChatConfig
    b,n=setup(tmp_path)
    b.active['s']['text']='把健身移到明天相同时间吧，明天应该没什么安排吧？'
    item=b.apple.detail('reminder','reminder-001')
    await b.execute('s','query','personalPlan',{'action':'query','kind':'event',
        'start':'2027-01-02T00:00:00+08:00','end':'2027-01-03T00:00:00+08:00'})
    result=await b.execute('s','update','personalPlan',{'action':'update','kind':'reminder',
        'id':item['id'],'expected_revision':item['revision'],'fields':{'due':'2027-01-02'}})
    card=result['text'];answer='明天日历里暂时没有其他日程。'
    for model_text in (answer,card+'\n'+answer,answer+'\n'+card):
        text=b.render('s',model_text)
        assert text.count(card)==1 and text.count(answer)==1
    assert b.render('s','')==card  # Tool receipt still survives an empty model reply.
    b.save_reply('m1',b.render('s',answer))
    # The durable outbox retains both the card and answer across restart.
    b=Business(tmp_path,AppleService(n,tmp_path/'apple'))
    sent=[]
    async def send(ref,text):sent.append(text);return SendResult(ok=True)
    adapter=WeChatOutbound(config=WeChatConfig(),send_text_fn=send)
    await b.deliver('s','m1',adapter,ConversationRef(platform='wechat',chat_id='user'))
    assert sent==[card,answer] and len(n.writes)==1
    assert b.latest_card_undo('s')==result['undo_id']
    active(b,'undo')
    assert (await b.undo('s',b.latest_card_undo('s')))['undone']
    assert n.items[0]['due']==item['due']

@run
async def test_one_keeps_pending_confirmation_and_does_not_undo_older_card(tmp_path):
    from telos.adapters.base import SendResult
    class Adapter:
        async def send_text(self,*args,**kwargs):return SendResult(ok=True)
    n=Apple();c=Channel(tmp_path,AppleService(n,tmp_path/'apple'),Adapter())
    try:
        b=c.business;active(b,'create')
        await b.execute('s','a','personalPlan',draft())
        await b.deliver('s','create',c.adapter,None)
        active(b,'preview')
        await b.execute('s','b','personalPlan',{**draft('第二个'),'require_confirmation':True})
        b.save_reply('preview',b.render('s','请确认'))
        await b.deliver('s','preview',c.adapter,None)
        b.accept('s','one','1')
        await c.prepare('s',[{'id':'one','hasMedia':False}])
        assert len(n.writes)==2 and all(w[0]=='create' for w in n.writes)
        assert not b.active['s']['receipt']['control']
        await b.deliver('s','one',c.adapter,None)
        b.accept('s','again','1')
        await c.prepare('s',[{'id':'again','hasMedia':False}])
        assert b.active['s']['receipt']['status']=='deny' and len(n.writes)==2
    finally:await c.close()
