import test from 'node:test';
import assert from 'node:assert/strict';
import {channelSchedule,proactiveSchedule,scheduleSession} from '../plugin/schedule-compat.mjs';

const entry=(id,session,status='active')=>({id,target:{kind:'session',sessionId:session},status,updatedAt:'revision-1'});
function host(items) {
  const writes=[];
  return {writes,
    deliveryLimits:()=>({newSessionIntervalSeconds:60}),
    async catalog(){return items;},
    async view({id}){return items.find(e=>e.id===id)??{id,code:'schedule_not_found'};},
    async list({limit=10,offset=0}={}){return {limit,offset,items,total:items.length};},
    async create(request,actor,signal){writes.push({request,actor,signal});return {...entry('created',request.target.sessionId),...request};},
    async edit(request,actor,signal){writes.push({request,actor,signal});return request;}
  };
}

test('proactive bridge keeps the old host and translates new timing and ownership',async()=>{
  const old={create(){}};assert.equal(proactiveSchedule(old),old);
  const native=host([entry('own','chat'),entry('foreign','other')]);const api=proactiveSchedule(native);
  await api.create('chat',{title:'check',prompt:'read activity',at:'2099-01-01T00:00:00Z'});
  assert.deepEqual(native.writes[0].request.timing,{kind:'at',at:'2099-01-01T00:00:00Z'});
  assert.deepEqual(native.writes[0].request.target,{kind:'session',sessionId:'chat'});
  await api.create('chat',{title:'overdue',prompt:'recover',after_seconds:1});
  assert.deepEqual(native.writes[1].request.timing,{kind:'after',after_seconds:1});
  assert.equal((await api.catalog())[0].sessionId,'chat');
  await api.delete({sessionId:'chat',id:'foreign'});assert.equal(native.writes.length,2);
  await api.delete({sessionId:'chat',id:'own'});
  assert.deepEqual(native.writes[2].request,{id:'own',action:'delete',expectedUpdatedAt:'revision-1'});
  assert.equal(scheduleSession({sessionId:'legacy'}),'legacy');
  assert.equal(scheduleSession({sessionId:'legacy',target:{kind:'new_once',sessionId:'other'}}),undefined);
});

test('new native tools list and view only their chat, paginating after filtering',async()=>{
  const native=host([entry('foreign','other'),entry('a','chat'),entry('b','chat','paused'),entry('c','chat')]);
  const api=channelSchedule(native,'chat');
  assert.deepEqual(await api.list({active:true,limit:1,offset:1}),{items:[entry('c','chat')],shown:1,total:2,limit:1,offset:1});
  assert.deepEqual(await api.view({id:'foreign'}),{id:'foreign',code:'schedule_not_found'});
  assert.equal((await api.view({id:'a'})).id,'a');
});

test('new native tools cannot redirect, alter another chat, or edit managed checks',async()=>{
  const native=host([entry('own','chat'),entry('managed','chat'),entry('foreign','other')]);
  const api=channelSchedule(native,'chat',async(_session,id)=>{if(id==='managed')throw Error('managed check');});
  for(const target of [{kind:'session',sessionId:'other'},{kind:'new_once'},{kind:'new'}]) {
    await assert.rejects(api.create({target},'chat'),/CURRENT_SESSION_ONLY/);
    await assert.rejects(api.edit({id:'own',action:'update',patch:{target}},'chat'),/CURRENT_SESSION_ONLY/);
  }
  await assert.rejects(api.create({},'other'),/CURRENT_SESSION_ONLY/);
  assert.equal((await api.edit({id:'foreign',action:'delete'},'chat')).code,'schedule_not_found');
  await assert.rejects(api.edit({id:'managed',action:'delete'},'chat'),/managed check/);
  assert.equal(native.writes.length,0);
  const signal=AbortSignal.abort();
  await api.create({title:'own',prompt:'read'},'chat',signal);
  assert.deepEqual(native.writes[0].request.target,{kind:'session',sessionId:'chat'});
  assert.equal(native.writes[0].signal,signal);
  await api.edit({id:'own',action:'update',patch:{title:'changed'}},'chat');
  assert.equal(native.writes[1].request.expectedUpdatedAt,'revision-1');
  await api.edit({id:'own',action:'delete',expectedUpdatedAt:'explicit-revision'},'chat');
  assert.equal(native.writes[2].request.expectedUpdatedAt,'explicit-revision');
});
