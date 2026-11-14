import test from 'node:test';
import assert from 'node:assert/strict';
import {Session} from '@deepseek-ai/dsh-session';
import {createUserMessage,createSystemMessage} from '@deepseek-ai/dsh-llm';
import {ContextRetention,RETIRED,RECENT} from '../plugin/context-retention.mjs';

const source=(session,id,kind='user',body=id)=>session.append('user/message',createUserMessage({id,source:{kind},content:[{type:'text',text:body}]}),{surfaceOp:'append'});
const fresh=()=>{const s=Session.create('retention-test');s.append('system/message',{turn:0,step:0,message:createSystemMessage('Persona and safety rules')},{surfaceOp:'append'});return s;};
const visible=s=>s.surface.nodes.map(seq=>s.eventAt(seq));
const allText=s=>JSON.stringify(visible(s).map(e=>e.type==='user/message'?e.data.content:e.data.message?.content));
function run(s,t,{scheduled=false,status='completed',error=false,name='query',body}={}) {
  s.append('turn/start',{turn:t});
  const input=source(s,'input-'+t,scheduled?'schedule':'user','human-or-timer-'+t);
  const thought=s.append('assistant/message',{turn:t,step:1,stream:[],message:{role:'assistant',source:{kind:'model',provider:'test',model:'test'},id:'a'+t,content:[{type:'text',text:'temporary-analysis-'+t},{type:'tool-call',id:'call-'+t,name,arguments:'{}'}]}},{surfaceOp:'append'});
  const result=s.append('tool/result',{turn:t,step:1,message:{role:'tool',source:{kind:'tool',callId:'call-'+t},id:'r'+t,toolCallId:'call-'+t,isError:error,content:[{type:'text',text:body??('QUERY_RESULT_'+t+'x'.repeat(20000))}]}},{surfaceOp:'append'});
  const final=s.append('assistant/message',{turn:t,step:2,stream:[],message:{role:'assistant',source:{kind:'model',provider:'test',model:'test'},id:'f'+t,content:[{type:'text',text:'Final answer '+t}]}},{surfaceOp:'append'});
  s.append('turn/end',{turn:t,reason:{kind:status}});
  return {input,thought,result,final};
}

test('only completed acknowledged turns retire; human dialogue and raw history survive restart',()=>{
  const s=fresh(),a=run(s,1),b=run(s,2),c=run(s,3,{status:'error'}),r=new ContextRetention(s);
  const before=s.snapshotEvents().length;r.apply({eligible:[1,3]});
  assert.ok(s.surface.nodes.includes(a.input.seq));assert.ok(s.surface.nodes.includes(a.final.seq));
  assert.ok(!s.surface.nodes.includes(a.result.seq));assert.ok(!s.surface.nodes.includes(a.thought.seq));
  assert.ok(s.surface.nodes.includes(b.result.seq));assert.ok(s.surface.nodes.includes(c.result.seq));
  assert.ok(s.snapshotEvents().slice(0,before).some(e=>e.seq===a.result.seq));
  const restored=Session.create('retention-test',JSON.parse(JSON.stringify(s.snapshotEvents())));
  assert.deepEqual(restored.surface.nodes,s.surface.nodes);
  new ContextRetention(restored).apply({eligible:[1,3]});assert.deepEqual(restored.surface.nodes,s.surface.nodes);
});

test('hundreds of scheduled turns keep a bounded notification summary, not receipt markers',()=>{
  const s=fresh(),r=new ContextRetention(s);let old;
  for(let t=1;t<=150;t++) {
    run(s,t,{scheduled:true});r.apply({eligible:Array.from({length:t},(_,i)=>i+1),recent:[{turn:t,seq:s.snapshotEvents().at(-1).seq,text:'Sent notification '+t}]});
    if(t===140)old=s.surface.nodes.length;
  }
  assert.ok(s.surface.nodes.length<=old+1);
  assert.equal(visible(s).filter(e=>e.data.source?.kind===RECENT).length,1);
  assert.ok(visible(s).filter(e=>e.data.source?.kind===RETIRED).length<=2);
  assert.ok(!allText(s).includes('QUERY_RESULT_'));assert.ok(allText(s).includes('Sent notification 150'));
  assert.ok(!allText(s).includes('Sent notification 147'));
});

test('current turn and uncertain result stay available',()=>{
  const s=fresh(),a=run(s,1,{error:true});s.append('turn/start',{turn:2});
  const current=source(s,'live','user','unanswered request');const r=new ContextRetention(s);
  r.apply({eligible:[1,2]});assert.ok(s.surface.nodes.includes(a.result.seq));assert.ok(s.surface.nodes.includes(current.seq));
});

test('profile baseline migration removes aliases only, preserves other instructions',()=>{
  const s=fresh();const add=(path,body)=>s.append('user/message',createUserMessage({source:{kind:'agent-instructions',baseline:true,
    changes:[{action:'set',path,scope:`.\0${path}`}]},content:[{type:'text',text:body}]}),{surfaceOp:'append'});
  add('user.md','OLD_PROFILE');add('AGENTS.md','NEW_PROFILE');const other=add('nested/AGENTS.md','PROJECT_RULES');
  new ContextRetention(s).apply();assert.ok(!allText(s).includes('OLD_PROFILE'));assert.ok(allText(s).includes('NEW_PROFILE'));
  assert.ok(s.surface.nodes.includes(other.seq));assert.ok(allText(s).includes('Persona and safety rules'));
});

test('native pruning of an old result still belongs to its original turn',()=>{
  const s=fresh(),a=run(s,1);s.append('turn/start',{turn:2});
  const replacement=s.append('tool/result',{...a.result.data,message:{...a.result.data.message,content:[{type:'text',text:'trimmed'}]}},
    {surfaceOp:{op:'replace',startSeq:a.result.seq,endSeq:a.result.seq},sourceEventSeqs:[a.result.seq]});
  new ContextRetention(s).apply({eligible:[1]});assert.ok(!s.surface.nodes.includes(replacement.seq));
});


test('delivered read-only partial/unknown data does not pin complete turns',()=>{
  const s=fresh(),a=run(s,1,{name:'activity_history',scheduled:true,body:'{"status":"partial","sources_status":[{"state":"unknown"}]}'});
  const b=run(s,2,{name:'personalPlan',body:'{"items":[{"status":"failed"}],"coverage":"partial"}'});
  const r=new ContextRetention(s);r.apply({eligible:[1,2]});
  assert.ok(!s.surface.nodes.includes(a.result.seq));assert.ok(!s.surface.nodes.includes(b.result.seq));
  assert.ok(s.surface.nodes.includes(b.input.seq));assert.ok(s.surface.nodes.includes(b.final.seq));
});

test('managed validation errors clear only after durable settlement, including after restart',()=>{
  const s=fresh(),a=run(s,1,{name:'notify_user',scheduled:true,error:true,body:'TELOS_PLAN_REQUIRED: read before notifying'});
  const b=run(s,2,{name:'personalPlan',error:true,body:'Old write error, now settled in the ledger'});
  const r=new ContextRetention(s);r.apply();
  assert.ok(s.surface.nodes.includes(a.result.seq));assert.ok(s.surface.nodes.includes(b.result.seq));
  const restored=Session.create('retention-test',JSON.parse(JSON.stringify(s.snapshotEvents())));
  new ContextRetention(restored).apply({eligible:[1,2]});
  assert.ok(!restored.surface.nodes.includes(a.result.seq));assert.ok(!restored.surface.nodes.includes(b.result.seq));
  assert.ok(restored.eventAt(a.result.seq));assert.ok(restored.eventAt(b.result.seq));
});

test('ambiguous unledgered operations and unattributed results remain protected',()=>{
  const s=fresh(),image=run(s,1,{name:'send_wechat_image',body:'{"status":"partial","caption":"unknown"}'});
  const shell=run(s,2,{name:'bash',error:true,body:'write outcome unknown'});
  const missing=run(s,3,{error:true});
  new ContextRetention(s).apply({eligible:[1,2,3]});
  for(const t of [image,shell,missing])assert.ok(s.surface.nodes.includes(t.result.seq));
});


test('read-only file/history contents cannot be mistaken for live operation status',()=>{
  const s=fresh(),a=run(s,1,{name:'read',body:'{"status":"partial"}'});
  const b=run(s,2,{name:'session_event_read',body:'Historical record: {"state":"failed"}'});
  const c=run(s,3,{name:'web_fetch',error:true,body:'Page unavailable'});
  new ContextRetention(s).apply({eligible:[1,2,3]});
  for(const t of [a,b,c])assert.ok(!s.surface.nodes.includes(t.result.seq));
});
