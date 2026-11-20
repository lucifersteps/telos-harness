"""Upgrade the published experimental package into core without losing user state."""
import json
import os
from pathlib import Path
import tarfile

import pytest

import test_package
from settings import read_config, write_config
from support import call
from test_package import Installed, run_async
from test_schedule import create, value

PREVIOUS = Path(os.environ.get('TELOS_TEST_PREVIOUS_ARCHIVE', '/missing-previous-telos-package'))
CURRENT = test_package.ARCHIVE


@pytest.mark.skipif(not PREVIOUS.is_file() or not CURRENT.is_file(), reason='需要新旧两个真实安装包')
@run_async
async def test_published_upgrade_preserves_login_history_config_and_user_schedule(tmp_path, monkeypatch):
    with tarfile.open(PREVIOUS) as archive:
        assert json.load(archive.extractfile('package/package.json'))['version'] in {'0.5.2', '0.6.0'}
    monkeypatch.setattr(test_package, 'ARCHIVE', PREVIOUS)
    h = Installed(tmp_path, [create('release-060-preserved-task', 3600), '旧版用户任务已设置。'])
    try:
        config = read_config(h.data)
        config['proactive'] = {**config.get('proactive', {}), 'enabled': False,
                              'policy': {'enabled': True, 'id': 'goal_execution',
                                         'configs': {'goal_execution': {'observation_enabled': True}}}}
        config['health_receiver'] = {'enabled': False, 'root': '/missing-test-health-receiver'}
        write_config(h.data, config)
        h.run('--telos-login')
        h.run('--telos-authorize')
        h.start()
        await h.wait(lambda: h.services.polls > 0)
        h.services.push('old-user-task', '一小时后给我发测试提醒')
        await h.wait(lambda: len(h.services.sent) == 1)
        assert value(h.model.requests[-1]).get('id')
        h.stop()
        config_before = (h.data / 'config.json').read_bytes()
        history = {p: p.read_bytes() for p in (h.home / 'sessions').glob('*/*/session.v*.jsonl*')}
        assert history
        h.install(CURRENT)
        assert (h.data / 'config.json').read_bytes() == config_before
        assert all(p.read_bytes() == content for p, content in history.items())
        h.services.apple.items[0].update(flagged=None, flagged_supported=False)
        verified = set()

        def check_tasks(request):
            assert 'release-060-preserved-task' in json.dumps(value(request))
            verified.add('schedule')
            return call('ordinary-reminders', {'action': 'query', 'kind': 'reminder'})

        def check_reminders(request):
            result = value(request)
            assert result.get('items') and result['items'][0]['flagged'] is None
            verified.add('reminder')
            return call('ordinary-calendar', {'action': 'query', 'kind': 'event'})

        def check_calendar(request):
            result = value(request)
            assert 'items' in result and 'error' not in result
            verified.add('calendar')
            return '新版基础查询正常。'

        h.model.script = [call('user-tasks', {}, 'schedule_list'), check_tasks,
                          check_reminders, check_calendar]
        h.start()
        h.services.push('new-basic-query', '查询我的定时任务、普通提醒事项和日历')
        await h.wait(lambda: len(h.services.sent) == 2)
        assert verified == {'schedule', 'reminder', 'calendar'}
        assert h.services.logins == 1 and h.services.authorizations == 1
        assert not h.services.apple.writes
        saved = read_config(h.data)
        assert saved['proactive']['policy'] == config['proactive']['policy']
        assert saved['health_receiver'] == config['health_receiver']
        names = {tool.get('name') or tool['function']['name'] for tool in h.model.requests[-1]['tools']}
        assert 'activity_history' not in names and 'baidu_maps' in names
        assert all(p.read_bytes().startswith(content) for p, content in history.items())
    finally:
        await h.close()
