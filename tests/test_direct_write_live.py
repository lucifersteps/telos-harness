"""Opt-in current macOS direct-write/undo checks with temporary Apple objects only."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4
import pytest
from apple_component import apple_client
from business import Business
from telos.tools.apple.service import AppleService
from telos.tools.apple.interaction import create_personal_plan_tool
from telos.agent.tools.types import ToolInvocationContext

pytestmark=pytest.mark.skipif(os.environ.get('TELOS_DIRECT_LIVE')!='1',reason='Explicit live validation required')

def test_native_direct_create_update_and_undo(tmp_path):
    client=apple_client({},Path('build/validation/Telos Apple Validation.app/Contents/MacOS/telos-apple').resolve())
    service=AppleService(client,tmp_path/'apple')
    tool=create_personal_plan_tool(data_dir=tmp_path,options={},service=service)
    async def run():
        b=Business(tmp_path,service)
        async def control(action,kind,fields=None,id=None):
            args={'action':action,'kind':kind}
            if fields is not None:args['fields']=fields
            if id:args.update(id=id,expected_revision=service.detail(kind,id)['revision'])
            ex=await tool.resolve_execution(args,context=ToolInvocationContext(session_id='test',turn_id=uuid4().hex,tool_call_id=uuid4().hex))
            return await ex.run()
        async def direct(args):
            key=uuid4().hex;b.accept('test',key,'explicit temporary test');b.active['test']={'key':key}
            result=await b.execute('test',uuid4().hex,'personalPlan',args)
            assert result.get('saved'),result
            return result
        containers=[];items=[]
        title='Telos直接写入验证-'+uuid4().hex[:8]
        try:
            for kind in ['list','event_calendar']:
                c=(await control('create',kind,{'title':title}))['item'];containers.append((kind,c['id']))
            for kind,cid in [('reminder',containers[0][1]),('event',containers[1][1])]:
                fields={'title':title,'calendar_id':cid,**({'due':'2027-02-08','tags':['Telos验证']} if kind=='reminder' else {'start':'2027-02-08T10:00:00+08:00','end':'2027-02-08T11:00:00+08:00','alarm_minutes_before':[30]})}
                created=await direct({'action':'create','kind':kind,'fields':fields})
                items.append((kind,created['item']['id']))
                assert (await b.undo('test'))['undone']
                assert (await b.undo('test'))['already_undone'];items.pop()
                created=await direct({'action':'create','kind':kind,'fields':fields})
                items.append((kind,created['item']['id']))
                before=service.detail(kind,created['item']['id'])
                patch={'title':title+'改名',**({'tags':['新标签'],'due':'2027-02-09'} if kind=='reminder' else {'start':'2027-02-08T12:00:00+08:00','end':'2027-02-08T13:00:00+08:00'})}
                changed=await direct({'action':'update','kind':kind,'id':before['id'],'expected_revision':before['revision'],'fields':patch})
                items[-1]=(kind,changed['item']['id'])
                undo=await b.undo('test');assert undo.get('undone'),undo
                restored=undo['item'];items[-1]=(kind,restored['id'])
                assert all(restored.get(k)==before.get(k) for k in patch)
                print('LIVE_DIRECT:',kind,'create/delete undo and update/restore passed',flush=True)
        finally:
            for kind,id in reversed(items):await control('delete',kind,id=id)
            for kind,id in reversed(containers):await control('delete',kind,id=id)
    asyncio.run(run())
