"""Authenticated proactive endpoints sharing Telos's existing durable outbox."""
import asyncio
import json

from proactive import ProactiveStore, prompt


class ProactiveChannel:
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
            return self.proactive.admit(data['event_id'], session, data['task_id'], data['input_id'])
        if path == '/proactive/context':
            context = self.proactive.context(session, data['inputs'])
            if data.get('human'):
                for event in context['events']:
                    if event['state'] in ('planned', 'running', 'queued'):
                        self.proactive.mark(event['id'], 'skipped', '用户消息优先，本次检查不单独发送')
                context['events'] = []
            else:
                context['events'] = [e for e in context['events'] if self.proactive.eligible(e, execution=True)]
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
            return await self.notify_proactive(session, data['event_id'], data['input_id'], data['message'])
        raise ValueError('Unknown proactive endpoint')

    async def notify_proactive(self, session, event_id, input_id, message):
        prompt(message)
        async with self.delivery_locks.setdefault(session, asyncio.Lock()):
            event = self.proactive.event(event_id, session)
            if event['input_id'] != input_id or not self.proactive.eligible(event):
                raise ValueError('检查已取消、暂停、过期或不属于本轮。')
            with self.business.db() as db:
                previous = db.execute('SELECT key FROM schedule_outbox WHERE proactive_id=?', (event_id,)).fetchone()
                if previous:
                    return {'status': 'already_queued', 'event_id': event_id, 'message': event['message']}
                # Native session sequence numbers are nonnegative. Negative rowids
                # reserve an outbox namespace for explicit proactive notifications.
                seq = -db.execute('SELECT rowid FROM proactive_events WHERE id=?', (event_id,)).fetchone()[0]
                key = 'proactive-' + event_id
                db.execute('INSERT INTO schedule_outbox(session,seq,key,parts,proactive_id) VALUES(?,?,?,?,?)',
                           (session, seq, key, json.dumps([message]), event_id))
                db.execute("INSERT INTO messages(id,session,input,reply) VALUES(?,?,'',?)", (key, session, message))
                db.execute("UPDATE proactive_events SET message=?,state='queued' WHERE id=?", (message, event_id))
        return {'status': 'queued', 'event_id': event_id, 'message': message}

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
