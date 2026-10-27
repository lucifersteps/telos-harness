"""Opt-in revision regression against a freshly authorized native Apple helper.

Only creates one uniquely named temporary reminder list and objects inside it.
No existing user task is updated, deleted, replayed or used as a write target.
The test has its own operation cache, never the installed Telos cache. Cleanup
requires the exact owned list plus this run's title prefix; foreign contents or
uncertain writes stop automatic cleanup and report the temporary list name.
No model, WeChat, event creation or system permission changes are performed.

TELOS_REVISION_LIVE=1 TELOS_REVISION_HELPER=/absolute/path/to/telos-apple \
PYTHONPATH=.:checkout/src:tests python -m pytest -q -s tests/test_revision_live.py
"""
import asyncio
from copy import deepcopy
import os
from pathlib import Path
from uuid import uuid4

import pytest

from apple_component import apple_client
from telos.agent.tools.types import ToolInvocationContext
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.service import AppleService, operation_key
from telos.tools.apple.tools import create_apple_tool


pytestmark = pytest.mark.skipif(
    os.environ.get('TELOS_REVISION_LIVE') != '1',
    reason='Requires explicit authorization to create and clean temporary Apple objects',
)


class DiagnosticBaselineClient:
    """Inject test-only baseline differences immediately before native checks."""
    def __init__(self, native):
        self.native = native
        self.expected_checks = 0
        self.parent_checks = 0
        self.change_expected_title = False
        self.change_parent_title = False

    def request(self, action, args):
        request = deepcopy(args)
        if action in {'create', 'update', 'delete'} and request.get('kind') == 'reminder':
            if 'expected' in request:
                request['expected']['section_read_error'] = 'Synthetic NSError 0x1111 underlying=0x2222'
                self.expected_checks += 1
                if self.change_expected_title:
                    self.change_expected_title = False
                    request['expected']['title'] += ' [stale native baseline]'
            parent = request.get('expected_references', {}).get('parent_id')
            if parent is not None:
                parent['section_read_error'] = 'Synthetic parent NSError 0x3333 underlying=0x4444'
                self.parent_checks += 1
                if self.change_parent_title:
                    self.change_parent_title = False
                    parent['title'] += ' [stale parent baseline]'
        return self.native.request(action, request)


def test_native_revision_roundtrip_conflicts_undo_and_sections(tmp_path):
    value = os.environ.get('TELOS_REVISION_HELPER', '')
    helper = Path(value).expanduser()
    assert value and helper.is_absolute() and helper.is_file(), 'Set TELOS_REVISION_HELPER to an existing absolute executable path'
    native = apple_client({}, helper.resolve())
    status = native.request('status', {})
    assert status['events'] and status['reminders'], 'Authorize both Apple permissions before running'
    noisy = DiagnosticBaselineClient(native)
    service = AppleService(noisy, tmp_path / 'cache')
    prefix = 'Telos版本验证-' + uuid4().hex[:12]
    list_id = None
    outside = None

    def prepare(action, kind, fields=None, identifier=None):
        args = {'kind': kind}
        if fields is not None:
            args['fields'] = fields
        if identifier:
            current = service.detail(kind, identifier)
            if kind in {'reminder', 'section'}:
                assert current['calendar_id'] == list_id and current['title'].startswith(prefix)
            else:
                assert kind == 'list' and identifier == list_id and current['title'] == prefix
            args.update(id=identifier, expected_revision=current['revision'])
        if action == 'create':
            assert fields['title'].startswith(prefix)
            if kind == 'list':
                assert list_id is None
            else:
                assert kind in {'reminder', 'section'} and fields.get('calendar_id') == list_id
        context = ToolInvocationContext(session_id=prefix, turn_id=uuid4().hex, tool_call_id=uuid4().hex)
        tool = create_apple_tool('apple.' + action, data_dir=tmp_path, options={}, service=service)
        execution = asyncio.run(tool.resolve_execution(args, context=context))
        assert execution.need_confirm
        return execution, operation_key(context.session_id, context.turn_id, context.tool_call_id)

    def save(action, kind, fields=None, identifier=None):
        execution, operation_id = prepare(action, kind, fields, identifier)
        result = asyncio.run(execution.run())
        assert result.get('saved'), result
        return result, operation_id

    def rows():
        result = service.query({'kind': 'reminder', 'calendar_ids': [list_id], 'include_completed': True, 'limit': 500})
        assert result['total'] <= 500, 'Unexpectedly large temporary list; refuse partial cleanup'
        return result['items']

    def stable(identifier):
        versions = []
        for _ in range(3):
            detail = service.detail('reminder', identifier)
            assert not detail.get('section_read_error'), 'Native section reading must work on the candidate helper'
            assert detail['section_id'] is not None
            matches = [item for item in rows() if item['id'] == identifier]
            assert len(matches) == 1 and matches[0]['revision'] == detail['revision']
            versions.append(detail['revision'])
        assert len(set(versions)) == 1, 'Unedited reminder revision changed between fresh reads'
        return detail

    try:
        print('REVISION_LIVE: temporary list prefix: ' + prefix, flush=True)
        created_list, _ = save('create', 'list', {'title': prefix})
        list_id = created_list['item']['id']
        service.list_ids = [list_id]
        outside = AppleService(native, tmp_path / 'external-cache', list_ids=[list_id])
        created, _ = save('create', 'reminder', {'title': prefix + '-事项', 'calendar_id': list_id, 'notes': 'Synthetic test'})
        identifier = created['item']['id']
        stable(identifier)
        save('update', 'reminder', {'title': prefix + '-改名', 'notes': 'Updated synthetic test'}, identifier)
        assert stable(identifier)['notes'] == 'Updated synthetic test'
        save('update', 'reminder', {'completed': True}, identifier)
        assert stable(identifier)['completed'] is True
        save('update', 'reminder', {'completed': False}, identifier)

        # Real out-of-band edit only touches this run's own temporary reminder.
        waiting, _ = prepare('update', 'reminder', {'notes': 'Must not overwrite'}, identifier)
        before = outside.detail('reminder', identifier)
        assert before['calendar_id'] == list_id and before['title'].startswith(prefix)
        outside.mutate('update', {'kind': 'reminder', 'id': identifier, 'expected_revision': before['revision'],
                                 'fields': {'title': prefix + '-外部改名'}}, uuid4().hex)
        with pytest.raises(AppleNativeError) as conflict:
            asyncio.run(waiting.run())
        assert conflict.value.code == 'conflict'
        assert service.detail('reminder', identifier)['notes'] == 'Updated synthetic test'

        # This mismatch is injected after Python's fresh revision check, so the
        # real helper must reject it before any native save.
        rejected, _ = prepare('update', 'reminder', {'notes': 'Must not save natively'}, identifier)
        noisy.change_expected_title = True
        with pytest.raises(AppleNativeError) as conflict:
            asyncio.run(rejected.run())
        assert conflict.value.code == 'conflict'
        assert service.detail('reminder', identifier)['notes'] == 'Updated synthetic test'
        assert service.status()['uncertain_writes'] == 0

        before = stable(identifier)
        _, update_id = save('update', 'reminder', {'notes': 'Undo this update'}, identifier)
        assert service.undo(update_id)['undone']
        assert stable(identifier)['notes'] == before['notes']
        assert service.undo(update_id)['already_undone']
        undo_create, create_id = save('create', 'reminder', {'title': prefix + '-创建撤销', 'calendar_id': list_id})
        assert service.undo(create_id)['undone']
        assert not any(item['id'] == undo_create['item']['id'] for item in rows())
        print('REVISION_LIVE: stable detail/query, update/complete, Python/native conflict checks and undo passed', flush=True)

        section, _ = save('create', 'section', {'title': prefix + '-分区', 'calendar_id': list_id})
        section_id = section['item']['id']
        save('update', 'reminder', {'section_id': section_id}, identifier)
        assert stable(identifier)['section_id'] == section_id
        assert identifier in service.detail('section', section_id)['member_ids']
        parent, _ = save('create', 'reminder', {'title': prefix + '-父任务', 'calendar_id': list_id})
        parent_id = parent['item']['id']
        child, _ = save('create', 'reminder', {'title': prefix + '-子任务', 'calendar_id': list_id, 'parent_id': parent_id})
        child_id = child['item']['id']
        assert service.detail('reminder', child_id)['parent_id'] == parent_id
        assert child_id in service.detail('reminder', parent_id)['subtask_ids']
        stale_child_title = prefix + '-拒绝失效父任务'
        stale_child, _ = prepare('create', 'reminder', {'title': stale_child_title,
                                 'calendar_id': list_id, 'parent_id': parent_id})
        noisy.change_parent_title = True
        with pytest.raises(AppleNativeError) as conflict:
            asyncio.run(stale_child.run())
        assert conflict.value.code == 'conflict'
        assert not any(item['title'] == stale_child_title for item in rows())
        save('update', 'reminder', {'parent_id': ''}, child_id)
        assert service.detail('reminder', child_id)['parent_id'] == ''
        assert noisy.expected_checks > 0 and noisy.parent_checks > 0
        save('delete', 'reminder', identifier=child_id)
        save('delete', 'reminder', identifier=parent_id)
        save('update', 'reminder', {'section_id': ''}, identifier)
        save('delete', 'section', identifier=section_id)
        save('delete', 'reminder', identifier=identifier)
        assert rows() == []
        print('REVISION_LIVE: native diagnostic baselines, parent references, section assignment/read/removal and delete passed', flush=True)
    finally:
        noisy.change_expected_title = False
        noisy.change_parent_title = False
        # Check both independent caches, including failure before list creation
        # returned an ID. Unknown writes require inspection, never an automatic
        # resolve-write or a second service used to evade the uncertainty guard.
        uncertain = service.status()['uncertain_operation_ids']
        if outside is not None:
            uncertain += outside.status()['uncertain_operation_ids']
        assert not uncertain, 'Uncertain test write; inspect temporary list manually: ' + prefix
        if list_id:
            remaining = rows()
            assert all(item['calendar_id'] == list_id and item['title'].startswith(prefix) for item in remaining), 'Foreign contents; leave temporary list untouched: ' + prefix
            for item in sorted(remaining, key=lambda row: bool(row.get('subtask_ids'))):
                save('delete', 'reminder', identifier=item['id'])
            section_query = service.query({'kind': 'section', 'calendar_ids': [list_id], 'limit': 500})
            assert section_query['total'] <= 500, 'Unexpected section count; refuse partial cleanup: ' + prefix
            sections = section_query['items']
            assert all(item['calendar_id'] == list_id and item['title'].startswith(prefix) for item in sections), 'Foreign section; leave temporary list untouched: ' + prefix
            for section in sections:
                save('delete', 'section', identifier=section['id'])
            assert rows() == []
            service.list_ids = []
            save('delete', 'list', identifier=list_id)
            remaining_lists = service.query({'kind': 'list', 'text': prefix, 'limit': 500})['items']
            assert not any(item['id'] == list_id or item['title'] == prefix for item in remaining_lists)
            print('REVISION_LIVE: uniquely owned temporary objects cleaned: ' + prefix, flush=True)
