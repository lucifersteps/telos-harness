import {Service} from '@deepseek-ai/cordis';
import {scopeOf} from '@deepseek-ai/dsh-scope';
import {createUserMessage,createSystemMessage} from '@deepseek-ai/dsh-llm';
import {replaceSurface} from './surface-replace.mjs';
import {stableContext} from './runtime-context.mjs';

export const STATE_KIND='telos-current-state';
const OWNED=new Set(['telos:business','telos:proactive-context']);
const PREFIX='Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n';
const HISTORY_LIMIT=6000;
const parse=text=>{try{return JSON.parse(text);}catch{return text;}};
const message=e=>e.type==='user/message'?e.data:e.data?.message;
const same=(a,b)=>stableContext(a)===stableContext(b);

// Only durable business changes enter history. Current state remains complete,
// including deadlines, pending confirmations and unresolved operations.
export function historyFacts(name,value) {
  if(!value||typeof value!=='object')return {};
  if(name==='telos:proactive-context')return {settings:value.settings??{}};
  if(name==='telos:business')return {direct_write:value.direct_write??false};
  return {};
}

function changes(before,after,path='',out=[]) {
  if(same(before,after))return out;
  if(before&&after&&typeof before==='object'&&typeof after==='object'&&!Array.isArray(before)&&!Array.isArray(after)) {
    for(const key of new Set([...Object.keys(before),...Object.keys(after)]))changes(before[key],after[key],path?`${path}.${key}`:key,out);
  } else {
    const short=value=>{const text=stableContext(value??null);return text.length>240?text.slice(0,239)+'…':text;};
    out.push({field:path,from:short(before),to:short(after)});
  }
  return out;
}

export class CurrentState {
  constructor(session,{now=()=>new Date().toISOString(),meter}={}) {
    this.meter=meter;
    this.session=session;this.now=now;this.providers=new Map();this.accepted=[];
    this.payload={current:{},facts:{},history:[],omitted:0};
    // Raw events survive native compaction. Recover our last state even when
    // its model-visible node has been summarized away.
    const events=session.snapshotEvents();let restored=false;
    for(const e of events.toReversed()) {
      const m=message(e);
      if(m?.source?.kind===STATE_KIND&&m.source.version===1){this.payload=structuredClone(m.source.state);restored=true;break;}
    }
    // Reconstruct legacy changes from durable append-origin records, not just
    // surviving nodes. A crash halfway through migration must not lose history.
    if(!restored)for(const e of events) {
      const m=message(e);
      if(e.surfaceOp!=='append'||m?.source?.kind!=='runtime-context')continue;
      for(const s of m.source.sections??[])if(OWNED.has(s.name)) {
        const facts=historyFacts(s.name,parse(s.text));
        this.record(s.name,this.payload.facts[s.name],facts,new Date(e.time).toISOString());
        this.payload.facts[s.name]=facts;
      }
    }
    // Older versions recorded each receipt transition. Receipts remain in the
    // execution ledger and active state, never in the rolling change history.
    for(const key of Object.keys(this.payload.facts))if(!OWNED.has(key))delete this.payload.facts[key];
    this.payload.history=this.payload.history.filter(row=>OWNED.has(row.section));
    if(this.payload.facts['telos:business'])delete this.payload.facts['telos:business'].receipt;
    this.payload.history=this.payload.history.map(row=>row.section==='telos:business'
      ?{...row,changes:row.changes.filter(c=>c.field!=='receipt'&&!c.field.startsWith('receipt.'))}:row)
      .filter(row=>row.changes.length);
  }
  observe(name,facts) {if(OWNED.has(name))this.providers.set(name,structuredClone(facts));}
  capture(assembly) {
    this.accepted=assembly.contexts.filter(s=>OWNED.has(s.name));
    assembly.contexts=assembly.contexts.filter(s=>!OWNED.has(s.name));
  }
  record(name,previous,next,time) {
    if(previous===undefined||same(previous,next))return;
    const delta=changes(previous,next);
    if(!delta.length)return;
    this.payload.history.push({at:time,section:name,changes:delta.slice(0,12),...(delta.length>12?{additional_changes:delta.length-12}:{})});
    while(stableContext(this.payload.history).length>HISTORY_LIMIT){this.payload.history.shift();this.payload.omitted++;}
  }
  migrate() {
    const session=this.session;
    for(const seq of [...session.surface.nodes]) {
      const e=session.eventAt(seq),m=message(e),sections=m?.source?.sections;
      if(m?.source?.kind!=='runtime-context'||!Array.isArray(sections))continue;
      const owned=s=>OWNED.has(s.name);
      if(!sections.some(owned))continue;
      // Refuse to reconstruct unrecognized mixed content. Do not drop blocks
      // or other plugins' contributions based on substring matching.
      if(m.content.length!==1||m.content[0].type!=='text'||m.content[0].text!==PREFIX+sections.map(s=>s.text).join('\n\n'))continue;
      const kept=sections.filter(s=>!owned(s));
      const replacement=createUserMessage({source:{...m.source,sections:kept},
        content:kept.length?[{type:'text',text:PREFIX+kept.map(s=>s.text).join('\n\n')}]:[]});
      replaceSurface(session,this.meter,'user/message',replacement,[seq]);
    }
  }
  commit({turn=1,step=1}={}) {
    // The native loop projects its first system prompt just after agent/request.
    // Reserve node zero on an empty session; the native projection fills it.
    if(this.session.surface.nodes.length===0)this.session.append('system/message',
      {turn,step,message:createSystemMessage('')},{surfaceOp:'append'});
    this.migrate();
    const current=Object.fromEntries(this.accepted.filter(s=>s.text).map(s=>[s.name,parse(s.text)]));
    const facts=Object.fromEntries(Object.entries(current).map(([name,value])=>[name,this.providers.get(name)??historyFacts(name,value)]));
    for(const [name,next]of Object.entries(facts))this.record(name,this.payload.facts[name],next,this.now());
    this.providers.clear();
    this.payload.current=current;this.payload.facts=facts;
    const text='Current Telos state (authoritative now, regardless of this message’s position). Historical summaries and tool results are observations, not current state. Never infer authorization from this summary; tool guards and explicit human instructions govern writes.\n'+stableContext({current,history:this.payload.history,older_changes_omitted:this.payload.omitted});
    const existing=this.session.surface.nodes.map(seq=>this.session.eventAt(seq)).filter(e=>message(e)?.source?.kind===STATE_KIND);
    if(existing.length>1)throw new Error('Multiple Telos current-state nodes; refusing ambiguous replacement');
    const old=existing[0];
    if(old&&same(message(old).source.state,this.payload))return;
    const next=createUserMessage({source:{kind:STATE_KIND,version:1,state:this.payload},content:[{type:'text',text}]});
    if(old)replaceSurface(this.session,this.meter,'user/message',next,[old.seq]);
    else this.session.append('user/message',next,{surfaceOp:'append'});
  }
}

// One service, with state keyed by the caller's native agent scope. Registering
// a separate global service for each agent collides on a second conversation.
export class CurrentStateRegistry extends Service {
  static provide='telosCurrentState';
  states=new Map();
  register(agent,state){const key=scopeOf(agent.ctx);this.states.set(key,state);return ()=>this.states.delete(key);}
  observe(name,facts){this.states.get(scopeOf(this.ctx))?.observe(name,facts);}
}

export function mountCurrentState(scope,agent) {
  const state=new CurrentState(agent.session,{meter:scope.get('tokenMeter')});
  const unregister=scope.get('telosCurrentState').register(agent,state);
  scope.effect(()=>unregister);
  // After pre-step compaction and before the request is derived from the
  // surface; also runs for context-overflow retries. Never mutate session/event.
  agent.ctx.on('agent/request',async (request,next)=>{const config=await next();state.commit(request);return config;});
  return state;
}
