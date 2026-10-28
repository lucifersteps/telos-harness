"""Native Desktop/CLI use the same write-mode setting and undo receipts."""
import json
import sqlite3
import pytest
from settings import read_config,write_config
from support import call,draft
from test_package import Installed,ARCHIVE,run_async
from test_desktop import Desktop,DESKTOP
from test_history import results,tool_text

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
        def saved_card(request):
            with sqlite3.connect(h.home/'telos/business.sqlite') as db:
                return db.execute('SELECT reply FROM messages WHERE reply IS NOT NULL ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        h.model.script=[call('direct-create',draft()),saved_card]
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


@pytest.mark.parametrize('desktop',[False,True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires built archive and official Desktop runtime')
@run_async
async def test_installed_mixed_cards_feedback_recheck_restart_and_confirm(tmp_path,desktop):
    h=Desktop(tmp_path) if desktop else Installed(tmp_path,[])
    root=h.home/'telos'
    listing='**处理进度**\n- 测试会议已经保存\n- 删除旧提醒需要确认'
    explanation='会议已保存，另一项仍需确认。'

    def pending():
        if not (root/'business.sqlite').exists():return None
        with sqlite3.connect('file:'+str(root/'business.sqlite')+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT value FROM pending').fetchone()
        return json.loads(row[0]) if row else None

    def result(request,cid):
        matches=[item for item in results(request) if item.get('tool_use_id')==cid]
        assert len(matches)==1,matches
        return json.loads(tool_text(matches[0]))

    async def message(key,text):
        h.services.push(key,text)
        def delivered():
            if not (root/'business.sqlite').exists():return False
            with sqlite3.connect('file:'+str(root/'business.sqlite')+'?mode=ro',uri=True) as db:
                row=db.execute('SELECT delivered FROM messages WHERE input=? ORDER BY rowid DESC LIMIT 1',(text,)).fetchone()
            return row==(1,)
        await h.wait(delivered)

    def sent_texts():
        return [item['msg']['item_list'][0]['text_item']['text'] for item in h.services.sent]

    try:
        if desktop:
            import sys
            await h.start();await h.action('save',direct_write=True)
            helper=tmp_path/'fake-apple'
            helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\nr=urllib.request.urlopen(urllib.request.Request({h.services.url+"/apple"!r},data=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode(),headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n');helper.chmod(0o700)
            config=read_config(root);config['apple_helper']=str(helper)
            config['wechat'].update(base_url=h.services.url,cdn_base_url=h.services.url);write_config(root,config)
            await h.action('login');await h.action('start')
        else:
            h.run(['--telos-direct-write','on']);h.run('--telos-login');h.start()

        def delete(request):
            item=result(request,'mixed-detail')
            return call('mixed-delete',{'action':'delete','kind':'reminder','id':item['id'],
                                       'expected_revision':item['revision']})

        def mixed_answer(request):
            return '\n\n'.join([result(request,'mixed-save')['text'],explanation,listing,
                                  result(request,'mixed-delete')['text']])

        h.model.script=[call('mixed-save',draft('保留的测试会议')),
                        call('mixed-detail',{'action':'detail','kind':'reminder','id':'reminder-001'}),
                        delete,mixed_answer]
        await message('mixed-input','创建测试会议，并预览删除旧提醒')
        await h.wait(lambda:bool(pending() and pending().get('delivered')))
        original=pending();native=h.services.apple
        assert len(native.writes)==1 and native.writes[0][0]=='create'
        texts=sent_texts()
        assert len(texts)==4
        assert texts[0].startswith('**已保存**') and texts[0].count('```')==2
        assert '回复【1】撤销' not in texts[0] and '撤销' in texts[0] and '取消' in texts[0]
        assert texts[1:]==[explanation,listing,original['text']]
        assert '回复【1】确认' in texts[-1]

        def rechecked_answer(request):
            checked=result(request,'mixed-rechecked')
            assert checked['status']=='awaiting_approval'
            return '这次仅重新核对并展示待删除提醒。\n\n'+checked['text']

        h.model.script=[call('mixed-rechecked',{'action':'revise','draft_call_id':original['id'],
                                               'request_mode':'continue','fields':{}}),rechecked_answer]
        await message('format-feedback','请把保存卡和待确认删除分开发送')
        await h.wait(lambda:bool(pending() and pending().get('delivered')))
        refreshed=pending()
        assert refreshed['id']=='mixed-rechecked' and refreshed['preview']==original['preview']
        assert sent_texts()[-1]==refreshed['text'] and len(native.writes)==1

        h.stop()
        if desktop:await h.start();await h.wait(lambda:h.get()['running'])
        else:h.start()
        assert pending()['id']=='mixed-rechecked' and pending()['delivered']
        h.model.script=['删除结果以实际回执为准。']
        await message('confirm-after-restart','1')
        assert pending() is None
        assert [action for action,_args in native.writes]==['create','delete']
        assert native.writes[-1][1]['id']=='reminder-001'
        assert any(item.get('title')=='保留的测试会议' for item in native.items)
    finally:await h.close()
