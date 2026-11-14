// DSH 0.2.1 binds tasks to a session; 0.2.2 uses explicit delivery targets.
// Keep native storage/timers and pin every Telos operation to its chat.
export const hasScheduleTargets=native=>typeof native.edit==='function';
export const scheduleSession=entry=>entry.target===undefined?entry.sessionId:
  entry.target.kind==='session'?entry.target.sessionId:undefined;
const boundTarget=sessionId=>({kind:'session',sessionId});
const missing=id=>({id,code:'schedule_not_found'});

export function proactiveSchedule(native) {
  if(!hasScheduleTargets(native))return native;
  return {
    async catalog(){return (await native.catalog()).map(entry=>({...entry,sessionId:scheduleSession(entry)}));},
    async create(sessionId,request){
      const timing=request.at!==undefined?{kind:'at',at:request.at}:{kind:'after',after_seconds:request.after_seconds};
      const entry=await native.create({title:request.title,prompt:request.prompt,timing,target:boundTarget(sessionId)},sessionId);
      return {...entry,sessionId:scheduleSession(entry)};
    },
    async delete({sessionId,id}){
      const entry=await native.view({id});
      if(scheduleSession(entry)!==sessionId)return missing(id);
      return native.edit({id,action:'delete',expectedUpdatedAt:entry.updatedAt??null},sessionId);
    }
  };
}

export function channelSchedule(native,sessionId,assertEditable,refuse=reason=>{throw new Error(reason);}) {
  const target=request=>{
    if(request!==undefined&&(request.kind!=='session'||request.sessionId!==sessionId))
      refuse('TELOS_SCHEDULE_CURRENT_SESSION_ONLY: 微信任务只能投递到当前会话。');
    return boundTarget(sessionId);
  };
  const actor=id=>{if(id!==undefined&&id!==sessionId)refuse('TELOS_SCHEDULE_CURRENT_SESSION_ONLY');};
  return {
    deliveryLimits:()=>native.deliveryLimits(),
    async create(request,actorId,signal){
      actor(actorId);
      return native.create({...request,target:target(request.target)},sessionId,signal);
    },
    async list(request={}){
      // Let the host validate paging, then paginate only this chat's entries.
      const {limit,offset}=await native.list(request);
      const all=(await native.catalog()).filter(entry=>scheduleSession(entry)===sessionId&&
        (request.active===undefined||(entry.status==='active')===request.active));
      const items=all.slice(offset,offset+limit);
      return {items,shown:items.length,total:all.length,limit,offset};
    },
    async view({id}){
      const entry=await native.view({id});
      return scheduleSession(entry)===sessionId?entry:missing(id);
    },
    async edit(request,actorId,signal){
      actor(actorId);
      const entry=await native.view({id:request.id});
      if(scheduleSession(entry)!==sessionId)return missing(request.id);
      if(request.patch?.target!==undefined)target(request.patch.target);
      await assertEditable?.(sessionId,request.id);
      // Protect ownership if another caller changes the task after our read.
      return native.edit({...request,expectedUpdatedAt:request.expectedUpdatedAt??entry.updatedAt??null},sessionId,signal);
    }
  };
}
