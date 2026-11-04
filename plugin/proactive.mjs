// Policy/intent storage belongs to Python; timers, inbox and model turns to DSH.
import {post} from './bridge.mjs';
export const PROACTIVE_PREFIX='telos_proactive_v1:';
const textOf=m=>(m.content??[]).filter(b=>b.type==='text').map(b=>b.text).join('\n');
export function scheduleId(message) {
  if(message.source?.kind!=='schedule')return null;
  const line=textOf(message).split('\n').find(line=>line.startsWith('schedule_id_json: '));
  if(!line)return null;
  try{return JSON.parse(line.slice('schedule_id_json: '.length));}catch{return null;}
}
export function createProactive(ctx,service,getSchedule) {
  let serial=Promise.resolve(),closed=false;
  const request=(path,data)=>post(service,'/proactive/'+path,data,AbortSignal.timeout(15000));
  async function resolve(session,message) {
    const taskId=scheduleId(message);if(!taskId)return null;
    const stored=await request('lookup',{session,task_id:taskId});
    if(stored.event)return stored.event;
    // Native create can durably succeed before its local acknowledgement is saved.
    const task=(await getSchedule().catalog()).find(r=>r.id===taskId&&r.sessionId===session);
    if(!task?.prompt?.startsWith(PROACTIVE_PREFIX))return null;
    const eventId=task.prompt.slice(PROACTIVE_PREFIX.length).trim();
    await request('bind',{session,event_id:eventId,task_id:taskId});
    return (await request('lookup',{session,task_id:taskId})).event;
  }
  async function admit(session,message) {
    const event=await resolve(session,message);
    if(!event)return true;
    return (await request('admit',{session,event_id:event.id,task_id:event.task_id,input_id:message.id})).allowed;
  }
  const reconcile=()=>{
    const task=serial.then(async()=>{
      if(closed)return;
      const native=getSchedule();if(!native)return;
      const {events,obsolete}=await request('plan',{});
      const records=await native.catalog();
      for(const row of records) {
        if(row.status==='active'&&obsolete.some(e=>e.task_id===row.id&&e.session===row.sessionId))
          await native.delete({sessionId:row.sessionId,id:row.id});
      }
      for(const event of events) {
        if(closed)return;
        const prompt=PROACTIVE_PREFIX+event.id;
        let record=records.find(r=>r.sessionId===event.session&&r.prompt===prompt);
        if(event.task_id&&!record){await request('cancel-missing',{session:event.session,event_id:event.id});continue;}
        if(!record) {
          const request={title:'Telos '+event.kind,prompt,at:new Date(event.due*1000).toISOString()};
          try {record=await native.create(event.session,request);}
          catch(error){
            // A recovered, unregistered intent may already be due. Keep its
            // original policy timestamp; only the native wakeup moves to now.
            if(error.code!=='not_future')throw error;
            record=await native.create(event.session,{title:request.title,prompt,after_seconds:1});
          }
          records.push(record);
        }
        await request('bind',{session:event.session,event_id:event.id,task_id:record.id});
      }
    });
    serial=task.catch(error=>console.error('Telos proactive reconciliation: '+error.message));
    return task;
  };
  return {
    resolve,admit,reconcile,
    async start(){await reconcile();const timer=setInterval(()=>void reconcile().catch(()=>{}),30000);timer.unref();ctx.effect(()=>()=>{closed=true;clearInterval(timer);});},
    async assertEditable(session,id){
      const {event}=await request('lookup',{session,task_id:id});
      if(event)throw new Error('主动消息任务由 Telos 配置管理；请修改固定提醒或使用 proactive_control。');
    },
    gate(sessions) {
      const pending=new WeakMap();
      return {
        agent(agent){return {session:agent.session,followup(message){
          if(pending.has(agent.session))throw new Error('Concurrent scheduler delivery');
          pending.set(agent.session,{agent,message});
        }};},
        sessions:{async flush(session){
          const work=pending.get(session);pending.delete(session);
          if(work&&await admit(session.id,work.message))work.agent.followup(work.message);
          return sessions.flush(session);
        }}
      };
    },
    mount(scope,agent) {
      let active=null,inputs=[],human=false,contextSection;
      agent.ctx.on('agent/inbox/claimed',({message})=>{
        inputs.push(message);
        if(message.source?.kind==='user')human=true;
      });
      agent.ctx.on('session/event',(session,event)=>{
        if(session===agent.session&&event.type==='turn/end'){active=null;inputs=[];human=false;}
      });
      scope.on('system-prompt/assemble',async(assembly,_context,next)=>{
        contextSection={name:'telos:proactive-context',order:0,text:'',required:true,interpolate:false};
        assembly.contexts.push(contextSection);return next();
      });
      scope.systemPrompt.section({name:'telos:proactive',order:0,interpolate:false,text:
        'Telos supports proactive checks. Only when a trusted proactive context is present, work silently: ordinary assistant text is NOT sent to WeChat. Call notify_user exactly once to submit the actual message; its queued result is not proof of delivery. Fixed reminders must query the required facts and call notify_user with the requested reminder. Heartbeat/followup checks may remain silent. Read recent proactive records and current chat; query live facts as needed, never invent missing sleep/task data. schedule_next_check registers a future check (not a promised notification); supply an absolute timestamp with timezone and a self-contained reason. Proactive checks may query information but must not modify Apple objects or perform unrelated writes. Human input takes priority over a simultaneous check. For explicit user requests to pause/resume/list/cancel proactive work use proactive_control; never claim a change before its tool succeeds.'});
      const tool=(name,description,properties,required,execute)=>scope.tools.register({name,description,
        parameters:{type:'object',properties,required,additionalProperties:false},
        output:{schema:{type:'object',additionalProperties:true},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},execute});
      tool('notify_user','Submit ONE message for the currently executing Telos proactive check. Recipient and event are host-bound. Only status queued means accepted, not delivered.',
        {message:{type:'string',minLength:1,maxLength:4000}},['message'],async(args,exec)=>{
          if(!active||human)throw new Error('notify_user is available only in an active proactive turn.');
          return request('notify',{session:agent.session.id,event_id:active.id,input_id:active.input_id,message:args.message});
        });
      tool('schedule_next_check','Register one future proactive check for this chat. Use the configured timezone explicitly. The reason is read at that time; fresh context is queried then. No minimum spacing or merging with heartbeat.',
        {at:{type:'string'},reason:{type:'string',minLength:1,maxLength:4000}},['at','reason'],async(args,exec)=>{
          if(!active&&!human)throw new Error('A human request or proactive turn is required.');
          const value=await request('followup',{session:agent.session.id,call_id:exec.callId,...(active?{event_id:active.id,input_id:active.input_id}:{}),...args});
          await reconcile();return value;
        });
      tool('proactive_control','Manage this chat’s proactive messages on an explicit HUMAN request: list, pause (optional until with timezone), resume, or cancel by event_id. Pause covers fixed reminders and followup checks too.',
        {action:{type:'string',enum:['list','pause','resume','cancel']},until:{type:'string'},event_id:{type:'string'}},['action'],async(args)=>{
          if(!human)throw new Error('Proactive settings changes require a human message.');
          const value=await request('control',{session:agent.session.id,...args});await reconcile();return value;
        });
      // Query-only execution during unattended checks. Unknown/new host tools are
      // denied; explicitly supported health tools remain available for morning briefs.
      const readOnly=new Set(['notify_user','schedule_next_check','session_event_read','session_event_search','web_fetch','web_search','read','read_image','glob','grep','skill','baidu_maps']);
      scope.tools.guard(exec=>{
        if(!active||human)return;
        if(exec.name==='personalPlan'&&['query','detail'].includes(exec.arguments?.action))return;
        if(exec.name==='user_memory'&&exec.arguments?.action==='read')return;
        if(readOnly.has(exec.name)||/^mcp__.*__(health_detail|heartbeat)$/.test(exec.name))return;
        return '主动检查仅允许只读查询、notify_user 和 schedule_next_check。';
      });
      return {async prepare(messages){
        if(messages.length){
          for(const message of messages)if(!inputs.some(m=>m.id===message.id))inputs.push(message);
          human=inputs.some(m=>m.source?.kind==='user');
        }
        // Recheck even on tool-continuation steps, where messages is empty.
        // Never turn a cancelled proactive turn into an unrestricted normal turn.
        for(const message of inputs) {
          const event=await resolve(agent.session.id,message);
          if(event&&!human&&!await admit(agent.session.id,message))throw new Error('主动检查已取消、暂停或过期。');
        }
        const context=await request('context',{session:agent.session.id,inputs:inputs.map(m=>m.id),human});
        if(!human&&context.events.length>1)throw new Error('Multiple proactive inputs in one turn; refusing ambiguous notification ownership.');
        active=context.events[0]??null;
        if(contextSection)contextSection.text=active?JSON.stringify({proactive:context}):human?JSON.stringify({proactive_settings:context.settings}):'';
        return active;
      }};
    }
  };
}
