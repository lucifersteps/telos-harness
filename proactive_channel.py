"""Authenticated proactive endpoints sharing Telos's existing durable outbox."""
import asyncio
import json
from datetime import datetime

from proactive import ProactiveStore, prompt
from wechat_budget import view as budget_view, NOTICE_PREFIX, NOTICE_PROMPT


def observation_bubble(window, body):
    """Render once at admission; the existing outbox owns all subsequent retries."""
    start, end = (datetime.fromisoformat(window[k]) for k in ('start', 'end'))
    fmt = '%H:%M' if start.date() == end.date() else '%m月%d日 %H:%M'
    if start.year != end.year:
        fmt = '%Y年%m月%d日 %H:%M'
    qualifiers = []
    if window.get('basis') == 'legacy_schedule_time':
        qualifiers.append('起点为旧记录近似时间')
    if window.get('truncated'):
        qualifiers.append('仅覆盖最近31天')
    suffix = '（'+'；'.join(qualifiers)+'）' if qualifiers else ''
    return f'{start.strftime(fmt)}—{end.strftime(fmt)}{suffix}\n{body.strip()}'


class ProactiveChannel:
    def budget_event(self, event):
        """Decorate this existing event; no separate scheduler or delivery state."""
        with self.business.db() as db:
            db.execute('BEGIN IMMEDIATE')
            budget=budget_view(db,event['session'],event['id'])
            if not budget or event['state'] in ('sent','unknown'):
                return event
            marker=event.get('detail','')
            if (marker.startswith(NOTICE_PREFIX) and marker!=budget['marker']) or budget['blocked']:
                db.execute("UPDATE proactive_events SET state='skipped',detail=? WHERE id=? AND state IN ('planned','running')",
                           ('微信发送预算保留，等待用户新消息',event['id']))
                return None
            if budget['remaining']<=2 or marker==budget['marker']:
                db.execute('UPDATE proactive_events SET detail=? WHERE id=?',(budget['marker'],event['id']))
                event={**event,'detail':budget['marker'],'budget_notice':True,'prompt':NOTICE_PROMPT,'notification_required':True}
                for key in ('observation_window','health_mentions','habit_reminder_allowed'):
                    event.pop(key,None)
            return event

    def init_proactive(self):
        self.proactive = ProactiveStore(self.root)
        self.proactive.sync()

    async def proactive_route(self, path, data):
        if path == '/proactive/plan':
            events = self.proactive.plan()
            allowed = []
            for event in events:
                try:
                    self.known_ref(event['session']); allowed.append(event)
                except ValueError:
                    self.proactive.mark(event['id'], 'cancelled', '会话账号已失效')
            with self.proactive.db() as db:
                obsolete=[dict(r) for r in db.execute("SELECT session,task_id FROM proactive_events WHERE task_id IS NOT NULL AND state NOT IN ('planned','running','queued')")]
            return {'events': allowed, 'obsolete': obsolete}
        session = data['session']; self.known_ref(session)
        if path == '/proactive/lookup':
            with self.proactive.db() as db:
                row=db.execute('SELECT * FROM proactive_events WHERE session=? AND task_id=?',(session,data['task_id'])).fetchone()
            return {'event':dict(row) if row else None}
        if path == '/proactive/cancel-missing':
            self.proactive.event(data['event_id'],session)
            self.proactive.mark(data['event_id'],'cancelled','原生调度任务已删除');return {}
        if path == '/proactive/bind':
            self.proactive.bind_task(data['event_id'], session, data['task_id']); return {}
        if path == '/proactive/admit':
            result=self.proactive.admit(data['event_id'], session, data['task_id'], data['input_id'])
            if result['allowed']:
                event=self.budget_event(result['event'])
                return {'allowed':bool(event),**({'event':event} if event else {})}
            return result
        if path == '/proactive/context':
            context = self.proactive.context(session, data['inputs'])
            if data.get('human'):
                for event in context['events']:
                    if event['state'] in ('planned', 'running', 'queued'):
                        self.proactive.mark(event['id'], 'skipped', '用户消息优先，本次检查不单独发送')
                context['events'] = []
            else:
                context['events'] = [e for e in context['events'] if self.proactive.eligible(e, execution=True)]
                context['events'] = [e for event in context['events'] if (e:=self.budget_event(event))]
                if any(e.get('budget_notice') for e in context['events']):
                    context['policy']={'id':None,'enabled':False,'config':{}}
                    with self.business.db() as db:
                        context['recent_budget_notices']=[r[0] for r in db.execute(
                            "SELECT message FROM proactive_events WHERE session=? AND detail LIKE ? AND state='sent' ORDER BY sent_at DESC LIMIT 3",
                            (session,NOTICE_PREFIX+'%'))]
            return context
        if path == '/proactive/followup':
            if data.get('event_id'):
                parent=self.proactive.event(data['event_id'],session)
                if parent['input_id'] != data.get('input_id') or not self.proactive.eligible(parent, execution=True):
                    raise ValueError('当前主动检查已取消、暂停或过期。')
            return self.proactive.followup(session, data['call_id'], data['at'], data['reason'])
        if path == '/proactive/control':
            return self.proactive.control(session, data['action'], data.get('event_id'), data.get('until'))
        if path == '/proactive/notify':
            return await self.notify_proactive(session, data['event_id'], data['input_id'], data['message'], data.get('observation'))
        raise ValueError('Unknown proactive endpoint')

    async def notify_proactive(self, session, event_id, input_id, message, observation=None):
        prompt(message)
        if observation is not None:
            prompt(observation)
            if len(observation)>1000 or len(message)>1800:raise ValueError('观察最多1000字，提醒最多1800字；请保持两个气泡简短。')
        config,_,_=self.proactive.sync()
        policy=config['policy']
        async with self.delivery_locks.setdefault(session, asyncio.Lock()):
            event = self.proactive.event(event_id, session)
            if event['input_id'] != input_id or not self.proactive.eligible(event):
                raise ValueError('检查已取消、暂停、过期或不属于本轮。')
            with self.business.db() as db:
                previous = db.execute('SELECT key FROM schedule_outbox WHERE proactive_id=?', (event_id,)).fetchone()
                if previous:
                    return {'status': 'already_queued', 'event_id': event_id, 'message': event['message']}
            event=self.budget_event(event)
            if event is None:
                raise ValueError('微信主动消息预算已保留，等待用户新消息后恢复。')
            notice=event.get('budget_notice',False)
            if notice:
                if observation is not None or len(message)>180:
                    raise ValueError('本轮只能发送180字以内的一条额度提醒，不传 observation。请按当前收尾提醒 prompt 重新生成。')
                with self.business.db() as db:
                    recent=[r[0] for r in db.execute("SELECT message FROM proactive_events WHERE session=? AND detail LIKE ? AND state='sent' ORDER BY sent_at DESC LIMIT 3",(session,NOTICE_PREFIX+'%'))]
                if any(message.strip()==text.strip() for text in recent if text):
                    raise ValueError('这段收尾提醒近期已经发过了，请结合当前对话换一种自然表达。')
            elif policy['enabled'] and policy['id']=='goal_execution' and policy['configs']['goal_execution']['observation_enabled'] and observation is None:
                raise ValueError('本策略要求单独的行为观察 observation。')
            with self.business.db() as db:
                if observation is not None:
                    context = self.proactive.context(session, [input_id])
                    window = next(e['observation_window'] for e in context['events'] if e['id'] == event_id)
                    observation = observation_bubble(window, observation)
                # Native session sequence numbers are nonnegative. Negative rowids
                # reserve an outbox namespace for explicit proactive notifications.
                seq = -db.execute('SELECT rowid FROM proactive_events WHERE id=?', (event_id,)).fetchone()[0]
                key = 'proactive-' + event_id
                parts=[observation,message] if observation is not None else [message]
                db.execute('INSERT INTO schedule_outbox(session,seq,key,parts,proactive_id) VALUES(?,?,?,?,?)',
                           (session, seq, key, json.dumps(parts), event_id))
                db.execute("INSERT INTO messages(id,session,input,reply) VALUES(?,?,'',?)", (key, session, '\n\n'.join(parts)))
                db.execute("UPDATE proactive_events SET message=?,observation=?,state='queued' WHERE id=?", (message, observation, event_id))
        return {'status': 'queued', 'event_id': event_id, 'message': message, **({'observation':observation} if observation is not None else {})}

    def finish_proactive(self, data):
        context = self.proactive.context(data['session'], data.get('inputs', []))
        if not context['events']:
            return False
        success = (data.get('finish') or {}).get('reason', {}).get('kind') == 'completed'
        for event in context['events']:
            if event['state'] == 'running':
                if not success:
                    self.proactive.mark(event['id'], 'failed', 'Agent 执行未正常完成')
                elif event['kind'] == 'fixed':
                    self.proactive.mark(event['id'], 'failed', '固定提醒未调用 notify_user')
                else:
                    self.proactive.mark(event['id'], 'quiet', 'Agent 本次未提交通知')
        # Explicit notify_user is the only proactive delivery path, including replay.
        return True
