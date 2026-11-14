import test from 'node:test';
import assert from 'node:assert/strict';
import {createPolicySlot} from '../plugin/proactive-policy.mjs';
import {allowsProactiveRead} from '../plugin/proactive-capabilities.mjs';
const now=Date.parse('2026-11-06T12:00:00+08:00');
const event={id:'check',input_id:'input',kind:'heartbeat'};
const requirements={goal_list_ids:['goals'],action_list_ids:[],day_start:'2026-11-06T00:00:00+08:00',day_end:'2026-11-07T00:00:00+08:00'};
const goal={id:'goal_execution',enabled:true,config:{},context:{query_requirements:requirements}};
const basic={id:'basic',enabled:true,config:{},context:{}};
const off={id:null,enabled:false,config:{},context:{}};
const exec={callId:'one',signal:new AbortController().signal};
async function read(slot){
 await slot.execute('activity_history',{start:'2026-11-06T11:00:00+08:00',end:'2026-11-06T12:00:00+08:00'},exec,async()=>({status:'ok'}));
 await slot.execute('personalPlan',{action:'query',kind:'reminder',include_completed:true},exec,async()=>({total:0,offset:0,items:[],calendars:[{id:'goals'}]}));
 await slot.execute('personalPlan',{action:'query',kind:'event',start:requirements.day_start,end:requirements.day_end},exec,async()=>({total:0,offset:0,items:[]}));
}
test('enabled policy retains prerequisites and labels; disabling removes both without disabling tools',async()=>{
 const slot=createPolicySlot({now:()=>now});await slot.bind(event,goal);
 assert.match(slot.view().instructions,/plan_missing/);assert.throws(()=>slot.beforeNotify({assessment:'on_track'}),/TELOS_ACTIVITY_REQUIRED/);
 await read(slot);assert.throws(()=>slot.beforeNotify({assessment:'on_track'}),/TELOS_ACTIVITY_REQUIRED/);
 await slot.bind(event,goal);slot.beforeNotify({assessment:'on_track'});
 assert.throws(()=>slot.beforeNotify({}),/TELOS_ASSESSMENT_REQUIRED/);
 await slot.bind(event,off);assert.equal(slot.view().instructions,undefined);assert.deepEqual(slot.view().context,{});
 slot.beforeNotify({});slot.beforeNotify({assessment:'custom'});
 assert.equal(await slot.execute('activity_history',{},exec,async()=>42),42);
 await slot.bind(event,goal);assert.throws(()=>slot.beforeNotify({assessment:'on_track'}),/TELOS_ACTIVITY_REQUIRED/);
});
test('switching to basic clears prompts, in-flight evidence and restricted permissions',async()=>{
 const slot=createPolicySlot({now:()=>now});await slot.bind({...event,habit_reminder_allowed:true},goal);
 assert.equal(allowsProactiveRead({name:'habit_tasks',arguments:{action:'list'}},slot),true);
 let finish;const pending=slot.execute('activity_history',{start:'2026-11-06T11:00:00+08:00',end:'2026-11-06T12:00:00+08:00'},exec,()=>new Promise(resolve=>finish=resolve));
 await slot.bind(event,basic);finish({status:'ok'});await pending;
 assert.doesNotMatch(slot.view().instructions,/plan_missing|TELOS_ACTIVITY_REQUIRED/);slot.beforeNotify({});
 assert.equal(allowsProactiveRead({name:'habit_tasks',arguments:{action:'list'}},slot),false);
 await slot.bind(event,goal);assert.throws(()=>slot.beforeNotify({assessment:'on_track'}),/TELOS_ACTIVITY_REQUIRED/);
});
test('policy cannot lift unattended-write restrictions; absent capabilities only allow unknown',async()=>{
 const slot=createPolicySlot();await slot.bind(event,goal,()=>false);slot.beforeNotify({assessment:'unknown'});
 assert.throws(()=>slot.beforeNotify({assessment:'on_track'}),/TELOS_EVIDENCE_UNKNOWN/);
 for(const selection of [goal,basic,off]){
  await slot.bind(event,selection);
  for(const tool of [{name:'exec'},{name:'personalPlan',arguments:{action:'create'}},{name:'habit_tasks',arguments:{action:'complete'}},{name:'proactive_control',arguments:{action:'pause'}}])assert.equal(allowsProactiveRead(tool,slot),false);
 }
});
test('human/reset during asynchronous activation cannot restore stale policy',async()=>{
 const slot=createPolicySlot();const pending=slot.bind(event,goal);slot.reset();await pending;
 assert.equal(slot.view().enabled,false);slot.beforeNotify({});
 await slot.bind(event,goal);await read(slot);await slot.bind(event,goal);slot.reset();
 await slot.bind(event,goal);assert.throws(()=>slot.beforeNotify({assessment:'unknown'}),/TELOS_ACTIVITY_REQUIRED/);
 await slot.bind(null,goal);assert.equal(slot.view().instructions,undefined);
});
