"""Opt-in real macOS permission test. Never logs calendar contents or writes Apple data.

Requires the person at the Mac to accept the native dialogs. Do not reset TCC
automatically: existing grants and unrelated applications must be preserved.
"""
import asyncio
import hashlib
import json
import os
import plistlib
import subprocess
import tarfile
import time

import pytest
from apple_component import apple_client
from settings import read_config,write_config
from test_desktop import Desktop,DESKTOP
from test_package import Installed,ARCHIVE,run_async

pytestmark=pytest.mark.skipif(os.environ.get('TELOS_APPLE_PERMISSIONS_LIVE')!='1' or DESKTOP is None,
                            reason='真实 macOS 授权需显式启用并由用户处理弹窗')


def granted(status):
    assert status['application']=='com.telos.harness.apple'
    assert status['events'] is True and status['reminders'] is True
    assert status['event_status']==status['reminder_status']=='full_access'


@run_async
async def test_real_desktop_authorization_cli_restart_and_package_upgrade(tmp_path):
    d=Desktop(tmp_path/'desktop');h=None
    try:
        await d.start();await d.action('save',max_steps=24)
        initial=d.get()['apple']
        print('INITIAL_PERMISSION_STATE',json.dumps(initial),flush=True)
        print('AWAITING_NATIVE_PERMISSION: 请允许 Telos Apple 访问日历和提醒事项。',flush=True)
        d.post('authorize')
        await d.wait(lambda:not d.get()['busy'],timeout=270)
        assert not d.get()['error'],d.get()['error']
        granted(d.get()['apple'])
        assert not d.services.polls and not d.model.requests
        d.stop();await d.start();granted(d.get()['apple'])
        print('DESKTOP_AUTHORIZATION_AND_RESTART_OK',flush=True)

        # A separately installed official Node/CLI instance uses the same Apple identity.
        h=Installed(tmp_path/'cli',[])
        config=read_config(h.data);config.pop('apple_helper');write_config(h.data,config)
        original=(h.data/'config.json').read_bytes()
        def status():return json.loads(h.run('--telos-status').stdout.strip().splitlines()[-1])['apple']
        granted(status());h.run('--telos-authorize');granted(status())
        helper=h.home/'profiles/telos/node_modules/telos-harness/native/Telos Harness Apple.app/Contents/MacOS/telos-apple'
        # Real EventKit read, local only; report counts, never titles/IDs/notes.
        snapshot=apple_client({},helper).request('snapshot',{'start':'2027-02-19T00:00:00+08:00','end':'2027-02-20T00:00:00+08:00'})
        assert isinstance(snapshot['items'],list) and isinstance(snapshot['calendars'],list)
        print('REAL_EVENTKIT_READ_OK',flush=True)
        del snapshot
        h.start();await h.wait(lambda:(h.data/'business.sqlite').exists());h.stop();granted(status())

        # True package-manager version upgrade with the identical signed Apple binary.
        upgrade=tmp_path/'upgrade';upgrade.mkdir()
        with tarfile.open(ARCHIVE) as tar:tar.extractall(upgrade,filter='data')
        package=upgrade/'package';meta=package/'package.json';value=json.loads(meta.read_text())
        value['version']='0.3.5';meta.write_text(json.dumps(value))
        manifest_path=package/'component-manifest.json';manifest=json.loads(manifest_path.read_text())
        manifest['version']='0.3.5';manifest['files']['package.json']=hashlib.sha256(meta.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        archive=tmp_path/'upgrade.tgz'
        with tarfile.open(archive,'w:gz') as tar:tar.add(package,arcname='package')
        before=hashlib.sha256(helper.read_bytes()).hexdigest()
        h.install(archive)
        assert hashlib.sha256(helper.read_bytes()).hexdigest()==before
        assert (h.data/'config.json').read_bytes()==original
        granted(status());h.run('--telos-authorize')
        assert not h.services.polls and not h.model.requests
        print('CLI_RESTART_AND_IDENTICAL_COMPONENT_UPGRADE_OK',flush=True)

        # Also exercise a changed ad-hoc signature, not just an npm version bump.
        # Do not manufacture a release identity or alter any existing TCC grants.
        native=package/'native/Telos Harness Apple.app'
        signing=subprocess.run(['codesign','-dv',str(native)],capture_output=True,text=True,check=True)
        if 'Signature=adhoc' in signing.stderr:
            info=native/'Contents/Info.plist';values=plistlib.loads(info.read_bytes())
            values['CFBundleVersion']='2';info.write_bytes(plistlib.dumps(values))
            entitlements=tmp_path/'entitlements.plist'
            entitlements.write_bytes(plistlib.dumps({'com.apple.security.personal-information.calendars':True}))
            subprocess.run(['codesign','--force','--sign','-','--identifier','com.telos.harness.apple',
                            '--options','runtime','--entitlements',str(entitlements),str(native)],capture_output=True,check=True)
            value['version']='0.3.6';meta.write_text(json.dumps(value))
            manifest['version']='0.3.6'
            manifest['files']={str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(package.rglob('*')) if p.is_file() and p!=manifest_path}
            manifest_path.write_text(json.dumps(manifest))
            changed=tmp_path/'changed-component.tgz'
            with tarfile.open(changed,'w:gz') as tar:tar.add(package,arcname='package')
            h.install(changed)
            assert hashlib.sha256(helper.read_bytes()).hexdigest()!=before
            after=status();print('CHANGED_ADHOC_COMPONENT_STATE',json.dumps(after),flush=True)
            if not (after['events'] and after['reminders']):
                print('AWAITING_NATIVE_PERMISSION_AFTER_UPGRADE: 组件签名变化，请再次允许 Telos Apple。',flush=True)
                result=await asyncio.to_thread(subprocess.run,[*h.command,'--profile','telos','--telos-authorize'],
                    env=h.env,cwd=h.path,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=280)
                assert result.returncode==0,result.stderr
            granted(status())
            assert (h.data/'config.json').read_bytes()==original
            print('CHANGED_COMPONENT_UPGRADE_OK',flush=True)
        print('APPLE_REAL_WRITES=0; WECHAT_POLLING=0; REAL_MODEL_REQUESTS=0',flush=True)
    finally:
        if h:await h.close()
        await d.close()
