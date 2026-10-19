"""Only actual assistant output and business receipts reach the conversation."""
import asyncio
import pytest
from support import Harness, call, draft


@pytest.mark.parametrize('question',[False,True])
def test_pending_presentation_has_one_owner(tmp_path,question):
    async def run():
        tool=call('ask',{'text':'请问是哪座城市？'},name='ask_user') if question else call('preview',draft())
        paraphrase='你说的是哪个城市呢？' if question else '现在是完整预览：创建测试会议。回复【1】确认保存。'
        h=Harness(tmp_path,[tool,paraphrase])
        try:
            await h.submit('安排会议')
            b=h.runtime.business;p=b.pending(h.session)
            presentation=p.get('text') or p['question']
            assert h.sent==[presentation]
            if not question:assert p['delivered']
            # A native final answer can include independent results alongside the
            # exact tool presentation; no output tags or extra protocol are needed.
            extra='另外，明天的天气是晴天。'
            assert b.render(h.session,extra+'\n\n'+presentation)==extra+'\n\n'+presentation
            assert b.render(h.session,presentation)==presentation
            assert b.render(h.session,presentation+'\n'+presentation)==presentation
            assert b.render(h.session,'')==presentation
            b.set_pending(h.session,None)
            assert b.render(h.session,'普通回答')=='普通回答'
        finally:await h.close()
    asyncio.run(run())


def test_partial_answer_survives_native_error(tmp_path):
    async def run():
        h=Harness(tmp_path,[{'content':'我先核对了当前提醒事项。',**call('q',{'action':'query','kind':'reminder'})}])
        h.runtime.env['TELOS_MAX_STEPS']='1'
        try:
            await h.submit('核对提醒事项后继续')
            assert h.sent==['我先核对了当前提醒事项。']
            assert any(e['type']=='turn/end' and e['data']['reason']['kind']=='error' for e in h.events)
        finally:await h.close()
    asyncio.run(run())


def test_preview_is_delivered_without_synthetic_failure_text(tmp_path):
    async def run():
        h=Harness(tmp_path,[call('p',draft())]);h.runtime.env['TELOS_MAX_STEPS']='1'
        try:
            await h.submit('创建会议')
            pending=h.runtime.business.pending(h.session)
            assert pending['delivered']
            assert h.sent==[pending['text']]
        finally:await h.close()
    asyncio.run(run())


def test_empty_output_and_recovery_do_not_send_status_messages(tmp_path):
    async def run():
        # DSH can retry an empty model response; keep every test response empty.
        h=Harness(tmp_path,['']*30);h.runtime.env['TELOS_MAX_STEPS']='1'
        try:
            await h.submit('你好')
            assert not h.sent
            h.runtime.business.accept(h.session,'crashed','未处理的输入')
            await h.runtime.recover(h.session,h.ref)
            assert not h.sent
            with h.runtime.business.db() as db:
                assert db.execute('SELECT reply,delivered FROM messages WHERE id=?',('crashed',)).fetchone()==('',1)
        finally:await h.close()
    asyncio.run(run())
