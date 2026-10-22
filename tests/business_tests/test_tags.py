import asyncio
import pytest
from telos.agent.tools.validation import validate_tool_args
from telos.connectors.apple.native import AppleNativeError
from telos.tools.apple.drafts import materialize_draft
from telos.tools.apple.tags import validate_tags
from business_tests.test_interaction import make_tool, resolve


def test_tags_preview_add_replace_clear_and_preserve(tmp_path):
    tool, native, service = make_tool(tmp_path)
    native.items[0].update(tags=['工作'], flagged=True)
    for i, fields in enumerate([{'tags':['工作','紧急']}, {'tags':['家庭']}, {'title':'新标题'}, {'tags':[]}]):
        current=service.detail('reminder','reminder-001')
        args={'action':'update','kind':'reminder','id':current['id'],
              'expected_revision':current['revision'],'fields':fields}
        execution=resolve(tool,args,call_id=f'edit-{i}')
        assert execution.need_confirm and len(native.writes)==i
        if 'tags' in fields:
            assert ('清空标签' if not fields['tags'] else '#'+fields['tags'][0]) in execution.approval_preview['summary']
        result=asyncio.run(execution.run())['item']
        assert result['tags']==fields.get('tags',current['tags']) and result['flagged']
        assert service.query({'kind':'reminder'})['items'][0]['tags']==result['tags']


def test_tags_create_and_concurrent_change_conflict(tmp_path):
    tool,native,service=make_tool(tmp_path)
    ex=resolve(tool,{'action':'create','kind':'reminder','fields':{'title':'新事项','tags':['工作']}})
    assert ex.need_confirm and not native.writes and '#工作' in ex.approval_preview['summary']
    created=asyncio.run(ex.run())['item']; assert created['tags']==['工作']
    current=service.detail('reminder',created['id'])
    ex=resolve(tool,{'action':'update','kind':'reminder','id':current['id'],
        'expected_revision':current['revision'],'fields':{'tags':['紧急']}},call_id='stale')
    native.items[-1]['tags']=['外部修改']
    with pytest.raises(AppleNativeError,match='修改'):asyncio.run(ex.run())
    assert len(native.writes)==1


@pytest.mark.parametrize('kind',['event','list','group'])
def test_tags_reject_other_kinds_even_partial_draft(tmp_path,kind):
    tool,native,_=make_tool(tmp_path)
    with pytest.raises(ValueError,match='仅适用于提醒事项'):
        resolve(tool,{'action':'create','kind':kind,'fields':{'tags':['工作']}})
    assert not native.writes


@pytest.mark.parametrize('tags',[None,'工作',[1],[''],['#工作'],['工 作'],['工作\n'],['Work','work']])
def test_invalid_tags_do_not_reach_preview(tmp_path,tags):
    tool,native,_=make_tool(tmp_path)
    args={'action':'create','kind':'reminder','fields':{'title':'测试','tags':tags}}
    if validate_tool_args(tool,args) is None:
        with pytest.raises(ValueError):resolve(tool,args)
    else:
        with pytest.raises(ValueError):validate_tags('reminder',args['fields'])
    assert not native.writes


def test_revise_preserves_tags_and_type_switch_drops_them():
    base={'action':'create','kind':'reminder','fields':{'title':'测试','tags':['工作']}}
    lookup=lambda _:base
    assert materialize_draft({'action':'revise','draft_call_id':'old','fields':{'title':'修改'}},lookup)['fields']['tags']==['工作']
    assert 'tags' not in materialize_draft({'action':'revise','draft_call_id':'old','kind':'event','fields':{'start':'2027-02-01T10:00:00+08:00'}},lookup)['fields']
