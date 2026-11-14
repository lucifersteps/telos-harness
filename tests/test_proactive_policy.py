"""Policy migration/lifecycle: preserve timing, remove current domain rules."""
import asyncio,json,time
import pytest
from proactive import ProactiveStore,defaults,validate
from proactive_policies import goal_execution
from settings import read_config,write_config
from test_proactive import configured,stamp
from support import Harness,activity_read,call
from test_schedule import wait
from test_current_state import states
from test_history import results,tool_text

def legacy():
    return {'enabled':True,'session':'wechat-test','timezone':'Asia/Shanghai','goal_list_ids':['goals'],'action_list_ids':[],
        'heartbeat':{'enabled':True,'start':'08:00','end':'01:00','interval_minutes':30,'prompt':goal_execution.DEFAULT_PROMPT},
        'fixed':[{'id':i,'time':t,'enabled':True,'days':list(range(7)),'prompt':p} for i,t,p in goal_execution.PRESETS]}

def test_legacy_migration_keeps_pending_ids_times_and_active_content(tmp_path):
    now=stamp('2026-11-06T07:59:00+08:00');old=legacy()
    write_config(tmp_path,{'version':1,'proactive':old});store=ProactiveStore(tmp_path,now=lambda:now)
    before=store.plan();revision=store.sync()[1]
    with store.db() as db:db.execute('UPDATE proactive_state SET config=?',(json.dumps(old,sort_keys=True,ensure_ascii=False),))
    migrated=validate(old);assert 'goal_list_ids' not in migrated
    assert migrated['policy']['configs']['goal_execution']['goal_list_ids']==['goals']
    config=read_config(tmp_path);config['proactive']=migrated;write_config(tmp_path,config)
    after=store.plan();assert before==after and store.sync()[1]==revision
    for event in before[:8]:
        with store.db() as db:db.execute('UPDATE proactive_events SET input_id=? WHERE id=?',(event['id'],event['id']))
        view=store.context('wechat-test',[event['id']])['events'][0]
        expected=goal_execution.DEFAULT_PROMPT if event['kind']=='heartbeat' else dict((i,p)for i,t,p in goal_execution.PRESETS)[next(i for i,t,p in goal_execution.PRESETS if t==__import__('datetime').datetime.fromtimestamp(event['due'],__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).strftime('%H:%M'))]
        assert view['prompt']==expected and view['notification_required']

def test_disable_switch_and_reenable_preserve_schedule_but_clear_policy_followups(tmp_path):
    now=stamp('2026-11-06T07:59:00+08:00');write_config(tmp_path,{'version':1,'proactive':legacy()});store=ProactiveStore(tmp_path,now=lambda:now)
    before=store.plan();first=before[0]
    with store.db() as db:db.execute('UPDATE proactive_events SET input_id=? WHERE id=?',('check',first['id']))
    follow=store.followup('wechat-test','policy-follow','2026-11-06T09:01:00+08:00','旧策略跟进')
    config=read_config(tmp_path);config['proactive']=validate(config['proactive']);config['proactive']['policy']['enabled']=False;write_config(tmp_path,config)
    off=store.context('wechat-test',['check']);assert off['policy']=={'id':None,'enabled':False,'context':{},'config':{}}
    assert '缺乏计划' not in off['events'][0]['prompt'];assert 'habit_reminder_allowed' not in off['events'][0]
    assert store.event(follow['event_id'],'wechat-test')['state']=='cancelled'
    assert [(e['id'],e['due']) for e in store.plan()]==[(e['id'],e['due']) for e in before]
    config['proactive']['policy'].update(enabled=True,id='basic');write_config(tmp_path,config)
    basic=store.context('wechat-test',['check']);assert basic['policy']['context']=={} and basic['policy']['id']=='basic'
    config['proactive']['policy']['id']='goal_execution';write_config(tmp_path,config)
    active=store.context('wechat-test',['check']);assert active['policy']['context']['query_requirements']['goal_list_ids']==['goals']
    assert '读取目标' in active['events'][0]['prompt']

def test_only_one_known_policy_and_custom_reminders_preserved():
    for bad in [[],{'id':'unknown'},{'enabled':'yes'},{'configs':{'unknown':{}}}]:
        with pytest.raises(ValueError):validate({'policy':bad})
    old=legacy();old['fixed'][1]['prompt']='记得带钥匙'
    migrated=validate(old);assert migrated['fixed'][1]['prompt']=='记得带钥匙'
    assert 'morning' not in migrated['policy']['configs']['goal_execution']['fixed_prompts']


def test_same_native_session_enable_disable_switch_restart(tmp_path):
    async def run():
        h=Harness(tmp_path,['已绑定。']);store=configured(tmp_path/'data',heartbeat=False,fixed=False)
        try:
            await h.submit('绑定会话')
            for index,(enabled,policy_id,needs_evidence) in enumerate([(True,'goal_execution',True),(False,'goal_execution',False),(True,'basic',False),(True,'goal_execution',True)]):
                await h.runtime.close()
                config=read_config(tmp_path/'data');config['proactive']['policy'].update(enabled=enabled,id=policy_id);write_config(tmp_path/'data',config)
                _,revision,_=store.sync()
                with store.db() as db:eid=store._insert(db,h.session,revision,'heartbeat',time.time()+1,'普通测试检查')
                message=f'本地策略测试{index}'
                send=call('send-'+str(index),{'message':message},'notify_user')
                if not needs_evidence:
                    # No assessment is required for disabled/basic policy.
                    send['tool_calls'][0]['function']['arguments']=json.dumps({'message':message})
                script=([call('blocked-'+str(index),{'message':'不应发送'},'notify_user'),activity_read('read-'+str(index))] if needs_evidence else [])+[send,'结束。']
                first=len(h.model.requests);h.model.script=script;h.runtime=h.restart();await h.runtime.start()
                await wait(lambda:store.event(eid,h.session)['state']=='sent')
                await wait(lambda:len(h.model.requests)>=first+len(script))
                current=states(h.model.requests[-1])[0]['current']['telos:proactive-context']['proactive']
                assert current['policy']['enabled']==enabled
                if needs_evidence:
                    assert 'plan_missing' in current['policy']['instructions']
                    assert any('TELOS_ACTIVITY_REQUIRED' in tool_text(r) for r in results(h.model.requests[-1]) if r.get('is_error'))
                else:
                    assert not current['policy'].get('context')
                    assert 'query_requirements' not in current['settings']
                    assert '缺乏计划' not in current['events'][0]['prompt']
                assert h.sent[-1]==message
            assert not h.native.writes
        finally:await h.close()
    asyncio.run(run())


def test_switch_rejects_stale_running_and_queued_rows(tmp_path):
    store=configured(tmp_path,now=lambda:stamp('2026-11-06T10:00:00+08:00'))
    before=store.plan();revision=store.sync()[1]
    with store.db() as db:
        ids=[store._insert(db,'wechat-test',revision,'heartbeat',store.now(),state, state=state) for state in ('running','queued')]
    stale=[store.event(i,'wechat-test') for i in ids]
    config=read_config(tmp_path);config['proactive']['policy']['enabled']=False;write_config(tmp_path,config)
    assert all(not store.eligible(e) for e in stale)
    assert all(store.event(i,'wechat-test')['state']=='cancelled' for i in ids)
    assert [(e['id'],e['due']) for e in store.plan()]==[(e['id'],e['due']) for e in before]


def test_status_and_context_unload_legacy_prompts_and_on_demand_is_consistent(tmp_path):
    now=stamp('2026-11-06T08:01:00+08:00');write_config(tmp_path,{'version':1,'proactive':legacy()})
    store=ProactiveStore(tmp_path,now=lambda:now);store.plan()
    config=read_config(tmp_path);config['proactive']=validate(config['proactive'])
    config['proactive']['policy']['enabled']=False;write_config(tmp_path,config)
    assert all('缺乏计划' not in r['prompt'] and '读取目标' not in r['prompt'] for r in store.status()['pending'])
    config['proactive']['policy']['enabled']=True
    config['proactive']['policy']['configs']['goal_execution']['notification_mode']='on_demand';write_config(tmp_path,config)
    e=next(e for e in store.plan() if e['kind']=='heartbeat')
    view=store._render_event(e,store.sync()[0])
    assert not view['notification_required'] and '必须调用 notify_user' not in view['prompt']
    with pytest.raises(ValueError):validate({'fixed':None})


def test_legacy_stored_prompt_is_removed_even_after_edit_and_disable(tmp_path):
    old=legacy();old['heartbeat']['prompt']='我的旧版定制督促提示词'
    now=stamp('2026-11-06T08:01:00+08:00');write_config(tmp_path,{'version':1,'proactive':old})
    store=ProactiveStore(tmp_path,now=lambda:now);before=store.plan()
    with store.db() as db:
        db.execute('UPDATE proactive_state SET config=?',(json.dumps(old),))
        db.execute("UPDATE proactive_events SET prompt=? WHERE kind='heartbeat'",(old['heartbeat']['prompt'],))
    config=read_config(tmp_path);config['proactive']=validate(old)
    config['proactive']['policy']['configs']['goal_execution']['heartbeat_prompt']='新的督促提示词'
    write_config(tmp_path,config);after=store.plan()
    assert [(e['id'],e['due']) for e in before]==[(e['id'],e['due']) for e in after]
    heartbeat=next(e for e in after if e['kind']=='heartbeat')
    assert store._render_event(heartbeat,store.sync()[0])['prompt']=='新的督促提示词'
    config['proactive']['policy']['enabled']=False;write_config(tmp_path,config)
    assert all('督促提示词' not in e['prompt'] for e in store.status()['pending'])
