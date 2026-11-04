"""Compiled CLI/Desktop preview recovery uses existing drafts across restart."""
import json
import sqlite3
import sys

import pytest

from settings import read_config, write_config
from support import call, draft
from test_desktop import Desktop, DESKTOP
from test_package import Installed, ARCHIVE, run_async


@pytest.mark.parametrize('desktop', [False, True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(), reason='Requires archive and official Desktop runtime')
@run_async
async def test_packaged_feedback_refresh_restart_and_confirm(tmp_path, desktop):
    h = Desktop(tmp_path) if desktop else Installed(tmp_path, [])
    root = h.home / 'telos'

    def pending():
        with sqlite3.connect('file:' + str(root / 'business.sqlite') + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT value FROM pending').fetchone()
        return json.loads(row[0]) if row else None

    async def message(key, text):
        h.services.push(key, text)
        def delivered():
            with sqlite3.connect('file:' + str(root / 'business.sqlite') + '?mode=ro', uri=True) as db:
                row = db.execute('SELECT delivered FROM messages WHERE input=? ORDER BY rowid DESC LIMIT 1', (text,)).fetchone()
            return row == (1,)
        await h.wait(delivered)

    def last_text():
        return h.services.sent[-1]['msg']['item_list'][0]['text_item']['text']

    try:
        if desktop:
            await h.start()
            await h.action('save', direct_write=True)
            helper = tmp_path / 'fake-apple'
            helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\nr=urllib.request.urlopen(urllib.request.Request({h.services.url+"/apple"!r},data=json.dumps({{"action":sys.argv[1],"args":json.load(sys.stdin)}}).encode(),headers={{"Content-Type":"application/json"}}))\nprint(r.read().decode())\n')
            helper.chmod(0o700)
            config = read_config(root)
            config['apple_helper'] = str(helper)
            config['wechat'].update(base_url=h.services.url, cdn_base_url=h.services.url)
            write_config(root, config)
            await h.action('login')
            await h.action('start')
        else:
            h.run(['--telos-direct-write', 'on'])
            h.run('--telos-login')
            h.start()
        await h.wait(lambda: h.services.polls > 0)
        h.model.script = [call('batch', {'action': 'batch', 'operations': [draft('会议一'), draft('会议二')]}), '请核对。']
        await message('preview', '先预览两个会议。')
        original = pending()
        assert original['delivered'] and not h.services.apple.writes
        h.model.script = ['你回一句「1」，我就执行。']
        await message('feedback', '就是这两个会议。')
        assert not pending()['delivered'] and '重新预览' in last_text()
        h.model.script = ['请先重新预览。']
        await message('stale-confirm', '1')
        assert not h.services.apple.writes

        h.stop()
        if desktop:
            await h.start()
            await h.wait(lambda: h.get()['running'])
        else:
            h.start()
        assert pending()['id'] == original['id']
        h.model.script = ['请核对新卡。']
        await message('refresh', '重新预览')
        current = pending()
        assert current['id'] != original['id'] and current['preview'] == original['preview']
        assert current['delivered'] and last_text() == current['text']
        assert not h.services.apple.writes
        h.model.script = ['已完成。']
        await message('confirm-new', '确认')
        assert len(h.services.apple.writes) == 2 and pending() is None
        h.model.script = ['没有可确认的预览。']
        await message('confirm-again', '同意')
        assert len(h.services.apple.writes) == 2
    finally:
        await h.close()
