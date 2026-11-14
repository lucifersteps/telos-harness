"""Real subprocesses and HTTP ingestion, always against disposable health stores."""
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

import pytest

ROOT=Path(__file__).resolve().parents[1]
INTEGRATION=ROOT.parent/'pulse-relay-integration'
SUPERVISOR=ROOT/'plugin/health-receiver.py'

def wait_for(predicate,timeout=10):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError('Condition did not become true')

def port_open(port):
    with socket.socket() as s:
        s.settimeout(.1)
        return s.connect_ex(('127.0.0.1',port))==0

@pytest.fixture
def receiver_root(tmp_path):
    root=tmp_path/'receiver'
    for directory in ['private','logs','backups','scripts','.venv/bin','data']:
        (root/directory).mkdir(parents=True)
    (root/'.venv/bin/python').symlink_to(sys.executable)
    (root/'source').symlink_to(INTEGRATION/'source',target_is_directory=True)
    for name in ['run_service.py','ingress.py','backup.py']:
        shutil.copy2(INTEGRATION/'scripts'/name,root/'scripts'/name)
    with socket.socket() as a,socket.socket() as b:
        a.bind(('127.0.0.1',0));b.bind(('127.0.0.1',0))
        config={'core_port':a.getsockname()[1],'ingress_port':b.getsockname()[1],'ingest_token':'synthetic-test-token-'+'x'*40}
    (root/'private/service.json').write_text(json.dumps(config))
    return root,config

def start(root):
    code=f'''import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('receiver',{str(SUPERVISOR)!r})
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
root=Path({str(root)!r});plan=m.commands(root)
for name in ('tailscaled','awake'):plan[name]=[{sys.executable!r},'-c','import time; time.sleep(300)']
raise SystemExit(m.run(root,plan))
'''
    return subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)

def ready(proc,root):
    path=root/'private/managed-status.json'
    wait_for(lambda:path.exists() and json.loads(path.read_text())['phase']=='running' and json.loads(path.read_text())['pid']==proc.pid)
    assert proc.poll() is None
    return json.loads(path.read_text())

def stop(proc):
    if proc.poll() is None:
        proc.stdin.close();proc.wait(timeout=10)
    assert proc.returncode==0,proc.stderr.read().decode()

def request(config,body):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req=urllib.request.Request(f'http://127.0.0.1:{config["ingress_port"]}/api/health-sync/v1/batches',data=body,
        headers={'Authorization':'Bearer '+config['ingest_token'],'X-Puls-Protocol':'1'})
    with opener.open(req,timeout=5) as response:return json.load(response)

def batch():
    kind='HKQuantityTypeIdentifierHeartRate'
    return '\n'.join(json.dumps(item) for item in [
        {'schemaVersion':1,'batchID':str(uuid.uuid4()),'type':kind,'sampleCount':1},
        {'uuid':str(uuid.uuid4()),'type':kind,'kind':'quantity','start':1791327600000,'end':1791327600000,
         'value':70,'unit':'count/min','sourceName':'Synthetic test'}]).encode()

def count(root):
    with sqlite3.connect(root/'data/health.sqlite3') as db:return db.execute('SELECT COUNT(*) FROM metric_samples').fetchone()[0]

def test_stop_restart_accepts_retry_and_preserves_records(receiver_root):
    root,cfg=receiver_root;proc=start(root)
    try:
        ready(proc,root);first=batch();assert request(cfg,first)['accepted']==1
        assert count(root)==1
        stop(proc)
        assert not port_open(cfg['core_port']) and not port_open(cfg['ingress_port'])
        pending=batch()
        with pytest.raises(OSError):request(cfg,pending)
        proc=start(root);ready(proc,root)
        request(cfg,first)  # Retried batch is idempotent.
        assert count(root)==1
        assert request(cfg,pending)['accepted']==1
        assert count(root)==2
    finally:
        stop(proc)

def test_supervisor_sigkill_cleans_entire_group_and_allows_restart(receiver_root):
    root,cfg=receiver_root;proc=start(root)
    try:
        status=ready(proc,root)
        proc.kill();proc.wait(timeout=5)
        wait_for(lambda:not port_open(cfg['core_port']) and not port_open(cfg['ingress_port']))
        def gone():
            try:os.killpg(proc.pid,0);return False
            except ProcessLookupError:return True
        wait_for(gone)
        proc=start(root);ready(proc,root)
    finally:
        stop(proc)

def test_failed_service_stops_siblings_and_releases_ownership(receiver_root):
    root,cfg=receiver_root;proc=start(root)
    status=ready(proc,root)
    os.kill(status['children']['core'],signal.SIGKILL)
    proc.wait(timeout=10)
    assert proc.returncode==1
    assert json.loads((root/'private/managed-status.json').read_text())['phase']=='failed'
    assert not port_open(cfg['ingress_port'])
    proc=start(root)
    try:ready(proc,root)
    finally:stop(proc)

def test_second_owner_cannot_interrupt_first(receiver_root):
    root,cfg=receiver_root;first=start(root)
    try:
        ready(first,root)
        second=start(root);second.wait(timeout=8)
        assert second.returncode!=0 and port_open(cfg['core_port']) and first.poll() is None
    finally:stop(first)

def test_occupied_port_is_not_taken_over(receiver_root):
    root,cfg=receiver_root
    with socket.socket() as external:
        external.bind(('127.0.0.1',cfg['core_port']));external.listen()
        proc=start(root);proc.wait(timeout=10)
        assert proc.returncode==1 and port_open(cfg['core_port'])
        assert not port_open(cfg['ingress_port'])

def test_disabled_and_invalid_configuration():
    from settings import health_receiver_options
    assert health_receiver_options({})=={'enabled':False}
    assert health_receiver_options({'health_receiver':{'enabled':False,'root':'/old/data'}})['root']=='/old/data'
    for invalid in [{'enabled':'yes'},{'enabled':True},{'enabled':True,'root':'relative'},{'token':'secret'}]:
        with pytest.raises(ValueError):health_receiver_options({'health_receiver':invalid})
