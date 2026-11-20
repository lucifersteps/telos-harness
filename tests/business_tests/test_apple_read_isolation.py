import asyncio
import json

import pytest

from business import Business
from business_tests.test_apple_service import service
from telos.agent.tools.types import ToolInputError
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.features import validate_features


@pytest.mark.parametrize('window', [{}, {'start': '2026-11-17T00:00:00+08:00', 'end': '2026-11-18T00:00:00+08:00'}])
@pytest.mark.parametrize('kind', ['event', 'event_calendar', 'reminder', 'list', 'group', 'section'])
def test_query_passes_kind_without_replacing_full_cache(tmp_path, kind, window):
    apple, native = service(tmp_path)
    apple.refresh()
    with apple.database() as db:
        before = db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()[0]
    def scoped(action, args):
        assert action == 'snapshot' and args['kind'] == kind
        assert all(args[k] == v for k, v in window.items())
        return {'items': [], 'calendars': [], 'containers': [],
                'container_capabilities': {'groups': True, 'sections': True}}
    native.request = scoped
    assert apple.query({'kind': kind, **window})['total'] == 0
    with apple.database() as db:
        assert db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()[0] == before


def test_unknown_attachments_preserve_ordinary_edits_and_reject_replacement(tmp_path):
    apple, native = service(tmp_path)
    native.items[0].update(attachments=None, attachments_supported=False)
    row = apple.query({'kind': 'reminder'})['items'][0]
    validate_features('reminder', {'title': '更新标题'}, row)
    saved = apple.mutate('update', {'kind': 'reminder', 'id': row['id'],
                         'expected_revision': row['revision'], 'fields': {'title': '更新标题'}}, 'ordinary')
    assert saved['item']['attachments'] is None
    assert native.writes[0][1]['fields'] == {'title': '更新标题'}
    for attachments in ([], [{'id': 'unknown'}], [{'url': 'https://example.com'}]):
        with pytest.raises(ValueError, match='附件不可读'):
            validate_features('reminder', {'attachments': attachments}, row)


def test_unprepared_session_can_read_but_cannot_write_or_record_draft(tmp_path):
    async def run():
        apple, native = service(tmp_path / 'apple')
        business = Business(tmp_path, apple, {'direct_write': True})
        session = 'wechat-restored-without-active-context'
        for args in ({'action': 'query', 'kind': 'reminder'},
                     {'action': 'detail', 'kind': 'reminder', 'id': 'reminder-001'}):
            await business.execute(session, 'read', 'personalPlan', args)
        for args in ({'action': 'create', 'kind': 'reminder', 'fields': {'title': '不能写入'}},
                     {'action': 'create', 'kind': 'invalid', 'fields': {}}):
            with pytest.raises(ToolInputError):
                await business.execute(session, 'write', 'personalPlan', args)
        assert not native.writes and session not in business.active
        assert business.lookup(session, 'write') is None
        assert business.pending(session) is None
    asyncio.run(run())


def test_endpoint_errors_keep_native_code_and_traceback_without_arguments(tmp_path):
    from channel import Channel
    import aiohttp

    async def run():
        apple, native = service(tmp_path / 'apple')
        channel = Channel(tmp_path, apple, None)
        def fail(action, args):
            raise AppleNativeError('附件接口不可用', code='unsupported_private_feature')
        native.request = fail
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f'http://127.0.0.1:{channel.server.server_port}/tool',
                        headers={'Authorization': 'Bearer ' + channel.token},
                        json={'session': 'wechat-restored', 'id': 'read', 'name': 'personalPlan',
                              'args': {'action': 'query', 'kind': 'event', 'text': 'private-search-phrase'}}) as response:
                    result = await response.json(content_type=None)
                    assert response.status == 400
                    assert result['error_code'] == 'unsupported_private_feature'
            log = (tmp_path / 'logs/tool-errors.jsonl').read_text()
            entry = json.loads(log.splitlines()[-1])
            assert entry['tool'] == 'personalPlan' and entry['session'] == 'wechat-restored'
            assert 'Traceback' in entry['exception'] and 'AppleNativeError' in entry['exception']
            assert 'private-search-phrase' not in log and channel.token not in log
        finally:
            await channel.close()
    asyncio.run(run())
