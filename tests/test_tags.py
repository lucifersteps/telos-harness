"""Same tag approval protocol in real CLI and Desktop hosts with simulated services."""
import json
import sys
import pytest
from settings import read_config,write_config
from support import call
from test_package import Installed,ARCHIVE,run_async
from test_desktop import Desktop,DESKTOP
from telos.tools.apple.service import revision

@pytest.mark.parametrize('desktop',[False,True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires built archive and desktop runtime')
@run_async
async def test_tags_approval_restart_edit_cancel_and_dedup(tmp_path,desktop):
    h=Desktop(tmp_path) if desktop else Installed(tmp_path,[])
    try:
        if desktop:
            await h.start();await h.action('save')
            helper=tmp_path/'fake-apple'
            helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\nr=urllib.request.urlopen(urllib.request.Request({h.services.url+"/apple"!r},data=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode(),headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n');helper.chmod(0o700)
            config=read_config(h.home/'telos');config['apple_helper']=str(helper)
            config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(h.home/'telos',config)
            await h.action('login');await h.action('start')
        else:h.run('--telos-login');h.start()
        native=h.services.apple
        async def message(key,text):
            before=len(h.services.sent);h.services.push(key,text)
            await h.wait(lambda:len(h.services.sent)>before)
        h.model.script=[call('create-tags',{'action':'create','kind':'reminder',
            'fields':{'title':'测试标签','tags':['工作'],'calendar_id':'list-001'}}),'请确认。']
        await message('create','创建带工作标签的提醒事项')
        assert not native.writes and '#工作' in json.dumps(h.services.sent,ensure_ascii=False)
        h.stop()
        if desktop:await h.start();await h.wait(lambda:h.get()['running'])
        else:h.start()
        await message('confirm-create','1')
        assert len(native.writes)==1 and native.items[-1]['tags']==['工作']
        await message('confirm-again','1');assert len(native.writes)==1
        item=native.items[-1]
        h.model.script=[call('edit-tags',{'action':'update','kind':'reminder','id':item['id'],
            'expected_revision':revision(item),'fields':{'tags':['工作','紧急']}}),'请确认。']
        await message('edit','再加一个紧急标签')
        assert len(native.writes)==1 and '#紧急' in json.dumps(h.services.sent[-1],ensure_ascii=False)
        await message('confirm-edit','1');assert native.items[-1]['tags']==['工作','紧急']
        item=native.items[-1]
        h.model.script=[call('clear-tags',{'action':'update','kind':'reminder','id':item['id'],
            'expected_revision':revision(item),'fields':{'tags':[]}}),'请确认。']
        await message('clear','清空标签')
        assert '清空标签' in json.dumps(h.services.sent[-1],ensure_ascii=False)
        await message('cancel','取消')
        assert len(native.writes)==2 and native.items[-1]['tags']==['工作','紧急']
    finally:await h.close()
