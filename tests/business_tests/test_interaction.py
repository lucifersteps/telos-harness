import asyncio
import json

import pytest
from types import SimpleNamespace
DRAFTS={}
def lookup(path,session,cid):return DRAFTS.get((str(path),session,cid))

from telos.adapters.question_channel import QuestionResponse
from telos.agent.tools.types import ToolInvocationContext
from telos.agent.tools.validation import validate_tool_args
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.interaction import NAME, create_personal_plan_tool, create_personal_question_tool, personal_result_text
from telos.tools.apple.service import AppleService
from business_tests.test_apple_containers import ContainerApple
from business_tests.test_apple_service import FakeApple


def make_tool(tmp_path, channel=None, native=None):
    native = native or FakeApple()
    native.calendars.append({'id': 'calendar-event', 'title': 'Telos-日程', 'kind': 'event', 'writable': True})
    service = AppleService(native, tmp_path / 'cache')
    return create_personal_plan_tool(data_dir=tmp_path, options={}, service=service,draft_lookup=lambda session,cid:lookup(tmp_path,session,cid)), native, service


def draft(**fields):
    return {'action': 'create', 'kind': 'event', 'fields': {'title': '测试活动',
        'start': '2026-10-11T20:00:00+08:00', 'end': '2026-10-11T21:00:00+08:00', **fields}}


def resolve(tool, args, call_id='call_plan'):
    if tool.name == 'ask_user':
        args = {k: v for k, v in args.items() if k != 'action'}
    assert validate_tool_args(tool, args) is None
    return asyncio.run(tool.resolve_execution(args, context=ToolInvocationContext(
        session_id='session_test', turn_id='turn_test', tool_call_id=call_id, tool_name=NAME)))


class Questions:
    def __init__(self, decision='answered', answer='21点结束'):
        self.requests = []
        self.response = QuestionResponse(decision=decision, answer=answer)

    async def request_question(self, request):
        self.requests.append(request)
        return self.response


def test_complete_draft_selects_real_calendar_without_optional_field_questions(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    execution = resolve(tool, draft())
    assert execution.need_confirm and native.writes == []
    assert '2026年10月11日 20:00' in execution.approval_preview['summary']
    assert 'Telos-日程' in execution.approval_preview['summary']
    assert asyncio.run(execution.run())['saved']
    assert native.writes[0][1]['fields']['calendar_id'] == 'calendar-event'
    assert 'notes' not in native.writes[0][1]['fields']


def test_early_departure_preview_keeps_event_times_and_shows_native_alarm(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    execution = resolve(tool, draft(start='2026-10-12T09:00:00+08:00',
        end='2026-10-12T12:00:00+08:00', alarm_minutes_before=[40]))
    summary = execution.approval_preview['summary']
    assert '2026年10月12日 09:00' in summary and '2026年10月12日 12:00' in summary
    assert '提前提醒：2026年10月12日 08:20（提前40分钟）' in summary
    assert execution.need_confirm and native.writes == []
    asyncio.run(execution.run())
    assert native.writes[0][1]['fields']['alarm_minutes_before'] == [40]


def test_alarm_field_only_accepts_event_and_valid_integer_minutes(tmp_path):
    tool, _, _ = make_tool(tmp_path)
    for value in [[-1], [1.5], [True], [525601], 40, None]:
        assert validate_tool_args(tool, draft(alarm_minutes_before=value)) is not None
    with pytest.raises(ValueError, match='仅适用于日程'):
        resolve(tool, {'action':'create','kind':'reminder','fields':{'title':'买菜','alarm_minutes_before':[40]}})


@pytest.mark.parametrize('minutes,label', [([], '提醒：清除'),
    ([0], '2026年10月12日 09:00（提前0分钟）'), ([40], '2026年10月12日 08:20（提前40分钟）')])
def test_alarm_update_previews_replacement_or_removal_before_writing(tmp_path, minutes, label):
    tool, native, service = make_tool(tmp_path)
    native.items.append({'id': 'event-001', 'kind': 'event', 'calendar_id': 'calendar-event',
        'title': '测试活动', 'start': '2026-10-12T09:00:00+08:00', 'end': '2026-10-12T12:00:00+08:00',
        'alarm_minutes_before': [15], 'alarms': [{'relative_offset': -900, 'absolute_date': ''}]})
    current = service.detail('event', 'event-001')
    args = {'action': 'update', 'kind': 'event', 'id': current['id'],
        'expected_revision': current['revision'], 'fields': {'alarm_minutes_before': minutes}}
    execution = resolve(tool, args)
    assert execution.need_confirm and native.writes == []
    summary = execution.approval_preview['summary']
    assert label in summary and '2026年10月12日 12:00' in summary
    assert ('替换该日程原有提醒' in summary) == bool(minutes)
    assert asyncio.run(execution.run())['saved']
    assert native.writes[0][1]['fields'] == {'alarm_minutes_before': minutes}


def test_change_to_another_native_alarm_invalidates_preview(tmp_path):
    tool, native, service = make_tool(tmp_path)
    native.items.append({'id': 'event-001', 'kind': 'event', 'calendar_id': 'calendar-event',
        'title': '测试活动', 'start': '2026-10-12T09:00:00+08:00', 'end': '2026-10-12T12:00:00+08:00',
        'alarm_minutes_before': [15], 'alarms': [{'relative_offset': -900, 'absolute_date': ''},
                                             {'relative_offset': -600, 'absolute_date': ''}]})
    current = service.detail('event', 'event-001')
    execution = resolve(tool, {'action': 'update', 'kind': 'event', 'id': current['id'],
        'expected_revision': current['revision'], 'fields': {'alarm_minutes_before': [40]}})
    native.items[-1]['alarms'][1]['relative_offset'] = -300
    with pytest.raises(AppleNativeError, match='已被修改'):
        asyncio.run(execution.run())
    assert native.writes == []


def test_missing_end_returns_constraints_then_explicit_question_without_approval(tmp_path):
    channel = Questions()
    tool, native, _ = make_tool(tmp_path, channel)
    args = draft()
    del args['fields']['end']
    execution = resolve(tool, args)
    assert not execution.need_confirm
    result = asyncio.run(execution.run())
    assert channel.requests == []
    assert result['status'] == 'incomplete_draft' and result['missing_fields'] == ['end']
    assert result['draft']['fields']['title'] == '测试活动'
    question = resolve(create_personal_question_tool(channel), {'action':'clarify','unresolved':['end'],'text':'这场测试活动预计几点结束？'}, 'ask')
    answer = asyncio.run(question.run())
    assert answer['status'] == 'answered' and answer['answer'] == '21点结束'
    assert channel.requests[0].question == '这场测试活动预计几点结束？'
    assert native.writes == []


@pytest.mark.parametrize('field', ['image_date', 'intent', 'timezone', 'date'])
def test_declared_uncertainty_never_reaches_write_approval(tmp_path, field):
    channel = Questions()
    tool, native, _ = make_tool(tmp_path, channel)
    execution = resolve(tool, {**draft(), 'unresolved': [field]})
    assert not execution.need_confirm
    assert asyncio.run(execution.run())['missing_fields'] == [field]
    assert channel.requests == []
    assert native.writes == []


def test_ambiguous_calendar_is_not_selected_by_program(tmp_path):
    channel = Questions()
    tool, native, _ = make_tool(tmp_path, channel)
    native.calendars.append({'id': 'calendar-event-2', 'title': 'Telos-日程', 'kind': 'event', 'writable': True})
    execution = resolve(tool, draft())
    assert not execution.need_confirm
    assert asyncio.run(execution.run())['missing_fields'] == ['calendar_id']
    assert channel.requests == []
    assert native.writes == []


def test_naive_event_time_requires_timezone_and_empty_draft_requires_fields(tmp_path):
    channel = Questions()
    tool, native, _ = make_tool(tmp_path, channel)
    execution = resolve(tool, draft(start='2026-10-11T20:00:00', end='2026-10-11T21:00:00'))
    assert not execution.need_confirm
    assert asyncio.run(execution.run())['missing_fields'] == ['timezone']
    assert channel.requests == []
    execution = resolve(tool, {'action': 'create', 'kind': 'event', 'fields': {}})
    assert asyncio.run(execution.run())['missing_fields'] == ['title', 'start', 'end']
    assert channel.requests == []
    assert native.writes == []


def test_prompt_injection_cannot_supply_free_question_or_confirmation_field(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    assert validate_tool_args(tool, {'action': 'clarify', 'question': '确认吗？'})
    assert validate_tool_args(tool, {'action': 'clarify', 'unresolved': ['confirmation']})
    assert native.writes == []


def test_update_without_revision_reads_baseline_then_preserves_conflict_guard(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    args = {'action': 'update', 'kind': 'reminder', 'id': 'reminder-001', 'fields': {'completed': True}}
    baseline = resolve(tool, args)
    assert not baseline.need_confirm
    result = asyncio.run(baseline.run())
    execution = resolve(tool, {**args, 'expected_revision': result['item']['revision']})
    assert execution.need_confirm
    native.items[0]['title'] = '用户刚刚在 Apple 中修改'
    with pytest.raises(AppleNativeError, match='已被修改'):
        asyncio.run(execution.run())
    assert native.writes == []


def test_unknown_write_still_blocks_new_operation(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    native.write_error = AppleNativeError('结果未知', code='connection_lost')
    execution = resolve(tool, draft())
    with pytest.raises(AppleNativeError):
        asyncio.run(execution.run())
    native.write_error = None
    next_execution = resolve(tool, draft(title='另一项活动'), call_id='call_other')
    assert not next_execution.need_confirm
    assert asyncio.run(next_execution.run())['status'] == 'write_uncertain'
    assert len(native.writes) == 1


def test_container_appearance_and_batch_share_native_execution_and_one_preview(tmp_path):
    tool, native, service = make_tool(tmp_path, native=ContainerApple())
    item = service.detail('list', 'list-001')
    operations = [
        {'action': 'update', 'kind': 'list', 'id': item['id'], 'expected_revision': item['revision'],
         'fields': {'color': 'green', 'emoji': '🎯'}},
        {'action': 'create', 'kind': 'group', 'fields': {'title': '测试项目'}},
    ]
    execution = resolve(tool, {'action': 'batch', 'operations': operations})
    assert execution.need_confirm and native.writes == []
    assert '绿色' in execution.approval_preview['summary']
    assert '测试项目' in execution.approval_preview['summary']
    result = asyncio.run(execution.run())
    assert result['saved'] and len(result['results']) == 2
    assert len(native.writes) == 2


def test_batch_incomplete_draft_writes_nothing_and_preserves_entire_plan(tmp_path):
    channel = Questions()
    tool, native, _ = make_tool(tmp_path, channel)
    second = draft(title='第二项')
    del second['fields']['end']
    args = {'action': 'batch', 'operations': [draft(), second]}
    execution = resolve(tool, args)
    assert not execution.need_confirm
    result = asyncio.run(execution.run())
    assert result['draft'] == args and len(result['draft']['operations']) == 2
    assert channel.requests == [] and result['status'] == 'incomplete_draft'
    assert result['missing_fields'] == ['end']
    assert native.writes == []










def test_batch_stops_after_second_operation_conflict_and_reports_partial_result(tmp_path):
    tool, native, service = make_tool(tmp_path)
    item = service.detail('reminder', 'reminder-001')
    second = {'action': 'update', 'kind': 'reminder', 'id': item['id'], 'expected_revision': item['revision'],
              'fields': {'completed': True}}
    execution = resolve(tool, {'action': 'batch', 'operations': [draft(), second]})
    native.items[0]['title'] = 'Apple 中已改名'
    result = asyncio.run(execution.run())
    assert result.is_error and result.output['completed'] == 1
    assert '已被修改' in result.output['reason']
    assert len(native.writes) == 1 and native.writes[0][0] == 'create'
    assert native.items[0]['completed'] is False


def test_batch_cannot_invent_missing_persistent_call_identifier(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    with pytest.raises(ValueError, match='持久化工具调用编号'):
        asyncio.run(tool.resolve_execution({'action': 'batch', 'operations': [draft()]}))
    assert native.writes == []


def test_multiple_event_alarms_share_one_preview_and_write(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    execution = resolve(tool, draft(start='2026-11-18T18:00:00+08:00',
        end='2026-11-18T19:00:00+08:00', alarm_minutes_before=[20160, 10080]))
    assert execution.need_confirm and native.writes == []
    assert '2026年11月04日 18:00' in execution.approval_preview['summary']
    assert '2026年11月11日 18:00' in execution.approval_preview['summary']
    asyncio.run(execution.run())
    assert len(native.writes) == 1
    assert native.writes[0][1]['fields']['alarm_minutes_before'] == [20160, 10080]


def test_duplicate_and_exchange_alarms_rejected_before_approval(tmp_path):
    tool, native, _ = make_tool(tmp_path)
    with pytest.raises(ValueError, match='不能重复'):
        resolve(tool, draft(alarm_minutes_before=[40, 40]))
    native.calendars[-1]['max_alarms'] = 1
    with pytest.raises(ValueError, match='不支持这么多'):
        resolve(tool, draft(alarm_minutes_before=[20160, 10080]))
    assert not native.writes


def test_reminder_early_alarm_preserves_due_and_asks_only_missing_time(tmp_path):
    questions = Questions()
    tool, native, _ = make_tool(tmp_path, questions)
    args = {'action':'create', 'kind':'reminder', 'fields':{
        'title':'提交论文', 'due':'2026-11-18', 'early_reminder_minutes':10080}}
    execution = resolve(tool, args)
    assert not execution.need_confirm
    result = asyncio.run(execution.run())
    assert result['missing_fields'] == ['due_time']
    assert result['draft']['fields']['due'] == '2026-11-18'
    assert questions.requests == [] and result['status'] == 'incomplete_draft'
    args['fields']['due'] = '2026-11-18T18:00:00+08:00'
    execution = resolve(tool, args, 'call_ready')
    summary = execution.approval_preview['summary']
    assert '到期：2026年11月18日 18:00' in summary
    assert '提前提醒：2026年11月11日 18:00' in summary
    assert execution.need_confirm and not native.writes
    asyncio.run(execution.run())
    assert len(native.writes) == 1


def test_reminder_early_alarm_update_clear_and_conflict(tmp_path):
    tool, native, service = make_tool(tmp_path)
    native.items[0].update(due='2026-11-18T18:00:00+08:00', early_reminder_minutes=10080,
                           early_reminder={'unit':2,'count':-7})
    current = service.detail('reminder', 'reminder-001')
    args = {'action':'update','kind':'reminder','id':current['id'],
            'expected_revision':current['revision'],'fields':{'early_reminder_minutes':None}}
    execution = resolve(tool, args)
    assert '清除，保留到期提醒' in execution.approval_preview['summary']
    native.items[0]['early_reminder']['count'] = -14
    with pytest.raises(AppleNativeError, match='已被修改'):
        asyncio.run(execution.run())
    assert not native.writes
    args['expected_revision'] = service.detail('reminder','reminder-001')['revision']
    asyncio.run(resolve(tool,args,'call_clear').run())
    assert native.writes[-1][1]['fields'] == {'early_reminder_minutes':None}
    assert native.items[0]['due'] == '2026-11-18T18:00:00+08:00'


def test_reminder_update_uses_existing_due_and_validates_single_early_alarm(tmp_path):
    questions = Questions()
    tool, native, service = make_tool(tmp_path, questions)
    current = service.detail('reminder', 'reminder-001')
    args = {'action':'update','kind':'reminder','id':current['id'],
            'expected_revision':current['revision'],'fields':{'early_reminder_minutes':10080}}
    result = asyncio.run(resolve(tool,args).run())
    assert result['missing_fields'] == ['due_time']
    for invalid in [[10080,20160], 0, -1, True, 1.5, 525601]:
        args['fields']['early_reminder_minutes'] = invalid
        assert validate_tool_args(tool,args) is not None
    assert not native.writes


def store_draft(tmp_path, call_id, args, *, session='session_test', result=None):
    DRAFTS[(str(tmp_path),session,call_id)]=SimpleNamespace(tool_name=NAME,turn_id='turn_test',args=args,result=result or {},is_error=False)


def test_revise_keeps_fields_changes_type_then_changes_date_without_reasking(tmp_path):
    channel = Questions(answer='保留为日程')
    tool, native, _ = make_tool(tmp_path, channel)
    initial = {'action':'create','kind':'reminder','fields':{
        'title':'测试评审','due':'2026-11-18','calendar_id':'list-001'},'unresolved':['early_reminder']}
    store_draft(tmp_path, 'original', initial)
    switched = {'action':'revise','draft_call_id':'original','kind':'event','fields':{
        'start':'2026-11-18T00:00:00+08:00','end':'2026-11-18T23:59:59+08:00',
        'all_day':True,'alarm_minutes_before':[10080,20160]}}
    execution = resolve(tool, switched, 'switched')
    assert execution.need_confirm and channel.requests == []
    assert execution.approval_preview['fields']['title'] == '测试评审'
    assert 'due' not in execution.approval_preview['fields']
    assert execution.approval_preview['fields']['calendar_id'] == 'calendar-event'
    store_draft(tmp_path, 'switched', switched)
    changed = resolve(tool, {'action':'revise','draft_call_id':'switched','fields':{
        'start':'2026-11-17T00:00:00+08:00','end':'2026-11-17T23:59:59+08:00'}}, 'changed')
    assert channel.requests == [] and native.writes == []
    assert changed.need_confirm
    result = asyncio.run(changed.run())
    assert result['saved'] and len(native.writes) == 1
    fields = native.writes[0][1]['fields']
    assert fields['alarm_minutes_before'] == [10080,20160] and fields['title'] == '测试评审'
    assert '11-17' in fields['start'] and fields['all_day'] is True


def test_revise_rejects_other_session_saved_or_cyclic_reference(tmp_path):
    tool, _, _ = make_tool(tmp_path)
    store_draft(tmp_path, 'private', draft(), session='other')
    with pytest.raises(ValueError, match='找不到'):
        resolve(tool, {'action':'revise','draft_call_id':'private'})
    store_draft(tmp_path, 'saved', draft(), result={'saved':True})
    with pytest.raises(ValueError, match='已保存'):
        resolve(tool, {'action':'revise','draft_call_id':'saved'})
    store_draft(tmp_path, 'cycle', {'action':'revise','draft_call_id':'cycle'})
    with pytest.raises(ValueError, match='循环'):
        resolve(tool, {'action':'revise','draft_call_id':'cycle'})


def test_uncertain_write_blocks_drafts_but_allows_read_and_generic_questions(tmp_path):
    channel = Questions()
    tool, native, service = make_tool(tmp_path, channel)
    with service.database() as db:
        db.execute("INSERT INTO operations VALUES ('uncertain','pending','{}')")
    execution = resolve(tool, {'action':'create','kind':'reminder','unresolved':['early_reminder'],
        'fields':{'title':'测试','due':'2026-11-18'}})
    assert not execution.need_confirm
    assert asyncio.run(execution.run())['status'] == 'write_uncertain'
    assert channel.requests == [] and native.writes == []
    assert asyncio.run(resolve(tool, {'action':'query','kind':'event'}).run())['items'] == []
    question = resolve(create_personal_question_tool(channel), {'action':'clarify','text':'出行时希望少换乘还是速度优先？'})
    assert not question.need_confirm
    assert asyncio.run(question.run())['status'] == 'answered'
    assert len(channel.requests) == 1 and native.writes == []
    assert service.status()['uncertain_writes'] == 1


def test_standalone_clarification_does_not_advertise_revisable_draft(tmp_path):
    channel = Questions(answer='11月17日的提前7天和14天各早上9点')
    tool, native, _ = make_tool(tmp_path, channel)
    execution = resolve(create_personal_question_tool(channel), {'action':'clarify','unresolved':['date'],'text':'活动的具体日期是哪一天？'})
    result = asyncio.run(execution.run())
    assert result['status'] == 'answered' and result['answer'] == channel.response.answer
    assert 'draft' not in result and 'draft_call_id' not in result
    assert 'draft' not in channel.requests[0].detail
    assert not execution.need_confirm and native.writes == []


def test_all_day_update_to_nine_am_keeps_dates_and_previews_both_reminders(tmp_path):
    tool, native, service = make_tool(tmp_path)
    original = {'id':'event-001','kind':'event','title':'测试评审','calendar_id':'calendar-event',
        'all_day':True,'start':'2026-11-17T00:00:00+08:00','end':'2026-11-17T23:59:59+08:00',
        'alarm_minutes_before':[10080,20160],'alarms':[{'relative_offset':-604800},{'relative_offset':-1209600}]}
    native.items.append(original.copy())
    item = service.detail('event',original['id'])
    execution = resolve(tool, {'action':'update','kind':'event','id':item['id'],
        'expected_revision':item['revision'],'fields':{'alarm_minutes_before':[19620,9540]}})
    assert execution.need_confirm and native.writes == []
    summary = execution.approval_preview['summary']
    assert '2026年11月03日 09:00' in summary and '2026年11月10日 09:00' in summary
    assert '2026年11月17日 00:00' in summary
    result = asyncio.run(execution.run())
    assert result['saved'] and result['item']['id'] == original['id']
    assert result['item']['start'] == original['start'] and result['item']['end'] == original['end']
    assert len(native.writes) == 1 and native.writes[0][0] == 'update'
    assert native.writes[0][1]['fields'] == {'alarm_minutes_before':[19620,9540]}
