"""Verify the released Python component preserves partial reads in real hosts."""
import json
import sys

import pytest

from settings import read_config, write_config
from support import call
from test_desktop import Desktop, DESKTOP
from test_package import Installed, ARCHIVE, run_async
from test_schedule import value


@pytest.mark.parametrize('desktop', [False, True])
@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(), reason='Requires archive and Desktop host')
@run_async
async def test_packaged_partial_queries_and_restart(tmp_path, desktop):
    host = Desktop(tmp_path) if desktop else Installed(tmp_path, [])
    try:
        if desktop:
            await host.start()
            await host.action('save')
            helper = tmp_path / 'fake-apple'
            helper.write_text(f'#!{sys.executable}\nimport sys,json,urllib.request\n'
                f'r=urllib.request.urlopen(urllib.request.Request({host.services.url + "/apple"!r},'
                'data=json.dumps({"action":sys.argv[1],"args":json.load(sys.stdin)}).encode(),'
                'headers={"Content-Type":"application/json"}))\nprint(r.read().decode())\n')
            helper.chmod(0o700)
            root = host.home / 'telos'
            config = read_config(root)
            config['apple_helper'] = str(helper)
            config['wechat'].update(base_url=host.services.url, cdn_base_url=host.services.url)
        else:
            root = host.data
            config = read_config(root)
        config['direct_write'] = True
        write_config(root, config)
        native = host.services.apple
        native.items[0].update(tags=None, attachments=None, attachments_supported=False,
                               unavailable_fields=['tags', 'attachments'])
        original = native.request
        def request(action, args):
            result = original(action, args)
            if action == 'snapshot' and not args.get('kind'):
                result['source_errors'] = {'event': {'code': 'read_failed', 'reason': '模拟日历不可用'}}
            return result
        native.request = request
        observed = {}
        def capture(name, reply):
            def model(request):
                observed[name] = value(request)
                return reply
            return model
        host.model.script = [call('calendar', {'action': 'query', 'kind': 'event'}),
            capture('event', call('reminders', {'action': 'query', 'kind': 'reminder'})),
            capture('reminder', call('mixed', {'action': 'query'})),
            capture('mixed', '读取了可用结果，部分字段和来源暂不可用。')]
        if desktop:
            await host.action('login')
            await host.action('start')
        else:
            host.run('--telos-login')
            host.start()
        host.services.push('read-partial', '分别查询日历、提醒事项和全部安排')
        await host.wait(lambda: len(host.services.sent) == 1)
        assert observed['event']['status'] == 'ok' and observed['event']['total'] == 0
        assert observed['reminder']['status'] == 'partial' and observed['reminder']['total'] == 1
        assert observed['reminder']['items'][0]['attachments'] is None
        assert observed['mixed']['status'] == 'partial' and observed['mixed']['total'] is None
        assert observed['mixed']['available_total'] == 1
        assert observed['mixed']['source_errors']['event']['code'] == 'read_failed'
        host.stop()
        host.model.script = [call('after-restart', {'action': 'query', 'kind': 'reminder'}),
                             capture('restart', '重启后仍可读取可用字段。')]
        if desktop:
            await host.start()
            await host.wait(lambda: host.get()['running'])
        else:
            host.start()
        host.services.push('read-restart', '再查询提醒事项')
        await host.wait(lambda: len(host.services.sent) == 2)
        assert observed['restart']['items'][0]['tags'] is None
        assert not native.writes and host.services.logins == 1
    finally:
        await host.close()
