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


class Business:
    def __init__(self, root, apple, config=None):
        self.enabled=runtime_options(config or {})['tools']
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'business.sqlite';self.apple=apple
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session TEXT, input TEXT, reply TEXT, delivered INTEGER DEFAULT 0);
              CREATE TABLE IF NOT EXISTS pending (session TEXT PRIMARY KEY, value TEXT);
              CREATE TABLE IF NOT EXISTS drafts (session TEXT, id TEXT, origin TEXT, root TEXT, args TEXT, result TEXT, PRIMARY KEY(session,id));
              CREATE TABLE IF NOT EXISTS facts (session TEXT PRIMARY KEY, value TEXT);
            ''')
        self.maps=BaiduMaps()
        maps_tool=ExecutableTool(name='baidu_maps',description='百度地图只读查询。先用原生 skill 加载 baidu-ai-map；支持地点、路线、地理编码和天气。',
            parameters={'type':'object','properties':{'operation':{'type':'string','enum':list(ENDPOINTS)},
                'parameters':PARAMETERS},'required':['operation','parameters'],'additionalProperties':False},
            resolver=lambda args,invocation:self.maps.resolve(args))
        self.tools={t.name:t for t in [create_personal_plan_tool(data_dir=self.root,options={},
            service=apple,draft_lookup=self.lookup),create_personal_question_tool(self),maps_tool]}
        # A request hint concerns draft lineage only; todo ownership stays in Harness.
        for tool in [self.tools['personalPlan']]:
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
        self.set_pending(request.session_id,{'kind':'question','question':request.question})
        return QuestionResponse(decision='awaiting_input')

    async def context(self,session):
        # DSH appends changed runtime contexts. Keep this contribution small and
        # stable; current Apple objects are read through personalPlan query/detail.
        return {'pending':self.pending(session),
                'receipt':self.active.get(session,{}).get('receipt'),
                'time':self.active.get(session,{}).get('time')}

    async def execute(self,session,cid,name,args,*,map_token=None):
        try:
            async with self.execution_locks.setdefault(session,asyncio.Lock()):
                return await self._execute(session,cid,name,args,map_token=map_token)
        except (ToolInputError,ValueError) as error:
            if name=='personalPlan' and isinstance(args,dict) and args.get('action') in {'create','update','delete','batch','revise'}:
                self.record_draft(session,cid,{k:v for k,v in args.items() if k!='request_mode'},
                                  {'status':'input_error','execution_started':False,'reason':str(error)})
            raise

    async def _execute(self,session,cid,name,args,*,map_token=None):
        """唯一模型工具入口：校验和预览。任何模型工具调用都不能直接批准写入。"""
        if (name=='personalPlan' and not self.enabled['apple']) or (name=='baidu_maps' and not self.enabled['maps']):
            raise ToolInputError('该工具已关闭，请在 Telos 插件页面启用后再使用。')
        tool=self.tools.get(name)
        if tool is None:raise ToolInputError('工具不存在。')
        error=validate_json_schema(tool.parameters,args)
        if error:raise ToolInputError(error)
        args=dict(args);mode=args.pop('request_mode','new')
        current=self.active[session]
        context=ToolInvocationContext(session_id=session,turn_id=current['key'],tool_call_id=cid,tool_name=name)
        pending=self.pending(session)
        write=name=='personalPlan' and args.get('action') not in {'query','detail'}
        if pending and (write or name=='ask_user'):
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
        if write:self.record_draft(session,cid,args,result)
        if name=='baidu_maps' and isinstance(result,dict) and result.get('status') in {'skill_result','needs_clarification'}:
            # Only location provenance is business state; no model or generic tool history is copied.
            records.append(SimpleNamespace(tool_name=name,args=args,result=result,is_error=False))
            with self.db() as db:db.execute('INSERT OR REPLACE INTO facts VALUES(?,?)',
                (session,canonical([vars(v) for v in records[-30:]])))
        return result

    async def confirm(self,session):
        """执行原样批准的草稿；沿用原身份和 Apple 幂等账本，不调用模型生成操作。"""
        if not self.enabled['apple']:return {'status':'disabled','text':'Apple 工具已关闭，本次未写入；请启用后再确认。'}
        pending=self.pending(session)
        if not pending or pending['kind']!='approval' or not pending.get('delivered'):
            return {'status':'deny','text':'当前没有可确认的有效预览，请说明需要的操作。'}
        if pending['expires_at']<=time.time():
            self.set_pending(session,None);return {'status':'deny','text':'预览已过期，请重新预览。'}
        if pending.get('executing'):return await self.reconcile(session,pending)
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
        preview=pending['preview'];count=len(preview['operations']) if preview['action']=='batch' else 1
        ids=[operation_key(session,pending['origin'],f"{pending['id']}:{i+1}" if preview['action']=='batch' else pending['id']) for i in range(count)]
        try:
            check=await asyncio.to_thread(self.apple.inspect_operations,ids)
            saved=sum(x['status']=='completed' and x['result'].get('saved') is True for x in check['operations'])
            result={'status':'reconciled','saved_count':saved,'text':f'已核对上次操作：{saved} 项有保存记录；未重放写入。'}
        except Exception:
            result={'status':'unavailable','text':'暂时无法核实上次写入；不会自动重复执行。'}
        # Retire the authorization even if the outcome remains unknown; Apple ledger blocks further writes.
        self.settle_drafts(session,pending['id'],{**result,'status':'failed'})
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
        try:result=await adapter.send_text(ref,row[0])
        finally:delivery_log_context.reset(token)
        if not result.ok:return
        with self.db() as db:db.execute('UPDATE messages SET delivered=1 WHERE id=?',(key,))
        pending=self.pending(session)
        if pending and pending['kind']=='approval' and pending['text'] in row[0]:
            pending['delivered']=True;pending['expires_at']=time.time()+600;self.set_pending(session,pending)

    def render(self,session,text):
        pending=self.pending(session)
        # The model composes the reply from native tool results. Do not append a
        # second presentation when it paraphrases or omits the exact approval.
        # Fall back to the authoritative text so confirmation covers what was shown.
        if pending:
            presentation=pending.get('text') or pending.get('question','')
            if presentation and text.count(presentation)!=1:text=presentation
        parts=[text.strip()] if text.strip() else []
        receipt=self.active.get(session,{}).get('receipt') or {}
        if receipt.get('text') and receipt['text'] not in '\n'.join(parts):parts.insert(0,receipt['text'])
        return '\n\n'.join(parts)
