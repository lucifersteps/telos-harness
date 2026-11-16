import test from 'node:test';
import assert from 'node:assert/strict';
import {Session} from '@deepseek-ai/dsh-session';
import {createUserMessage,createSystemMessage} from '@deepseek-ai/dsh-llm';
import {CurrentState,STATE_KIND,historyFacts} from '../plugin/current-state.mjs';
import {stableContext} from '../plugin/runtime-context.mjs';

const native=()=>{const s=Session.create('state-test');s.append('system/message',{turn:0,step:0,message:createSystemMessage('Rules')},{surfaceOp:'append'});return s;};
const user=(s,text)=>s.append('user/message',createUserMessage({source:{kind:'user'},content:[{type:'text',text}]}),{surfaceOp:'append'});
const nodes=s=>s.surface.nodes.map(seq=>s.eventAt(seq)).filter(e=>e.data?.source?.kind===STATE_KIND);
function update(state,value,extra=[]) {
  const a={contexts:[{name:'telos:business',text:stableContext(value)},...extra],sections:[{name:'habit-coach:method',text:'Fixed method'}]};
  state.capture(a);state.commit();return a;
}

test('one replaceable state, unchanged continuations do not write, human history remains',()=>{
  const s=native(),state=new CurrentState(s);
  update(state,{pending:{id:'p1',expires:123},receipt:null});const old=nodes(s)[0];
  const human=user(s,'确认');
  update(state,{pending:null,receipt:{text:'已保存',id:'r1'}});
  assert.equal(nodes(s).length,1);assert.ok(!s.surface.nodes.includes(old.seq));assert.ok(s.eventAt(old.seq));
  assert.ok(s.surface.nodes.includes(human.seq));
  assert.equal(nodes(s)[0].data.source.state.history.length,0);
  const count=s.snapshotEvents().length;
  update(state,{pending:null,receipt:{text:'已保存',id:'r1'}});assert.equal(s.snapshotEvents().length,count);
});

test('restart and a native-style compaction both recover current state and bounded history',()=>{
  let s=native(),state=new CurrentState(s);
  update(state,{direct_write:false});update(state,{direct_write:true});
  s=Session.create('state-test',JSON.parse(JSON.stringify(s.snapshotEvents())));state=new CurrentState(s);
  const count=s.snapshotEvents().length;update(state,{direct_write:true});assert.equal(s.snapshotEvents().length,count);
  const old=nodes(s)[0];s.append('user/message',createUserMessage({source:{kind:'compact-checkpoint'},content:[{type:'text',text:'Conversation summary'}]}),
    {surfaceOp:{op:'replace',startSeq:old.seq,endSeq:old.seq},sourceEventSeqs:[old.seq]});
  state=new CurrentState(Session.create('state-test',JSON.parse(JSON.stringify(s.snapshotEvents()))));s=state.session;
  update(state,{direct_write:true});assert.equal(nodes(s).length,1);assert.equal(nodes(s)[0].data.source.state.history.length,1);
  for(let i=0;i<100;i++)update(state,{direct_write:i%2===0});
  assert.ok(stableContext(nodes(s)[0].data.source.state.history).length<=6000);
  assert.ok(nodes(s)[0].data.source.state.omitted>0);
});

test('legacy migration preserves other plugins, user input, and source attribution',()=>{
  const s=native();
  const runtime=sections=>s.append('user/message',createUserMessage({source:{kind:'runtime-context',form:'snapshot',sections},
    content:[{type:'text',text:'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n'+sections.map(p=>p.text).join('\n\n')}]}),{surfaceOp:'append'});
  const first=runtime([{name:'sandbox:policy',text:'Keep sandbox policy'},{name:'telos:business',text:'{"receipt":null}'},{name:'habit-coach:method',text:'Fixed method'}]);
  const human=user(s,'Keep this human message');
  runtime([{name:'sandbox:policy',text:'Keep sandbox policy'},{name:'telos:business',text:'{"receipt":{"text":"done"}}'}]);
  const state=new CurrentState(s);update(state,{receipt:{text:'done'}});
  assert.ok(s.surface.nodes.includes(human.seq));assert.ok(!s.surface.nodes.includes(first.seq));
  const legacy=s.surface.nodes.map(seq=>s.eventAt(seq)).filter(e=>e.data?.source?.kind==='runtime-context');
  assert.equal(legacy.length,2);assert.deepEqual(legacy.map(e=>e.data.source.sections.length),[2,1]);for(const e of legacy){assert.equal(e.data.source.sections[0].name,'sandbox:policy');assert.ok(e.sourceEventSeqs.length);}
  assert.equal(nodes(s)[0].data.source.state.history.length,0);
});

test('unrecognized legacy blocks are not destructively reconstructed; habit switches are not plan changes',()=>{
  const s=native();const old=s.append('user/message',createUserMessage({source:{kind:'runtime-context',sections:[{name:'telos:business',text:'{}'}]},content:[{type:'text',text:'Unrecognized content'}]}),{surfaceOp:'append'});
  const state=new CurrentState(s);
  const habit=id=>[{name:'habit-coach:current',text:stableContext({habit:{id,name:id,lifecycle:'active'},plan:{accepted:true,plan:{cue:'morning'}}})}];
  update(state,{},habit('a'));update(state,{},habit('b'));
  assert.ok(s.surface.nodes.includes(old.seq));assert.equal(nodes(s)[0].data.source.state.history.length,0);
  const a=update(state,{pending:{id:'unresolved',expires:44}},[{name:'external:context',text:'untouched'}]);
  assert.deepEqual(a.contexts,[{name:'external:context',text:'untouched'}]);
  assert.equal(nodes(s)[0].data.source.state.current['telos:business'].pending.id,'unresolved');
});

test('interrupted legacy migration reconstructs changes from original log records',()=>{
  let s=native();
  for(const text of ['first','second']) {
    const section={name:'telos:business',text:stableContext({direct_write:text==='second'})};
    s.append('user/message',createUserMessage({source:{kind:'runtime-context',sections:[section]},content:[{type:'text',text:'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n'+section.text}]}),{surfaceOp:'append'});
  }
  const first=new CurrentState(s);first.migrate(); // Simulate termination before current-state commit.
  s=Session.create('state-test',JSON.parse(JSON.stringify(s.snapshotEvents())));
  const recovered=new CurrentState(s);update(recovered,{direct_write:true});
  assert.equal(nodes(s)[0].data.source.state.history.length,1);
  assert.equal(nodes(s)[0].data.source.state.history[0].changes[0].to,'true');
});

test('legacy diagnostic metadata is not reported as a meaningful plan change',()=>{
  const before={habit:{id:'h',name:'reading',lifecycle:'active'},plan:{version:1,accepted:true,plan:{cue:'morning'},source_event_id:'old',created_at:123}};
  const after={...before,plan:{version:1,accepted:true,plan:{cue:'morning'}}};
  assert.deepEqual(historyFacts('habit-coach:current',before),historyFacts('habit-coach:current',after));
});

test('current-state observers route to the caller agent, never another conversation',async()=>{
  const {Context}=await import('@deepseek-ai/cordis');
  const {createScope}=await import('@deepseek-ai/dsh-scope');
  const {CurrentStateRegistry}=await import('../plugin/current-state.mjs');
  const root=new Context();const provider=await root.plugin(CurrentStateRegistry);
  const a=createScope(root,{}),b=createScope(root,{}),seenA=[],seenB=[];
  const registry=root.get('telosCurrentState');
  const offA=registry.register(a,{observe:(...v)=>seenA.push(v)});
  const offB=registry.register(b,{observe:(...v)=>seenB.push(v)});
  a.ctx.get('telosCurrentState').observe('habit-coach:current',{id:'a'});
  b.ctx.get('telosCurrentState').observe('habit-coach:current',{id:'b'});
  assert.deepEqual(seenA,[['habit-coach:current',{id:'a'}]]);
  assert.deepEqual(seenB,[['habit-coach:current',{id:'b'}]]);
  offA();offB();await a.dispose();await b.dispose();await provider.dispose();
});
