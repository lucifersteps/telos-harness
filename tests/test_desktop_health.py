"""Official DSH host lifecycle; no live accounts, network tunnel or health data."""
import json
import os
from pathlib import Path
import sys

import pytest

from test_desktop import Desktop, DESKTOP
from test_package import ARCHIVE, run_async
from test_health_receiver import receiver_root, port_open, request, batch, count


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(),reason='Requires official DSH and packaged Telos')
@run_async
async def test_desktop_owns_receiver_toggle_disable_and_abrupt_exit(tmp_path,receiver_root):
    root,cfg=receiver_root
    python=root/'.venv/bin/python';python.unlink()
    # Only replace the external tunnel/caffeinate in this disposable installation.
    # All receiver, ingress, database, plugin and lifecycle code remains real.
    python.write_text(f'''#!{sys.executable}
import sys,os,importlib.util
from pathlib import Path
if Path(sys.argv[1]).name=='health-receiver.py':
 spec=importlib.util.spec_from_file_location('receiver',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 root=Path(sys.argv[3]);plan=m.commands(root)
 for name in ('tailscaled','awake'):plan[name]=[{sys.executable!r},'-c','import time; time.sleep(300)']
 raise SystemExit(m.run(root,plan))
os.execv({sys.executable!r},[{sys.executable!r},*sys.argv[1:]])
''');python.chmod(0o700)
    d=Desktop(tmp_path/'desktop')
    try:
        await d.start()
        await d.action('save',tools={'apple':False,'maps':False},health_receiver={'enabled':True,'root':str(root)})
        assert not port_open(cfg['core_port'])  # Saving/status never launches it.
        await d.action('start');await d.wait(lambda:d.get()['health_receiver']['phase']=='running')
        assert request(cfg,batch())['accepted']==1 and count(root)==1
        await d.action('stop')
        assert not port_open(cfg['core_port']) and not port_open(cfg['ingress_port'])
        pending=batch()
        with pytest.raises(OSError):request(cfg,pending)
        await d.action('start');await d.wait(lambda:d.get()['health_receiver']['phase']=='running')
        request(cfg,pending);request(cfg,pending);assert count(root)==2
        await d.action('apply',health_receiver={'enabled':False,'root':str(root)})
        assert d.get()['running'] and d.get()['health_receiver']['phase']=='disabled'
        assert not port_open(cfg['core_port']) and count(root)==2
        await d.action('apply',health_receiver={'enabled':True,'root':str(root)})
        await d.wait(lambda:d.get()['health_receiver']['phase']=='running')
        await __import__('asyncio').to_thread(d.manage,'disable')
        assert not port_open(cfg['core_port'])
        await __import__('asyncio').to_thread(d.manage,'enable')
        await d.wait(lambda:not d.get()['running'])
        assert not port_open(cfg['core_port'])
        await d.action('start');await d.wait(lambda:d.get()['health_receiver']['phase']=='running')
        d.proc.kill();d.proc.wait(timeout=10);d.proc=None;d.log.close()
        from test_health_receiver import wait_for
        await __import__('asyncio').to_thread(wait_for,lambda:not port_open(cfg['core_port']) and not port_open(cfg['ingress_port']))
        assert count(root)==2 and not d.model.requests and not d.services.sent
    finally:
        await d.close()
