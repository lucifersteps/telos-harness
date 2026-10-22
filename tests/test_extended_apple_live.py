"""Opt-in macOS tests, confined to unique temporary containers and cleaned afterwards."""
import asyncio
import base64
import os
from pathlib import Path
from uuid import uuid4
import pytest
from apple_component import apple_client
from telos.tools.apple.service import AppleService
from telos.tools.apple.interaction import create_personal_plan_tool
from telos.agent.tools.types import ToolInvocationContext
from telos.connectors.apple.native import AppleNativeError

pytestmark=pytest.mark.skipif(os.environ.get('TELOS_EXTENDED_LIVE')!='1',reason='Explicit Apple test authorization required')

def test_extended_native_roundtrip(tmp_path):
    helper=Path('build/validation/Telos Apple Validation.app/Contents/MacOS/telos-apple').resolve()
    client=apple_client({},helper);service=AppleService(client,tmp_path/'cache')
    tool=create_personal_plan_tool(data_dir=tmp_path,options={},service=service)
    title='Telos扩展验证-'+uuid4().hex[:8]; list_id=calendar_id=None
    def prepare(action,kind,fields=None,id=None,scope=None):
        args={'action':action,'kind':kind}
        if fields is not None:args['fields']=fields
        if id:
            item=service.detail(kind,id);args.update(id=id,expected_revision=item['revision'])
            if item.get('recurring'):args['recurrence_scope']=scope or ('future' if kind=='event' else 'series')
        ex=asyncio.run(tool.resolve_execution(args,context=ToolInvocationContext(session_id='extended-live',turn_id=uuid4().hex,tool_call_id=uuid4().hex)))
        assert ex.need_confirm, asyncio.run(ex.run()) if not ex.need_confirm else ''
        return ex
    def save(action,kind,fields=None,id=None,scope=None):
        return asyncio.run(prepare(action,kind,fields,id,scope).run())
    def rows():
        return [r for r in client.request('snapshot',{**service.scope(),'list_ids':[list_id]})['items'] if r['kind']=='reminder' and r['calendar_id']==list_id]
    try:
        list_id=save('create','list',{'title':title})['item']['id']
        section=save('create','section',{'title':title+'分区','calendar_id':list_id})['item']
        section=save('update','section',{'title':title+'改名分区'},section['id'])['item'];assert section['title']==title+'改名分区'
        parent=save('create','reminder',{'title':title+'父任务','calendar_id':list_id,'due':'2027-02-08T10:00:00+08:00','urgent':True,'tags':['Telos验证'],'flagged':True})['item']
        assert parent['urgent'] and parent['tags']==['Telos验证']
        parent=save('update','reminder',{'section_id':section['id']},parent['id'])['item'];assert parent['section_id']==section['id']
        assert parent['id'] in service.detail('section',section['id'])['member_ids']
        with pytest.raises(AppleNativeError):save('delete','section',id=section['id'])
        parent=save('update','reminder',{'completed':True},parent['id'])['item']
        assert parent['id'] in service.detail('section',section['id'])['member_ids']
        parent=save('update','reminder',{'section_id':'','completed':False},parent['id'])['item'];assert parent['section_id']==''
        save('delete','section',id=section['id'])
        print('EXTENDED_LIVE: section create/rename/assign/read/completed/unassign/delete and nonempty guard passed',flush=True)
        child=save('create','reminder',{'title':title+'子任务','calendar_id':list_id,'parent_id':parent['id']})['item']
        assert child['parent_id']==parent['id'] and child['id'] in service.detail('reminder',parent['id'])['subtask_ids']
        with pytest.raises(AppleNativeError):save('delete','reminder',id=parent['id'])
        with pytest.raises(AppleNativeError):save('update','reminder',{'parent_id':child['id']},parent['id'])
        child=save('update','reminder',{'completed':True},child['id'])['item']
        assert child['id'] in service.detail('reminder',parent['id'])['subtask_ids']
        child=save('update','reminder',{'parent_id':''},child['id'])['item'];assert child['parent_id']==''
        parent=save('update','reminder',{'urgent':False,'notes':'Synthetic test'},parent['id'])['item']
        assert not parent['urgent'] and parent['flagged'] and parent['tags']==['Telos验证']
        print('EXTENDED_LIVE: subtask create/read/complete/detach/cycle/deletion guard and urgent toggle passed',flush=True)
        image=tmp_path/'sample.png';image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII='))
        parent=save('update','reminder',{'attachments':[{'file_path':str(image)},{'url':'https://example.com/telos-test'}]},parent['id'])['item']
        assert len(parent['attachments'])==2
        keep=parent['attachments'][0]['id']
        parent=save('update','reminder',{'attachments':[{'id':keep}]},parent['id'])['item'];assert [a['id'] for a in parent['attachments']]==[keep]
        parent=save('update','reminder',{'title':title+'附件保留'},parent['id'])['item'];assert len(parent['attachments'])==1
        parent=save('update','reminder',{'attachments':[]},parent['id'])['item'];assert parent['attachments']==[]
        print('EXTENDED_LIVE: image/url attachments read/replace/preserve/clear passed',flush=True)
        rule={'frequency':'monthly','ordinal_weekdays':[{'day':'mon','week':2}],'count':3}
        parent=save('update','reminder',{'recurrence':rule},parent['id'])['item'];assert parent['recurrence_rules'][0]['ordinal_weekdays']==rule['ordinal_weekdays']
        parent=save('update','reminder',{'recurrence':None},parent['id'])['item'];assert not parent['recurring']
        assert service.query({'kind':'reminder','calendar_ids':[list_id],'tags':['Telos验证'],'flagged':True,'priority':0,'due_start':'2027-02-08','due_end':'2027-02-09'})['total']==1
        calendar=save('create','event_calendar',{'title':title,'color':'blue'})['item'];calendar_id=calendar['id']
        calendar=save('update','event_calendar',{'title':title+'改名','color':'red'},calendar_id)['item'];assert calendar['title']==title+'改名'
        event=save('create','event',{'title':title+'会议','calendar_id':calendar_id,'start':'2027-02-08T10:00:00+08:00','end':'2027-02-08T11:00:00+08:00','travel_time_minutes':30,'recurrence':rule})['item']
        assert event['travel_time_minutes']==30 and 'attendees' in event and 'availability' in event
        event=save('update','event',{'travel_time_minutes':15},event['id'])['item'];assert event['travel_time_minutes']==15
        event=save('update','event',{'travel_time_minutes':0},event['id'])['item'];assert event['travel_time_minutes']==0
        print('EXTENDED_LIVE: ordinal recurrence, combined query, calendar create/rename/color, travel create/change/clear, meeting detail fields passed',flush=True)
    finally:
        # Recover receipts only after a fresh read, restricted to this run's test objects.
        pending=service.status()['uncertain_operation_ids']
        if list_id:
            remaining=rows()
            if pending:
                print('EXTENDED_LIVE: inspecting temporary objects after uncertain write',flush=True)
                for op in pending:service.resolve_write(op,saved=True)
            for r in sorted(remaining,key=lambda r:bool(r.get('subtask_ids'))):
                assert r['title'].startswith(title)
                save('delete','reminder',id=r['id'])
            for section in service.query({'kind':'section','calendar_ids':[list_id]})['items']:
                save('delete','section',id=section['id'])
            save('delete','list',id=list_id)
        if calendar_id:
            save('delete','event_calendar',id=calendar_id)
        print('EXTENDED_LIVE: temporary test containers cleaned',flush=True)
