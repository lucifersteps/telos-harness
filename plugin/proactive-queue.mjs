import {createUserMessage} from '@deepseek-ai/dsh-llm';

// A wake is transport-only. It must never enter a model request or WeChat output.
const WAKE_KIND='telos-proactive-queue-wake';
export const isQueueWake=message=>message.source?.kind===WAKE_KIND;

// Use the host's public inbox/Agent interfaces. Do not retry a consumed turn,
// replay a notification, or alter another plugin's or the human's pending input.
export function mountProactiveQueue(scope,agent,{resolve,admit,flush}) {
  let disposed=false,timer=null,working=false,rerun=false;
  let lastReason=agent.session.snapshotEvents().findLast(e=>e.type==='turn/end')?.data.reason?.kind;
  const schedule=()=>{
    if(disposed||timer!==null)return;
    timer=setTimeout(()=>{timer=null;void recover().catch(e=>console.error('Telos proactive queue: '+e.message));},0);
  };
  async function recover() {
    if(disposed)return;
    if(working){rerun=true;return;}
    working=true;
    try {
      await agent.whenIdle();
      // User cancellation and blocked work must not be automatically restarted.
      if(disposed||['aborted','blocked'].includes(lastReason))return;
      while(!disposed&&agent.status==='idle'&&!agent.inbox.nextStep.length) {
        const head=agent.inbox.nextTurn[0];if(!head)return;
        if(isQueueWake(head)){agent.inbox.remove(head.id);await flush();continue;}
        const event=await resolve(head);
        if(!event)return; // Ordinary schedules and other plugins retain ownership.
        const allowed=await admit(head);
        // Human input or a new driver may have arrived during the I/O above.
        if(disposed||agent.status!=='idle'||agent.inbox.nextStep.length||agent.inbox.nextTurn[0]?.id!==head.id)return;
        if(!allowed){agent.inbox.remove(head.id);await flush();continue;}
        // A transport-only next-step wake is claimed together with the existing
        // head. This preserves FIFO order and every original queued message ID.
        const wake=createUserMessage({source:{kind:WAKE_KIND},content:[]});
        agent.steer(wake);
        await flush();return;
      }
    } finally {working=false;if(rerun){rerun=false;schedule();}}
  }
  const offEnd=agent.ctx.on('session/event',(session,event)=>{
    if(session===agent.session&&event.type==='turn/end')lastReason=event.data.reason?.kind;
  });
  const offStatus=agent.ctx.on('agent/status',({status})=>{if(status==='idle')schedule();});
  // A restored inbox can already contain checks behind a previously failed turn.
  // Detach from factory setup: awaiting whenIdle there would deadlock creation.
  schedule();
  scope.effect(()=>()=>{disposed=true;if(timer!==null)clearTimeout(timer);offEnd();offStatus();});
}
