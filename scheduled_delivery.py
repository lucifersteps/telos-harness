"""Durable delivery of completed native schedule turns; never executes a model."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging

from telos.adapters.base import ConversationRef
from telos.adapters.delivery import split_reply_bubbles,split_text_by_length,delivery_log_context


class ScheduledDelivery:
    def init_scheduled_delivery(self):
        with self.business.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS channel_refs (session TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS schedule_outbox (
                    session TEXT, seq INTEGER, key TEXT UNIQUE, parts TEXT, next_part INTEGER DEFAULT 0, approval_id TEXT,
                    PRIMARY KEY(session,seq));
            ''')
            rows=db.execute('SELECT session,value FROM channel_refs').fetchall()
        self.refs.update({session:ConversationRef(**json.loads(value)) for session,value in rows})
        self.schedule_sender=None

    def bind_ref(self,session,ref):
        old=self.refs.get(session)
        if old and (old.platform,old.tenant_id,old.user_id,old.chat_id)!=(ref.platform,ref.tenant_id,ref.user_id,ref.chat_id):
            raise ValueError('Session recipient identity changed')
        with self.business.db() as db:
            db.execute('INSERT OR REPLACE INTO channel_refs VALUES(?,?)',(session,json.dumps(asdict(ref))))
        self.refs[session]=ref

    def known_ref(self,session):
        ref=self.refs.get(session)
        if not ref or ref.platform!='wechat' or not ref.user_id or not ref.chat_id:
            raise ValueError('Unknown WeChat conversation')
        return ref

    async def stage_scheduled(self,data):
        session=data['session'];self.known_ref(session)
        seq=data['seq'];text=data['text']
        if type(seq) is not int or seq<0 or not isinstance(text,str):raise ValueError('Invalid scheduled output')
        async with self.delivery_locks.setdefault(session,asyncio.Lock()):
            with self.business.db() as db:
                if db.execute('SELECT 1 FROM schedule_outbox WHERE session=? AND seq=?',(session,seq)).fetchone():return {}
            # Only a genuinely new output is rendered. A replay must not acquire
            # a newer draft or receipt from an unrelated user turn.
            pending=self.business.pending(session) or {}
            protected=(pending.get('text') or pending.get('question','')) if pending.get('origin') in data.get('inputs',[]) else ''
            if protected and text.count(protected)!=1:text=protected
            size=getattr(getattr(self.adapter,'config',None),'max_text_chars',2000)
            parts=[part for bubble in split_reply_bubbles(text,protected_text=protected) for part in split_text_by_length(bubble,size)]
            key='schedule-'+hashlib.sha256(f'{session}\0{seq}'.encode()).hexdigest()
            with self.business.db() as db:
                approval_id=pending.get('id') if protected and pending.get('kind')=='approval' else None
                db.execute('INSERT INTO schedule_outbox(session,seq,key,parts,approval_id) VALUES(?,?,?,?,?)',(session,seq,key,json.dumps(parts),approval_id))
                db.execute('INSERT INTO messages(id,session,input,reply) VALUES(?,?,?,?)',(key,session,'',text))
        return {}

    async def drain_scheduled(self):
        with self.business.db() as db:
            rows=db.execute('SELECT o.session,o.seq,o.key FROM schedule_outbox o JOIN messages m ON m.id=o.key WHERE m.delivered=0 ORDER BY m.rowid').fetchall()
        blocked=set()
        for session,seq,key in rows:
            if session in blocked:continue
            try:
                try:ref=self.known_ref(session)
                except ValueError:continue
                async with self.delivery_locks.setdefault(session,asyncio.Lock()):
                    with self.business.db() as db:
                        parts,index,approval_id=db.execute('SELECT parts,next_part,approval_id FROM schedule_outbox WHERE session=? AND seq=?',(session,seq)).fetchone()
                    parts=json.loads(parts)
                    while index<len(parts):
                        token=delivery_log_context.set({'delivery_id':key,'part':index,'input_event_ids':[]})
                        try:result=await self.adapter.send_text(ref,parts[index],protected_text=parts[index])
                        finally:delivery_log_context.reset(token)
                        if not result.ok:blocked.add(session);break
                        index+=1
                        with self.business.db() as db:db.execute('UPDATE schedule_outbox SET next_part=? WHERE session=? AND seq=?',(index,session,seq))
                    if index==len(parts):
                        with self.business.db() as db:db.execute('UPDATE messages SET delivered=1 WHERE id=?',(key,))
                        pending=self.business.pending(session)
                        if pending and pending['kind']=='approval' and pending.get('id')==approval_id:
                            import time
                            pending['delivered']=True;pending['expires_at']=time.time()+600
                            self.business.set_pending(session,pending)
            except Exception:
                blocked.add(session)
                logging.exception('定时回复发送失败，保留待发送记录')

    def start_schedule_sender(self):
        if self.schedule_sender:return
        async def send():
            while True:
                await self.drain_scheduled()
                await asyncio.sleep(2)
        self.schedule_sender=asyncio.create_task(send())

    async def stop_schedule_sender(self):
        if self.schedule_sender:
            self.schedule_sender.cancel()
            await asyncio.gather(self.schedule_sender,return_exceptions=True)
