"""DSH 服务适配器边界测试；官方 Node 发行版安装后可直接执行。"""
import os
from pathlib import Path
import shutil
import subprocess
import pytest

def test_native_voice_adapter():
    node=os.environ.get('TELOS_TEST_NODE') or shutil.which('node')
    if not node:pytest.skip('需要 Node 以运行原生适配器测试')
    result=subprocess.run([node,'--test',str(Path(__file__).with_name('native_voice.test.mjs'))],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
