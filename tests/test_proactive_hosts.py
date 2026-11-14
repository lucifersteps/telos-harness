"""Compiled package in isolated official CLI/Desktop hosts; no live accounts."""
import asyncio
import json
import time
import pytest
from proactive import ProactiveStore, defaults
from settings import read_config, write_config
from support import activity_read, call
from test_proactive import next_check
from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, Installed, run_async


def isolate_activity(root,services):
    config=read_config(root)
    # Exercise unavailable sources without touching the developer's AW/Screen Time.
    config['activity_history']={'aw_url':services.url,'screen_time_db':str(root/'missing-test-screen-time.db')}
    write_config(root,config)


@pytest.mark.parametrize('desktop',[False,True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires built package and official Desktop')
@run_async
async def test_proactive_configuration_notification_pause_and_restart_in_hosts(tmp_path,desktop):
    h=Desktop(tmp_path) if desktop else Installed(tmp_path,[])
    root=h.home/'telos'
    def sent():return json.dumps(h.services.sent,ensure_ascii=False)
    try:
        if desktop:
            await h.start();await h.action('save',tools={'apple':False,'maps':False})
            config=read_config(root);config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(root,config)
            isolate_activity(root,h.services)
            await h.action('login');await h.action('start')
        else:
            isolate_activity(root,h.services)
            h.run('--telos-login');h.start()
        h.model.script=['绑定成功。'];h.services.push('bind','测试主动消息设置')
        await h.wait(lambda:'绑定成功。' in sent())
        store=ProactiveStore(root);session=store.status()['sessions'][0]['session']
        p=defaults();p.update(enabled=True,session=session);p['heartbeat']['enabled']=False
        p['fixed'][1]['prompt']='TEST_FIXED_PROMPT：今日最重要的一件事。'
        if desktop:
            await h.action('apply',proactive=p)
            assert h.get()['running'] and h.get()['config']['proactive']==p
        else:
            h.stop();config=read_config(root);config['proactive']=p;write_config(root,config);h.start()
        h.model.script=[lambda _:next_check('first',2),'已登记第一次。',activity_read(),call('notify',{'message':'安装包主动消息。'},'notify_user'),'静默总结。']
        h.services.push('first','登记下次检查')
        await h.wait(lambda:'安装包主动消息。' in sent())
        assert '静默总结' not in sent()
        if desktop:
            await h.action('proactive',operation={'action':'pause'})
            assert h.get()['running'] and h.get()['proactive']['paused_until']>0
            await h.action('proactive',operation={'action':'resume'})
            assert h.get()['running'] and h.get()['proactive']['paused_until']==0
        h.model.script=[lambda _:next_check('cold',5),'已登记重启检查。']
        h.services.push('cold','稍后再检查')
        await h.wait(lambda:'已登记重启检查。' in sent())
        assert any(e['kind']=='followup' for e in store.status()['pending'])
        h.stop();await asyncio.sleep(5)
        h.model.script=[activity_read('restart-read'),call('restart-notify',{'message':'重启后的主动消息。'},'notify_user'),'不要转发这段。']
        if desktop:await h.start()
        else:h.start()
        await h.wait(lambda:'重启后的主动消息。' in sent())
        assert sent().count('安装包主动消息。')==sent().count('重启后的主动消息。')==1
        assert '不要转发' not in sent()
        if desktop:
            h.model.script=[lambda _:next_check('cancel',8),'已登记待取消检查。']
            h.services.push('cancel','登记一条后续检查')
            await h.wait(lambda:'已登记待取消检查。' in sent())
            pending=next(e for e in h.get()['proactive']['pending'] if e['kind']=='followup')
            await h.action('proactive',operation={'action':'cancel','event_id':pending['id']})
            assert store.event(pending['id'],session)['state']=='cancelled' and h.get()['running']
    finally:await h.close()


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires built package and official Desktop')
@run_async
async def test_desktop_package_recovers_after_failed_model_with_expired_queue_head(tmp_path):
    h=Desktop(tmp_path);root=h.home/'telos'
    try:
        await h.start();await h.action('save',tools={'apple':False,'maps':False})
        config=read_config(root);config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(root,config)
        isolate_activity(root,h.services)
        await h.action('login');await h.action('start')
        h.model.script=['绑定成功。'];h.services.push('bind-queue','绑定队列恢复测试')
        await h.wait(lambda:'绑定成功。' in json.dumps(h.services.sent,ensure_ascii=False))
        store=ProactiveStore(root);session=store.status()['sessions'][0]['session']
        await h.action('stop')
        config=read_config(root);p=defaults();p.update(enabled=True,session=session);p['heartbeat']['enabled']=False
        config['proactive']=p;write_config(root,config)
        _,revision,_=store.sync();due=time.time()+2
        with store.db() as db:
            first=store._insert(db,session,revision,'heartbeat',due,'FAIL_IN_DESKTOP')
            expired=store._insert(db,session,revision,'followup',due+.1,'EXPIRED')
            valid=store._insert(db,session,revision,'heartbeat',due+.2,'VALID')
            db.execute('UPDATE proactive_events SET expires=? WHERE id=?',(due+1,expired))
        def fail(_request):
            time.sleep(1.5);return {'_http_status':401}
        h.model.script=[fail,activity_read(),call('desktop-recovered',{'message':'桌面安装包队列恢复成功。'},'notify_user'),'内部结束。']
        await h.action('start')
        await h.wait(lambda:store.event(valid,session)['state']=='sent')
        assert store.event(first,session)['state']=='failed'
        assert store.event(expired,session)['state']=='expired'
        sent=json.dumps(h.services.sent,ensure_ascii=False)
        assert sent.count('桌面安装包队列恢复成功。')==1 and '内部结束' not in sent
    finally:await h.close()


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires built package and official Desktop')
@run_async
async def test_desktop_policy_enable_disable_switch_and_quiet(tmp_path):
    h=Desktop(tmp_path);root=h.home/'telos'
    try:
        await h.start();await h.action('save',tools={'apple':False,'maps':False})
        config=read_config(root);config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(root,config)
        isolate_activity(root,h.services);await h.action('login');await h.action('start')
        h.model.script=['已绑定策略测试。'];h.services.push('policy-bind','绑定本地策略测试')
        await h.wait(lambda:'已绑定策略测试。' in json.dumps(h.services.sent,ensure_ascii=False))
        store=ProactiveStore(root);session=store.status()['sessions'][0]['session']
        p=defaults();p.update(enabled=True,session=session);p['heartbeat']['enabled']=False
        for index,(enabled,policy_id,quiet) in enumerate([(True,'goal_execution',False),(False,'goal_execution',False),(True,'basic',True),(True,'goal_execution',False)]):
            p['policy'].update(enabled=enabled,id=policy_id)
            await h.action('apply',proactive=p)
            state=h.get();assert state['running'] and state['config']['proactive']==p
            assert {r['id'] for r in state['proactive']['policy_catalog']}=={'goal_execution','basic'}
            await h.action('stop');_,revision,_=store.sync()
            with store.db() as db:eid=store._insert(db,session,revision,'heartbeat',time.time()+1,p['heartbeat']['prompt'])
            message=f'打包策略测试{index}'
            send=call('send-'+str(index),{'message':message},'notify_user')
            send['tool_calls'][0]['function']['arguments']=json.dumps({'message':message})
            if enabled and policy_id=='goal_execution':
                h.model.script=[send,activity_read('read-'+str(index)),call('valid-'+str(index),{'message':message},'notify_user'),'内部结束。']
            elif quiet:h.model.script=['没有具体事项，保持静默。']
            else:h.model.script=[send,'内部结束。']
            count=len(h.services.sent);await h.action('start')
            await h.wait(lambda:store.event(eid,session)['state']==('quiet' if quiet else 'sent'))
            if quiet:assert len(h.services.sent)==count
            else:assert message in json.dumps(h.services.sent,ensure_ascii=False)
        assert '内部结束' not in json.dumps(h.services.sent,ensure_ascii=False)
    finally:await h.close()
