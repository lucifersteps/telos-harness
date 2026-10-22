"""Apple extended fields; shares the existing preview and write receipt path."""
from datetime import datetime
from pathlib import Path
import hashlib

CONTAINER_KINDS = {'list', 'group', 'section', 'event_calendar'}
EXTRA_FIELDS = {
    'parent_id': {'type': 'string', 'description': '仅提醒事项：父事项编号；空串移出父任务。创建子任务用 reminder + parent_id，其他字段照常设置。'},
    'section_id': {'type': 'string', 'description': '仅提醒事项：所在清单内的分区编号；空串移出分区。'},
    'urgent': {'type': 'boolean', 'description': '仅提醒事项：原生紧急提醒开关；开启需要明确到期日期和时刻。'},
    'attachments': {'type': 'array', 'description': '仅提醒事项：完整附件列表，省略保留、[]清除。图片用file_path，网页用url；保留已有附件用id。替换会移除未列出的附件。',
        'items': {'type': 'object', 'properties': {k:{'type':'string','minLength':1} for k in ('id','file_path','url')}, 'additionalProperties': False}},
    'travel_time_minutes': {'type':'integer','minimum':0,'maximum':10080,'description':'仅日程：出行时间分钟数，0清除；不改变日程开始时刻，不自动计算路况。'},
}
QUERY_FIELDS = {
    **{k:{'type':'array','items':{'type':'string'},'uniqueItems':True} for k in ('calendar_ids','section_ids','parent_ids','tags')},
    **{k:{'type':'boolean'} for k in ('flagged','urgent','include_overdue')},
    'priority': {'type':'integer','minimum':0,'maximum':9},
    'due_start': {'type':'string','description':'提醒事项到期范围起点（含），YYYY-MM-DD或带时区ISO日期。'},
    'due_end': {'type':'string','description':'提醒事项到期范围终点（不含），YYYY-MM-DD或带时区ISO日期。'},
}

def validate_features(kind, fields, current=None):
    current = current or {}
    for key in ('parent_id','section_id','urgent','attachments'):
        if key in fields and kind != 'reminder':
            raise ValueError(f'{key} 仅适用于提醒事项。')
    if 'travel_time_minutes' in fields and kind != 'event':
        raise ValueError('出行时间仅适用于日程。')
    if kind in CONTAINER_KINDS:
        allowed = {'section':{'title','calendar_id'}, 'event_calendar':{'title','account_id','color'},
                   'list':{'title','account_id','color','emoji','symbol','group_id'},'group':{'title','account_id'}}[kind]
        if set(fields)-allowed:
            raise ValueError('该容器不支持这些字段：'+','.join(sorted(set(fields)-allowed)))
    values = {**current, **fields}
    if values.get('urgent') and ('urgent' in fields or 'due' in fields):
        due = values.get('due','')
        if len(due)<=10 or datetime.fromisoformat(due).tzinfo is None:
            raise ValueError('紧急提醒需要明确到期日期、时刻和时区。')
    existing = {a['id'] for a in current.get('attachments',[]) if a.get('id')}
    seen = set()
    for attachment in fields.get('attachments',[]):
        if len(attachment)!=1 or not set(attachment)<={'id','file_path','url'}:
            raise ValueError('每个附件只能指定 id、file_path 或 url 中的一项。')
        key,value = next(iter(attachment.items()))
        if (key,value) in seen: raise ValueError('附件不能重复。')
        seen.add((key,value))
        if key=='id' and value not in existing: raise ValueError('要保留的附件不属于当前事项，请重新查询。')
        if key=='file_path' and (not Path(value).is_absolute() or not Path(value).is_file()):
            raise ValueError('图片附件必须使用已存在的本机绝对路径。')
        if key=='url':
            from urllib.parse import urlparse
            parsed=urlparse(value)
            if parsed.scheme not in {'http','https'} or not parsed.netloc: raise ValueError('网页附件需要完整 http/https 链接。')

def attachment_fingerprints(fields):
    return {a['file_path']:hashlib.sha256(Path(a['file_path']).read_bytes()).hexdigest()
            for a in fields.get('attachments',[]) if 'file_path' in a}

def feature_summary(fields, current):
    lines=[]
    for key,label in [('parent_id','父任务'),('section_id','清单内分区')]:
        if key in fields: lines.append(label+'：'+(fields[key] or '移出'))
    if 'urgent' in fields: lines.append('紧急提醒：'+('开启' if fields['urgent'] else '关闭'))
    if 'travel_time_minutes' in fields: lines.append(f"出行时间：{fields['travel_time_minutes']} 分钟（不改变开始时刻）")
    if 'attachments' in fields:
        lines.append('附件：'+('清空全部' if not fields['attachments'] else '替换为 '+ '、'.join(a.get('file_path',a.get('url','保留附件 '+a.get('id',''))) for a in fields['attachments'])))
        lines.append(f"当前附件 {len((current or {}).get('attachments',[]))} 个；未列出的附件将移除。")
    return lines

def matches_filters(item, args):
    for key,field in [('calendar_ids','calendar_id'),('section_ids','section_id'),('parent_ids','parent_id')]:
        if args.get(key) and item.get(field) not in args[key]: return False
    if args.get('tags') and not {t.casefold() for t in args['tags']} <= {t.casefold() for t in item.get('tags',[])}: return False
    for key in ('flagged','urgent','priority'):
        if key in args and item.get(key)!=args[key]: return False
    if any(k in args for k in ('due_start','due_end','include_overdue')):
        if item.get('kind')!='reminder': return False
        due=item.get('due','')
        def parsed(value):
            d=datetime.fromisoformat(value)
            return d.astimezone() if d.tzinfo is None else d
        if args.get('due_start') and (not due or parsed(due)<parsed(args['due_start'])): return False
        if args.get('due_end') and (not due or parsed(due)>=parsed(args['due_end'])): return False
        if args.get('include_overdue') is False and due and not item.get('completed'):
            if (len(due)==10 and due<datetime.now().date().isoformat()) or (len(due)>10 and parsed(due)<datetime.now().astimezone()): return False
    return True
