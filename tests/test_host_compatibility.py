"""Check the release range with DSH's own plugin compatibility gate."""
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from test_desktop import DESKTOP
from test_package import ARCHIVE


@pytest.mark.skipif(DESKTOP is None or not ARCHIVE.exists(), reason='须提供官方桌面应用和插件归档')
def test_host_accepts_supported_versions_and_rejects_next_minor():
    root = Path(__file__).resolve().parents[1]
    with tarfile.open(ARCHIVE) as archive:
        packaged = json.load(archive.extractfile('package/package.json'))
        manifest = json.load(archive.extractfile('package/component-manifest.json'))
    source = json.loads((root / 'package.json').read_text())
    assert packaged['engines'] == source['engines']
    assert manifest['harness'] == packaged['engines']['dsh']
    versions = {
        '0.2.1-rc.1': False,
        '0.2.1-rc.2': True,
        '0.2.1-rc.3': True,
        '0.2.1-rc.99': True,
        '0.2.1': True,
        '0.2.2-0': True,
        '0.2.2-rc.1': True,
        '0.2.2': True,
        '0.2.99': True,
        '0.3.0-0': False,
        '0.3.0-rc.1': False,
        '0.3.0': False,
    }
    script = """
import {readFileSync} from 'node:fs';
const {evaluatePluginCompatibility} = await import(process.argv[1]);
const {packaged, versions} = JSON.parse(readFileSync(0, 'utf8'));
const results = Object.fromEntries(Object.keys(versions).map(version =>
  [version, evaluatePluginCompatibility(packaged, {}, version) === undefined]));
// Reproduce the old exact-version failure without granting an exemption.
const old = {...packaged, engines: {dsh: '0.2.1-rc.2'}};
const originalRejected = evaluatePluginCompatibility(old, {}, '0.2.1-rc.3') !== undefined;
console.log(JSON.stringify({results, originalRejected}));
"""
    boot = DESKTOP / 'Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js'
    result = subprocess.run(
        [str(DESKTOP / 'Contents/MacOS/DeepSeek Harness'), '--expose-internals',
         '--input-type=module', '-e', script, boot.as_uri()],
        input=json.dumps({'packaged': packaged, 'versions': versions}),
        env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    checked = json.loads(result.stdout)
    assert checked['originalRejected']
    assert checked['results'] == versions
