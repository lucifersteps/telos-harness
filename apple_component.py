"""Launch the bundled Apple app through LaunchServices, for every request.

No daemon or model runtime: private temporary files carry one JSON request/reply.
The app owns TCC permission; the existing AppleService owns write receipts.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

from telos.tools.apple.service import PersonalAppleClient


class AppleAppRunner:
    def run(self, argv, *, input, timeout):
        executable = Path(argv[0]).resolve()
        app = executable.parents[2]
        if app.suffix != '.app' or executable.parent.name != 'MacOS':
            raise ValueError('Apple 组件必须是 macOS 应用包。')
        action = json.loads(input).get('action')
        if action == 'watch':
            raise ValueError('此通道只支持单次请求，不启动常驻监听。')
        with tempfile.TemporaryDirectory(prefix='telos-apple-') as directory:
            root = Path(directory)
            paths = [root / name for name in ('request.json', 'reply.json', 'error.log')]
            for path in paths:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as file:
                    if path == paths[0]:
                        file.write(input)
            # A new LaunchServices instance, not a child exec of its Mach-O binary.
            # The native deadline is shorter than the parent wait: a timed-out app
            # cannot remain alive indefinitely after /usr/bin/open is terminated.
            result = subprocess.run([
                '/usr/bin/open', '-n', '-W', '-g', '-j', '-a', str(app),
                '--stdin', str(paths[0]), '--stdout', str(paths[1]), '--stderr', str(paths[2]),
                '--args', '--telos-timeout', str(timeout),
            ], capture_output=True, text=True, timeout=timeout + 10, check=False)
            return subprocess.CompletedProcess(argv, result.returncode, paths[1].read_text(),
                                               paths[2].read_text() or result.stderr)


def apple_client(config, helper):
    # Explicit overrides are for existing external/test connectors. The distributed
    # app always follows the same launch path for status, approval and actual I/O.
    runner = None if config.get('apple_helper') else AppleAppRunner()
    return PersonalAppleClient(helper, runner=runner)
