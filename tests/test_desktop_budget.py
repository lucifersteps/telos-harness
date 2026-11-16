import json
import time
import pytest
from proactive import ProactiveStore,defaults
from settings import read_config,write_config
from support import call
from test_desktop import Desktop,DESKTOP
from test_package import ARCHIVE,run_async


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires packaged Telos and official DSH')
@run_async
async def test_packaged_budget_notice_and_new_inbound_restore(tmp_path):
    h=Desktop(tmp_path);root=h.home/'telos'
    def texts():
        return [i['text_item']['text'] for d in h.services.sent for i in d['msg']['item_list'] if 'text_item' in i]
    try:
        await h.start();await h.action('save',tools={'apple':False,'maps':False})
        cfg=read_config(root);cfg['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url)
        write_config(root,cfg);await h.action('login');await h.action('start')
        h.model.script=['\n'.join('普通回复'+str(i) for i in range(8))];h.services.push('bind-budget','隔离测试')
        await h.wait(lambda:len(texts())==8)
        await h.action('stop');store=ProactiveStore(root);session=store.status()['sessions'][0]['session']
        p=defaults();p.update(enabled=True,session=session);p['heartbeat']['enabled']=False
        await h.action('save',proactive=p);_,rev,_=store.sync()
        with store.db() as db:eid=store._insert(db,session,rev,'heartbeat',time.time()+1,'normal check')
        notice='微信快把我的小喇叭静音啦🥺\n回我一句就能续上，这条之后先乖乖等你～'
        h.services.send_rejections[notice]=1
        h.model.script=[call('notice',{'message':notice},'notify_user'),'内部结束']
        await h.action('start');await h.wait(lambda:store.event(eid,session)['state']=='sent')
        assert texts()[8:]==[notice]
        await h.action('stop');_,rev,_=store.sync()
        with store.db() as db:blocked=store._insert(db,session,rev,'heartbeat',time.time()+1,'must not run model')
        await h.action('start');await h.wait(lambda:store.event(blocked,session)['state']=='skipped')
        assert texts()[8:]==[notice]
        h.model.script=['收到你的回复啦。'];h.services.push('renew-budget','我回来啦')
        await h.wait(lambda:texts()[-1]=='收到你的回复啦。')
        with store.db() as db:
            from wechat_budget import view
            assert view(db,session)['remaining']==9
    finally:await h.close()
