"""Conservative bubble budget derived from the existing delivery receipts.

Only the last genuine inbound ID and its receipt baseline are persisted in
facts. Proactive events/outboxes still own admission, retries and completion.
Ten is a locally observed allowance, not a quota reported by WeChat.
"""
import json

LIMIT = 10
NOTICE_PREFIX = 'wechat_budget_notice:'

NOTICE_PROMPT = '''本轮改为微信发送额度收尾提醒，替代原来的观察和建议两个气泡。
只调用一次 notify_user，只传 message，发一个气泡，最多180字。
无需查询行为、健康、设备或计划；不要追加任何缺失说明、观察统计或任务建议。
语气温柔、可爱、自然，可以轻轻撒娇，但不责备、不内疚绑架、不威胁永远不理用户。
自然说明微信主动消息额度快用完了，这条之后会暂时安静，用户在微信回一句就能继续。
不要声称额度已经用尽，也不要承诺永久停发。可用一个表情，不要像系统报错。
结合对话称呼和近期提醒，自行组织措辞；不要照抄示例或上一条提醒，不要每次固定同一比喻。
这是收尾提醒，发送后结束本轮，不调用 schedule_next_check，也不调用 proactive_control。'''


def totals(db, session, exclude=None):
    """Cumulative spent (including frozen unknown receipts), plus reservations.

    Unknown receipts cannot auto-replay. Treat their uncertain part as spent,
    so a fresh inbound baseline retires it instead of reserving it forever.
    An in-flight send still reserves its remainder across an inbound reset.
    """
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    confirmed = pending = 0
    text_keys = set()
    if 'text_outbox' in tables:
        for key, encoded, index, state, delivered in db.execute(
                'SELECT t.key,t.parts,t.next_part,t.state,m.delivered FROM text_outbox t '
                'JOIN messages m ON m.id=t.key WHERE m.session=?', (session,)):
            text_keys.add(key)
            confirmed += index
            if state in ('unknown','legacy_unknown'):
                confirmed += min(1, max(0,len(json.loads(encoded))-index)) if state=='unknown' else max(0,len(json.loads(encoded))-index)
            elif key != exclude and not delivered and state != 'complete':
                pending += max(0, len(json.loads(encoded))-index)
    if 'schedule_outbox' in tables:
        for key, encoded, index, delivered in db.execute(
                'SELECT o.key,o.parts,o.next_part,m.delivered FROM schedule_outbox o '
                'JOIN messages m ON m.id=o.key WHERE o.session=?', (session,)):
            if key in text_keys:
                continue  # Proactive receipts are owned by text_outbox.
            confirmed += index
            if key != exclude and not delivered:
                pending += max(0, len(json.loads(encoded))-index)
    if 'image_receipts' in tables:
        for state, encoded in db.execute('SELECT state,result FROM image_receipts'):
            result = json.loads(encoded) if encoded else {}
            if result.get('_session') != session:
                continue
            for part in ('image', 'caption'):
                receipt = result.get(part)
                if receipt == 'api_accepted':
                    confirmed += 1
                elif receipt == 'unknown':
                    confirmed += 1
            if state == 'sending_image':
                pending += result['_parts']
            elif state == 'image_api_accepted':
                confirmed += 1
                pending += result['_parts']-1
    return confirmed, pending


def reset(db, session, input_id):
    confirmed, _ = totals(db, session)
    db.execute('INSERT OR REPLACE INTO facts(session,value) VALUES(?,?)',
               ('wechat-budget:'+session, json.dumps({'input_id': input_id, 'baseline': confirmed})))
    # A new inbound supersedes an unsent farewell. Never replay it afterwards.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='proactive_events'").fetchone():
        db.execute("UPDATE proactive_events SET state='skipped',detail=detail||':superseded' "
                   "WHERE session=? AND detail LIKE ? AND state IN ('planned','running','queued')",
                   (session, NOTICE_PREFIX+'%'))


def view(db, session, event_id=None):
    row = db.execute('SELECT value FROM facts WHERE session=?', ('wechat-budget:'+session,)).fetchone()
    if not row:
        return None  # Legacy installs start tracking with the next fresh inbound.
    anchor = json.loads(row[0])
    confirmed, pending = totals(db, session, 'proactive-'+event_id if event_id else None)
    marker = NOTICE_PREFIX+anchor['input_id']
    notice = db.execute("SELECT id FROM proactive_events WHERE session=? AND detail=? "
                        "AND state IN ('running','queued','sent','unknown') LIMIT 1", (session, marker)).fetchone()
    remaining = max(0, LIMIT-max(0, confirmed-anchor['baseline'])-pending)
    return {'remaining': remaining, 'marker': marker,
            'notice_event_id': notice[0] if notice else None,
            'blocked': remaining == 0 or bool(notice and notice[0] != event_id)}
