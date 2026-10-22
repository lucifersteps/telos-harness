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
from build_codec import build_codec
from release_hygiene import prepare_sysconfig_hook, collect_licenses, archive_metadata

ROOT=Path(__file__).resolve().parents[1]

def run(*args):subprocess.run([str(a) for a in args],check=True,cwd=ROOT)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--version',default='0.4.13');parser.add_argument('--reuse-components',action='store_true')
    parser.add_argument('--reuse-apple',action='store_true',help='Preserve the existing signed Apple component while rebuilding Python')
    parser.add_argument('--sign-identity',default='-',help='Developer ID Application identity; - is local ad-hoc validation only')
    args=parser.parse_args()
    if platform.system()!='Darwin' or platform.machine()!='arm64':raise SystemExit('当前构建目标仅支持 macOS Apple Silicon。')
    build=ROOT/'build';package=build/'package';dist=ROOT/'dist'
    build.mkdir(exist_ok=True);dist.mkdir(exist_ok=True)
    os.environ['PYINSTALLER_CONFIG_DIR']=str(build/'cache')
    os.environ['CLANG_MODULE_CACHE_PATH']=str(build/'clang-cache')
    if package.exists():shutil.rmtree(package)
    shutil.copytree(ROOT/'plugin',package)
    shutil.copytree(ROOT/'skills',package/'skills')
    shutil.copytree(ROOT/'personas',package/'personas')
    # Public metadata lives at the repository root; plugin/package.json is a test fixture.
    # Source modules live in plugin/, while release modules live at the archive root.
    metadata=json.loads((ROOT/'package.json').read_text());metadata['version']=args.version
    metadata['main']=metadata['main'].replace('./plugin/','./',1)
    metadata['exports']={key:value.replace('./plugin/','./',1) for key,value in metadata['exports'].items()}
    (package/'package.json').write_text(json.dumps(metadata,indent=2)+'\n')
    shutil.copy2(ROOT/'LICENSE',package/'LICENSE')
    shutil.copy2(ROOT/'THIRD_PARTY_NOTICES.md',package/'THIRD_PARTY_NOTICES.md')
    shutil.copy2(ROOT/'packaging/README.md',package/'README.md')
    # Only Telos is installed. Generic services belong to the selected DSH host.
    (package/'cordis.patch.yml').write_text("- insert:\n    - id: telos-bootstrap\n      name: telos-harness\n")
    build_codec(build/'native/telos-silk')
    hooks=prepare_sysconfig_hook(build)
    if not args.reuse_components:
        run(sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name','telos-component',
            '--distpath',build/'components','--workpath',build/'pyinstaller','--specpath',build,
            '--strip','--exclude-module','pilk','--additional-hooks-dir',hooks,
            '--paths',ROOT/'checkout/src','--paths',ROOT,'--add-data',f'{ROOT}/personal_prompt.md:.',
            ROOT/'sidecar.py')
    if not (args.reuse_components or args.reuse_apple):
        app=build/'native/Telos Harness Apple.app';contents=app/'Contents';exe=contents/'MacOS/telos-apple'
        exe.parent.mkdir(parents=True,exist_ok=True)
        info={'CFBundleIdentifier':'com.telos.harness.apple','CFBundleName':'Telos Apple','CFBundleDisplayName':'Telos Apple',
            'CFBundleExecutable':'telos-apple','CFBundlePackageType':'APPL','LSUIElement':True,
            'NSCalendarsFullAccessUsageDescription':'Telos 在你确认后管理日历。',
            'NSRemindersFullAccessUsageDescription':'Telos 在你确认后管理提醒事项。',
            'NSCalendarsUsageDescription':'Telos 读取日历并在确认后保存。',
            'NSRemindersUsageDescription':'Telos 读取提醒事项并在确认后保存。'}
        (contents/'Info.plist').write_bytes(plistlib.dumps(info))
        run('swiftc','-debug-prefix-map',str(ROOT)+'=.', '-target','arm64-apple-macosx14.0','-framework','AppKit','-framework','EventKit',
            '-Xlinker','-sectcreate','-Xlinker','__TEXT','-Xlinker','__info_plist','-Xlinker',contents/'Info.plist',
            ROOT/'checkout/src/telos/connectors/apple/personal_helper.swift','-o',exe)
        entitlements=build/'apple-entitlements.plist'
        entitlements.write_bytes(plistlib.dumps({'com.apple.security.personal-information.calendars':True}))
        run('codesign','--force','--sign',args.sign_identity,'--identifier','com.telos.harness.apple',
            '--options','runtime','--entitlements',entitlements,*(['--timestamp'] if args.sign_identity!='-' else []),app)
    shutil.copytree(build/'components/telos-component',package/'bin')
    shutil.copytree(build/'native',package/'native')
    collect_licenses(ROOT,package)
    files={str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.rglob('*')) if p.is_file()}
    (package/'component-manifest.json').write_text(json.dumps({'version':args.version,'harness':'0.2.1rc2',
        'platform':'macos-arm64','min_macos':'14','files':files},indent=2)+'\n')
    archive=dist/f'telos-harness-{args.version}-macos-arm64.tgz'
    temporary=archive.with_suffix('.tgz.tmp')
    with tarfile.open(temporary,'w:gz') as tar:tar.add(package,arcname='package',filter=archive_metadata)
    os.replace(temporary,archive)
    (archive.with_suffix('.tgz.sha256')).write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    print(archive)
if __name__=='__main__':main()
