"""Text receipts survive partial failure and process death without replaying prefixes."""
import asyncio
import pytest
from types import SimpleNamespace
from business import Business
from telos.adapters.base import ConversationRef, SendResult
from telos.adapters.delivery import split_reply_bubbles
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.outbound import WeChatOutbound, create_wechat_send_text_fn

REF = ConversationRef(platform='wechat', chat_id='fixture-user')


def setup(root, reply='第一段\n第二段\n第三段\n第四段'):
    b = Business(root, SimpleNamespace())
    if b.accept('session', 'reply', 'fixture question'):
        b.save_reply('reply', reply)
    return b


def test_confirmed_prefix_survives_restart_and_rejection_reuses_client_id(tmp_path):
    calls = []
    async def post(url, body, headers, timeout):
        calls.append(body['msg'])
        return {'ret': -2, 'errmsg': 'prepare failed'} if len(calls) == 3 else {'ret': 0}
    async def run():
        config = WeChatConfig(token='fixture')
        adapter = WeChatOutbound(config=config, send_text_fn=create_wechat_send_text_fn(config, post_json_fn=post))
        b = setup(tmp_path)
        await b.deliver('session', 'reply', adapter, REF)
        with b.db() as db:
            assert db.execute('SELECT next_part,state FROM text_outbox').fetchone() == (2, 'rejected')
            assert db.execute('SELECT delivered FROM messages').fetchone() == (0,)
        b = setup(tmp_path)
        await b.deliver('session', 'reply', adapter, REF)
        await b.deliver('session', 'reply', adapter, REF)
        assert [c['item_list'][0]['text_item']['text'] for c in calls] == ['第一段', '第二段', '第三段', '第三段', '第四段']
        assert calls[2]['client_id'] == calls[3]['client_id']
        assert len({c['client_id'] for c in calls}) == 4
        with b.db() as db:
            assert db.execute('SELECT delivered FROM messages').fetchone() == (1,)
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['timeout', 'cancel', 'unknown_result'])
def test_ambiguous_send_never_replays_on_restart(tmp_path, kind):
    calls = []
    async def send(ref, text):
        calls.append(text)
        if len(calls) == 2:
            if kind == 'timeout': raise TimeoutError()
            if kind == 'cancel': raise asyncio.CancelledError()
            return SendResult(ok=False, error='unknown')
        return SendResult(ok=True)
    async def run():
        adapter = WeChatOutbound(config=WeChatConfig(), send_text_fn=send)
        b = setup(tmp_path)
        if kind == 'cancel':
            with pytest.raises(asyncio.CancelledError): await b.deliver('session', 'reply', adapter, REF)
        else: await b.deliver('session', 'reply', adapter, REF)
        b = setup(tmp_path)
        await b.deliver('session', 'reply', adapter, REF)
        assert calls == ['第一段', '第二段']
        with b.db() as db:
            assert db.execute('SELECT next_part FROM text_outbox').fetchone() == (1,)
            assert db.execute('SELECT delivered FROM messages').fetchone() == (0,)
    asyncio.run(run())


def test_old_incomplete_reply_is_preserved_but_not_replayed(tmp_path):
    b = setup(tmp_path)
    # A legacy database has no per-part receipts, even if its reply is complete.
    with b.db() as db: db.execute('DROP TABLE text_outbox')
    b = setup(tmp_path)
    async def send(*args, **kwargs): pytest.fail('legacy reply must not be replayed')
    adapter = WeChatOutbound(config=WeChatConfig(), send_text_fn=send)
    asyncio.run(b.deliver('session', 'reply', adapter, REF))
    with b.db() as db:
        assert db.execute('SELECT state FROM text_outbox').fetchone() == ('legacy_unknown',)
        assert db.execute('SELECT reply,delivered FROM messages').fetchone() == ('第一段\n第二段\n第三段\n第四段', 0)


def test_changed_reply_and_concurrent_duplicate_do_not_repeat_sent_parts(tmp_path):
    calls = []
    async def send(ref, text):
        calls.append(text)
        await asyncio.sleep(0)
        return SendResult(ok=True)
    async def run():
        b = setup(tmp_path)
        adapter = WeChatOutbound(config=WeChatConfig(), send_text_fn=send)
        await asyncio.gather(*(b.deliver('session', 'reply', adapter, REF) for _ in range(2)))
        assert calls == ['第一段', '第二段', '第三段', '第四段']
        with b.db() as db: db.execute("UPDATE messages SET reply='changed',delivered=0 WHERE id='reply'")
        await b.deliver('session', 'reply', adapter, REF)
        assert len(calls) == 4
    asyncio.run(run())


def test_note_title_summary_and_url_stay_together_without_changing_links():
    cards = [f'**{n} 餐厅推荐**\n这是一段摘要。\nhttps://www.xiaohongshu.com/explore/{n}?xsec_token=keep%3D&xsec_source=pc_search' for n in range(4)]
    text = '\n\n'.join(cards + ['以上供参考。'])
    assert split_reply_bubbles(text) == cards + ['以上供参考。']
    assert split_reply_bubbles('普通聊天。\n下一句话。') == ['普通聊天。', '下一句话。']
