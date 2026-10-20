// Native scheduling owns rules, durable tasks and timers. This adapter only
// selects Telos storage and resolves an already-bound WeChat conversation.
import ScheduleService,{scheduleDomain} from '@deepseek-ai/dsh-schedule';
import * as ScheduleTools from '@deepseek-ai/dsh-tool-schedule';
import {post} from './bridge.mjs';

export function createScheduler(ctx,service,namespace,open) {
  if(!/^telos_schedule_[a-f0-9]{16}$/.test(namespace))throw new Error('Invalid Telos schedule namespace');
  const scope=ctx.isolate('schedule').isolate('sessionController').isolate('storageDomain');
  const storage=ctx.get('storageDomain');
  const storageAdapter={open(spec){
    if(spec!==scheduleDomain)throw new Error('Unexpected schedule storage domain');
    return storage.open({...spec,name:namespace});
  }};
  const resolver={async resolveAgent(id){
    try {
      // The business service validates persisted ownership and current account.
      await post(service,'/memory-profile',{session:id},AbortSignal.timeout(15000));
      return {agent:(await open(id,true)).agent};
    } catch(error){return {error};}
  }};
  return {scope,start:async()=>{
    await scope.plugin({name:'telos-schedule-adapters',apply(child){
      child.provide('storageDomain',storageAdapter);child.provide('sessionController',resolver);
    }});
    await scope.plugin(ScheduleService);
    if(!scope.get('schedule'))throw new Error('DSH native schedule service did not activate');
  }};
}

export async function mountScheduleTools(scope,scheduler) {
  // Keep the original native definitions and caller-session binding. Do not
  // inherit the desktop's unrelated schedule instance into this Agent.
  const local=scope.isolate('schedule');
  const native=scheduler.get('schedule');
  local.provide('schedule',Object.fromEntries(['create','list','update','delete','catalog'].map(name=>[name,async(...args)=>{
    try{return await native[name](...args);}catch(error){console.error('Telos schedule '+name+': '+error.stack);throw error;}
  }])));
  await local.plugin(ScheduleTools);
  scope.systemPrompt.section({name:'telos:schedule',order:0,interpolate:false,text:
    'Use schedule_create/list/update/delete for reminders and recurring tasks requested in this WeChat conversation. Successful tasks run through the native DSH scheduler and replies are sent to this same chat. Tasks require DSH and Telos to be running; overdue work can resume when they restart. These tools manage this chat only, not Apple calendar/reminder objects or desktop chat tasks. Use an explicit time zone for calendar rules; clarify ambiguous times. A scheduled message is never human confirmation for a pending Apple write. Report creation or changes only when the tool succeeds.'});
}

export function conversationTurns(events) {
  const turns=[];let current;
  for(const event of events){
    if(event.type==='turn/start')current={turn:event.data.turn,scheduled:false,inputs:[],users:[],text:''};
    if(!current)continue;
    if(event.type==='user/message'){
      if(event.data.source?.kind==='schedule'){current.scheduled=true;current.inputs.push(event.data.id);}
      if(event.data.source?.kind==='user')current.users.push(event.data.id);
    }
    if(event.type==='assistant/message')current.text=event.data.message.content.filter(b=>b.type==='text').map(b=>b.text).join('');
    if(event.type==='turn/end'){
      turns.push({...current,seq:event.seq,finish:event.data});current=undefined;
    }
  }
  return turns;
}

export function scheduleDelivery(ctx,service) {
  let chain=Promise.resolve();const pending=new Map(),acknowledged=new Map();
  const drain=()=>{
    chain=chain.then(async()=>{
      for(const [key,output] of pending){
        await post(service,'/scheduled-output',output,AbortSignal.timeout(15000));
        acknowledged.set(output.session,output.seq);pending.delete(key);
      }
    }).catch(error=>console.error('Telos scheduled output: '+error.message));
    return chain;
  };
  const retry=setInterval(()=>{if(pending.size)void drain();},5000);
  retry.unref();ctx.effect(()=>()=>clearInterval(retry));
  const enqueue=(session,events)=>{
    const turns=conversationTurns(events).filter(t=>t.scheduled&&!t.users.length);
    for(const turn of turns){
      if(turn.seq<=(acknowledged.get(session)??-1))continue;
      pending.set(session+'\0'+turn.seq,{session,seq:turn.seq,text:turn.text,inputs:turn.inputs});
    }
    return drain();
  };
  async function replay(){
    const {sessions}=await post(service,'/sessions',{},AbortSignal.timeout(15000));
    for(const session of sessions){
      try {
        const observation=await ctx.sessionQuery.observeSession(session,{projectionMode:'none'});
        try {await enqueue(session,observation.events);} finally {observation[Symbol.dispose]();}
      } catch(error){console.error('Telos schedule recovery: '+error.message);}
    }
  }
  return {enqueue,replay,drain};
}
