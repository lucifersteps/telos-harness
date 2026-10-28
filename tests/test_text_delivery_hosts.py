"""The compiled CLI/Desktop component resumes only unsent, confirmed-rejected parts."""
import sqlite3
import pytest
from settings import read_config, write_config
from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, Installed, run_async


@pytest.mark.parametrize('desktop', [False, True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(), reason='Requires package and Desktop runtime')
@run_async
async def test_text_progress_and_legacy_quarantine_survive_real_host_restart(tmp_path, desktop):
    h = Desktop(tmp_path) if desktop else Installed(tmp_path, [])
    reply = '第一段\n第二段\n第三段\n第四段'
    h.services.send_rejections['第三段'] = 1
    root = h.home/'telos'
    try:
        if desktop:
            await h.start()
            await h.action('save', tools={'apple':False, 'maps':False})
            config=read_config(root)
            config['wechat'].update(base_url=h.services.url, cdn_base_url=h.services.url)
            write_config(root,config)
            await h.action('login');await h.action('start')
        else:
            h.run('--telos-login');h.start()
        h.model.script=[reply]
        h.services.push('multipart', '测试分段发送')
        def state():
            with sqlite3.connect(root/'business.sqlite') as db:
                try:row=db.execute('SELECT next_part,state FROM text_outbox').fetchone()
                except sqlite3.OperationalError:return None
                return row
        await h.wait(lambda:len(h.services.send_attempts)==3)
        await h.wait(lambda:state()==(2,'rejected'))
        requests=len(h.model.requests)
        h.stop()
        if desktop:
            await h.start();await h.wait(lambda:h.get()['running'])
        else:h.start()
        await h.wait(lambda:len(h.services.sent)==4)
        assert [x['msg']['item_list'][0]['text_item']['text'] for x in h.services.sent]==reply.splitlines()
        assert h.services.send_attempts[2]['msg']['client_id']==h.services.send_attempts[3]['msg']['client_id']
        assert len(h.model.requests)==requests
        h.stop()
        with sqlite3.connect(root/'business.sqlite') as db:
            session=db.execute('SELECT session FROM messages LIMIT 1').fetchone()[0]
            db.execute('DROP TABLE text_outbox')
            db.execute('INSERT INTO messages(id,session,input,reply,delivered) VALUES(?,?,?,?,0)',
                       ('legacy',session,'old question','旧回复\n不得重发'))
        if desktop:
            await h.start();await h.wait(lambda:h.get()['running'])
        else:h.start()
        await h.wait(lambda:state()==(0,'legacy_unknown'))
        assert len(h.services.sent)==4 and len(h.model.requests)==requests
        with sqlite3.connect(root/'business.sqlite') as db:
            assert db.execute("SELECT reply,delivered FROM messages WHERE id='legacy'").fetchone()==('旧回复\n不得重发',0)
    finally:await h.close()
