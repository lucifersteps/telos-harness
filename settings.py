"""插件自己的非敏感配置；凭据由 Harness credentials 服务保存。"""
import json
import os
from pathlib import Path

DEFAULTS={'version':1,'max_steps':24,'persona':'default','wechat':{'enabled':False}}

def runtime_options(config):
    tools=config.get('tools',{})
    if not isinstance(tools,dict) or set(tools)-{'apple','maps'} or any(type(v) is not bool for v in tools.values()):
        raise ValueError('工具开关必须为 apple/maps 布尔值。')
    model=config.get('model_selection')
    if model is not None:
        if (not isinstance(model,dict) or set(model)-{'provider','model','reasoningEffort'}
            or not all(isinstance(model.get(k),str) and model[k].strip() for k in ('provider','model'))
            or any(not isinstance(v,str) or not v.strip() or len(v)>256 for v in model.values())):
            raise ValueError('请选择有效的 DSH 模型。')
    return {'tools':{'apple':True,'maps':True,**tools},'model_selection':model}

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
