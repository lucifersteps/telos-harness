"""Goal/plan/activity policy configuration; no scheduling or delivery ownership."""
from datetime import timedelta
from copy import deepcopy

DEFAULT_PROMPT = ('读取目标、当天行动与完整日程、实际行为，按顺序诊断：缺乏计划则提醒制定具体行动和时间安排；'
    '已有计划但出现明确执行偏差则直接指出并提醒行动，确实临近截止时说明剩余时间；没有明显问题则具体肯定。'
    '每次只给一个主要判断，证据不足则说明无法核实，不把缺数据当作没行动。'
    '每次有效 heartbeat 必须调用 notify_user 恰好一次，附内部 assessment，保持简短，不机械播报没有新变化。'
    '不自动改计划，不新增提醒频率；heartbeat 不主动提醒习惯。')
PRESETS = [
    ('midnight', '00:00', '简短收尾；仅当 habit_reminder_allowed=true 时只读查询刚过去一天的习惯完成情况，对确认未完成的习惯给一次轻量提醒；无法核实则说明，不猜测、不补记、不新增任务、不要求回复。全部完成时简短确认即可。'),
    ('morning', '08:00', '读取目标、当天行动、完整当日日程和行为。先判断是否缺少具体计划，缺少则直接提醒制定；已有计划再检查截至现在的执行情况，尚未到时间不催执行；没有明显问题则肯定安排或进展。只给一个主要结论，不提醒习惯。'),
    ('noon', '12:00', '读取目标、当天行动、完整当日日程和上午行为，优先判断今天有无具体计划；有计划再看上午是否明显偏离。缺计划提醒制定，执行偏离提醒行动，没有明显问题就具体肯定；证据不足说明未知，不反复盘问，不提醒习惯。'),
    ('evening', '18:00', '读取目标、当天行动、完整当日日程及今日行为，先判断缺乏计划，再判断明确的执行偏差，给一个直接提醒；确实临近截止时说明剩余时间，没有明显问题就肯定进展。证据不足说明未知，不编造紧迫感，不提醒习惯。'),
]

ID = 'goal_execution'
LABEL = '目标执行督促'
DEPENDENCIES = ['personalPlan', 'activity_history']

def defaults():
    return {'goal_list_ids': [], 'action_list_ids': [], 'notification_mode': 'always', 'observation_enabled': False,
            'heartbeat_prompt': DEFAULT_PROMPT, 'fixed_prompts': {i:p for i,t,p in PRESETS}}

def validate(value):
    base = defaults()
    if not isinstance(value, dict) or set(value)-set(base):
        raise ValueError('无效的目标执行督促配置。')
    base.update(deepcopy(value))
    if type(base['observation_enabled']) is not bool:
        raise ValueError('行为观察开关必须为布尔值。')
    for key in ('goal_list_ids','action_list_ids'):
        ids=base[key]
        if (not isinstance(ids,list) or len(ids)>100 or any(not isinstance(i,str) or not i.strip() or len(i)>256 for i in ids) or len(set(ids))!=len(ids)):
            raise ValueError('目标和行动来源必须为不重复的真实清单 ID 数组。')
    if base['notification_mode'] not in ('always','on_demand'):
        raise ValueError('通知方式应为 always 或 on_demand。')
    texts=[base['heartbeat_prompt']]
    if not isinstance(base['fixed_prompts'],dict) or len(base['fixed_prompts'])>32 or any(not isinstance(k,str) or not k or len(k)>64 for k in base['fixed_prompts']):
        raise ValueError('无效的策略固定提醒内容。')
    texts.extend(base['fixed_prompts'].values())
    if any(not isinstance(p,str) or not p.strip() or len(p)>4000 for p in texts):
        raise ValueError('策略提示词应为 1–4000 字。')
    return base

def context(config,now):
    day=now.replace(hour=0,minute=0,second=0,microsecond=0)
    return {'observation_enabled':config['observation_enabled'], 'query_requirements': {'goal_list_ids':config['goal_list_ids'],'action_list_ids':config['action_list_ids'],
        'day_start':day.isoformat(),'day_end':(day+timedelta(days=1)).isoformat()}}

def event_fields(config,event,local_due):
    return {'habit_reminder_allowed':event['kind']=='fixed' and local_due.strftime('%H:%M')=='00:00'}

def event_prompt(config,kind,base,fixed_id):
    if kind=='heartbeat':
        text=config['heartbeat_prompt']
        if config['notification_mode']=='on_demand' and text==DEFAULT_PROMPT:
            text=text.replace('每次有效 heartbeat 必须调用 notify_user 恰好一次，附内部 assessment，保持简短，不机械播报没有新变化。','有值得联系的具体事项时才调用 notify_user，附内部 assessment；没有则静默。')
        return text
    if kind=='fixed':return config['fixed_prompts'].get(fixed_id,base)
    return base
