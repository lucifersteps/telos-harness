"""Apple 重复规则的输入校验和审批摘要；省略保留，null 清除。"""
from datetime import datetime

WEEKDAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']
DAY_NAMES = dict(zip(WEEKDAYS, ['日', '一', '二', '三', '四', '五', '六']))
RECURRENCE_SCHEMA = {
    'type': ['object', 'null'], 'additionalProperties': False,
    'required': ['frequency'],
    'description': '一个完整重复规则，替换原规则；null取消重复，省略保留。按日/周/月/年；工作日用weekly和mon到fri。每月最后一天用month_days:[-1]。支持每月或每年第几个星期几；不支持农历、按完成时间间隔，不能近似替代。',
    'properties': {
        'frequency': {'type': 'string', 'enum': ['daily', 'weekly', 'monthly', 'yearly']},
        'ordinal_weekdays': {'type':'array','minItems':1,'uniqueItems':True,'description':'仅monthly/yearly；如每月第二个周一用[{day:mon,week:2}]，最后一个周五用week:-1。与weekdays/month_days互斥。yearly可配months。','items':{'type':'object','properties':{'day':{'type':'string','enum':WEEKDAYS},'week':{'type':'integer','minimum':-5,'maximum':5}},'required':['day','week'],'additionalProperties':False}},
        'interval': {'type': 'integer', 'minimum': 1, 'maximum': 999},
        'weekdays': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                     'items': {'type': 'string', 'enum': WEEKDAYS},
                     'description': '仅weekly；省略则按首次日期所在星期。'},
        'month_days': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                       'items': {'type': 'integer', 'minimum': -31, 'maximum': 31},
                       'description': '仅monthly，不能为0；负数从月底倒数。不存在的日期跳过。'},
        'months': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                   'items': {'type': 'integer', 'minimum': 1, 'maximum': 12},
                   'description': '仅yearly；日取首次日期。'},
        'count': {'type': 'integer', 'minimum': 1, 'maximum': 10000,
                  'description': '总次数，包含首次；与until互斥。'},
        'until': {'type': 'string', 'description': '截止时刻，带时区ISO 8601；与count互斥。未指定结束则持续重复。'},
    },
}


def validate_recurrence(rule, anchor=None):
    if rule is None:
        return
    if not isinstance(rule, dict) or set(rule) - RECURRENCE_SCHEMA['properties'].keys():
        raise ValueError('重复规则包含不支持的字段。')
    frequency = rule.get('frequency')
    if frequency not in {'daily', 'weekly', 'monthly', 'yearly'}:
        raise ValueError('重复频率必须为 daily、weekly、monthly 或 yearly。')
    for key, maximum in [('interval', 999), ('count', 10000)]:
        if key in rule and (type(rule[key]) is not int or not 1 <= rule[key] <= maximum):
            raise ValueError('重复间隔和次数必须为范围内的正整数。')
    for key, required_frequency in [('weekdays', 'weekly'), ('month_days', 'monthly'), ('months', 'yearly')]:
        if key not in rule:
            continue
        values = rule[key]
        if frequency != required_frequency or not isinstance(values, list) or not values:
            raise ValueError(f'{key} 仅适用于 {required_frequency}，且不能为空。')
        if key == 'weekdays':
            valid = all(isinstance(v, str) and v in WEEKDAYS for v in values)
        else:
            valid = all(type(v) is int and (1 <= v <= 12 if key == 'months' else 1 <= abs(v) <= 31) for v in values)
        if not valid or len(values) != len(set(values)):
            raise ValueError('重复日期无效或重复。')
    if 'ordinal_weekdays' in rule:
        values=rule['ordinal_weekdays']
        if frequency not in {'monthly','yearly'} or 'weekdays' in rule or 'month_days' in rule or not isinstance(values,list) or not values:
            raise ValueError('第几个星期几仅适用于每月/年，不能同时指定星期或月内日期。')
        if any(not isinstance(v,dict) or set(v)!={'day','week'} or v['day'] not in WEEKDAYS or type(v['week']) is not int or not 1<=abs(v['week'])<=5 for v in values):
            raise ValueError('星期序号须为1到5或-1到-5。')
        if len({(v['day'],v['week']) for v in values})!=len(values): raise ValueError('星期序号不能重复。')
    if 'until' in rule:
        if 'count' in rule:
            raise ValueError('重复截止时间与次数只能选择一个。')
        end = datetime.fromisoformat(rule['until'])
        if end.tzinfo is None:
            raise ValueError('重复截止时刻必须包含时区。')
        if anchor:
            start = datetime.fromisoformat(anchor)
            if start.tzinfo is None:
                start = start.replace(tzinfo=end.tzinfo)
            if end < start:
                raise ValueError('重复截止时间不能早于首次日期。')


def recurrence_summary(rule):
    if rule is None:
        return '不重复'
    unit = {'daily': '天', 'weekly': '周', 'monthly': '月', 'yearly': '年'}.get(rule.get('frequency'), '?')
    result = f"每{rule.get('interval', 1)}{unit}"
    if rule.get('weekdays'):
        result += '，星期' + '、'.join(DAY_NAMES.get(d, str(d)) for d in rule['weekdays'])
    if rule.get('ordinal_weekdays'):
        result += '，' + '、'.join(('第'+str(v['week']) if v['week']>0 else '倒数第'+str(-v['week']))+'个星期'+DAY_NAMES[v['day']] for v in rule['ordinal_weekdays'])
    if rule.get('month_days'):
        result += '，' + '、'.join(f'{d}日' if d > 0 else f'倒数{-d}日' for d in rule['month_days'])
        result += '（不存在的日期跳过）'
    if rule.get('months'):
        result += '，' + '、'.join(str(m) + '月' for m in rule['months'])
    if rule.get('count'):
        result += f"，共{rule['count']}次（含首次）"
    elif rule.get('until'):
        result += '，截至' + rule['until']
    else:
        result += '，无截止日期'
    if rule.get('advanced'):
        result += '；另含高级规则：' + str(rule['advanced'])
    return result


def validate_recurrence_scope(kind, action, current, fields, scope):
    if scope is not None and (action == 'create' or scope not in {'this', 'future', 'series'}):
        raise ValueError('重复范围无效。')
    if scope is not None and ((kind == 'event' and scope == 'series') or (kind == 'reminder' and scope != 'series') or kind not in {'event', 'reminder'}):
        raise ValueError('日程范围为 this/future，提醒事项范围为 series。')
    if (current or {}).get('recurring'):
        if kind == 'event' and scope not in {'this', 'future'}:
            raise ValueError('需要明确仅这一次，还是本次及以后。')
        if kind == 'reminder' and (action == 'delete' or 'recurrence' in fields) and scope != 'series':
            raise ValueError('需要明确修改或删除整条重复提醒事项。')
    if kind == 'event' and 'recurrence' in fields and scope == 'this':
        raise ValueError('修改重复规则需要选择本次及以后。')
