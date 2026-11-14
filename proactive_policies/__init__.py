"""Single-policy registry. Modules contribute data/rules, never run another Agent."""
from copy import deepcopy
from . import goal_execution, basic
MODULES={m.ID:m for m in (goal_execution,basic)}

def defaults():
    return {'enabled':True,'id':goal_execution.ID,'configs':{k:m.defaults() for k,m in MODULES.items()}}

def validate(value):
    base=defaults()
    if not isinstance(value,dict) or set(value)-set(base):raise ValueError('每次只能选择一个主动消息策略。')
    base.update(deepcopy(value))
    if type(base['enabled']) is not bool or not isinstance(base['id'],str) or base['id'] not in MODULES:
        raise ValueError('未知主动消息策略；请选择已安装策略。')
    configs=base['configs']
    if not isinstance(configs,dict) or set(configs)-set(MODULES):raise ValueError('无效的主动消息策略配置。')
    base['configs']={k:m.validate(configs.get(k,{})) for k,m in MODULES.items()}
    return base

def migrate(value,core_prompt,core_fixed):
    # Only known legacy defaults move with the policy. Explicit user-authored
    # fixed reminders remain independent instructions and are never deleted.
    value=deepcopy(value)
    legacy='policy' not in value
    policy=validate(value.get('policy',{}))
    goal=policy['configs'][goal_execution.ID]
    for key in ('goal_list_ids','action_list_ids'):
        if key in value:
            incoming=value.pop(key)
            if goal[key] and goal[key]!=incoming:raise ValueError('旧版与策略中的目标来源配置冲突。')
            goal[key]=incoming
    if legacy:
        hb=value.get('heartbeat')
        if isinstance(hb,dict) and 'prompt' in hb:
            goal['heartbeat_prompt']=hb['prompt'];hb['prompt']=core_prompt
        known={i:p for i,t,p in goal_execution.PRESETS}
        fixed=value.get('fixed',[])
        if not isinstance(fixed,list):raise ValueError('固定提醒必须为数组。')
        for item in fixed:
            if isinstance(item,dict) and item.get('id') in known:
                if item.get('prompt')==known[item['id']]:
                    goal['fixed_prompts'][item['id']]=item['prompt'];item['prompt']=core_fixed[item['id']]
                else:goal['fixed_prompts'].pop(item['id'],None)
    value['policy']=validate(policy)
    return value

def active(policy):
    return MODULES[policy['id']] if policy['enabled'] else None

def view(policy,now):
    module=active(policy)
    if module is None:return {'enabled':False,'id':None,'context':{},'config':{}}
    config=policy['configs'][module.ID]
    return {'enabled':True,'id':module.ID,'config':deepcopy(config),'dependencies':module.DEPENDENCIES,'context':module.context(config,now)}

def event_prompt(policy,event,base,fixed_id=''):
    module=active(policy)
    return module.event_prompt(policy['configs'][module.ID],event['kind'],base,fixed_id) if module else base

def event_fields(policy,event,local_due):
    module=active(policy)
    config=policy['configs'][module.ID] if module else {}
    required=event['kind']=='fixed' or event['kind']=='heartbeat' and config.get('notification_mode')=='always'
    return {'notification_required':required,**(module.event_fields(config,event,local_due) if module else {})}

def catalog():
    return [{'id':k,'label':m.LABEL,'dependencies':m.DEPENDENCIES} for k,m in MODULES.items()]

def owns_prompt(policy,kind,text):
    # Recognize stored policy prompts during legacy migration, including when off.
    sentinel=object()
    return any(m.event_prompt(policy['configs'][k],kind,sentinel,'')==text or
        kind=='heartbeat' and policy['configs'][k].get('heartbeat_prompt')==text for k,m in MODULES.items())
