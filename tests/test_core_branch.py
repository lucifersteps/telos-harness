"""Core/experimental boundaries, persisted settings and managed task cancellation."""
import asyncio
import json
import time

from proactive import ProactiveStore, defaults, validate
from settings import read_config, write_config, runtime_options
from support import Harness, call
from test_proactive import configured
from test_schedule import wait


def test_core_ignores_experiments_and_preserves_them_on_save(tmp_path):
    config={'version':1,'health_receiver':{'enabled':True,'root':'/missing-experimental-receiver'},
            'activity_history':{'aw_url':'http://not-used.invalid'},
            'proactive':{**defaults(),'policy':{'enabled':True,'id':'goal_execution','configs':{'goal_execution':{'observation_enabled':True}}}}}
    write_config(tmp_path,config)
    runtime=runtime_options(config)
    assert 'health_receiver' not in runtime and 'policy' not in runtime['proactive']
    write_config(tmp_path,{'version':1,'proactive':{**runtime['proactive'],'notification_mode':'always'}})
    saved=read_config(tmp_path)
    assert saved['proactive']['policy']==config['proactive']['policy']
    assert saved['health_receiver']==config['health_receiver']
    assert saved['activity_history']==config['activity_history']
    assert saved['proactive']['notification_mode']=='always'


def test_switch_cancels_old_experimental_checks_but_keeps_history_and_user_tasks(tmp_path):
    store=configured(tmp_path,heartbeat=False,fixed=False)
    config,revision,_=store.sync()
    with store.db() as db:
        db.execute('CREATE TABLE user_schedules(id TEXT PRIMARY KEY, prompt TEXT)')
        db.execute("INSERT INTO user_schedules VALUES('user-task','每天九点发日程')")
        old={**config,'policy':{'enabled':True,'id':'goal_execution'}}
        db.execute('UPDATE proactive_state SET config=?',(json.dumps(old),))
        queued=store._insert(db,'wechat-test',revision,'followup',time.time()+60,'旧督促',state='queued')
        sent=store._insert(db,'wechat-test',revision,'fixed',time.time()-60,'历史',state='sent')
        db.execute('UPDATE proactive_events SET observation=? WHERE id=?',('旧观察',sent))
    _,new_revision,_=store.sync()
    assert new_revision==revision+1
    assert store.event(queued,'wechat-test')['state']=='cancelled'
    assert store.event(sent,'wechat-test')['observation']=='旧观察'
    with store.db() as db:assert db.execute('SELECT prompt FROM user_schedules').fetchone()[0]=='每天九点发日程'
    assert store.sync()[1]==new_revision


def test_basic_heartbeat_has_no_policy_prerequisites(tmp_path):
    async def run():
        h=Harness(tmp_path,['已连接'])
        store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            await h.submit('连接问候');await h.runtime.close()
            _,revision,_=store.sync()
            with store.db() as db:eid=store._insert(db,h.session,revision,'heartbeat',time.time()+1,'简单问候')
            h.model.script=[call('greeting',{'message':'晚上好，有需要随时找我。'},'notify_user'),'内部结束']
            h.runtime=h.restart();await h.runtime.start()
            await wait(lambda:store.event(eid,h.session)['state']=='sent')
            assert h.sent.count('晚上好，有需要随时找我。')==1
            tools={t['name']:t for t in h.model.requests[-2]['tools']}
            assert 'activity_history' not in tools and 'baidu_maps' in tools
            assert set(tools['notify_user'].get('input_schema',tools['notify_user'].get('parameters'))['properties'])=={'message'}
            assert not h.native.writes
        finally:await h.close()
    asyncio.run(run())


def test_default_contact_can_remain_quiet_and_fixed_is_required(tmp_path):
    assert defaults()['notification_mode']=='on_demand'
    store=ProactiveStore(tmp_path)
    config=validate()
    assert store._render_event({'kind':'heartbeat'},config)['notification_required'] is False
    assert store._render_event({'kind':'fixed'},config)['notification_required'] is True
    config['notification_mode']='always'
    assert store._render_event({'kind':'heartbeat'},config)['notification_required'] is True
