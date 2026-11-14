"""Basic contact policy: no required domain tools or diagnostic labels."""
ID='basic'
LABEL='基础按需联系'
DEPENDENCIES=[]
def defaults():return {'notification_mode':'on_demand'}
def validate(value):
    if not isinstance(value,dict) or set(value)-{'notification_mode'} or value.get('notification_mode','on_demand') not in ('always','on_demand'):
        raise ValueError('无效的基础主动联系配置。')
    return {**defaults(),**value}
def context(config,now):return {}
def event_fields(config,event,local_due):return {}
def event_prompt(config,kind,base,fixed_id):return base
