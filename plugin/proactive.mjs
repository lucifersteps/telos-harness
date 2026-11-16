// Policy/intent storage belongs to Python; timers, inbox and model turns to DSH.
import {post} from './bridge.mjs';
import {proactiveContextView,proactiveInput,stableContext} from './runtime-context.mjs';
import {mountProactiveQueue} from './proactive-queue.mjs';
import {allowsProactiveRead} from './proactive-capabilities.mjs';
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
        agent(agent){let delivered=Promise.resolve();return {
          session:agent.session,get inbox(){return agent.inbox;},get status(){return agent.status;},
          // 0.2.2 waits for execution after followup. Admission happens during
          // flush, so wait for that handoff before inspecting the real Agent.
          whenIdle(){return delivered.then(()=>agent.whenIdle());},followup(message){
          if(pending.has(agent.session))throw new Error('Concurrent scheduler delivery');
          const ready=Promise.withResolvers();delivered=ready.promise;
          pending.set(agent.session,{agent,message,ready});
        }};},
        sessions:{async flush(session){
          const work=pending.get(session);pending.delete(session);
          try {
            if(work&&await admit(session.id,work.message))work.agent.followup(work.message);
            return await sessions.flush(session);
          } finally {work?.ready.resolve();}
        }}
      };
    },
    mount(scope,agent) {
      mountProactiveQueue(scope,agent,{
        resolve:message=>resolve(agent.session.id,message),
        admit:message=>admit(agent.session.id,message),
        flush:()=>ctx.sessions.flush(agent.session)
      });
      let active=null,inputs=[],human=false,contextSection,checkTime;
      agent.ctx.on('agent/inbox/claimed',({message})=>{
        inputs.push(message);
        if(message.source?.kind==='user'){human=true;}
      });
      agent.ctx.on('session/event',(session,event)=>{
        if(session===agent.session&&event.type==='turn/end'){active=null;inputs=[];human=false;}
      });
      scope.on('system-prompt/assemble',async(assembly,_context,next)=>{
        contextSection={name:'telos:proactive-context',order:0,text:'',required:true,interpolate:false};
        assembly.contexts.push(contextSection);return next();
      });
      scope.systemPrompt.section({name:'telos:proactive',order:0,interpolate:false,text:
        `Telos proactive execution rules apply only when the current trusted proactive context contains an active event. Ordinary assistant text is NOT sent to WeChat during a proactive check; use notify_user for the actual message. Ordinary chat output rules do not replace this tool call.
The current trusted proactive context supplies notification_required. When notification_required=true, call notify_user exactly once before finishing; lack of new information, unanswered questions or a focus block does not waive that requirement. When false, you may remain silent if there is nothing worth contacting the user about. Fixed reminders and followups retain their own explicit purposes. Ordinary assistant text is never a substitute for notify_user.
Use basic contact only: decide what is worth sharing from the current purpose and conversation. Do not apply historical goal-coaching policies, mandatory activity/health queries, or diagnostic classifications. Preserve uncertainty when sources fail.
Use event.prompt for the current check purpose, settings.preferences as fallback, and actual recent delivery records to avoid repeating content. Do not invent progress or facts. A queued/sent/unknown event must not be submitted again; queued is not proof of delivery and unknown must not be retried blindly. A plan is not proof of execution.
schedule_next_check registers a future check, not a promised message; supply an absolute timestamp with timezone and a self-contained reason. Check pending_checks first; do not create duplicates or use scheduling as a substitute for a required notification.
Proactive checks are read-only except for notify_user and scheduling a later check. No check may change the recipient, override pause/expiry/human-input priority, replay cancelled events, or authorize unrelated writes. Only explicit human requests may use proactive_control to pause/resume/list/cancel.`});
      const tool=(name,description,properties,required,execute)=>scope.tools.register({name,description,
        parameters:{type:'object',properties,required,additionalProperties:false},
        output:{schema:{type:'object',additionalProperties:true},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},execute});
      tool('notify_user','Submit ONE basic notification for the current proactive check. Recipient and event are host-bound. queued means accepted, not delivered.',
        {message:{type:'string',minLength:1,maxLength:4000}},['message'],async(args)=>{
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
      // A policy's permission never exceeds the host's read-only ceiling.
      scope.tools.guard(exec=>{
        if(!active||human)return;
        if(allowsProactiveRead(exec))return;
        return '主动检查仅允许只读查询、notify_user 和 schedule_next_check。';
      });
      return {
        instruct:messages=>human?messages:messages.map(message=>proactiveInput(message,active)),async prepare(messages){
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
        if(active?.id!==context.events[0]?.id)checkTime=context.now;
        active=context.events[0]??null;
        if(contextSection)contextSection.text=active?stableContext({proactive:{...proactiveContextView(context),check_time:checkTime}}):human?stableContext({proactive_settings:context.settings}):'';
        return active;
      }};
    }
  };
}
