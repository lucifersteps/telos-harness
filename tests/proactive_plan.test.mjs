import test from 'node:test';
import assert from 'node:assert/strict';
import {createPlanCheck} from '../plugin/proactive-plan.mjs';
const event={id:'check',input_id:'input'};
const req={goal_list_ids:['goals'],action_list_ids:[],day_start:'2026-11-06T00:00:00+08:00',day_end:'2026-11-07T00:00:00+08:00'};
const reminders={action:'query',kind:'reminder',include_completed:true};
const calendar={action:'query',kind:'event',start:req.day_start,end:req.day_end};
const result=(items=[],total=items.length,offset=0)=>({items,total,offset,calendars:[{id:'goals'},{id:'work'}]});
const exec={callId:'read',signal:new AbortController().signal};
const create=()=>{const g=createPlanCheck();g.bind(event,req);return g;};
const read=async(g,args,res=result())=>g.execute(args,exec,async()=>res);
const blocked=g=>assert.throws(()=>g.assertRead(),/TELOS_PLAN_REQUIRED/);

test('goal-only read never substitutes for action or full-day calendar; next model step required',async()=>{
 const g=create();await read(g,{...reminders,calendar_ids:['goals']});g.bind(event,req);
 assert.deepEqual(g.status(),{goals:'read',actions:'missing',calendar:'missing'});blocked(g);
 await read(g,reminders);await read(g,calendar);blocked(g);g.bind(event,req);g.assertRead();
});
test('wrong sources, selective filters, details, missing completion and short/no-zone windows cannot qualify',async()=>{
 for(const args of [{...reminders,calendar_ids:['other']},{...reminders,include_completed:false},
 {...reminders,text:'论文'},{...reminders,due_start:'2026-11-06'},{...reminders,tags:['goal']},
 {action:'detail',kind:'reminder',id:'one'},{action:'query',kind:'list'},
 {...calendar,end:'2026-11-06T12:00:00+08:00'},{...calendar,start:'2026-11-06T00:00:00'},
 {...calendar,calendar_ids:['one-calendar']}]){
  const g=create();await read(g,args);g.bind(event,req);assert.notEqual(g.status().goals,'read');blocked(g);
 }
});
test('pagination requires full coverage; unrelated pages cannot be combined',async()=>{
 const g=create();await read(g,{...reminders,limit:1},result([{id:'a'}],3));await read(g,calendar);g.bind(event,req);blocked(g);
 await read(g,{...reminders,offset:2,limit:1},result([{id:'c'}],3,2));g.bind(event,req);blocked(g);
 await read(g,{...reminders,calendar_ids:['goals'],offset:1},result([{id:'b'}],3,1));g.bind(event,req);blocked(g);
 await read(g,{...reminders,offset:1,limit:1},result([{id:'b'}],3,1));g.bind(event,req);g.assertRead();
});
test('failure permits unknown only after receipt; missing goal source is not an empty goal list',async()=>{
 for(const r of [{is_error:true},{status:'failed'},result(),{}]){
  const g=create();await read(g,reminders,r);await read(g,calendar,r);g.bind(event,req);g.assertRead();
 }
 const g=create();await assert.rejects(g.execute(reminders,exec,async()=>{throw new Error('offline');}),/offline/);
 await read(g,calendar);blocked(g);g.bind(event,req);g.assertRead();assert.equal(g.status().goals,'unavailable');
 const missing=create();await read(missing,reminders,{...result(),calendars:[]});missing.bind(event,req);assert.equal(missing.status().goals,'unavailable');
});
test('pending/cancelled reads, human input, new event, changed sources and restart never reuse evidence',async()=>{
 for(const transition of [g=>g.reset(),g=>g.bind(null),g=>g.bind({...event,id:'next'},req),g=>g.bind(event,{...req,goal_list_ids:['changed']})]){
  const g=create();let finish;const pending=g.execute(reminders,exec,()=>new Promise(resolve=>finish=resolve));
  g.bind(event,req);blocked(g);transition(g);finish(result());await pending;blocked(g);
 }
 const g=create();const controller=new AbortController();await g.execute(reminders,{...exec,signal:controller.signal},async()=>{controller.abort();return result();});g.bind(event,req);blocked(g);
 const chat=createPlanCheck();await read(chat,reminders);chat.bind(event,req);blocked(chat);
});
test('configured action scope must be covered, including all configured lists',async()=>{
 const g=createPlanCheck(),r={...req,action_list_ids:['work','life']};g.bind(event,r);
 await read(g,{...reminders,calendar_ids:['work']});g.bind(event,r);assert.equal(g.status().actions,'missing');
 await read(g,{...reminders,calendar_ids:['work','life']},{...result(),calendars:[{id:'work'},{id:'life'}]});g.bind(event,r);assert.equal(g.status().actions,'read');
});

test('trusted disabled Apple tool permits unknown degradation, re-enable resets coverage',()=>{
 const g=createPlanCheck();g.bind(event,req,false);g.assertRead();
 assert.deepEqual(g.status(),{goals:'unavailable',actions:'unavailable',calendar:'unavailable'});
 g.bind(event,req,true);blocked(g);
});
