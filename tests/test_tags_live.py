"""Opt-in native write test: only a unique temporary list/reminder; cleaned in finally.

Run with TELOS_TAGS_LIVE=1 after authorizing the freshly built Apple component.
No real model or WeChat requests. Never prints existing reminders or credentials.
"""
import asyncio
import os
from pathlib import Path
from uuid import uuid4
import pytest
from apple_component import apple_client
from telos.agent.tools.types import ToolInvocationContext
from telos.tools.apple.service import AppleService
from telos.tools.apple.interaction import create_personal_plan_tool
from telos.connectors.apple.native import AppleNativeError
from runtime import ROOT

pytestmark=pytest.mark.skipif(os.environ.get('TELOS_TAGS_LIVE')!='1',reason='Requires explicit real Apple test authorization')


def test_native_tags_roundtrip_preview_conflict_and_cleanup(tmp_path):
    helper=Path(os.environ.get('TELOS_TAGS_HELPER',ROOT/'build/native/Telos Harness Apple.app/Contents/MacOS/telos-apple'))
    client=apple_client({},helper.resolve())
    status=client.request('status',{});assert status['events'] and status['reminders']
    service=AppleService(client,tmp_path/'cache')
    tool=create_personal_plan_tool(data_dir=tmp_path,options={},service=service)
    suffix=uuid4().hex[:10];title='Telos标签验证-'+suffix;list_id=None;item_id=None
    def prepare(action,kind,fields=None,identifier=None):
        args={'action':action,'kind':kind}
        if fields is not None:args['fields']=fields
        if identifier:
            current=service.detail(kind,identifier)
            args.update(id=identifier,expected_revision=current['revision'])
        ex=asyncio.run(tool.resolve_execution(args,context=ToolInvocationContext(session_id='tags-live',turn_id=uuid4().hex,tool_call_id=uuid4().hex)))
        assert ex.need_confirm and ex.approval_preview
        return ex
    def test_items():
        scope={**service.scope(), "list_ids":[list_id], "start":"2035-01-01T00:00:00Z", "end":"2035-01-01T00:00:01Z"}
        return [r for r in client.request("snapshot",scope)["items"] if r["kind"]=="reminder" and r["calendar_id"]==list_id]
    def save(action,kind,fields=None,identifier=None):
        return asyncio.run(prepare(action,kind,fields,identifier).run())
    try:
        list_id=save('create','list',{'title':title})['item']['id']
        ex=prepare('create','reminder',{'title':title,'calendar_id':list_id,'tags':['Telos'+suffix,'工作'+suffix],
            'flagged':True,'priority':1,'notes':'Synthetic tag test'})
        assert '#Telos'+suffix in ex.approval_preview['summary']
        assert not test_items()
        item=asyncio.run(ex.run())['item'];item_id=item['id']
        assert set(item['tags'])=={'Telos'+suffix,'工作'+suffix}
        current=service.detail('reminder',item_id);assert current['tags']==item['tags']
        assert test_items()[0]['tags']==item['tags']
        current=save('update','reminder',{'tags':current['tags']+['紧急'+suffix]},item_id)['item']
        assert len(current['tags'])==3 and current['flagged'] and current['priority']==1
        stale=prepare('update','reminder',{'title':title+'-stale'},item_id)
        current=save('update','reminder',{'tags':['修改'+suffix]},item_id)['item']
        with pytest.raises(AppleNativeError) as err:asyncio.run(stale.run())
        assert err.value.code=='conflict'
        current=save('update','reminder',{'notes':'Updated synthetic note'},item_id)['item']
        assert current['tags']==['修改'+suffix] and current['flagged']
        # Native helper rejects tags for events before writing, even without Python validation.
        calendar=next(c for c in service.refresh()['calendars'] if c['kind']=='event' and c['writable'])
        with pytest.raises(AppleNativeError) as err:
            client.request('create',{'kind':'event','fields':{'title':title,'calendar_id':calendar['id'],'tags':['禁止']}})
        assert err.value.code=='invalid_field'
        current=save('update','reminder',{'tags':[]},item_id)['item'];assert current['tags']==[]
        print('TAGS_LIVE: create/read/append/replace/preserve/clear/conflict/event-rejection passed')
    finally:
        if list_id:
            # Only the unique test list is inspected; handles a post-save unknown result safely.
            rows=test_items()
            for row in rows:
                assert row['calendar_id']==list_id and row['title'].startswith(title)
                save('delete','reminder',identifier=row['id'])
            assert not test_items()
            save('delete','list',identifier=list_id)
            print('TAGS_LIVE: temporary reminders and list cleaned')
