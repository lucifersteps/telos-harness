import asyncio
from copy import deepcopy

import pytest

from telos.agent.tools.types import ToolInvocationContext
from telos.tools.apple.recurrence import validate_recurrence, recurrence_summary
from telos.tools.apple.tools import create_apple_tool
from business_tests.test_interaction import make_tool, resolve, Questions, draft


@pytest.mark.parametrize('rule', [
    {'frequency':'daily'}, {'frequency':'weekly','interval':2,'weekdays':['mon','fri'],'count':10},
    {'frequency':'monthly','month_days':[-1,15]}, {'frequency':'yearly','months':[1,7]},
    {'frequency':'daily','until':'2027-12-31T23:59:59+08:00'}, None,
])
def test_recurrence_preview_and_native_request(tmp_path, rule):
    tool,native,_ = make_tool(tmp_path)
    args = {'action':'create','kind':'reminder','fields':{'title':'虚构重复','due':'2027-02-01',
        'recurrence':rule,'flagged':True}}
    execution = resolve(tool,args)
    assert execution.need_confirm and not native.writes
    assert recurrence_summary(rule) in execution.approval_preview['summary']
    assert '旗标：已标记' in execution.approval_preview['summary']
    asyncio.run(execution.run())
    assert native.writes[0][1]['fields']['recurrence'] == rule
    assert native.writes[0][1]['fields']['flagged'] is True


@pytest.mark.parametrize('rule', [
    {}, {'frequency':'hourly'}, {'frequency':'daily','interval':0}, {'frequency':'daily','interval':True},
    {'frequency':'daily','weekdays':['mon']}, {'frequency':'weekly','month_days':[1]},
    {'frequency':'monthly','month_days':[0]}, {'frequency':'monthly','month_days':[True]},
    {'frequency':'monthly','month_days':[32]}, {'frequency':'yearly','months':[13]},
    {'frequency':'weekly','weekdays':['mon','mon']}, {'frequency':'weekly','weekdays':[]},
    {'frequency':'daily','count':True}, {'frequency':'daily','count':1,'until':'2027-03-01T00:00:00Z'},
    {'frequency':'daily','until':'2027-03-01'}, {'frequency':'daily','calendar':'chinese'},
    {'frequency':'daily','until':'2026-01-01T00:00:00Z'},
])
def test_invalid_rules_rejected_before_preview(rule):
    with pytest.raises((ValueError, TypeError)):
        validate_recurrence(rule,'2027-02-01T08:00:00+08:00')


def test_missing_first_date_asks_without_writing(tmp_path):
    questions=Questions()
    tool,native,_=make_tool(tmp_path,channel=questions)
    execution=resolve(tool,{'action':'create','kind':'reminder','fields':{'title':'测试','recurrence':{'frequency':'daily'}}})
    assert not execution.need_confirm
    result=asyncio.run(execution.run())
    assert 'due' in result['missing_fields'] and not native.writes


@pytest.mark.parametrize('scope', ['this','future'])
def test_recurring_event_scope_is_previewed_and_forwarded(tmp_path,scope):
    tool,native,apple=make_tool(tmp_path)
    native.items=[{'id':'event-001','kind':'event','title':'测试','calendar_id':'calendar-event',
        'start':'2027-02-01T08:00:00+08:00','end':'2027-02-01T09:00:00+08:00',
        'recurring':True,'recurrence_rules':[{'frequency':'daily','interval':1}]}]
    current=apple.detail('event','event-001')
    args={'action':'update','kind':'event','id':current['id'],'expected_revision':current['revision'],
          'recurrence_scope':scope,'fields':{'title':'修改标题'}}
    execution=resolve(tool,args)
    assert ('仅本次日程' if scope=='this' else '本次及以后') in execution.approval_preview['summary']
    asyncio.run(execution.run())
    assert native.writes[0][1]['recurrence_scope']==scope


def test_scope_missing_asks_and_direct_tool_rejects(tmp_path):
    tool,native,apple=make_tool(tmp_path,channel=Questions())
    native.items[0].update(recurring=True,recurrence_rules=[{'frequency':'daily'}])
    current=apple.detail('reminder','reminder-001')
    args={'action':'update','kind':'reminder','id':current['id'],'expected_revision':current['revision'],
          'fields':{'recurrence':None}}
    execution=resolve(tool,args)
    assert not execution.need_confirm
    assert 'recurrence_scope' in asyncio.run(execution.run())['missing_fields']
    direct=create_apple_tool('apple.update',data_dir=tmp_path,options={},service=apple)
    with pytest.raises(ValueError,match='整条'):
        asyncio.run(direct.resolve_execution({k:v for k,v in args.items() if k!='action'},
                    context=ToolInvocationContext(tool_call_id='direct')))
    assert not native.writes


def test_flag_clear_preserves_recurrence_and_conflict_is_detected(tmp_path):
    tool,native,apple=make_tool(tmp_path)
    native.items[0].update(flagged=True,recurring=True,recurrence_rules=[{'frequency':'daily'}])
    current=apple.detail('reminder','reminder-001')
    args={'action':'update','kind':'reminder','id':current['id'],'expected_revision':current['revision'],'fields':{'flagged':False}}
    execution=resolve(tool,args)
    assert '旗标：取消标记' in execution.approval_preview['summary']
    native.items[0]['recurrence_rules']=[{'frequency':'weekly'}]
    from telos.connectors.apple.native import AppleNativeError
    with pytest.raises(AppleNativeError,match='修改'):
        asyncio.run(execution.run())
    assert not native.writes
    args['expected_revision']=apple.detail('reminder',current['id'])['revision']
    result=asyncio.run(resolve(tool,args,call_id='fresh').run())
    assert result['item']['recurrence_rules']==[{'frequency':'weekly'}]
    assert result['item']['flagged'] is False


def test_event_flag_and_single_occurrence_rule_change_rejected(tmp_path):
    tool,_,_=make_tool(tmp_path)
    with pytest.raises(ValueError,match='旗标'):
        resolve(tool,draft(flagged=True))


def test_rule_change_preview_explains_future_exceptions(tmp_path):
    tool,native,apple=make_tool(tmp_path)
    native.items=[{'id':'event-1','kind':'event','title':'重复日程','calendar_id':'calendar-event',
        'start':'2027-02-01T08:00:00+08:00','end':'2027-02-01T09:00:00+08:00','recurring':True}]
    current=apple.detail('event','event-1')
    execution=resolve(tool,{'action':'update','kind':'event','id':current['id'],
        'expected_revision':current['revision'],'recurrence_scope':'future','fields':{'recurrence':None}})
    assert '单独修改的未来实例' in execution.approval_preview['summary']
    assert not native.writes
