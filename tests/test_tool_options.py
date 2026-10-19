"""Feature switches gate execution and I/O, retaining the same confirmation ledger."""
import asyncio
import pytest
from business import Business
from settings import runtime_options
from support import Apple, draft
from telos.tools.apple.service import AppleService
from telos.agent.tools.types import ToolInputError

@pytest.mark.parametrize('config',[{'tools':{'maps':'false'}},{'tools':{'other':True}},{'model_selection':{'provider':'p'}},{'model_selection':{'provider':'p','model':'m','api_key':'secret'}}])
def test_invalid_options(config):
    with pytest.raises(ValueError):runtime_options(config)

def test_disabled_tools_never_touch_external_services(tmp_path):
    class Forbidden:
        def __getattr__(self,name):raise AssertionError('external access: '+name)
    async def run():
        b=Business(tmp_path,Forbidden(),{'tools':{'apple':False,'maps':False}})
        b.active['s']={'key':'request','text':'查地图并创建日程'}
        assert [t['name'] for t in b.schemas('apple')]==['ask_user'] and b.schemas('maps')==[]
        assert 'external_state' not in await b.context('s')
        for name,args in [('personalPlan',draft()),('baidu_maps',{'operation':'weather','parameters':{'region':'北京'}})]:
            with pytest.raises(ToolInputError,match='关闭'):await b.execute('s',name,name,args)
        b.set_pending('s',{'kind':'approval','delivered':True,'id':'kept'})
        assert (await b.confirm('s'))['status']=='disabled'
        assert (await b.reconcile('s',b.pending('s')))['status']=='disabled'
        assert b.pending('s')['id']=='kept'
    asyncio.run(run())

def test_disable_restart_reenable_confirm_writes_once(tmp_path):
    async def run():
        native=Apple();apple=AppleService(native,tmp_path/'apple')
        b=Business(tmp_path,apple);b.active['s']={'key':'request','text':'创建会议'}
        await b.execute('s','draft','personalPlan',draft())
        pending=b.pending('s');pending['delivered']=True;b.set_pending('s',pending)
        off=Business(tmp_path,apple,{'tools':{'apple':False}})
        off.active['s']={'key':'confirm-off','text':'1'}
        assert (await off.confirm('s'))['status']=='disabled' and not native.writes
        on=Business(tmp_path,apple);on.active['s']={'key':'confirm-on','text':'1'}
        await on.confirm('s');await on.confirm('s')
        assert len(native.writes)==1 and on.pending('s') is None
    asyncio.run(run())
