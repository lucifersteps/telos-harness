"""Telos-owned Pulse Relay process group. Stdin is the host lifetime lease."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def watchdog(group: int) -> None:
    # A separate session survives supervisor SIGKILL and holds the ownership lock
    # until all orphaned children have been killed. No PID file is used to kill.
    clean = sys.stdin.buffer.read() == b'clean\n'
    if clean:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            for _ in range(30):
                time.sleep(.1)
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    return


def commands(root: Path) -> dict[str, list[str]]:
    python = str(root / '.venv/bin/python')
    service = str(root / 'scripts/run_service.py')
    return {
        'core': [python, service, 'core'],
        'ingress': [python, service, 'ingress'],
        'tailscaled': ['/opt/homebrew/opt/tailscale/bin/tailscaled', '--tun=userspace-networking',
                      '--state=' + str(root / 'private/tailscale/tailscaled.state'),
                      '--statedir=' + str(root / 'private/tailscale'),
                      '--socket=' + str(root / 'private/tailscaled.sock'),
                      '--port=0', '--no-logs-no-support'],
        'awake': ['/usr/bin/caffeinate', '-s', '-w', str(os.getpid())],
    }


def run(root: Path, plan: dict[str, list[str]] | None = None) -> int:
    os.umask(0o077)
    if os.getpgrp() != os.getpid():
        raise RuntimeError('Health receiver must run in its own process session')
    root = root.resolve(strict=True)
    settings = json.loads((root / 'private/service.json').read_text())
    ports = [settings['core_port'], settings['ingress_port']]
    if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != 2:
        raise ValueError('Invalid health receiver ports')
    lock = (root / 'private/managed.lock').open('a')
    deadline = time.monotonic() + 4
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                lock.close()
                raise RuntimeError('Health receiver is already owned by another Telos instance') from None
            time.sleep(.1)
    children: dict[str, subprocess.Popen] = {}
    logs = []
    guard = None
    stopped = threading.Event()
    failure = None
    def publish(phase, error=None):
        value = {'phase': phase, 'pid': os.getpid(), 'children': {k: p.pid for k, p in children.items() if p.poll() is None},
                 'updated_at': datetime.now(timezone.utc).isoformat()}
        if error:
            value['error'] = error
        status = root / 'private/managed-status.json'
        temporary = status.with_suffix('.tmp')
        temporary.write_text(json.dumps(value) + '\n')
        temporary.replace(status)
        try:
            print(json.dumps(value), flush=True)
        except BrokenPipeError:
            stopped.set()
    def lease():
        while not stopped.is_set():
            readable, _, _ = select.select([sys.stdin], [], [], .2)
            if readable and not os.read(sys.stdin.fileno(), 4096):
                stopped.set()
    def launch(name, args):
        log = (root / 'logs' / (name + '.managed.log')).open('ab', buffering=0)
        logs.append(log)
        child = subprocess.Popen(args, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 close_fds=True)
        children[name] = child
        return child
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    threading.Thread(target=lease, daemon=True).start()
    try:
        guard = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--watchdog', str(os.getpid())],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, pass_fds=(lock.fileno(),))
        # Never take over or kill unrelated listeners. Migration removes old jobs.
        for port in ports:
            with socket.socket() as probe:
                probe.settimeout(.3)
                if probe.connect_ex(('127.0.0.1', port)) == 0:
                    raise RuntimeError('Health receiver port is already in use')
        publish('starting')
        for name, args in (commands(root) if plan is None else plan).items():
            if stopped.is_set():
                break
            launch(name, args)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 15
        ready = False
        last_backup = max((p.stat().st_mtime for p in (root / 'backups').glob('health-????????.sqlite3')), default=0)
        while not stopped.wait(.1):
            if guard.poll() is not None:
                raise RuntimeError('Health receiver cleanup guard stopped')
            if any(p.poll() is not None for k, p in children.items() if k != 'backup'):
                raise RuntimeError('A health receiver process stopped; restart Telos to recover')
            if not ready:
                try:
                    with opener.open(f'http://127.0.0.1:{ports[0]}/healthz', timeout=.5) as response:
                        healthy = response.status == 200
                    with socket.create_connection(('127.0.0.1', ports[1]), timeout=.5):
                        pass
                    if healthy:
                        ready = True
                        publish('running')
                except OSError:
                    pass
                if not ready and time.monotonic() > deadline:
                    raise RuntimeError('Health receiver did not become ready')
            if ready and time.time() - last_backup >= 86400:
                launch('backup', [str(root / '.venv/bin/python'), str(root / 'scripts/backup.py')])
                last_backup = time.time()
    except Exception as exc:
        failure = str(exc)
    finally:
        stopped.set()
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 4
        for child in children.values():
            try:
                child.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()
        try:
            publish('failed' if failure else 'stopped', failure)
        finally:
            if guard and guard.poll() is None:
                guard.stdin.write(b'clean\n')
                guard.stdin.close()
                guard.wait(timeout=5)
            lock.close()
    return 1 if failure else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('--watchdog', type=int)
    args = parser.parse_args()
    if args.watchdog:
        watchdog(args.watchdog)
    elif args.root:
        raise SystemExit(run(args.root))
    else:
        parser.error('--root is required')
