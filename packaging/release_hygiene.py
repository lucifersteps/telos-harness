"""Build-only privacy normalization and third-party license collection."""
from pathlib import Path
import importlib
import importlib.metadata as metadata
import json
import pprint
import re
import shutil
import sysconfig


def prepare_sysconfig_hook(build):
    name=sysconfig._get_sysconfigdata_name()
    values=importlib.import_module(name).build_time_vars
    def clean(value):
        if isinstance(value,str):
            value=re.sub(r'/Users/[^/\s]+', '/build', value)
            value=re.sub(r'/private/var/folders/[^\s]+', '/tmp/build', value)
        return value
    generated=build/'generated';generated.mkdir(exist_ok=True)
    (generated/(name+'.py')).write_text('build_time_vars = '+pprint.pformat({k:clean(v) for k,v in values.items()})+'\n')
    hooks=build/'hooks/pre_find_module_path';hooks.mkdir(parents=True,exist_ok=True)
    (hooks/('hook-'+name+'.py')).write_text('def pre_find_module_path(api):\n    api.search_dirs = '+repr([str(generated)])+'\n')
    return hooks.parent


def collect_licenses(root,package):
    target=package/'licenses';target.mkdir(exist_ok=True)
    # Keep notices for the pinned build dependencies, including bootloader exception.
    rows=[]
    for line in (root/'requirements.lock').read_text().splitlines():
        if '==' not in line:continue
        name=line.split('==')[0]
        if name.lower()=='pilk':raise ValueError('GPL pilk must not be included')
        dist=metadata.distribution(name);files=[]
        for file in dist.files or []:
            if not re.match(r'(?i)(?:LICENSE|COPYING|NOTICE)(?:$|[._-])',Path(file).name):continue
            source=Path(dist.locate_file(file))
            if not source.is_file():continue
            relative=Path(*[p for p in Path(file).parts if p not in ('.','..')])
            output=target/name/relative;output.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,output);files.append(str(output.relative_to(package)))
        rows.append({'name':name,'version':dist.version,'license':dist.metadata.get('License-Expression') or dist.metadata.get('License'),'notices':files})
    python_license=Path(sysconfig.get_path('stdlib'))/'LICENSE.txt'
    shutil.copy2(python_license,target/'PYTHON-LICENSE.txt')
    shutil.copytree(root/'vendor/silk',target/'silk',ignore=shutil.ignore_patterns('src','interface'))
    (target/'dependency-notices.json').write_text(json.dumps(rows,indent=2)+'\n')


def archive_metadata(member):
    member.uid=member.gid=0
    member.uname=member.gname=''
    member.mtime=0
    member.pax_headers={k:v for k,v in member.pax_headers.items() if k not in ('atime','ctime','mtime')}
    return member
