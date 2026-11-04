"""Proactive policy and durable intents. DSH owns timers and model execution."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

DEFAULT_PROMPT = ('结合当前时间、近期对话、用户偏好和最近主动消息，判断是否值得联系用户。'
    '上午关注重要事务，午后关注推进，晚间偏向生活与收尾。按需查询日历、待办、睡眠；'
    '数据缺失不要猜测，不把未回复当作未完成。避免重复催促，每次简短，不必提问。'
    '需要联系时调用 notify_user；没有合适内容保持安静。值得稍后关注时可登记下一次检查。')
PRESETS = [
    ('midnight', '00:00', '回顾刚过去一天的完成情况，提示今天早晨的重要安排，简短收尾，不新增任务、不要求回复。'),
    ('morning', '08:00', '查询最近一晚主睡眠、今日日历和待办；回顾睡眠，列出今日重点，给一条具体建议。数据未同步需明确说明。'),
    ('noon', '12:00', '回顾上午已确认的进展，梳理下午日程与待办，给一条优先级建议。'),
    ('evening', '18:00', '总结白天已确认的完成情况，提示紧急未完事项和晚间安排，帮助切换到个人生活。'),
]


def defaults():
    return {'enabled': False, 'session': '', 'timezone': 'Asia/Shanghai',
        'heartbeat': {'enabled': True, 'interval_minutes': 30, 'start': '08:00', 'end': '01:00', 'prompt': DEFAULT_PROMPT},
        'fixed': [{'id': i, 'enabled': False, 'time': t, 'prompt': p, 'days': list(range(7))} for i, t, p in PRESETS]}


def validate(value=None):
    base = defaults()
    if value is None:
        return base
    if not isinstance(value, dict) or set(value) - set(base):
        raise ValueError('无效的主动消息配置。')
    base.update(value)
    if type(base['enabled']) is not bool or not isinstance(base['session'], str) or len(base['session']) > 256:
        raise ValueError('请选择主动消息接收会话并设置有效开关。')
    if base['enabled'] and not base['session']:
        raise ValueError('开启主动消息前请选择接收会话；先在微信与 Telos 聊天。')
    if not isinstance(base['timezone'], str):
        raise ValueError('时区必须是 IANA 时区名称。')
    try:
        ZoneInfo(base['timezone'])
    except (ValueError, KeyError):
        raise ValueError('无效的 IANA 时区。') from None
    hb = {**defaults()['heartbeat'], **base['heartbeat']} if isinstance(base['heartbeat'], dict) else {}
    if set(hb) != set(defaults()['heartbeat']) or type(hb['enabled']) is not bool:
        raise ValueError('无效的 heartbeat 配置。')
    if type(hb['interval_minutes']) is not int or not 1 <= hb['interval_minutes'] <= 1440:
        raise ValueError('检查间隔应为 1–1440 分钟的整数。')
    for key in ('start', 'end'):
        clock_minutes(hb[key])
    prompt(hb['prompt'])
    base['heartbeat'] = hb
    if not isinstance(base['fixed'], list) or len(base['fixed']) > 32:
        raise ValueError('固定提醒最多 32 条。')
    ids = set()
    for item in base['fixed']:
        if not isinstance(item, dict) or set(item) != {'id', 'enabled', 'time', 'prompt', 'days'}:
            raise ValueError('固定提醒需要 id、开关、时间、prompt 和重复日期。')
        if not isinstance(item['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item['id']) or item['id'] in ids:
            raise ValueError('固定提醒 ID 无效或重复。')
        ids.add(item['id'])
        if type(item['enabled']) is not bool:
            raise ValueError('固定提醒开关必须为布尔值。')
        clock_minutes(item['time']); prompt(item['prompt'])
        days = item['days']
        if not isinstance(days, list) or not days or any(type(d) is not int or not 0 <= d <= 6 for d in days) or len(set(days)) != len(days):
            raise ValueError('重复日期应为 0（周一）至 6（周日）。')
    return base


def prompt(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError('Prompt／检查原因应为 1–4000 字。')
    return value


def clock_minutes(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ValueError('时间格式应为 HH:MM。')
    h, m = map(int, value.split(':'))
    return h * 60 + m


def absolute_time(value):
    if not isinstance(value, str):
        raise ValueError('请提供带时区的绝对时间。')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('检查时间必须包含时区。')
    return dt.timestamp()


class ProactiveStore:
    def __init__(self, root, now=time.time):
        self.root = Path(root); self.path = self.root / 'business.sqlite'; self.now = now
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS proactive_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), config TEXT, revision INTEGER, paused_until REAL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS proactive_events (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, revision INTEGER NOT NULL,
                kind TEXT NOT NULL, due REAL NOT NULL, expires REAL NOT NULL, prompt TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'planned', task_id TEXT, input_id TEXT, message TEXT,
                detail TEXT DEFAULT '', created REAL NOT NULL);
              CREATE UNIQUE INDEX IF NOT EXISTS proactive_input ON proactive_events(input_id) WHERE input_id IS NOT NULL;
              CREATE TABLE IF NOT EXISTS proactive_calls (session TEXT, call_id TEXT, result TEXT, PRIMARY KEY(session,call_id));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30); db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def sync(self):
        from settings import read_config
        config = validate(read_config(self.root).get('proactive'))
        encoded = json.dumps(config, sort_keys=True, ensure_ascii=False)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM proactive_state WHERE singleton=1').fetchone()
            revision = row['revision'] if row else 0
            if not row or row['config'] != encoded:
                revision += 1
                db.execute("UPDATE proactive_events SET state='cancelled',detail='配置已修改' WHERE state IN ('planned','running','queued')")
                db.execute('INSERT INTO proactive_state VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET config=excluded.config,revision=excluded.revision',
                           (encoded, revision, 0))
            paused = db.execute('SELECT paused_until FROM proactive_state WHERE singleton=1').fetchone()[0]
        return config, revision, paused

    def event(self, event_id, session):
        with self.db() as db:
            row = db.execute('SELECT * FROM proactive_events WHERE id=? AND session=?', (event_id, session)).fetchone()
        if not row:
            raise ValueError('检查任务不存在或不属于当前会话。')
        return dict(row)

    def mark(self, event_id, state, detail=''):
        with self.db() as db:
            db.execute('UPDATE proactive_events SET state=?,detail=? WHERE id=?', (state, detail, event_id))

    def eligible(self, event, *, execution=False):
        config, revision, paused = self.sync(); now = self.now()
        if not config['enabled'] or event['revision'] != revision or event['session'] != config['session']:
            return False
        if event['state'] not in (('planned', 'running', 'queued', 'sent', 'unknown') if execution else ('planned', 'running', 'queued')):
            return False
        if paused > now:
            self.mark(event['id'], 'cancelled', '主动消息已暂停'); return False
        if now > event['expires']:
            self.mark(event['id'], 'expired', '已超过发送有效期'); return False
        return True

    def _insert(self, db, session, revision, kind, due, text, suffix='', state='planned'):
        key = hashlib.sha256(f'{session}\0{revision}\0{kind}\0{due}\0{suffix}'.encode()).hexdigest()
        ttl = 1800
        db.execute('INSERT OR IGNORE INTO proactive_events(id,session,revision,kind,due,expires,prompt,state,created) VALUES(?,?,?,?,?,?,?,?,?)',
                   (key, session, revision, kind, due, due + ttl, text, state, self.now()))
        return key

    def plan(self):
        config, revision, paused = self.sync(); now = self.now()
        with self.db() as db:
            db.execute("UPDATE proactive_events SET state='expired',detail='已超过发送有效期' WHERE expires<? AND state IN ('planned','running','queued')", (now,))
        if config['enabled'] and paused <= now:
            zone = ZoneInfo(config['timezone']); hb = config['heartbeat']
            # Actual UTC minutes preserve both occurrences of an autumn fold and omit nonexistent spring times.
            begin = int(now // 60 + 1) * 60; end = now + 26 * 3600
            with self.db() as db:
                for stamp in range(begin, int(end), 60):
                    dt = datetime.fromtimestamp(stamp, zone); minute = dt.hour * 60 + dt.minute
                    matches = [r for r in config['fixed'] if r['enabled'] and dt.weekday() in r['days'] and r['time'] == dt.strftime('%H:%M')]
                    for r in matches:
                        self._insert(db, config['session'], revision, 'fixed', float(stamp), r['prompt'], r['id'])
                    start, stop = clock_minutes(hb['start']), clock_minutes(hb['end'])
                    active = start == stop or (start <= minute < stop if start < stop else minute >= start or minute < stop)
                    if hb['enabled'] and active and (minute - start) % 1440 % hb['interval_minutes'] == 0:
                        self._insert(db, config['session'], revision, 'heartbeat', float(stamp), hb['prompt'], state='covered' if matches else 'planned')
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM proactive_events WHERE state='planned' AND expires>=? ORDER BY due,kind", (now,))]

    def bind_task(self, event_id, session, task_id):
        self.event(event_id, session)
        with self.db() as db:
            db.execute('UPDATE proactive_events SET task_id=? WHERE id=? AND (task_id IS NULL OR task_id=?)', (task_id, event_id, task_id))

    def admit(self, event_id, session, task_id, input_id):
        event = self.event(event_id, session)
        if event['task_id'] != task_id or self.now() < event['due'] or not self.eligible(event, execution=True):
            return {'allowed': False}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT input_id,state FROM proactive_events WHERE id=?', (event_id,)).fetchone()
            if row['input_id'] not in (None, input_id) or row['state'] not in ('planned','running','queued','sent','unknown'):
                return {'allowed': False}
            db.execute('UPDATE proactive_events SET input_id=?,state=CASE WHEN state=\'planned\' THEN \'running\' ELSE state END WHERE id=?', (input_id, event_id))
        return {'allowed': True, 'event': self.event(event_id, session)}

    def context(self, session, input_ids):
        config, _, paused = self.sync()
        with self.db() as db:
            placeholders = ','.join('?' for _ in input_ids)
            events = [dict(r) for r in db.execute(f'SELECT * FROM proactive_events WHERE session=? AND input_id IN ({placeholders})', (session, *input_ids))] if input_ids else []
            recent = [dict(r) for r in db.execute("SELECT kind,due,state,message,detail FROM proactive_events WHERE session=? AND state NOT IN ('planned','covered') AND due<=? ORDER BY due DESC LIMIT 12", (session,self.now()))]
        return {'events': events, 'recent': recent, 'settings': {'enabled': config['enabled'] and config['session'] == session,
            'timezone': config['timezone'], 'preferences': config['heartbeat']['prompt'], 'paused_until': paused}, 'now': datetime.fromtimestamp(self.now(), timezone.utc).isoformat()}

    def followup(self, session, call_id, at, reason):
        config, revision, paused = self.sync()
        if not config['enabled'] or config['session'] != session or paused > self.now():
            raise ValueError('当前会话的主动消息未开启或已暂停。')
        with self.db() as db:
            previous = db.execute('SELECT result FROM proactive_calls WHERE session=? AND call_id=?', (session, call_id)).fetchone()
            if previous:
                return json.loads(previous[0])
            due = absolute_time(at); prompt(reason)
            if due <= self.now():
                raise ValueError('下次检查时间必须在未来。')
            key = self._insert(db, session, revision, 'followup', due, reason, call_id)
            result = {'status': 'registered', 'event_id': key, 'at': at, 'reason': reason}
            db.execute('INSERT INTO proactive_calls VALUES(?,?,?)', (session, call_id, json.dumps(result)))
        return result

    def control(self, session, action, event_id=None, until=None):
        config, revision, paused = self.sync()
        if session != config['session']:
            raise ValueError('只能管理当前绑定会话的主动消息。')
        if action == 'cancel':
            event = self.event(event_id, session)
            if event['state'] in ('planned','running','queued'):
                self.mark(event_id, 'cancelled', '用户取消')
        elif action in ('pause','resume'):
            if action == 'resume' and not paused:
                return self.status()
            value = (absolute_time(until) if until else 253402214400.0) if action == 'pause' else 0
            if action == 'pause' and value <= self.now():
                raise ValueError('暂停截止时间必须在未来。')
            with self.db() as db:
                db.execute('UPDATE proactive_state SET paused_until=? WHERE singleton=1', (value,))
                if action == 'pause':
                    db.execute("UPDATE proactive_events SET state='cancelled',detail='用户暂停' WHERE session=? AND state IN ('planned','running','queued') AND due<=?", (session, value))
                else:
                    # New schedule generation, without resurrecting cancelled one-off followups.
                    db.execute('UPDATE proactive_state SET revision=revision+1 WHERE singleton=1')
                    db.execute("UPDATE proactive_events SET revision=? WHERE kind='followup' AND state='planned'", (revision+1,))
                    db.execute("UPDATE proactive_events SET state='cancelled',detail='计划重新启用' WHERE kind!='followup' AND state IN ('planned','running','queued')")
        elif action != 'list':
            raise ValueError('不支持的主动消息操作。')
        return self.status()

    def status(self):
        config, _, paused = self.sync()
        with self.db() as db:
            refs = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='channel_refs'").fetchone():
                for r in db.execute('SELECT session,value FROM channel_refs'):
                    ref = json.loads(r['value'])
                    refs.append({'session': r['session'], 'label': ref.get('user_id', r['session'])})
            pending = [dict(r) for r in db.execute("SELECT id,session,kind,due,prompt,state FROM proactive_events WHERE state IN ('planned','running','queued') AND (kind='followup' OR due<?) ORDER BY due LIMIT 100", (self.now()+3600,))]
            recent = [dict(r) for r in db.execute("SELECT id,session,kind,due,state,detail,message FROM proactive_events WHERE state NOT IN ('planned','covered') AND due<=? ORDER BY due DESC LIMIT 30", (self.now(),))]
        return {'config': config, 'paused_until': paused, 'sessions': refs, 'pending': pending, 'recent': recent}
