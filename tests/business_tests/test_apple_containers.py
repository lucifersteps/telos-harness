import asyncio
from copy import deepcopy
import json

import pytest

from telos.adapters.approval_channel import AdapterApprovalChannel
from telos.adapters.base import ConversationRef, SendResult
from telos.adapters.wechat.adapter import WeChatAdapter
from telos.agent.tools.types import ToolInvocationContext
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.service import AppleService
from telos.tools.apple.tools import create_apple_tool
from business_tests.test_apple_service import FakeApple


class ContainerApple(FakeApple):
    def __init__(self):
        super().__init__()
        self.containers = [
            {'id': 'list-001', 'kind': 'list', 'title': '生活', 'writable': True,
             'account_id': 'account-1', 'group_id': 'group-1', 'color': '#8E8E93',
             'emoji': '📥', 'symbol': '', 'item_count': 1},
            {'id': 'group-1', 'kind': 'group', 'title': '个人', 'writable': True,
             'account_id': 'account-1', 'child_list_ids': ['list-001']},
            {'id': 'group-2', 'kind': 'group', 'title': '工作', 'writable': True,
             'account_id': 'account-1', 'child_list_ids': []},
        ]
        self.capabilities = {'groups': True, 'icons': True,
            'accounts': [{'id': 'account-1', 'title': 'iCloud', 'writable': True}]}

    def request(self, action, args):
        if action == 'snapshot':
            return {**super().request(action, args), 'containers': deepcopy(self.containers),
                    'container_capabilities': deepcopy(self.capabilities)}
        if args.get('kind') not in {'list', 'group'}:
            return super().request(action, args)
        item = next((c for c in self.containers if c['id'] == args.get('id')), None)
        if action == 'detail':
            if item is None:
                raise AppleNativeError('不存在', code='not_found')
            return {'item': deepcopy(item)}
        expected_destination = args.get('expected_destination')
        if expected_destination:
            destination = next(c for c in self.containers if c['id'] == expected_destination['id'])
            if destination != expected_destination:
                raise AppleNativeError('目标群组已变化', code='conflict')
        self.writes.append((action, deepcopy(args)))
        if self.write_error:
            raise self.write_error
        if action == 'delete':
            self.containers.remove(item)
            return {'deleted': True}
        if action == 'create':
            item = {'id': 'new-container', 'kind': args['kind'], **args['fields']}
            self.containers.append(item)
        else:
            assert item == args['expected']
            item.update(args['fields'])
        return {'item': deepcopy(item)}


def setup(tmp_path):
    native = ContainerApple()
    return AppleService(native, tmp_path), native


def args_for(apple, kind='list', item_id='list-001', fields=None):
    item = apple.detail(kind, item_id)
    args = {'kind': kind, 'id': item_id, 'expected_revision': item['revision']}
    if fields is not None:
        args['fields'] = fields
    return args


def resolve(apple, tmp_path, action, args):
    tool = create_apple_tool('apple.' + action, data_dir=tmp_path, options={}, service=apple)
    return asyncio.run(tool.resolve_execution(args, context=ToolInvocationContext(
        session_id='s1', turn_id='t1', tool_call_id='call-1')))


def test_container_query_and_turn_state_include_real_hierarchy_and_appearance(tmp_path):
    apple, native = setup(tmp_path)
    result = apple.query({'kind': 'group'})
    assert result['total'] == 2
    assert result['items'][0]['revision']
    assert result['containers'][0]['emoji'] == '📥'
    content = asyncio.run(apple.context_refresh())
    assert 'child_list_ids' in content and 'group-1' in content
    native.containers[0]['color'] = '#FF383C'
    assert apple.query({'kind': 'list'})['items'][0]['color'] == '#FF383C'


def test_failed_container_read_is_not_reported_as_an_empty_account(tmp_path):
    apple, native = setup(tmp_path)
    native.capabilities = {'groups': False, 'error': '原生群组读取失败'}
    with pytest.raises(AppleNativeError, match='最新读取失败'):
        apple.query({'kind': 'group'})
    assert apple.query({'kind': 'reminder'})['total'] == 1


@pytest.mark.parametrize('field,value', [('title', '新名称'), ('color', '#FF383C'), ('group_id', ''), ('item_count', 2)])
def test_container_edits_during_approval_reject_write(tmp_path, field, value):
    apple, native = setup(tmp_path)
    execution = resolve(apple, tmp_path, 'update', args_for(apple, fields={'emoji': '🎯'}))
    native.containers[0][field] = value
    with pytest.raises(AppleNativeError, match='已被修改'):
        asyncio.run(execution.run())
    assert native.writes == []


def test_group_membership_change_blocks_deletion_after_preview(tmp_path):
    apple, native = setup(tmp_path)
    execution = resolve(apple, tmp_path, 'delete', args_for(apple, 'group', 'group-1'))
    assert '保留' in execution.approval_preview['summary']
    assert '生活' in execution.approval_preview['summary']
    native.containers[1]['child_list_ids'].append('new-child')
    with pytest.raises(AppleNativeError, match='已被修改'):
        asyncio.run(execution.run())
    assert native.writes == []


def test_destination_is_bound_to_the_approved_group(tmp_path):
    apple, native = setup(tmp_path)
    execution = resolve(apple, tmp_path, 'update', args_for(apple, fields={'group_id': 'group-2'}))
    assert '归属：工作' in execution.approval_preview['summary']
    native.containers[2]['title'] = '其他'
    with pytest.raises(AppleNativeError, match='目标群组'):
        asyncio.run(execution.run())
    assert native.writes == [] and apple.status()['uncertain_writes'] == 0


def test_container_create_replay_and_partial_save_protection(tmp_path):
    apple, native = setup(tmp_path)
    args = {'kind': 'list', 'fields': {'title': '读书', 'color': 'purple', 'emoji': '📚'}}
    execution = resolve(apple, tmp_path, 'create', args)
    assert asyncio.run(execution.run())['saved']
    assert asyncio.run(execution.run())['saved']
    assert len(native.writes) == 1
    native.write_error = AppleNativeError('部分字段可能已保存', code='native_error')
    with pytest.raises(AppleNativeError):
        apple.mutate('create', args, 'different-call')
    with pytest.raises(AppleNativeError, match='新写入已暂停'):
        apple.mutate('create', {'kind': 'reminder', 'fields': {'title': 'x'}}, 'another-call')
    assert apple.status()['uncertain_writes'] == 1


def test_creation_pins_the_account_shown_in_preview(tmp_path):
    apple, native = setup(tmp_path)
    execution = resolve(apple, tmp_path, 'create', {'kind': 'list', 'fields': {'title': '读书'}})
    assert '账户：iCloud' in execution.approval_preview['summary']
    asyncio.run(execution.run())
    assert native.writes[0][1]['fields']['account_id'] == 'account-1'


def test_restricted_scope_refuses_structure_changes_before_approval(tmp_path):
    apple, native = setup(tmp_path)
    native.capabilities['restricted_scope'] = True
    with pytest.raises(ValueError, match='同步范围'):
        resolve(apple, tmp_path, 'delete', args_for(apple, 'group', 'group-1'))
    assert native.writes == []


