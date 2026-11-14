import test from 'node:test';
import assert from 'node:assert/strict';
import {businessContextView,proactiveContextView,proactiveInput,stableContext} from '../plugin/runtime-context.mjs';

test('time-only refreshes are stable; confirmations and receipts remain actionable',()=>{
  const state={direct_write:false,time:'before',pending:{id:'draft',expires_at:100,text:'请确认'},receipt:{text:'已保存',operation_id:'op'}};
  const before=structuredClone(state);
  assert.equal(stableContext(businessContextView(state)),stableContext(businessContextView({...state,time:'after'})));
  assert.deepEqual(businessContextView(state).pending,state.pending);
  assert.deepEqual(businessContextView(state).receipt,state.receipt);
  assert.notEqual(stableContext(businessContextView(state)),stableContext(businessContextView({...state,pending:null})));
  assert.deepEqual(state,before);
  assert.equal(stableContext({b:2,a:{d:4,c:3}}),stableContext({a:{c:3,d:4},b:2}));
});

test('current task instructions preserve scheduler identity and do not touch human or unrelated inputs',()=>{
  const event={input_id:'owned',kind:'heartbeat',due:1,expires:2,prompt:'读取本轮目标'};
  const input={id:'owned',source:{kind:'schedule'},content:[{type:'text',text:'native schedule'}]};
  const copy=structuredClone(input),result=proactiveInput(input,event);
  assert.equal(result.id,input.id);assert.equal(result.source,input.source);
  assert.deepEqual(result.content[0],input.content[0]);assert.deepEqual(input,copy);
  assert.ok(result.content[1].text.includes(event.prompt));
  for(const other of [{...input,id:'unrelated'},{...input,source:{kind:'user'}}])
    assert.equal(proactiveInput(other,event),other);
  assert.equal(proactiveInput(input,null),input);
});

test('proactive view keeps deadlines, preferences, delivery state and custom reasons; handler retains identity',()=>{
  const context={now:'first',events:[{id:'event',input_id:'input',task_id:'native',revision:2,session:'chat',created:1,
    kind:'heartbeat',due:100,expires:200,state:'running',prompt:'看看是否需要联系'}],
    settings:{enabled:true,timezone:'Asia/Shanghai',paused_until:150,preferences:'看看是否需要联系'},recent:[{kind:'heartbeat',due:90,state:'sent',message:'已经通知'}]};
  const saved=structuredClone(context),view=proactiveContextView(context);
  assert.equal(stableContext(view),stableContext(proactiveContextView({...context,now:'later'})));
  assert.deepEqual(view.events,[{kind:'heartbeat',due:100,expires:200,state:'running',prompt:'看看是否需要联系',notification_required:true}]);
  assert.deepEqual(view.settings,context.settings);assert.deepEqual(view.recent,context.recent);
  for(const kind of ['fixed','followup','heartbeat']) {
    const changed=proactiveContextView({...context,events:[{...context.events[0],kind,prompt:'独立要求',state:'queued',message:'已入队'}]});
    assert.equal(changed.events[0].prompt,'独立要求');assert.equal(changed.events[0].state,'queued');assert.equal(changed.events[0].message,'已入队');
    assert.equal(changed.events[0].notification_required,kind!=='followup');
  }
  assert.deepEqual(context,saved);
});

test('current check includes recovery arguments only when supplied by the active policy',()=>{
 const event={input_id:'owned',kind:'heartbeat',due:1,expires:2,prompt:'check'};
 const input={id:'owned',source:{kind:'schedule'},content:[]};
 const activity={next_call:{name:'activity_history',arguments:{view:'summary',start:'2026-11-07T22:00:37+08:00',end:'2026-11-09T09:00:00+08:00'}}};
 assert.ok(proactiveInput(input,event,activity).content[0].text.includes(stableContext(activity)));
 assert.ok(!proactiveInput(input,event).content[0].text.includes('activity_check'));
});
