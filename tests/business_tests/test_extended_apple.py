import asyncio
import pytest
from telos.tools.apple.features import validate_features, matches_filters
from telos.tools.apple.recurrence import validate_recurrence, recurrence_summary
from telos.connectors.apple.native import AppleNativeError
from business_tests.test_interaction import make_tool, resolve

@pytest.mark.parametrize('frequency,week,months',[('monthly',2,None),('monthly',-1,None),('yearly',2,[1,7])])
def test_ordinal_recurrence_preview(tmp_path,frequency,week,months):
    tool,native,_=make_tool(tmp_path)
    rule={'frequency':frequency,'ordinal_weekdays':[{'day':'mon','week':week}]}
    if months:rule['months']=months
    validate_recurrence(rule)
    ex=resolve(tool,{'action':'create','kind':'reminder','fields':{'title':'test','due':'2027-02-01','recurrence':rule}})
    assert ex.need_confirm and not native.writes
    assert '个星期一' in ex.approval_preview['summary']
    asyncio.run(ex.run())
    assert native.writes[-1][1]['fields']['recurrence']==rule

@pytest.mark.parametrize('rule',[
 {'frequency':'weekly','ordinal_weekdays':[{'day':'mon','week':2}]},
 {'frequency':'monthly','ordinal_weekdays':[{'day':'mon','week':0}]},
 {'frequency':'monthly','ordinal_weekdays':[{'day':'mon','week':True}]},
 {'frequency':'monthly','ordinal_weekdays':[{'day':'mon','week':2}],'month_days':[1]},
 {'frequency':'monthly','ordinal_weekdays':[{'day':'mon','week':2}]*2},
])
def test_invalid_ordinal(rule):
    with pytest.raises(ValueError):validate_recurrence(rule)

@pytest.mark.parametrize('kind,fields', [('event',{'urgent':True}),('list',{'parent_id':'x'}),('reminder',{'travel_time_minutes':10}),('section',{'color':'blue'}),('reminder',{'urgent':True,'due':'2027-01-01'}),('reminder',{'attachments':[{'url':'file:///tmp/a'}]}),('reminder',{'attachments':[{'id':'unknown'}]})])
def test_invalid_features(kind,fields):
    with pytest.raises(ValueError):validate_features(kind,fields)

def test_attachment_change_after_preview_stops_write(tmp_path):
    tool,native,_=make_tool(tmp_path)
    image=tmp_path/'image.png';image.write_bytes(b'old')
    ex=resolve(tool,{'action':'create','kind':'reminder','fields':{'title':'test','attachments':[{'file_path':str(image)}]}})
    assert ex.need_confirm and '未列出的附件将移除' in ex.approval_preview['summary'] and not native.writes
    image.write_bytes(b'changed')
    with pytest.raises(AppleNativeError,match='附件'):asyncio.run(ex.run())
    assert not native.writes

def test_combined_filters_before_pagination(tmp_path):
    _,native,service=make_tool(tmp_path)
    base=native.items[0]
    native.items=[{**base,'id':str(i),'tags':['Work'],'priority':1,'flagged':i==1,'urgent':True,'section_id':'s','parent_id':'p','due':'2027-02-01T09:00:00+08:00'} for i in range(3)]
    result=service.query({'kind':'reminder','tags':['work'],'flagged':True,'urgent':True,'priority':1,'section_ids':['s'],'parent_ids':['p'],'due_start':'2027-02-01','due_end':'2027-02-02','limit':1})
    assert result['total']==1 and result['items'][0]['id']=='1'
    assert service.query({'tags':['missing']})['total']==0

@pytest.mark.parametrize('field,args',[('section_id',{'section_ids':['s']}),('urgent',{'urgent':True})])
def test_unavailable_native_fields_are_not_empty_results(tmp_path,field,args):
    _,native,service=make_tool(tmp_path)
    native.items[0][field]=None
    with pytest.raises(AppleNativeError,match='完整筛选结果'):
        service.query(args)
    assert service.query({'kind':'reminder'})['total']==1
