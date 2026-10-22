"""Conservative direct-write policy and inverse patches; no independent state store."""
from copy import deepcopy
from datetime import datetime

COMMON = {'title', 'notes', 'url', 'calendar_id'}
FIELDS = {
    'event': COMMON | {'start', 'end', 'all_day', 'location', 'alarm_minutes_before'},
    'reminder': COMMON | {'due', 'completed', 'priority', 'flagged', 'tags', 'early_reminder_minutes'},
}
DEFAULTS = {'notes':'', 'url':'', 'location':'', 'due':'', 'completed':False,
            'priority':0, 'flagged':False, 'tags':[], 'early_reminder_minutes':None,
            'all_day':False, 'alarm_minutes_before':[]}


def inverse_fields(action, kind, fields, current=None):
    """None means confirmation required; {} means a new object can be deleted."""
    current = current or {}
    if action not in {'create','update'} or kind not in FIELDS or not fields or set(fields)-FIELDS[kind]:
        return None
    if current.get('recurring') or current.get('recurrence_rules') or current.get('attendees'):
        return None
    if current.get('parent_id') or current.get('subtask_ids'):
        return None
    if action == 'create':
        return {}
    # Moving objects can change identifiers/accounts or shared-calendar side effects.
    if 'calendar_id' in fields or ('completed' in fields and current.get('completed')):
        return None
    # Absolute/location/custom alarms cannot be reconstructed by relative-minute fields.
    if kind == 'event' and set(fields) & {'start','end','all_day','alarm_minutes_before'}:
        if len(current.get('alarms', [])) != len(current.get('alarm_minutes_before', [])) or any(a.get('email') or a.get('sound') or a.get('type',0) != 0 for a in current.get('alarms', [])):
            return None
    if kind == 'reminder' and 'due' in fields:
        # A nonstandard time alarm cannot be faithfully restored by the due field.
        alarms = current.get('alarms', [])
        due = current.get('due','')
        if len(due)>10:
            try:
                standard=(len(alarms)==1 and datetime.fromisoformat(alarms[0].get('absolute_date',''))==datetime.fromisoformat(due))
            except ValueError:standard=False
            if not standard:return None
        elif alarms:return None
        if any(a.get('location') or a.get('email') or a.get('sound') or a.get('type',0)!=0 for a in alarms):return None
    inverse = {}
    for key in fields:
        if key not in current and key not in DEFAULTS:
            return None
        inverse[key] = deepcopy(current.get(key, DEFAULTS.get(key)))
        if inverse[key] is None and key != 'early_reminder_minutes':
            return None
    if kind == 'event' and set(fields) & {'start','end','all_day'}:
        # EventKit may rewrite alarms when toggling all-day status.
        inverse.update({k:deepcopy(current[k]) for k in ('start','end')})
        inverse['all_day']=current.get('all_day',False)
        inverse['alarm_minutes_before'] = deepcopy(current.get('alarm_minutes_before', []))
    return inverse


def direct_allowed(preview):
    return inverse_fields(preview.get('action'), preview.get('kind'),
                          preview.get('fields', {}), preview.get('current')) is not None
