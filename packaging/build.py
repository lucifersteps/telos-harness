"""构建可移动的 macOS 插件归档。源路径只用于构建，不写进运行配置。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]

def run(*args):subprocess.run([str(a) for a in args],check=True,cwd=ROOT)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--version',default='0.2.0');parser.add_argument('--reuse-components',action='store_true')
    args=parser.parse_args()
    if platform.system()!='Darwin' or platform.machine()!='arm64':raise SystemExit('当前构建目标仅支持 macOS Apple Silicon。')
    build=ROOT/'build';package=build/'package';dist=ROOT/'dist'
    build.mkdir(exist_ok=True);dist.mkdir(exist_ok=True)
    os.environ['PYINSTALLER_CONFIG_DIR']=str(build/'cache')
    os.environ['CLANG_MODULE_CACHE_PATH']=str(build/'clang-cache')
    if package.exists():shutil.rmtree(package)
    shutil.copytree(ROOT/'plugin',package)
    shutil.copytree(ROOT/'skills',package/'skills')
    metadata=json.loads((package/'package.json').read_text());metadata.update(name='telos-harness',version=args.version,os=['darwin'],cpu=['arm64'])
    metadata['exports']['.']='./bootstrap.mjs';metadata['exports']['./channel']='./index.mjs'
    (package/'package.json').write_text(json.dumps(metadata,indent=2)+'\n')
    (package/'NOTICE.txt').write_text('Telos 私有验证分发包。第三方依赖保留各自许可；未授予 Telos 源代码的公开再分发许可。\n')
    shutil.copy2(ROOT/'packaging/README.md',package/'README.md')
    # Install-time bundle applies only in this selected, isolated profile.
    patch=(ROOT/'native.patch.yml').read_text()
    patch=patch[:patch.index('- id: skill-filesystem')]
    patch+='\n- id: llm-deepseek\n  disabled: true\n'
    patch+='''- insert:
    - id: speech-to-text
      name: '@deepseek-ai/dsh-experimental-speech-to-text'
      config:
        defaultProvider: sensevoice-local
        language: zh
    - id: speech-to-text-sensevoice
      name: '@deepseek-ai/dsh-experimental-speech-to-text-sensevoice'
      config:
        dataRoot: !!js dshHomePath('speech-to-text', 'sensevoice')
    - id: telos-credentials
      name: '@deepseek-ai/dsh-credentials-local'
    - id: skill
      name: '@deepseek-ai/dsh-skill'
    - id: tool-skill
      name: '@deepseek-ai/dsh-tool-skill'
    - id: tool-todo
      name: '@deepseek-ai/dsh-tool-todo'
      config:
        allowParallelInProgress: false
    - id: telos-bootstrap
      name: telos-harness
    - id: telos-apple
      name: telos-harness/apple
    - id: telos-maps
      name: telos-harness/maps
    - id: telos-wechat
      name: telos-harness/channel
'''
    (package/'cordis.patch.yml').write_text(patch)
    if not args.reuse_components:
        run(sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name','telos-component',
            '--distpath',build/'components','--workpath',build/'pyinstaller','--specpath',build,
            '--paths',ROOT/'checkout/src','--paths',ROOT,'--add-data',f'{ROOT}/personal_prompt.md:.',
            ROOT/'sidecar.py')
        app=build/'native/Telos Harness Apple.app';contents=app/'Contents';exe=contents/'MacOS/telos-apple'
        exe.parent.mkdir(parents=True,exist_ok=True)
        info={'CFBundleIdentifier':'com.telos.harness.apple','CFBundleName':'Telos Harness Apple',
            'CFBundleExecutable':'telos-apple','CFBundlePackageType':'APPL','LSUIElement':True,
            'NSCalendarsFullAccessUsageDescription':'Telos 在你确认后管理日历。',
            'NSRemindersFullAccessUsageDescription':'Telos 在你确认后管理提醒事项。',
            'NSCalendarsUsageDescription':'Telos 读取日历并在确认后保存。',
            'NSRemindersUsageDescription':'Telos 读取提醒事项并在确认后保存。'}
        (contents/'Info.plist').write_bytes(plistlib.dumps(info))
        run('swiftc','-target','arm64-apple-macosx14.0','-framework','AppKit','-framework','EventKit',
            '-Xlinker','-sectcreate','-Xlinker','__TEXT','-Xlinker','__info_plist','-Xlinker',contents/'Info.plist',
            ROOT/'checkout/src/telos/connectors/apple/personal_helper.swift','-o',exe)
        run('codesign','--force','--sign','-','--identifier','com.telos.harness.apple',app)
    shutil.copytree(build/'components/telos-component',package/'bin')
    shutil.copytree(build/'native',package/'native')
    files={str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.rglob('*')) if p.is_file()}
    (package/'component-manifest.json').write_text(json.dumps({'version':args.version,'harness':'0.2.1rc2',
        'platform':'macos-arm64','min_macos':'14','files':files},indent=2)+'\n')
    archive=dist/f'telos-harness-{args.version}-macos-arm64.tgz'
    temporary=archive.with_suffix('.tgz.tmp')
    with tarfile.open(temporary,'w:gz') as tar:tar.add(package,arcname='package')
    os.replace(temporary,archive)
    (archive.with_suffix('.tgz.sha256')).write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    print(archive)
if __name__=='__main__':main()
