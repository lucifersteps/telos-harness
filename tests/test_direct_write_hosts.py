"""Native Desktop/CLI use the same write-mode setting and undo receipts."""
import json
import pytest
from settings import read_config,write_config
from support import call,draft
from test_package import Installed,ARCHIVE,run_async
from test_desktop import Desktop,DESKTOP

@pytest.mark.parametrize('desktop',[False,True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires built archive and official Desktop runtime')
@run_async
async def test_direct_write_toggle_restart_and_undo(tmp_path,desktop):
    h=Desktop(tmp_path) if desktop else Installed(tmp_path,[])
    try:
        if desktop:
            import sys
            await h.start();await h.action('save',direct_write=True)
            helper=tmp_path/'fake-apple'
            helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\nr=urllib.request.urlopen(urllib.request.Request({h.services.url+"/apple"!r},data=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode(),headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n');helper.chmod(0o700)
            config=read_config(h.home/'telos');config['apple_helper']=str(helper)
            config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(h.home/'telos',config)
            await h.action('login');await h.action('start')
            assert h.get()['config']['direct_write'] is True
        else:
            h.run(['--telos-direct-write','on']);h.run('--telos-login');h.start()
        native=h.services.apple
        async def message(key,text):
            before=len(h.services.sent);h.services.push(key,text)
            await h.wait(lambda:len(h.services.sent)>before)
        h.model.script=[call('direct-create',draft()),'已保存。']
        await message('create','创建测试会议')
        assert len(native.writes)==1 and '回复【1】撤销' in json.dumps(h.services.sent,ensure_ascii=False)
        assert len(h.services.sent)==1
        h.stop()
        if desktop:await h.start();await h.wait(lambda:h.get()['running'])
        else:h.start()
        await message('undo','1')
        assert len(native.writes)==2 and native.writes[-1][0]=='delete'
        await message('undo-again','1');assert len(native.writes)==2
        if desktop:await h.action('apply',direct_write=False);assert h.get()['config']['direct_write'] is False
        else:h.stop();h.run(['--telos-direct-write','off']);h.start()
        h.model.script=[call('confirmed-create',draft('第二个会议')),'请确认。']
        await message('preview','再创建一个会议');assert len(native.writes)==2
        await message('confirm','1');assert len(native.writes)==3
        h.stop()
        if desktop:await h.start();await h.wait(lambda:h.get()['running']);assert h.get()['config']['direct_write'] is False
        else:assert read_config(h.data)['direct_write'] is False
    finally:await h.close()
