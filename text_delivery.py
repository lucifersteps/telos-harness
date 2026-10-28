"""Persist each text bubble's receipt; ambiguous sends never replay automatically."""
import asyncio
import hashlib
import json
import logging

from telos.adapters.delivery import split_reply_bubbles, split_text_by_length, delivery_log_context


def init_text_delivery(business):
    business.text_delivery_locks = {}
    with business.db() as db:
        existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='text_outbox'").fetchone()
        db.execute('''CREATE TABLE IF NOT EXISTS text_outbox (
            key TEXT PRIMARY KEY, reply_hash TEXT NOT NULL, parts TEXT NOT NULL,
            next_part INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL)''')
        if existed:
            return
        # Old versions stored no per-bubble progress. Never guess which bubbles
        # of an old incomplete reply arrived. Preserve the reply for inspection.
        rows = db.execute('''SELECT id,reply FROM messages WHERE delivered=0
            AND reply IS NOT NULL AND trim(reply)!='' AND id NOT LIKE 'schedule-%'
            AND id NOT IN (SELECT key FROM text_outbox)''').fetchall()
        for key, reply in rows:
            db.execute('INSERT INTO text_outbox VALUES(?,?,?,?,?)',
                       (key, hashlib.sha256(reply.encode()).hexdigest(), '[]', 0, 'legacy_unknown'))


async def send_reply_parts(business, session, key, reply, adapter, ref, protected):
    async with business.text_delivery_locks.setdefault(key, asyncio.Lock()):
        digest = hashlib.sha256(reply.encode()).hexdigest()
        with business.db() as db:
            row = db.execute('SELECT reply_hash,parts,next_part,state FROM text_outbox WHERE key=?', (key,)).fetchone()
            if not row:
                size = getattr(getattr(adapter, 'config', None), 'max_text_chars', 1900)
                parts = [part for bubble in split_reply_bubbles(reply, protected_texts=protected)
                         for part in split_text_by_length(bubble, size)]
                row = (digest, json.dumps(parts), 0, 'ready')
                db.execute('INSERT INTO text_outbox VALUES(?,?,?,?,?)', (key, *row))
        old_digest, encoded, index, state = row
        if old_digest != digest or state in ('sending', 'unknown', 'legacy_unknown'):
            logging.warning('Text delivery paused: unconfirmed prior receipt or changed reply; no automatic replay')
            return False
        parts = json.loads(encoded)
        while index < len(parts):
            with business.db() as db:
                db.execute("UPDATE text_outbox SET state='sending' WHERE key=?", (key,))
            client_id = 'telos-text-' + hashlib.sha256(f'{session}\0{key}\0{index}\0{digest}'.encode()).hexdigest()[:32]
            token = delivery_log_context.set({'delivery_id': key, 'part': index,
                                             'input_event_ids': [key], 'client_id': client_id})
            try:
                result = await adapter.send_text(ref, parts[index], protected_text=parts[index])
            except asyncio.CancelledError:
                # The durable sending marker also covers cancellation/process death
                # after the server accepted a request but before our local receipt.
                raise
            except Exception:
                with business.db() as db:
                    db.execute("UPDATE text_outbox SET state='unknown' WHERE key=?", (key,))
                logging.exception('Text delivery outcome unknown; automatic replay paused')
                return False
            finally:
                delivery_log_context.reset(token)
            if not result.ok:
                with business.db() as db:
                    db.execute('UPDATE text_outbox SET state=? WHERE key=?',
                               ('rejected' if getattr(result, 'retryable', False) else 'unknown', key))
                return False
            index += 1
            with business.db() as db:
                db.execute('UPDATE text_outbox SET next_part=?,state=? WHERE key=?',
                           (index, 'complete' if index == len(parts) else 'ready', key))
        return True
