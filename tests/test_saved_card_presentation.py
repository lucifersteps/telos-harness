"""Saved card echoes are removed without losing other answers or authorizations."""
import asyncio

import pytest

from business import SAVED_UNDO_INSTRUCTION, SAVED_PENDING_INSTRUCTION
from receipt_presentation import strip_saved_card_echoes
from support import Harness, call, draft
from test_direct_write import setup, active


ANSWER = '明天日历里暂时没有其他日程。'


def echoed(card, style):
    if style == 'duplicate':
        return card + '\n\n' + card
    if style == 'no-heading':
        return card.removeprefix('**已保存**\n')
    if style == 'plain':
        return card.replace('**已保存**\n', '').replace('```text\n', '').replace('```\n', '')
    if style == 'markdown':
        lines = card.replace('**已保存**', '### 已保存').replace('```text', '~~~text').replace('```', '~~~').splitlines()
        return '\n'.join('- **' + line.replace('：', '：** ', 1) if '：' in line else line for line in lines)
    if style == 'pending-footer':
        return card.replace(SAVED_UNDO_INSTRUCTION, SAVED_PENDING_INSTRUCTION)
    raise AssertionError(style)


@pytest.mark.parametrize('style', ['duplicate', 'no-heading', 'plain', 'markdown', 'pending-footer'])
def test_current_saved_card_is_canonical_once_with_independent_answer(tmp_path, style):
    async def run():
        b, native = setup(tmp_path)
        result = await b.execute('s', 'saved', 'personalPlan', draft())
        card = result['text']
        answer = ANSWER + '\n\n' + echoed(card, style)
        rendered = b.render('s', answer)
        assert rendered == card + '\n\n' + ANSWER
        assert rendered.count('标题：测试会议') == 1
        assert rendered.count(SAVED_UNDO_INSTRUCTION) == 1
        assert b.render('s', rendered) == rendered
        assert len(native.writes) == 1
        # Receipt from an earlier spliced input is owned by the same renderer.
        active(b, 'next-input')
        assert b.render('s', answer, receipts=[card]) == rendered
    asyncio.run(run())


def test_other_objects_code_and_pending_preview_are_preserved(tmp_path):
    async def run():
        b, _ = setup(tmp_path)
        card = (await b.execute('s', 'saved', 'personalPlan', draft()))['text']
        changed = card.replace('标题：测试会议', '标题：另一个会议')
        unrelated = '```python\nprint("标题：测试会议")\n```'
        assert strip_saved_card_echoes(changed + '\n\n' + unrelated, [card]) == changed + '\n\n' + unrelated
        preview = card.replace('**已保存**', '**创建 Apple 日程**').replace(SAVED_UNDO_INSTRUCTION, '回复【1】确认，回复【2】取消')
        assert strip_saved_card_echoes(echoed(card, 'no-heading') + '\n\n' + preview,
                                      [card], protected=[preview]).strip() == preview
        surrounded = ANSWER + '\n\n' + preview + '\n\n独立补充。'
        assert strip_saved_card_echoes(surrounded, [card], protected=[preview]) == surrounded
        assert strip_saved_card_echoes(card, []) == card
    asyncio.run(run())


def test_native_missing_heading_card_delivered_once_and_undo_still_works(tmp_path):
    async def run():
        h = Harness(tmp_path, [])
        h.runtime.business.direct_write = True
        card = None
        def reply(_request):
            nonlocal card
            card = h.runtime.business.lookup(h.session, 'saved').result['text']
            return ANSWER + '\n\n刚才那条「测试会议」也保存了：\n' + echoed(card, 'no-heading')
        h.model.script = [call('saved', draft()), reply]
        try:
            await h.submit('创建测试会议并说明明天其他安排', key='saved-input')
            assert h.sent == [card, ANSWER]
            assert len(h.native.writes) == 1
            assert h.runtime.business.latest_card_undo(h.session)
            await h.runtime.recover(h.session, h.ref)
            assert h.sent == [card, ANSWER]
            h.model.script = ['已处理撤销。']
            await h.submit('1', key='undo-input')
            assert [action for action, _ in h.native.writes] == ['create', 'delete']
            assert h.sent[-1] == '已撤销这次保存。'
        finally:
            await h.close()
    asyncio.run(run())
