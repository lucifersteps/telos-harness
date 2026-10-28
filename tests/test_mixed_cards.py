"""Mixed saved/approval cards through native turns and the real WeChat adapter."""
import asyncio

from settings import read_config, write_config
from support import Harness, call, draft
from test_history import results


LISTING = '**处理进度**\n- 测试会议已经保存\n- 删除旧提醒需要确认'
EXPLANATION = '会议已保存，另一项仍需确认。'


async def mixed_reply(h):
    config = read_config(h.runtime.root)
    config['direct_write'] = True
    write_config(h.runtime.root, config)
    h.runtime.business.direct_write = True
    reminder = h.apple.detail('reminder', 'reminder-001')

    def final(_request):
        business = h.runtime.business
        saved = business.lookup(h.session, 'save-meeting').result['text']
        preview = business.pending(h.session)['text']
        return '\n\n'.join([saved, EXPLANATION, LISTING, preview])

    h.model.script = [
        call('save-meeting', draft('独立保存的测试会议')),
        call('delete-reminder', {
            'action': 'delete', 'kind': 'reminder', 'id': reminder['id'],
            'expected_revision': reminder['revision'],
        }),
        final,
    ]
    await h.submit('创建测试会议，并预览删除旧提醒', key='mixed-input')
    return h.runtime.business.pending(h.session)


def assert_saved_event_survives(h):
    assert [action for action, _args in h.native.writes].count('create') == 1
    assert any(item.get('title') == '独立保存的测试会议' for item in h.native.items)


def test_saved_card_and_delete_preview_are_separate_and_one_confirms_only_delete(tmp_path):
    async def run():
        h = Harness(tmp_path, [])
        try:
            pending = await mixed_reply(h)
            assert pending['delivered'] and pending['id'] == 'delete-reminder'
            assert len(h.native.writes) == 1
            assert len(h.sent) == 4
            card, explanation, listing, preview = h.sent
            assert card.startswith('**已保存**') and card.count('```') == 2
            assert '回复【1】撤销' not in card
            assert '撤销' in card and '取消' in card
            assert explanation == EXPLANATION
            assert listing == LISTING
            assert preview == pending['text'] and preview.count('```') == 2
            assert '回复【1】确认' in preview
            assert not h.runtime.business.latest_card_undo(h.session)

            h.model.script = ['删除结果以实际回执为准。']
            await h.submit('1', key='confirm-delete')
            assert [action for action, _args in h.native.writes] == ['create', 'delete']
            assert h.native.writes[-1][1]['id'] == 'reminder-001'
            assert h.runtime.business.pending(h.session) is None
            assert_saved_event_survives(h)
        finally:
            await h.close()
    asyncio.run(run())


def test_format_feedback_revalidates_matching_pending_and_survives_restart(tmp_path):
    async def run():
        h = Harness(tmp_path, [])
        try:
            original = await mixed_reply(h)
            original_args = original['args']

            def revalidate(request):
                # Neither a wrong reference nor an omitted continuation mode
                # can replace the pending draft or silently restore approval.
                rejected = results(request)[-2:]
                assert len(rejected) == 2 and all(item.get('is_error') for item in rejected)
                pending = h.runtime.business.pending(h.session)
                assert pending['id'] == original['id'] and not pending['delivered']
                assert pending['args'] == original_args
                return call('delete-rechecked', {
                    'action': 'revise', 'draft_call_id': pending['id'],
                    'request_mode': 'continue', 'fields': {},
                })

            def show_rechecked(_request):
                pending = h.runtime.business.pending(h.session)
                assert pending['id'] == 'delete-rechecked'
                assert pending['origin'] == 'format-feedback' and not pending['delivered']
                return '这次仅重新核对并展示待删除提醒。\n\n' + pending['text']

            h.model.script = [
                call('wrong-reference', {
                    'action': 'revise', 'draft_call_id': 'unrelated-draft',
                    'request_mode': 'continue', 'fields': {},
                }),
                call('missing-continuation', {
                    'action': 'revise', 'draft_call_id': original['id'], 'fields': {},
                }),
                revalidate, show_rechecked,
            ]
            await h.submit('怎么两项粘在一起了，请把待确认的一项分开', key='format-feedback')
            pending = h.runtime.business.pending(h.session)
            assert pending['id'] == 'delete-rechecked' and pending['delivered']
            assert pending['preview'] == original['preview']
            assert h.sent[-1] == pending['text']
            assert len(h.native.writes) == 1
            record = h.runtime.business.lookup(h.session, 'delete-rechecked')
            assert record.result['status'] == 'awaiting_approval'
            assert record.args['draft_call_id'] == original['id']

            await h.runtime.close()
            h.runtime = h.restart()
            restored = h.runtime.business.pending(h.session)
            assert restored['id'] == 'delete-rechecked' and restored['delivered']
            h.model.script = ['已处理本次确认。']
            await h.submit('1', key='confirm-after-restart')
            assert [action for action, _args in h.native.writes] == ['create', 'delete']
            assert h.native.writes[-1][1]['id'] == 'reminder-001'
            assert h.runtime.business.pending(h.session) is None
            assert_saved_event_survives(h)

            h.model.script = ['不能重复执行已经处理的预览。']
            await h.submit('1', key='duplicate-confirmation')
            assert len(h.native.writes) == 2
            assert_saved_event_survives(h)
        finally:
            await h.close()
    asyncio.run(run())


def test_quoting_old_preview_after_feedback_does_not_restore_confirmation(tmp_path):
    async def run():
        h = Harness(tmp_path, [])
        try:
            pending = await mixed_reply(h)
            previous_count = len(h.sent)
            h.model.script = [pending['text']]
            await h.submit('请重新分开发送刚才的预览', key='quoted-preview')
            retained = h.runtime.business.pending(h.session)
            assert retained['id'] == pending['id'] and not retained['delivered']
            followup = h.sent[previous_count:]
            assert followup and all('回复【1】确认' not in text for text in followup)
            assert all(pending['text'] not in text for text in followup)
            h.model.script = ['旧预览不能作为新的确认授权。']
            await h.submit('1', key='one-after-quotation')
            assert len(h.native.writes) == 1
            assert_saved_event_survives(h)
        finally:
            await h.close()
    asyncio.run(run())


def test_control_without_pending_sends_only_authoritative_denial(tmp_path):
    async def run():
        h = Harness(tmp_path, [
            call('forbidden-write', draft('不应保存的会议')),
            '刚才那条预览已经过期，请重新发起删除。',
        ])
        h.runtime.business.direct_write = True
        try:
            await h.submit('1', key='one-without-pending')
            receipt = h.runtime.business.active[h.session]['receipt']
            assert receipt['control'] and receipt['status'] == 'deny'
            assert h.sent == [receipt['text']]
            assert all('过期' not in text for text in h.sent)
            assert not h.native.writes and h.runtime.business.pending(h.session) is None
            blocked = h.runtime.business.lookup(h.session, 'forbidden-write')
            assert blocked.result['execution_started'] is False
            assert blocked.result['status'] == 'input_error'
            with h.runtime.business.db() as db:
                assert db.execute('SELECT reply FROM messages WHERE id=?', ('one-without-pending',)).fetchone()[0] == receipt['text']
        finally:
            await h.close()
    asyncio.run(run())
