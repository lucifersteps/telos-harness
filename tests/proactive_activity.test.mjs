import test from 'node:test';
import assert from 'node:assert/strict';
import {createActivityCheck} from '../plugin/proactive-activity.mjs';

const now=Date.parse('2026-11-06T12:00:00+08:00');
const query={view:'timeline',start:'2026-11-06T11:00:00+08:00',end:'2026-11-06T12:00:00+08:00'};
const event={id:'check-1',input_id:'input-1'};
const exec={callId:'read-1',signal:new AbortController().signal};
const blocked=gate=>assert.throws(()=>gate.assertRead(),/TELOS_ACTIVITY_REQUIRED/);

test('send waits for a real completed query; same-event tool continuations retain it',async()=>{
  const gate=createActivityCheck(()=>now);gate.bind(event);blocked(gate);
  let complete;
  const pending=gate.execute(query,exec,()=>new Promise(resolve=>complete=resolve));
  blocked(gate);complete({status:'ok'});await pending;
  blocked(gate); // Same model response cannot send a prewritten notification.
  gate.bind({...event});gate.assertRead();
  gate.bind({...event,input_id:'other-input'});blocked(gate);
});

test('status, calendar-only and invalid ranges do not unlock a notification',async()=>{
  for(const args of [{view:'status'}, {...query,view:'status'}, {...query,sources:['calendar']},
    {...query,sources:[]}, {...query,sources:['unknown']}, {...query,view:'invalid'},
    {...query,start:'2026-11-06T11:00:00'}, {...query,end:query.start},
    {...query,start:'2026-01-01T00:00:00Z'}, {...query,start:'2026-12-01T00:00:00Z',end:'2026-12-02T00:00:00Z'}]) {
    const gate=createActivityCheck(()=>now);gate.bind(event);
    await gate.execute(args,exec,async()=>({status:'ok'}));blocked(gate);
  }
});

test('source failures, partial results and transport exceptions permit degradation',async()=>{
  for(const result of [{status:'ok'}, {status:'partial',sources_status:[{state:'unavailable'}]},
    {status:'failed'}, {is_error:true}, new Error('source offline')]) {
    const gate=createActivityCheck(()=>now);gate.bind(event);
    const read=gate.execute({...query,sources:['screen_time']},exec,async()=>{
      if(result instanceof Error)throw result;
      return result;
    });
    if(result instanceof Error)await assert.rejects(read,/source offline/);
    else assert.equal(await read,result);
    gate.bind(event);gate.assertRead();
  }
});

test('human, next event, turn end, restart and cancellation cannot reuse a read',async()=>{
  for(const transition of [gate=>gate.reset(),gate=>gate.bind(null),gate=>gate.bind({id:'check-2',input_id:'input-2'}),
    gate=>{gate.reset();gate.bind(event);}]) {
    const gate=createActivityCheck(()=>now);gate.bind(event);
    let complete;
    const pending=gate.execute(query,exec,()=>new Promise(resolve=>complete=resolve));
    transition(gate);complete({status:'ok'});await pending;blocked(gate);
  }
  const gate=createActivityCheck(()=>now);gate.bind(event);
  await gate.execute(query,exec,async()=>({status:'ok'}));gate.bind(event);gate.assertRead();
  gate.reset();gate.bind(event);blocked(gate);
  const restarted=createActivityCheck(()=>now);restarted.bind(event);blocked(restarted);
  const abort=new AbortController();
  await gate.execute(query,{...exec,signal:abort.signal},async()=>{abort.abort();return {};});blocked(gate);
});

test('ordinary chat reads do not unlock the next proactive check',async()=>{
  const gate=createActivityCheck(()=>now);
  await gate.execute(query,exec,async()=>({status:'ok'}));
  gate.bind(event);blocked(gate);
});

test('observation collects all sources; health-only and arbitrary supplements retain completed evidence',async()=>{
 const gate=createActivityCheck(()=>now);gate.bind(event,query);
 await gate.execute({...query,sources:['aw','health']},exec,async()=>({status:'partial'}));
 gate.bind(event,query);blocked(gate);
 await gate.execute({...query,sources:['screen_time']},exec,async()=>({status:'ok'}));
 gate.bind(event,query);gate.assertRead();
 await gate.execute({...query,sources:['health']},exec,async()=>({status:'ok'}));
 gate.bind(event,query);gate.assertRead();
 const only=createActivityCheck(()=>now);only.bind(event);
 await only.execute({...query,sources:['health']},exec,async()=>({status:'ok'}));only.bind(event);only.assertRead();
});

test('observation requires the entire trusted interval, including failed source attempts',async()=>{
 const window={start:'2026-11-06T09:00:00+08:00',end:query.end};
 const gate=createActivityCheck(()=>now);gate.bind(event,window);
 await gate.execute(query,exec,async()=>({status:'ok'}));gate.bind(event,window);blocked(gate);
 await gate.execute({...query,...window,sources:['aw','screen_time','health']},exec,async()=>({status:'failed'}));
 gate.bind(event,window);gate.assertRead();
});


test('each source must cover the full window and be reviewed before send',async()=>{
 const window={start:'2026-11-06T09:00:00+08:00',end:query.end};
 const gate=createActivityCheck(()=>now);gate.bind(event,window);
 for(const source of ['aw','screen_time'])await gate.execute({...query,...window,sources:[source]},exec,async()=>({status:'ok'}));
 gate.bind(event,window);blocked(gate);
 await gate.execute({...query,sources:['health']},exec,async()=>({status:'ok'}));
 gate.bind(event,window);blocked(gate); // Health's shorter range does not count.
 await gate.execute({...query,...window,sources:['health']},exec,async()=>({status:'partial'}));
 blocked(gate);gate.bind(event,window);gate.assertRead();
 // A new pending/rejected supplement cannot erase already-reviewed evidence.
 let finish;
 const supplement=gate.execute({...query,...window,sources:['aw']},exec,()=>new Promise(resolve=>finish=resolve));
 gate.assertRead();finish({status:'failed'});await supplement;gate.assertRead();
 await gate.execute({view:'detail',event_id:'health:sleep:test'},exec,async()=>({status:'ok'}));gate.assertRead();
});

test('parallel observation reads tolerate failures but not unfinished or cancelled sources',async()=>{
 const gate=createActivityCheck(()=>now);gate.bind(event,query);
 let finish;
 const pending=gate.execute({...query,sources:['health']},exec,()=>new Promise(resolve=>finish=resolve));
 await Promise.all(['aw','screen_time'].map(source=>gate.execute({...query,sources:[source]},exec,async()=>({status:'partial'}))));
 gate.bind(event,query);blocked(gate);
 finish({status:'failed'});await pending;blocked(gate);
 gate.bind(event,query);gate.assertRead();
 gate.reset();gate.bind(event,query);
 await gate.execute({...query,sources:['aw','screen_time']},exec,async()=>({status:'ok'}));
 const abort=new AbortController();
 await gate.execute({...query,sources:['health']},{...exec,signal:abort.signal},async()=>{abort.abort();return {};});
 gate.bind(event,query);blocked(gate);
 await assert.rejects(gate.execute({...query,sources:['health']},exec,async()=>{throw new Error('offline');}),/offline/);
 gate.bind(event,query);gate.assertRead();
});

test('missed deliveries expose an exact recovery query instead of repeating a short window',async()=>{
 const window={start:'2026-11-07T22:00:37.574435+08:00',end:'2026-11-09T09:00:00.007256+08:00'};
 const gate=createActivityCheck(()=>Date.parse(window.end)+1000);gate.bind(event,window);
 const short={view:'summary',start:'2026-11-09T08:00:00+08:00',end:'2026-11-09T09:00:00+08:00'};
 const facts={status:'partial',activities:[{title:'existing observation'}]};
 const result=await gate.execute(short,exec,async()=>facts);
 assert.deepEqual(result.activities,facts.activities);
 assert.equal(facts.proactive_observation,undefined);
 const recovery=result.proactive_observation.next_call;
 assert.deepEqual(recovery,{name:'activity_history',arguments:{view:'summary',...window,sources:['aw','screen_time','health']}});
 gate.bind(event,window);
 assert.throws(()=>gate.assertRead(),error=>error.message.includes(JSON.stringify(recovery)));
 const checked=await gate.execute(recovery.arguments,exec,async()=>({status:'failed'}));
 assert.equal(checked.proactive_observation.state,'awaiting_review');blocked(gate);
 gate.bind(event,window);gate.assertRead();assert.equal(gate.status().state,'ready');
 gate.reset();assert.equal(gate.status(),null);
});

test('recovery requests only uncovered sources and does not bleed into ordinary chat',async()=>{
 const gate=createActivityCheck(()=>now);gate.bind(event,query);
 await gate.execute({...query,sources:['aw']},exec,async()=>({status:'partial'}));
 gate.bind(event,query);
 assert.deepEqual(gate.status().next_call.arguments.sources,['screen_time','health']);
 gate.bind(null);
 const result={status:'ok'};
 assert.equal(await gate.execute(query,exec,async()=>result),result);
});
