"""Preview feedback retains drafts without reviving a superseded authorization.

Uses isolated Business/Channel state, a fake Apple backend and the real WeChat
text splitter. No real model, WeChat account, Apple data or installed state.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from channel import Channel
from support import Apple, draft
from telos.adapters.base import ConversationRef, SendResult
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.outbound import WeChatOutbound
from telos.agent.tools.types import ToolInputError
from telos.tools.apple.service import AppleService


SESSION = 'preview-feedback-test'


@asynccontextmanager
async def local_channel(root):
    sent = []
    gate = SimpleNamespace(block=False, entered=asyncio.Event(), release=asyncio.Event())

    async def send(ref, text):
        if gate.block:
            gate.block = False
            gate.entered.set()
            await gate.release.wait()
        sent.append(text)
        return SendResult(ok=True)

    native = Apple()
    outbound = WeChatOutbound(config=WeChatConfig(), send_text_fn=send)
    channel = Channel(root, AppleService(native, root / 'apple'), outbound)
    h = SimpleNamespace(channel=channel, b=channel.business, native=native, sent=sent, gate=gate,
                        ref=ConversationRef(platform='wechat', chat_id='test', user_id='test'))
    try:
        yield h
    finally:
        gate.release.set()
        await channel.close()


async def message(h, key, text):
    assert h.b.accept(SESSION, key, text)
    await h.channel.prepare(SESSION, [{'id': key, 'hasMedia': False}])


async def present(h, key, text):
    rendered = h.b.render(SESSION, text)
    h.b.save_reply(key, rendered)
    await h.b.deliver(SESSION, key, h.channel.adapter, h.ref)
    return rendered


async def initial_preview(h):
    assert h.b.direct_write is True
    await message(h, 'initial', '先预览这项测试会议。')
    result = await h.b.execute(SESSION, 'initial-draft', 'personalPlan',
                               {**draft(), 'require_confirmation': True})
    assert result['status'] == 'awaiting_approval'
    await present(h, 'initial', result['text'])
    pending = h.b.pending(SESSION)
    assert pending['delivered'] and not h.native.writes
    assert h.sent == [pending['text']]
    return deepcopy(pending)


def assert_suspended(h, original):
    pending = h.b.pending(SESSION)
    assert pending is not None and pending['kind'] == 'approval'
    assert pending['id'] == original['id'] and pending['origin'] == original['origin']
    assert pending['args'] == original['args'] and pending['preview'] == original['preview']
    assert pending['expires_at'] == original['expires_at']
    assert pending['delivered'] is False


def test_format_feedback_empty_revision_and_confirmation_keep_one_preview(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            await message(h, 'feedback', '怎么两项粘在一起了？请重新展示。')
            assert_suspended(h, original)
            stale = h.b.render(SESSION, original['text'])
            assert original['text'] not in stale and '回复【1】确认' not in stale
            result = await h.b.execute(SESSION, 'refreshed-draft', 'personalPlan', {
                'action': 'revise', 'draft_call_id': original['id'],
                'request_mode': 'continue', 'fields': {},
            })
            assert result['status'] == 'awaiting_approval' and not h.native.writes
            current = h.b.pending(SESSION)
            assert current['id'] == 'refreshed-draft' and current['origin'] == 'feedback'
            assert current['preview']['fields'] == original['preview']['fields']
            assert current['delivered'] is False
            await present(h, 'feedback', result['text'])
            assert h.b.pending(SESSION)['delivered'] is True
            assert h.sent[-1] == current['text']
            await message(h, 'confirm', '1')
            assert len(h.native.writes) == 1 and h.native.writes[0][0] == 'create'
            assert h.b.pending(SESSION) is None
    asyncio.run(run())


@pytest.mark.parametrize('failed_revision', [False, True])
def test_feedback_or_failed_revision_cannot_reactivate_old_preview(tmp_path, failed_revision):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            await message(h, 'feedback', '请解释一下格式。' if not failed_revision else '把会议时间修改一下。')
            assert_suspended(h, original)
            if failed_revision:
                with pytest.raises(ToolInputError):
                    await h.b.execute(SESSION, 'bad-revision', 'personalPlan', {
                        'action': 'revise', 'draft_call_id': original['id'], 'request_mode': 'continue',
                        'fields': {'start': '2027-02-19T18:00:00+08:00', 'end': '2027-02-19T17:00:00+08:00'},
                    })
                assert_suspended(h, original)
                rendered = await present(h, 'feedback', '修改没有完成。\n' + original['text'])
                assert original['text'] not in rendered and '回复【1】确认' not in rendered
            else:
                explanation = '保存回执与待确认预览应当分开发送。'
                assert await present(h, 'feedback', explanation) == explanation
                assert original['text'] not in h.b.render(SESSION, original['text'])
            assert_suspended(h, original)
            assert h.b.lookup(SESSION, original['id']) is not None
            await message(h, 'confirm', '1')
            assert h.b.active[SESSION]['receipt']['status'] == 'deny'
            assert_suspended(h, original)
            assert not h.native.writes
    asyncio.run(run())


def test_undelivered_old_preview_cannot_appear_as_first_delivery_after_failed_feedback(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            await message(h, 'initial', '先预览这项测试会议。')
            result = await h.b.execute(SESSION, 'initial-draft', 'personalPlan',
                                      {**draft(), 'require_confirmation': True})
            assert result['status'] == 'awaiting_approval'
            h.b.save_reply('initial', h.b.render(SESSION, result['text']))
            original = deepcopy(h.b.pending(SESSION))
            assert original['delivered'] is False and not h.sent
            await message(h, 'feedback', '把会议开始时间改一下。')
            with pytest.raises(ToolInputError):
                await h.b.execute(SESSION, 'bad-revision', 'personalPlan', {
                    'action': 'revise', 'draft_call_id': original['id'], 'request_mode': 'continue',
                    'fields': {'start': '2027-02-19T18:00:00+08:00', 'end': '2027-02-19T17:00:00+08:00'},
                })
            assert_suspended(h, original)
            with h.b.db() as db:
                assert db.execute('SELECT delivered FROM messages WHERE id=?', ('initial',)).fetchone() == (0,)
            rendered = await present(h, 'feedback', '修改没有完成。\n' + original['text'])
            assert original['text'] not in rendered and '回复【1】确认' not in rendered
            assert_suspended(h, original)
            await message(h, 'confirm', '1')
            assert h.b.active[SESSION]['receipt']['status'] == 'deny'
            assert_suspended(h, original)
            assert not h.native.writes
    asyncio.run(run())


def test_new_write_or_unrelated_revision_cannot_replace_existing_preview(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            await message(h, 'new-request', '另建一个会议。')
            with pytest.raises(ToolInputError):
                await h.b.execute(SESSION, 'new-draft', 'personalPlan', draft('独立新会议'))
            assert_suspended(h, original)
            h.b.record_draft(SESSION, 'unrelated-draft', draft('另一份草稿'), {'status': 'incomplete_draft'})
            with pytest.raises(ToolInputError):
                await h.b.execute(SESSION, 'wrong-revision', 'personalPlan', {
                    'action': 'revise', 'draft_call_id': 'unrelated-draft', 'request_mode': 'continue',
                    'fields': {},
                })
            assert_suspended(h, original)
            assert not h.native.writes
    asyncio.run(run())


def test_incomplete_revision_can_replace_approval_with_a_question(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            await message(h, 'clarify', '开始时间还没有确定。')
            incomplete = await h.b.execute(SESSION, 'incomplete-revision', 'personalPlan', {
                'action': 'revise', 'draft_call_id': original['id'], 'request_mode': 'continue',
                'fields': {}, 'unresolved': ['start'],
            })
            assert incomplete['status'] == 'incomplete_draft'
            assert h.b.pending(SESSION) is None
            assert h.b.lookup(SESSION, original['id']) is not None
            question = await h.b.execute(SESSION, 'question', 'ask_user', {'text': '新的开始时间是什么时候？'})
            assert question['status'] == 'awaiting_input'
            assert h.b.pending(SESSION)['kind'] == 'question'
            await message(h, 'answer', '1')
            assert h.b.pending(SESSION) is None and not h.native.writes
    asyncio.run(run())


def test_expired_suspended_preview_is_not_renewed_by_reprinting(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            original['expires_at'] = time.time() - 1
            h.b.set_pending(SESSION, original)
            await message(h, 'feedback', '再解释一下这张卡。')
            assert_suspended(h, original)
            rendered = await present(h, 'feedback', original['text'])
            assert original['text'] not in rendered
            assert_suspended(h, original)
            await message(h, 'confirm', '1')
            assert '过期' in h.b.active[SESSION]['receipt']['text']
            assert h.b.pending(SESSION) is None and not h.native.writes
    asyncio.run(run())


@pytest.mark.parametrize('changed', ['id', 'origin', 'active_key'])
def test_late_send_ack_does_not_reauthorize_changed_or_suspended_preview(tmp_path, changed):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            await message(h, 'refresh', '重新展示预览。')
            result = await h.b.execute(SESSION, 'refresh-draft', 'personalPlan', {
                'action': 'revise', 'draft_call_id': original['id'], 'request_mode': 'continue', 'fields': {},
            })
            pending = deepcopy(h.b.pending(SESSION))
            assert result['status'] == 'awaiting_approval' and not pending['delivered']
            h.b.save_reply('refresh', h.b.render(SESSION, result['text']))
            h.gate.block = True
            sending = asyncio.create_task(h.b.deliver(SESSION, 'refresh', h.channel.adapter, h.ref))
            try:
                await asyncio.wait_for(h.gate.entered.wait(), 2)
                if changed == 'active_key':
                    await message(h, 'newer-feedback', '这个时间还需要修改。')
                else:
                    replacement = deepcopy(pending)
                    replacement[changed] = 'replacement-' + changed
                    h.b.set_pending(SESSION, replacement)
                h.gate.release.set()
                await asyncio.wait_for(sending, 2)
                current = h.b.pending(SESSION)
                assert current['delivered'] is False
                assert current['expires_at'] == pending['expires_at']
                if changed != 'active_key':
                    assert current[changed] == 'replacement-' + changed
                assert not h.native.writes
            finally:
                h.gate.release.set()
                await asyncio.gather(sending, return_exceptions=True)
    asyncio.run(run())


def test_late_send_ack_does_not_authorize_unprocessed_media(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            await message(h, 'refresh-media', '重新展示这项预览。')
            result = await h.b.execute(SESSION, 'media-refresh-draft', 'personalPlan', {
                'action': 'revise', 'draft_call_id': original['id'],
                'request_mode': 'continue', 'fields': {},
            })
            pending = deepcopy(h.b.pending(SESSION))
            h.b.save_reply('refresh-media', h.b.render(SESSION, result['text']))
            h.gate.block = True
            sending = asyncio.create_task(h.b.deliver(SESSION, 'refresh-media', h.channel.adapter, h.ref))
            try:
                await asyncio.wait_for(h.gate.entered.wait(), 2)
                # Sidecar accepts an image before its asynchronous download and
                # normalization fill input or enter the native prepare boundary.
                assert h.b.accept(SESSION, 'newer-image', '')
                assert h.b.active[SESSION]['key'] == 'refresh-media'
                h.gate.release.set()
                await asyncio.wait_for(sending, 2)
                current = h.b.pending(SESSION)
                assert current['id'] == pending['id'] and current['origin'] == pending['origin']
                assert current['delivered'] is False
                assert current['expires_at'] == pending['expires_at']
                await message(h, 'confirm-before-media', '1')
                assert h.b.active[SESSION]['receipt']['status'] == 'deny'
                assert not h.native.writes
            finally:
                h.gate.release.set()
                await asyncio.gather(sending, return_exceptions=True)
    asyncio.run(run())


def test_executing_undo_without_preview_fields_can_render_and_reconcile(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            await message(h, 'undo-recovery', '核对上次撤销状态。')
            h.b.set_pending(SESSION, {
                'kind': 'approval', 'executing': True, 'undo_id': 'unknown-test-operation',
                'confirmation_key': 'undo-recovery',
            })
            assert h.b.render(SESSION, '正在核对现有记录。') == '正在核对现有记录。'
            result = await h.b.confirm(SESSION)
            assert result.get('text') and h.b.pending(SESSION) is None
            assert not h.native.writes
    asyncio.run(run())


def test_unprocessed_media_suspends_already_delivered_preview_before_confirmation(tmp_path):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            assert original['delivered']
            assert h.b.accept(SESSION, 'media-before-confirm', '')
            # The new image has not entered prepare yet. A later plain numeric
            # reply must not authorize the card that preceded that image.
            assert h.b.active[SESSION]['key'] == 'initial'
            await message(h, 'confirm-after-media', '1')
            assert h.b.active[SESSION]['receipt']['status'] == 'deny'
            assert_suspended(h, original)
            assert not h.native.writes
    asyncio.run(run())


@pytest.mark.parametrize('machine_text', ['后台作业已完成。', '1'])
def test_machine_notice_preserves_delivered_preview_until_human_confirms(tmp_path, machine_text):
    async def run():
        async with local_channel(tmp_path) as h:
            original = await initial_preview(h)
            h.channel.bind_ref(SESSION, h.ref)
            await h.channel.prepare(SESSION, [{
                'id': 'machine-after-preview', 'scheduled': True, 'text': machine_text,
            }])
            assert h.b.pending(SESSION) == original
            assert h.b.active[SESSION]['scheduled'] is True
            assert h.b.active[SESSION]['text'] == machine_text
            with h.b.db() as db:
                row = db.execute('SELECT input,reply,delivered FROM messages WHERE id=?',
                                 ('machine-after-preview',)).fetchone()
            assert row == ('', '', 1)
            assert not h.native.writes
            await message(h, 'human-confirm-after-machine', '1')
            assert len(h.native.writes) == 1 and h.b.pending(SESSION) is None
    asyncio.run(run())


@pytest.mark.parametrize('machine_text', ['后台作业已完成。', '1'])
def test_machine_notice_during_initial_send_allows_ack_but_cannot_confirm(tmp_path, machine_text):
    async def run():
        async with local_channel(tmp_path) as h:
            await message(h, 'initial', '请先预览会议。')
            result = await h.b.execute(SESSION, 'initial-draft', 'personalPlan', {
                **draft(), 'require_confirmation': True,
            })
            pending = deepcopy(h.b.pending(SESSION))
            h.b.save_reply('initial', h.b.render(SESSION, result['text']))
            h.channel.bind_ref(SESSION, h.ref)
            h.gate.block = True
            sending = asyncio.create_task(h.b.deliver(SESSION, 'initial', h.channel.adapter, h.ref))
            try:
                await asyncio.wait_for(h.gate.entered.wait(), 2)
                await h.channel.prepare(SESSION, [{
                    'id': 'machine-during-send', 'scheduled': True, 'text': machine_text,
                }])
                assert h.b.pending(SESSION) == pending and not h.native.writes
                h.gate.release.set()
                await asyncio.wait_for(sending, 2)
                current = h.b.pending(SESSION)
                assert current['delivered'] is True
                assert current['id'] == pending['id'] and current['origin'] == pending['origin']
                assert current['expires_at'] == pending['expires_at']
                assert h.sent == [pending['text']] and not h.native.writes
                await message(h, 'human-confirm-after-machine', '1')
                assert len(h.native.writes) == 1 and h.b.pending(SESSION) is None
            finally:
                h.gate.release.set()
                await asyncio.gather(sending, return_exceptions=True)
    asyncio.run(run())
