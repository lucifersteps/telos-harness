"""业务通道：收件、确认与投递。由 Harness 插件启动，不启动模型运行时。"""
from __future__ import annotations
import asyncio
import hashlib
from datetime import datetime
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import threading

import aiohttp
from business import Business
from personas import system_prompt
from settings import read_config
from telos.agent.tools.types import ToolInputError

ROOT=Path(__file__).resolve().parent


class Channel:
    def __init__(self,root,apple,adapter,*,harness_home=None,token=None,timeout=180,event_sink=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.prompt=system_prompt(read_config(self.root))
        self.business=Business(self.root,apple,read_config(self.root));self.adapter=adapter
        self.delivery_locks={};self.loop=asyncio.get_running_loop();self.refs={};self.event_sink=event_sink
        self.ready=self.loop.create_future();self.timeout=timeout
        self.harness_home=Path(harness_home) if harness_home else self.root/'harness'
        self.token=token or secrets.token_hex(24);owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+owner.token:
                    self.send_error(403);return
                try:
                    data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    value=asyncio.run_coroutine_threadsafe(owner.route(self.path,data),owner.loop).result(timeout=timeout)
                    code=200;body=json.dumps(value,ensure_ascii=False).encode()
                except Exception as error:
                    status='input_error' if isinstance(error,(ToolInputError,ValueError)) else 'tool_failed'
                    code=400;body=json.dumps({'status':status,'reason':str(error),**({'execution_started':False} if status=='input_error' else {})},ensure_ascii=False).encode()
                try:
                    self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    async def route(self,path,data):
        if path=='/ready':
            if not self.ready.done():self.ready.set_result(data['url'])
            return {}
        if path=='/boot':return {'tools':self.business.schemas(data.get('family')),'prompt':self.prompt}
        if path=='/memory-profile':
            ref=self.refs[data['session']]
            if not ref.user_id:raise ValueError('User identity is required for personal memory')
            identity=json.dumps([ref.platform,ref.tenant_id,ref.user_id],ensure_ascii=False,separators=(',',':'))
            key=hashlib.sha256(identity.encode()).hexdigest()
            return {'directory':str((self.root/'users'/key).resolve())}
        if path=='/prepare':return await self.prepare(data['session'],data['messages'])
        if path=='/context':return await self.business.context(data['session'])
        if path=='/tool':return await self.business.execute(data['session'],data['id'],data['name'],data['args'],
            **({'map_token':data.get('map_token','')} if data['name']=='baidu_maps' else {}))
        raise ValueError('Unknown business endpoint')

    async def prepare(self,session,messages):
        # Runs only at Harness's native pre-step boundary, after any in-flight tool finishes.
        for message in messages:
            with self.business.db() as db:row=db.execute('SELECT input FROM messages WHERE id=? AND session=?',(message['id'],session)).fetchone()
            if row is None:continue  # Native Skill catalogs and other internal contexts are not user approvals.
            key=message['id'];text=row[0]
            self.business.active[session]={'key':key,'text':text,'time':datetime.now().astimezone().isoformat()}
            pending=self.business.pending(session)
            control=text.strip() if not message['hasMedia'] else ''
            if control in {'取消','拒绝','2','/stop'} and (not pending or pending['kind']=='approval' or control!='2'):
                if pending and pending.get('id'):self.business.settle_drafts(session,pending['id'],{'status':'cancelled'})
                self.business.set_pending(session,None)
                receipt={'status':'declined','text':'已取消当前操作。'}
            elif control in {'1','确认','同意'} and (not pending or pending['kind']=='approval'):
                receipt=await self.business.confirm(session)
            else:
                if pending and not pending.get('executing'):self.business.set_pending(session,None)
                continue
            self.business.active[session]['receipt']=receipt
            self.business.save_reply(key,receipt['text'])
        return {}

    async def handle(self,session,key,text,ref,*,blocks=None,accepted=False):
        self.refs[session]=ref
        if not accepted and not self.business.accept(session,key,text):return
        events=[];keys=[key]
        try:
            await asyncio.wait_for(asyncio.shield(self.ready),30)
            resume=any((self.harness_home/'sessions').glob(f'*/{session}/session.v*.jsonl*'))
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None,sock_connect=15)) as client:
                async with client.post(self.ready.result()+'/steer',headers={'Authorization':'Bearer '+self.token},
                    json={'session':session,'key':key,'resume':resume,'content':[{'type':'text','text':text},*(blocks or [])]}) as response:
                    response.raise_for_status();result=await response.json()
            events=result['events'];keys=result['keys'];reply=result['text']
            if self.event_sink:self.event_sink(events)
            finish=result.get('finish') or {}
            if finish.get('reason',{}).get('kind')!='completed':
                # Keep actual model output/receipts. Native failures are diagnostics,
                # not an assistant utterance and not a reason to invent one.
                logging.error('Harness turn did not complete: %s',json.dumps(finish,ensure_ascii=False))
        except Exception:
            logging.exception('Harness native turn failed')
            reply=''
        # Only delivery is serialized. Input reaches native steer while a turn is running.
        async with self.delivery_locks.setdefault(session,asyncio.Lock()):
            with self.business.db() as db:
                rows=[db.execute('SELECT id,delivered,reply,input FROM messages WHERE id=? AND session=?',(k,session)).fetchone() for k in dict.fromkeys(keys)]
                rows=[r for r in rows if r]
                if not rows or rows[-1][1]:return
                target=rows[-1][0]
                receipts=[r[2] for r in rows if r[2] and not r[1]]
                # A control-only turn with no further tool work reports its actual
                # receipt, not an additional model promise to perform the same save.
                control_only=receipts and all(r[3].strip() in {'1','确认','同意','2','取消','拒绝','/stop'} for r in rows)
                if control_only and not any(e.get('type')=='tool/result' for e in events):
                    reply='\n\n'.join(dict.fromkeys(receipts))
                else:reply=self.business.render(session,reply)
                for receipt in receipts:
                    if receipt not in reply:reply=receipt+'\n\n'+reply
                # Save the combined outbox and retire its input receipts atomically.
                for earlier,_,_,_ in rows[:-1]:db.execute("UPDATE messages SET reply='',delivered=1 WHERE id=?",(earlier,))
                db.execute('UPDATE messages SET reply=? WHERE id=?',(reply,target))
            await self.business.deliver(session,target,self.adapter,self.refs[session])

    async def recover(self,session,ref):
        self.refs[session]=ref
        pending=self.business.pending(session)
        if pending and pending.get('executing'):
            receipt=await self.business.reconcile(session,pending)
            key=pending.get('confirmation_key')
            if key:
                with self.business.db() as db:db.execute('UPDATE messages SET reply=?,delivered=0 WHERE id=? AND session=?',(receipt['text'],key,session))
        with self.business.db() as db:rows=db.execute('SELECT id,reply FROM messages WHERE session=? AND delivered=0 ORDER BY rowid',(session,)).fetchall()
        for key,reply in rows:
            if reply is None:self.business.save_reply(key,'')
            await self.business.deliver(session,key,self.adapter,ref)
        # Harness restores its own history/status. Python never rebuilds model messages.

    async def close(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
