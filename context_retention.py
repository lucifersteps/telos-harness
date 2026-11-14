"""Read-only delivery gate for model-context retention. Never edits the ledger."""
import json


def retention_status(channel, data):
    session = data['session']
    channel.known_ref(session)
    turns = data.get('turns', [])
    if not isinstance(turns, list) or len(turns) > 2000:
        raise ValueError('Invalid retention batch')
    pending = channel.business.pending(session) or {}
    protected = {pending.get('origin'), pending.get('confirmation_key')} - {None}
    eligible, recent = [], []
    with channel.business.db() as db:
        # A failed write may have reached Apple. Retain its context until the
        # durable write ledger is reconciled; never infer success from delivery.
        for origin, raw in db.execute('SELECT origin,result FROM drafts WHERE session=?', (session,)):
            result = json.loads(raw)
            if result.get('status') in {'executing', 'unknown', 'failed', 'partial'} or result.get('is_error'):
                protected.add(origin)
        for turn in turns:
            ids, users = turn.get('inputs', []), turn.get('users', [])
            if (type(turn.get('turn')) is not int or type(turn.get('seq')) is not int
                    or not isinstance(ids, list) or not isinstance(users, list)
                    or len(ids) > 100 or len(users) > 100 or any(not isinstance(x, str) for x in ids + users)):
                raise ValueError('Invalid retained turn')
            if not ids or protected.intersection(ids):
                continue
            if users:
                # Desktop-only messages without a channel ledger entry stay intact.
                rows = [db.execute('SELECT delivered,reply FROM messages WHERE session=? AND id=?',
                                   (session, key)).fetchone() for key in ids]
                settled = all(r and r[0] and r[1] is not None for r in rows)
                text = ''
            else:
                events = [db.execute('SELECT state,message,observation FROM proactive_events WHERE session=? AND input_id=?',
                                     (session, key)).fetchone() for key in ids]
                if all(events):
                    settled = all(r[0] in {'sent', 'quiet', 'skipped', 'cancelled', 'expired'} for r in events)
                    text = '\n'.join(('已发送：' + '\n'.join(x for x in r[1:] if x)) if r[0] == 'sent'
                                     else '本次检查结束，未发送通知。' for r in events)
                else:
                    row = db.execute('SELECT m.delivered,m.reply FROM schedule_outbox o JOIN messages m ON m.id=o.key '
                                     'WHERE o.session=? AND o.seq=?', (session, turn['seq'])).fetchone()
                    settled = bool(row and row[0])
                    text = ('已发送：' + row[1]) if settled and row[1] else '本次任务无消息输出。'
            if settled:
                eligible.append(turn['turn'])
                if not users:
                    recent.append({'turn': turn['turn'], 'seq': turn['seq'], 'text': text[:1600],
                                   'truncated': len(text) > 1600})
    return {'eligible': eligible, 'recent': sorted(recent, key=lambda r: r['seq'])[-2:]}
