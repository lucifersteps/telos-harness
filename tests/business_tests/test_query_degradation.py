"""Partial reads keep usable evidence without inventing empty state or write baselines."""
import asyncio

import pytest

from business_tests.test_apple_service import service
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.direct import inverse_fields
from telos.tools.apple.features import require_write_fields
from telos.tools.apple.tools import create_apple_tool
from telos.agent.tools.types import ToolInvocationContext


@pytest.mark.parametrize('field,filters', [
    ('tags', {'tags': ['Work']}), ('parent_id', {'parent_ids': ['parent']}),
    ('flagged', {'flagged': False}), ('urgent', {'urgent': True}), ('section_id', {'section_ids': ['section']})])
def test_unknown_field_keeps_plain_query_but_not_filtered_answer(tmp_path, field, filters):
    apple, native = service(tmp_path)
    native.items[0].update({field: None, 'unavailable_fields': [field]})
    result = apple.query({'kind': 'reminder'})
    assert result['status'] == 'partial' and result['items_complete'] and not result['fields_complete']
    assert result['total'] == 1 and result['items'][0]['title'] == '买菜'
    with pytest.raises(AppleNativeError, match='完整筛选结果'):
        apple.query({'kind': 'reminder', **filters})
    assert apple.query({'kind': 'reminder', **filters, 'calendar_ids': ['other']})['total'] == 0
    native.items[0]['completed'] = True
    assert apple.query({'kind': 'reminder', **filters})['total'] == 0


def partial_native(native, failed):
    original = native.request
    def request(action, args):
        result = original(action, args)
        if action == 'snapshot':
            result['items'] = [item for item in result['items'] if item['kind'] not in failed]
            result['source_errors'] = {kind: {'code': 'permission_denied', 'reason': '来源未授权'} for kind in failed}
        return result
    native.request = request


@pytest.mark.parametrize('failed', ['event', 'reminder'])
def test_mixed_source_failure_preserves_results_and_complete_cache(tmp_path, failed):
    apple, native = service(tmp_path)
    native.items.append({'id': 'event1', 'kind': 'event', 'title': '会议', 'start': '2026-11-20T10:00:00+08:00'})
    apple.refresh()
    with apple.database() as db:
        original_cache = db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()[0]
    partial_native(native, [failed])
    result = apple.query({})
    assert result['status'] == 'partial' and not result['items_complete']
    assert result['total'] is None and result['available_total'] == 1
    assert len(result['items']) == 1 and result['items'][0]['kind'] != failed
    with apple.database() as db:
        assert db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()[0] == original_cache
    assert apple.status()['last_error']
    with pytest.raises(AppleNativeError, match='未授权'):
        apple.query({'kind': failed})
    with pytest.raises(AppleNativeError, match='完整筛选结果'):
        apple.query({'text': '会议'})
    paged = apple.query({'offset': 1})
    assert paged['items'] == [] and paged['total'] is None and paged['status'] == 'partial'
    assert 'partial' in asyncio.run(apple.context_refresh())


def test_total_source_failure_remains_explicit_failure(tmp_path):
    apple, native = service(tmp_path)
    partial_native(native, ['event', 'reminder'])
    with pytest.raises(AppleNativeError, match='未授权'):
        apple.query({})


@pytest.mark.parametrize('missing,action,fields', [
    ('tags', 'update', {'tags': []}), ('attachments', 'update', {'attachments': []}),
    ('early_reminder', 'update', {'due': '2026-12-01'}),
    ('early_reminder_minutes', 'update', {'early_reminder_minutes': None}),
    ('urgent', 'update', {'due': '2026-12-01'}),
    ('subtask_ids', 'delete', {}), ('subtask_ids', 'update', {'completed': True}),
    ('parent_id', 'update', {'parent_id': ''}), ('parent_id', 'update', {'section_id': ''}),
    ('section_id', 'update', {'calendar_id': 'other-list'}),
])
def test_unknown_dependent_fields_block_before_preview_and_journal(tmp_path, missing, action, fields):
    apple, native = service(tmp_path)
    native.items[0].update({missing: None, 'unavailable_fields': [missing]})
    row = apple.detail('reminder', 'reminder-001')
    args = {'kind': 'reminder', 'id': row['id'], 'expected_revision': row['revision'], 'fields': fields}
    tool = create_apple_tool('apple.' + action, data_dir=tmp_path, options={}, service=apple)
    with pytest.raises(ValueError, match='不可读'):
        asyncio.run(tool.resolve_execution(args, context=ToolInvocationContext(tool_call_id='blocked')))
    with pytest.raises(ValueError, match='不可读'):
        apple.mutate(action, args, 'blocked')
    assert native.writes == []
    with apple.database() as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    require_write_fields('reminder', 'update', {'title': '仍可改标题'}, row)


def test_full_private_failure_keeps_title_detail_and_avoids_unsafe_direct_undo(tmp_path):
    apple, native = service(tmp_path)
    fields = ['tags', 'flagged', 'urgent', 'parent_id', 'subtask_ids', 'section_id', 'attachments', 'early_reminder']
    native.items[0].update({**dict.fromkeys(fields), 'unavailable_fields': fields})
    row = apple.detail('reminder', 'reminder-001')
    assert row['title'] == '买菜' and row['tags'] is None
    assert inverse_fields('update', 'reminder', {'title': '新标题'}, row) is None
    result = apple.mutate('update', {'kind': 'reminder', 'id': row['id'],
        'expected_revision': row['revision'], 'fields': {'title': '新标题'}}, 'title')
    assert result['saved'] and result['item']['tags'] is None


def test_basic_list_fallback_is_readable_but_not_writable(tmp_path):
    from business_tests.test_apple_containers import setup
    apple, native = setup(tmp_path)
    native.capabilities = {'lists': True, 'groups': False, 'sections': False}
    native.containers = [dict(native.containers[0], item_count=None, group_id=None,
                              unavailable_fields=['item_count', 'group_id'])]
    result = apple.query({'kind': 'list'})
    assert result['status'] == 'partial' and result['total'] == 1
    assert result['items'][0]['title'] == '生活' and result['items'][0]['item_count'] is None
    row = apple.detail('list', 'list-001')
    with pytest.raises(ValueError, match='不可读'):
        apple.mutate('delete', {'kind': 'list', 'id': row['id'], 'expected_revision': row['revision']}, 'delete')
    for kind in ['group', 'section']:
        with pytest.raises(AppleNativeError):
            apple.query({'kind': kind})
    assert not native.writes


def test_unknown_target_parent_cannot_be_assumed_top_level(tmp_path):
    apple, native = service(tmp_path)
    native.items.append(dict(native.items[0], id='parent', parent_id=None, subtask_ids=[],
                             unavailable_fields=['parent_id']))
    row = apple.detail('reminder', 'reminder-001')
    tool = create_apple_tool('apple.update', data_dir=tmp_path, options={}, service=apple)
    with pytest.raises(ValueError, match='不可读'):
        asyncio.run(tool.resolve_execution({'kind': 'reminder', 'id': row['id'],
            'expected_revision': row['revision'], 'fields': {'parent_id': 'parent'}},
            context=ToolInvocationContext(tool_call_id='unknown-parent')))
    assert native.writes == []
