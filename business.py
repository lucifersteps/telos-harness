"""业务边界：草稿、确认、Apple 幂等与微信收发；不保存模型历史或运行步骤。"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

from telos.adapters.delivery import delivery_log_context
from telos.adapters.base import ApprovalRequest
from telos.adapters.wechat.adapter import _approval_markdown_message
from telos.adapters.question_channel import QuestionResponse
from telos.agent.tools.types import ToolInvocationContext, ToolResult, ToolInputError
from telos.tools.baidu_maps import BaiduMaps, PARAMETERS, ENDPOINTS
from telos.agent.tools.types import ExecutableTool
from telos.core.jsonschema import validate_json_schema
from telos.tools.apple.interaction import create_personal_plan_tool, create_personal_question_tool, personal_result_text
from telos.tools.apple.repair import payload
from telos.tools.apple.service import canonical, operation_key
from settings import runtime_options
from telos.tools.apple.direct import direct_allowed
from text_delivery import init_text_delivery, send_reply_parts

SAVED_UNDO_INSTRUCTION='回复【1】撤销；需要修改时直接回复修改内容'
SAVED_PENDING_INSTRUCTION='如需撤销这次保存，请先回复“取消”结束当前待处理内容，再回复“撤销”。'
PREVIEW_CONTROLS={'1','确认','同意','2','取消','拒绝','/stop'}


class Business:
    def __init__(self, root, apple, config=None):
        self.enabled=runtime_options(config or {})['tools']
        self.direct_write=runtime_options(config or {})['direct_write']
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'business.sqlite';self.apple=apple
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session TEXT, input TEXT, reply TEXT, delivered INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS pending (session TEXT PRIMARY KEY, value TEXT);
              CREATE TABLE IF NOT EXISTS drafts (session TEXT, id TEXT, origin TEXT, root TEXT, args TEXT, result TEXT, PRIMARY KEY(session,id));
              CREATE TABLE IF NOT EXISTS facts (session TEXT PRIMARY KEY, value TEXT);
            ''')
        init_text_delivery(self)
        self.maps=BaiduMaps()
        maps_tool=ExecutableTool(name='baidu_maps',description='百度地图只读查询。先用原生 skill 加载 baidu-ai-map；支持地点、路线、地理编码和天气。',
            parameters={'type':'object','properties':{'operation':{'type':'string','enum':list(ENDPOINTS)},
                'parameters':PARAMETERS},'required':['operation','parameters'],'additionalProperties':False},
            resolver=lambda args,invocation:self.maps.resolve(args))
        self.tools={t.name:t for t in [create_personal_plan_tool(data_dir=self.root,options={},
            service=apple,draft_lookup=self.lookup),create_personal_question_tool(self),maps_tool]}
        # A request hint concerns draft lineage only; todo ownership stays in Harness.
        for tool in [self.tools['personalPlan']]:
            tool.parameters['properties']['require_confirmation']={'type':'boolean','description':'用户要求先预览/确认时设为 true；不能取消强制确认。'}
            tool.parameters['properties']['request_mode']={'type':'string','enum':['new','continue']}
        self.active={};self.execution_locks={}

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=30)
        try:
            with db:yield db
        finally:db.close()

    def accept(self,session,key,text):
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO messages(id,session,input) VALUES(?,?,?)',
                              (key,session,text)).rowcount==1

    def pending(self,session):
        with self.db() as db:row=db.execute('SELECT value FROM pending WHERE session=?',(session,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_pending(self,session,value):
        with self.db() as db:
            if value is None:db.execute('DELETE FROM pending WHERE session=?',(session,))
            else:db.execute('INSERT OR REPLACE INTO pending VALUES(?,?)',(session,canonical(value)))

    def feedback_after(self,session,key,before=None):
        # Derive feedback from the existing inbox; numeric controls alone do not
        # change the proposed fields or invalidate an initial delivery retry.
        with self.db() as db:
            rows=db.execute('SELECT input,reply FROM messages WHERE session=? AND rowid>(SELECT rowid FROM messages WHERE id=?)'
                +(' AND rowid<(SELECT rowid FROM messages WHERE id=?)' if before else ''),
                (session,key,before) if before else (session,key)).fetchall()
        return any(row[1] is None or (row[0].strip() and row[0].strip() not in PREVIEW_CONTROLS) for row in rows)

    def lookup(self,session,call_id):
        with self.db() as db:row=db.execute('SELECT origin,args,result FROM drafts WHERE session=? AND id=?',(session,call_id)).fetchone()
        if row is None:return None
        result=json.loads(row[2]);return SimpleNamespace(tool_name='personalPlan',turn_id=row[0],
            args=json.loads(row[1]),result=result,is_error=result.get('status') in {'failed','cancelled'})

    def record_draft(self,session,cid,args,result):
        parent=args.get('draft_call_id');root=cid
        if parent:
            with self.db() as db:row=db.execute('SELECT root FROM drafts WHERE session=? AND id=?',(session,parent)).fetchone()
            if row:root=row[0]
        with self.db() as db:db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?,?,?,?)',
            (session,cid,self.active[session]['key'],root,canonical(args),canonical(result)))

    def settle_drafts(self,session,cid,result):
        with self.db() as db:db.execute('UPDATE drafts SET result=? WHERE session=? AND root=(SELECT root FROM drafts WHERE session=? AND id=?)',
            (canonical(result),session,session,cid))

    def schemas(self,family=None):
        selected={'apple':{'personalPlan','ask_user'},'maps':{'baidu_maps'}}.get(family,set())
        if not self.enabled['apple']:selected.discard('personalPlan')
        if not self.enabled['maps']:selected.discard('baidu_maps')
        return [{'name':t.name,'description':t.description,'parameters':t.parameters} for t in self.tools.values() if t.name in selected]

    async def request_question(self,request):
        self.set_pending(request.session_id,{'kind':'question','question':request.question,'origin':self.active[request.session_id]['key']})
        return QuestionResponse(decision='awaiting_input')

    async def context(self,session):
        # DSH appends changed runtime contexts. Keep this contribution small and
        # stable; current Apple objects are read through personalPlan query/detail.
        return {'direct_write':self.direct_write,'pending':self.pending(session),
                'receipt':self.active.get(session,{}).get('receipt'),
                'time':self.active.get(session,{}).get('time')}

    async def execute(self,session,cid,name,args,*,map_token=None):
        try:
            async with self.execution_locks.setdefault(session,asyncio.Lock()):
                return await self._execute(session,cid,name,args,map_token=map_token)
        except (ToolInputError,ValueError) as error:
            existing=self.lookup(session,cid)
            if name=='personalPlan' and isinstance(args,dict) and args.get('action') in {'create','update','delete','batch','revise'} and not (existing and existing.result.get('direct')):
                self.record_draft(session,cid,{k:v for k,v in args.items() if k not in {'request_mode','require_confirmation'}},
                                  {'status':'input_error','execution_started':False,'reason':str(error)})
            raise

    async def _execute(self,session,cid,name,args,*,map_token=None):
        """唯一模型工具入口：按已配置策略执行；模型不能批准受保护操作。"""
        if (name=='personalPlan' and not self.enabled['apple']) or (name=='baidu_maps' and not self.enabled['maps']):
            raise ToolInputError('该工具已关闭，请在 Telos 插件页面启用后再使用。')
        tool=self.tools.get(name)
        if tool is None:raise ToolInputError('工具不存在。')
        error=validate_json_schema(tool.parameters,args)
        if error:raise ToolInputError(error)
        args=dict(args);mode=args.pop('request_mode','new');require_confirmation=args.pop('require_confirmation',False)
        current=self.active[session]
        context=ToolInvocationContext(session_id=session,turn_id=current['key'],tool_call_id=cid,tool_name=name)
        pending=self.pending(session)
        write=name=='personalPlan' and args.get('action') not in {'query','detail'}
        revising_pending=bool(pending and pending['kind']=='approval' and not pending.get('executing')
            and pending.get('origin')!=current['key'] and name=='personalPlan'
            and args.get('action')=='revise' and args.get('draft_call_id')==pending.get('id') and mode=='continue')
        if write:
            existing=self.lookup(session,cid)
            if existing and existing.result.get('direct'):
                if existing.turn_id==current['key'] and canonical(args)==canonical(existing.args):return existing.result
                raise ToolInputError('已执行调用的编号不能复用，请使用新的工具调用编号。')
            if current.get('receipt',{}).get('control'):
                raise ToolInputError('本轮用户已执行控制指令，不再创建或修改安排。')
            with self.db() as db:
                prior=db.execute('SELECT args,result FROM drafts WHERE session=? AND origin=? ORDER BY rowid', (session,current['key'])).fetchall()
            for old_args,old_result in prior:
                value=json.loads(old_result)
                if value.get('direct') and canonical(args)==canonical(json.loads(old_args)):
                    return value
        if pending and (write or name=='ask_user') and not revising_pending:
            raise ToolInputError('已有待处理预览或追问，不能覆盖；可以继续独立查询，然后等待用户回应。')
        if name=='personalPlan' and args.get('action')=='revise':
            record=self.lookup(session,args.get('draft_call_id',''))
            if record and record.turn_id!=current['key'] and mode!='continue':
                raise ToolInputError('跨轮修改草稿须使用 request_mode=continue。失败不会锁定请求，请修正后继续。')
        if name=='baidu_maps':
            with self.db() as db:row=db.execute('SELECT value FROM facts WHERE session=?',(session,)).fetchone()
            records=[SimpleNamespace(**v) for v in json.loads(row[0])] if row else []
            with self.db() as db:
                inputs=db.execute('SELECT input FROM messages WHERE session=? AND rowid<=(SELECT rowid FROM messages WHERE id=?) ORDER BY rowid DESC LIMIT 30',
                                  (session,current['key'])).fetchall()
            execution=self.maps.resolve(args,state={'task_inputs':[{'text':r[0]} for r in inputs]},records=records,token=map_token)
        else:execution=await tool.resolve_execution(args,context=context)
        if execution.need_confirm:
            preview=execution.approval_preview_value()
            # Native validation/normalization is shared with confirmation mode.
            fingerprint=canonical({k:preview.get(k) for k in ('action','kind','id','fields','recurrence_scope')})
            for _,old_result in prior:
                value=json.loads(old_result)
                if value.get('direct') and value.get('fingerprint')==fingerprint:
                    return value
            can_direct=(self.direct_write and not require_confirmation and not revising_pending and not current.get('scheduled')
                and not any(json.loads(r).get('direct') for _,r in prior) and direct_allowed(preview))
            if can_direct:
                pending={'kind':'approval','id':cid,'origin':current['key'],'args':args,'preview':preview,
                    'direct':True,'executing':True,'confirmation_key':current['key'],'fingerprint':fingerprint}
                self.record_draft(session,cid,args,{'status':'executing','direct':True,'fingerprint':fingerprint})
                self.set_pending(session,pending)
                try:
                    result=await execution.run()
                    result=self.direct_receipt(session,pending,result)
                except Exception as error:
                    result={'direct':True,'status':'failed','fingerprint':fingerprint,'reason':str(error),
                        'text':'本次写入未能核实，请先查询 Apple；不会自动重试。'}
                self.settle_drafts(session,cid,result)
                self.persist_receipt(session,result)
                self.set_pending(session,None)
                return result
            detail={'args':args,'preview':preview}
            request=ApprovalRequest(request_id=cid,turn_id=current['key'],
                tool_call_id=cid,tool_name=name,action=execution.description,detail=detail)
            text=_approval_markdown_message(request)
            self.set_pending(session,{'kind':'approval','id':cid,'origin':current['key'],'args':args,
                'preview':preview,'text':text,'delivered':False,'expires_at':time.time()+600})
            result={'status':'awaiting_approval','text':text}
        else:
            result=await execution.run()
            if isinstance(result,ToolResult):result={**(payload(result.output) or {}),'is_error':result.is_error}
            if revising_pending and result.get('status') in {'incomplete_draft','needs_clarification','location_candidates','baseline'}:
                # A successfully revised partial draft can ask for its missing
                # fields through the existing question flow. It cannot authorize
                # the previous, now superseded preview.
                self.set_pending(session,None)
        if write:self.record_draft(session,cid,args,result)
        if name=='baidu_maps' and isinstance(result,dict) and result.get('status') in {'skill_result','needs_clarification'}:
            # Only location provenance is business state; no model or generic tool history is copied.
            records.append(SimpleNamespace(tool_name=name,args=args,result=result,is_error=False))
            with self.db() as db:db.execute('INSERT OR REPLACE INTO facts VALUES(?,?)',
                (session,canonical([vars(v) for v in records[-30:]])))
        return result

    def persist_receipt(self,session,result):
        current=self.active[session]
        previous=current.get('receipt',{}).get('text','')
        text=result.get('text','')
        if previous and text not in previous:text=previous+'\n\n'+text
        current['receipt']={**result,'text':text}
        self.save_reply(current['key'],text)

    def direct_receipt(self,session,pending,result):
        result={k:v for k,v in result.items() if k!='_undo'}
        op=operation_key(session,pending['origin'],pending['id'])
        result.update(direct=True,fingerprint=pending['fingerprint'])
        if result.get('saved') is True:
            result['undo_id']=op
            request=ApprovalRequest(request_id=pending['id'],turn_id=pending['origin'],
                tool_call_id=pending['id'],tool_name='personalPlan',action='已保存',
                detail={'preview':pending['preview']})
            result['text']=_approval_markdown_message(request,saved=True)
        else:result['text']=personal_result_text(pending['args'],result) or '尚未核实保存结果，请先查询，勿重复写入。'
        return result

    def saved_cards(self,session,text):
        # Reuse durable drafts/outbox: no extra card state or model interpretation.
        with self.db() as db:
            rows=db.execute('SELECT result FROM drafts WHERE session=? ORDER BY rowid DESC',(session,)).fetchall()
        cards=[]
        for row in rows:
            result=json.loads(row[0])
            if not result.get('undo_id') or not result.get('text'):continue
            for presentation in dict.fromkeys((result['text'],result['text'].replace(SAVED_UNDO_INSTRUCTION,SAVED_PENDING_INSTRUCTION))):
                if presentation in text:cards.append({**result,'text':presentation})
        return cards

    def latest_card_undo(self,session):
        with self.db() as db:
            row=db.execute("SELECT reply FROM messages WHERE session=? AND delivered=1 AND reply IS NOT NULL AND trim(reply)!='' ORDER BY rowid DESC LIMIT 1",(session,)).fetchone()
        targets={r['undo_id'] for r in self.saved_cards(session,row[0]) if SAVED_UNDO_INSTRUCTION in r['text']} if row else set()
        return next(iter(targets)) if len(targets)==1 else ''

    async def undo(self,session,token=''):
        async with self.execution_locks.setdefault(session,asyncio.Lock()):
            if not self.enabled['apple']:return {'status':'disabled','text':'Apple 工具已关闭，尚未撤销。'}
            if self.pending(session):return {'status':'deny','text':'还有待确认或待回答的操作，请先处理或取消，再撤销已保存的操作。'}
            with self.db() as db:
                rows=db.execute('SELECT result FROM drafts WHERE session=? ORDER BY rowid DESC',(session,)).fetchall()
            candidates=[json.loads(r[0]) for r in rows if json.loads(r[0]).get('undo_id')]
            candidates=[r for r in candidates if not token or r['undo_id'].endswith(token)]
            if not candidates:return {'status':'deny','text':'当前会话没有对应的可撤销直接写入记录。'}
            target=candidates[0]
            self.set_pending(session,{'kind':'approval','executing':True,'undo_id':target['undo_id'],
                'confirmation_key':self.active[session]['key']})
            try:
                result=await asyncio.to_thread(self.apple.undo,target['undo_id'])
                result={**result,'text':'这次操作已经撤销，不会继续撤销更早的记录。' if result.get('already_undone') else '已撤销这次保存。'}
            except Exception as error:
                result={'status':'deny','reason':str(error),'text':'未能安全撤销：对象可能已被修改、删除或结果未知。请先查询核实，不会覆盖后续修改或重复执行。'}
            self.persist_receipt(session,result)
            self.set_pending(session,None)
            return result

    async def confirm(self,session):
        """执行原样批准的草稿；沿用原身份和 Apple 幂等账本，不调用模型生成操作。"""
        if not self.enabled['apple']:return {'status':'disabled','text':'Apple 工具已关闭，本次未写入；请启用后再确认。'}
        pending=self.pending(session)
        if not pending or pending['kind']!='approval':
            return {'status':'deny','text':'当前没有可确认的有效预览，请说明需要的操作。'}
        if pending.get('executing'):return await self.reconcile(session,pending)
        if pending['expires_at']<=time.time():
            self.set_pending(session,None);return {'status':'deny','text':'预览已过期，请重新预览。'}
        if self.feedback_after(session,pending['origin'],self.active.get(session,{}).get('key')):
            pending['delivered']=False;self.set_pending(session,pending)
        if not pending.get('delivered'):
            return {'status':'deny','text':'当前没有可确认的已展示预览；请先重新展示并核对草稿。本次未执行操作。'}
        args=pending['args']
        invocation=ToolInvocationContext(session_id=session,turn_id=pending['origin'],tool_call_id=pending['id'],tool_name='personalPlan')
        try:
            execution=await self.tools['personalPlan'].resolve_execution(args,context=invocation)
            if not execution.need_confirm or canonical(execution.approval_preview_value())!=canonical(pending['preview']):
                raise ToolInputError('对象或预览已变化，请重新查询并预览。')
        except Exception as error:
            self.set_pending(session,None)
            return {'status':'deny','text':'该操作未保存，请重新核实并预览。','reason':str(error)}
        pending['executing']=True;pending['confirmation_key']=self.active[session]['key'];self.set_pending(session,pending)
        try:
            result=await execution.run()
            if isinstance(result,ToolResult):result={**(payload(result.output) or {}),'is_error':result.is_error}
        except Exception as error:
            result={'status':'failed','reason':str(error),'text':'写入未能完整确认，请先核实 Apple 当前状态；不会自动重试。'}
        self.settle_drafts(session,pending['id'],result)
        self.set_pending(session,None)
        result['text']=result.get('text') or personal_result_text(args,result) or '已确认操作未能完成，请核实 Apple 当前状态。'
        return result

    async def reconcile(self,session,pending):
        if not self.enabled['apple']:return {'status':'disabled','text':'Apple 工具已关闭，尚未核实上次操作。'}
        if pending.get('undo_id'):
            try:
                check=await asyncio.to_thread(self.apple.inspect_operations,[pending['undo_id']+':undo'])
                saved=check['operations'][0]['status']=='completed' and check['operations'][0]['result'].get('saved') is True
                result={'text':'已核实上次撤销成功；没有重复执行。' if saved else '上次撤销尚无成功回执，请先查询核实；没有重放撤销。'}
            except Exception:result={'text':'暂时无法核实上次撤销；没有重复执行。'}
            self.set_pending(session,None)
            return result
        preview=pending['preview'];count=len(preview['operations']) if preview['action']=='batch' else 1
        ids=[operation_key(session,pending['origin'],f"{pending['id']}:{i+1}" if preview['action']=='batch' else pending['id']) for i in range(count)]
        try:
            check=await asyncio.to_thread(self.apple.inspect_operations,ids)
            saved=sum(x['status']=='completed' and x['result'].get('saved') is True for x in check['operations'])
            result={'status':'reconciled','saved_count':saved,'text':f'已核对上次操作：{saved} 项有保存记录；未重放写入。'}
            if pending.get('direct') and saved==1:
                result=self.direct_receipt(session,pending,check['operations'][0]['result'])
        except Exception:
            result={'status':'unavailable','text':'暂时无法核实上次写入；不会自动重复执行。'}
        # Retire the authorization even if the outcome remains unknown; Apple ledger blocks further writes.
        self.settle_drafts(session,pending['id'],result if result.get('saved') else {**result,'status':'failed'})
        self.set_pending(session,None)
        return result

    def save_reply(self,key,text):
        with self.db() as db:db.execute('UPDATE messages SET reply=? WHERE id=?',(text,key))

    async def deliver(self,session,key,adapter,ref):
        with self.db() as db:row=db.execute('SELECT reply,delivered FROM messages WHERE id=?',(key,)).fetchone()
        if not row or row[0] is None or row[1]:return
        if not row[0].strip():
            with self.db() as db:db.execute('UPDATE messages SET delivered=1 WHERE id=?',(key,))
            return
        token=delivery_log_context.set({'delivery_id':key,'input_event_ids':[key]})
        pending=self.pending(session)
        pending_identity=(pending.get('id'),pending.get('origin')) if pending else None
        active_key=self.active.get(session,{}).get('key')
        preview=pending.get('text','') if pending and pending['kind']=='approval' else ''
        cards=self.saved_cards(session,row[0])
        # Keep a saved card intact without folding independent answers into it.
        protected=list(dict.fromkeys([r['text'] for r in cards]+([preview] if preview else [])))
        try:complete=await send_reply_parts(self,session,key,row[0],adapter,ref,protected)
        finally:delivery_log_context.reset(token)
        if not complete:return
        with self.db() as db:db.execute('UPDATE messages SET delivered=1 WHERE id=?',(key,))
        pending=self.pending(session)
        if (pending and pending['kind']=='approval' and pending.get('text') and pending['text'] in row[0]
                and (pending.get('id'),pending.get('origin'))==pending_identity
                and (self.active.get(session,{}).get('key')==active_key or self.active.get(session,{}).get('scheduled'))
                and not self.feedback_after(session,key)):
            pending['delivered']=True;self.set_pending(session,pending)

    def render(self,session,text):
        pending=self.pending(session)
        # The model composes the reply from native tool results. Do not append a
        # second presentation when it paraphrases or omits the exact approval.
        # Fall back to the authoritative text so confirmation covers what was shown.
        with self.db() as db:
            origin=db.execute('SELECT delivered FROM messages WHERE id=?',(pending.get('origin'),)).fetchone() if pending else None
        # An early numeric confirmation can be spliced before the initial card
        # is sent. Ordinary feedback, even after a failed send, requires revise.
        current_preview=bool(pending and (pending.get('origin')==self.active.get(session,{}).get('key')
            or (origin and not origin[0] and not self.feedback_after(session,pending.get('origin')))))
        if current_preview:
            presentation=pending.get('text') or pending.get('question','')
            if presentation and text.count(presentation)!=1:text=presentation
        elif pending and pending['kind']=='approval' and pending.get('text') and not pending.get('delivered'):
            # A quotation is not a newly checked preview. Only a successful
            # structured revision can restore its confirmation controls.
            text=text.replace(pending['text'],'这份旧预览尚未重新校验，当前不能确认；需要重新生成预览。')
        parts=[text.strip()] if text.strip() else []
        receipt=self.active.get(session,{}).get('receipt') or {}
        if receipt.get('text') and receipt['text'] not in '\n'.join(parts):parts.insert(0,receipt['text'])
        text='\n\n'.join(parts)
        if pending:
            for card in self.saved_cards(session,text):
                text=text.replace(card['text'],card['text'].replace(SAVED_UNDO_INSTRUCTION,SAVED_PENDING_INSTRUCTION))
        return text
