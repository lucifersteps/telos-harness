"""插件自己的非敏感配置；凭据由 Harness credentials 服务保存。"""
import json
import os
from pathlib import Path

DEFAULTS={'version':1,'model':'deepseek-flash','base_url':'https://api.deepseek.com/anthropic',
          'max_steps':24,'wechat':{'enabled':False}}

def read_config(root):
    path=Path(root)/'config.json'
    if not path.exists():return json.loads(json.dumps(DEFAULTS))
    value=json.loads(path.read_text())
    if value.get('version')!=1:raise ValueError('不支持此配置版本；请使用对应版本插件，原文件未修改。')
    return value

def write_config(root,value):
    root=Path(root);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=root/'config.json';temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
