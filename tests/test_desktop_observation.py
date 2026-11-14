import asyncio
from datetime import datetime,timezone,timedelta
import json
import time
import pytest
from proactive import ProactiveStore,defaults
from settings import read_config,write_config
from support import call
from test_desktop import Desktop,DESKTOP
from test_package import ARCHIVE,run_async

@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires packaged Telos and official DSH')
@pytest.mark.parametrize('reject_second', [0, 1, 2])
@run_async
async def test_packaged_observation_delivers_two_local_bubbles(tmp_path, reject_second):
    h=Desktop(tmp_path);root=h.home/'telos'
    try:
        await h.start();await h.action('save',tools={'apple':False,'maps':False})
        cfg=read_config(root);cfg['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url)
        cfg['activity_history']={'aw_url':h.services.url,'screen_time_db':str(root/'absent.db'),'health_enabled':False}
        write_config(root,cfg)
        await h.action('login');await h.action('start')
        h.model.script=['已绑定。'];h.services.push('bind-observation','绑定本地测试')
        await h.wait(lambda:'已绑定。' in json.dumps(h.services.sent,ensure_ascii=False))
        await h.action('stop');store=ProactiveStore(root);session=store.status()['sessions'][0]['session']
        p=defaults();p.update(enabled=True,session=session);p['heartbeat']['enabled']=False
        p['policy']['configs']['goal_execution']['observation_enabled']=True
        await h.action('save',proactive=p);_,rev,_=store.sync()
        with store.db() as db:eid=store._insert(db,session,rev,'heartbeat',time.time()+1,'请根据观察区间给出两个气泡。')
        def read(_):
            now=datetime.now(timezone.utc)
            return call('observe',{'view':'summary','start':(now-timedelta(hours=2)).isoformat(),'end':now.isoformat(),'sources':['aw','screen_time','health']},'activity_history')
        observation='这段时间的行为来源不可用，暂时无法核实。'
        reminder='当前目标与计划也无法核实，本次先不作执行判断。'
        if reject_second:h.services.send_rejections[reminder]=reject_second
        h.model.script=[read,call('send',{'observation':observation,'message':reminder},'notify_user'),'内部结束']
        await h.action('start');await h.wait(lambda:store.event(eid,session)['state']=='sent')
        row=store.event(eid,session)
        assert row['observation'].endswith('\n'+observation) and row['message']==reminder and row['sent_at']
        observation=row['observation']
        with store.db() as db:
            receipt=db.execute('SELECT parts,next_part,state FROM text_outbox WHERE key=?',('proactive-'+eid,)).fetchone()
        assert json.loads(receipt['parts'])==[observation,reminder] and receipt['next_part']==2 and receipt['state']=='complete'
        sent=[item['text_item']['text'] for delivery in h.services.sent for item in delivery['msg']['item_list'] if 'text_item' in item]
        assert sent.count(observation)==sent.count(reminder)==1 and '内部结束' not in sent
        log_path=root/'logs/wechat-delivery.jsonl'
        logs=[json.loads(line) for line in log_path.read_text().splitlines()]
        receipts=[r for r in logs if r.get('event')=='wechat.send.response' and r.get('delivery_id')=='proactive-'+eid]
        assert [r['part'] for r in receipts]==[0]+[1]*(reject_second+1)
        assert all('errcode' in r and 'ret' in r for r in receipts)
        assert receipts[-1]['receipt']=='api_accepted'
        if reject_second:
            assert receipts[1]['ret']==-2 and receipts[1]['errmsg']=='prepare failed'
            assert receipts[1]['client_id']==receipts[2]['client_id']
        assert log_path.stat().st_mode & 0o777 == 0o600
        assert observation not in log_path.read_text() and reminder not in log_path.read_text()
    finally:await h.close()
