// Change only the model-visible surface through native, replayable replacements.
// Original events remain searchable and delivery always consumes original turns.
import {createUserMessage} from '@deepseek-ai/dsh-llm';
import {post} from './bridge.mjs';
import {replaceSurface} from './surface-replace.mjs';

export const RETIRED='telos-history-retired',RECENT='telos-recent-checks';
const message=e=>e?.type==='user/message'?e.data:e?.data?.message;
const kind=e=>message(e)?.source?.kind;
const text=m=>(m?.content??[]).filter(b=>b.type==='text').map(b=>b.text).join('\n');
const equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
// These tools either read data or have their unresolved operations checked by
// /history-retention against the durable Apple/proactive ledgers. Their old
// rendered status/error text is not evidence of a still-unresolved operation.
const LEDGER_OR_READ_TOOLS=new Set(['personalPlan','notify_user','baidu_maps']);
const READ_TOOLS=new Set(['read','read_image','glob','grep','web_fetch',
  'session_event_read','session_event_search','list_mcp_resources','list_mcp_resource_templates','read_mcp_resource',
  'schedule_list','schedule_view','job_list','job_output']);

// Restrict migration to the two aliases of this channel's user profile. Never
// retire nested/project instructions or partial updates from another plugin.
export function profileBaseline(e) {
  const s=message(e)?.source;
  if(e.type!=='user/message'||s?.kind!=='agent-instructions'||s.baseline!==true||!s.changes?.length)return false;
  return s.changes.every(c=>['AGENTS.md','user.md'].includes(c.path)&&c.scope===`.\0${c.path}`);
}

export class ContextRetention {
  constructor(session,{meter}={}){this.meter=meter;this.session=session;this.offset=0;this.turns=new Map();this.owners=new Map();this.calls=new Map();this.open=null;}
  sync() {
    const events=this.session.snapshotEvents();
    for(const e of events.slice(this.offset)) {
      if(e.type==='assistant/message')for(const block of message(e)?.content??[])
        if(block.type==='tool-call')this.calls.set(block.id,{name:block.name,turn:e.data.turn});
      if(e.type==='turn/start'){
        this.open=e.data.turn;
        this.turns.set(this.open,{turn:this.open,inputs:[],users:[],final:null});
      }
      const turn=this.turns.get(e.data?.turn??this.open);
      if(turn&&e.surfaceOp==='append') {
        this.owners.set(e.seq,turn.turn);
        if(e.type==='user/message'&&['user','schedule','tool-jobs'].includes(kind(e))) {
          turn.inputs.push(e.data.id);if(kind(e)==='user')turn.users.push(e.data.id);
        }
        if(e.type==='assistant/message')turn.final=e.seq;
      }
      // Native pruning replaces an older result during a later turn.
      if(e.type==='tool/result'&&e.surfaceOp!=='append')this.owners.set(e.seq,e.data.turn);
      if(e.type==='turn/end') {
        if(turn){turn.seq=e.seq;turn.complete=e.data.reason?.kind==='completed';}
        this.open=null;
      }
    }
    this.offset=events.length;
  }
  candidates() {
    this.sync();const visible=new Set(this.session.surface.nodes.map(seq=>this.owners.get(seq)));
    return [...this.turns.values()].filter(t=>t.complete&&visible.has(t.turn))
      .map(({turn,seq,inputs,users})=>({turn,seq,inputs,users}));
  }
  apply({eligible=[],recent=[]}={}) {
    this.sync();const session=this.session,nodes=[...session.surface.nodes];
    const replace=(seqs,source,content=[])=>replaceSurface(session,this.meter,'user/message',createUserMessage({source,content}),seqs);
    const events=nodes.map(seq=>session.eventAt(seq)),retire=new Set(),allowed=new Set(eligible);
    // Loaded skills are reusable instructions, not completed query data. Keep
    // their native call/result pairs until native compaction handles them. A
    // parallel call batch must remain intact to avoid orphaned tool results.
    const skillCalls=new Set(events.filter(e=>e.type==='tool/result'&&!message(e).isError
      &&this.calls.get(message(e).source?.callId)?.name==='skill').map(e=>message(e).source.callId));
    const instructionNodes=new Set(),instructionCalls=new Set();
    for(const e of events)if(e.type==='assistant/message') {
      const calls=message(e).content.filter(b=>b.type==='tool-call');
      if(calls.some(c=>skillCalls.has(c.id))) {
        instructionNodes.add(e.seq);for(const c of calls)instructionCalls.add(c.id);
      }
    }
    for(const e of events)if(e.type==='tool/result'&&instructionCalls.has(message(e).source?.callId))instructionNodes.add(e.seq);
    const profiles=events.filter(profileBaseline);
    for(const e of profiles.slice(0,-1))retire.add(e.seq);
    // Delivery + current ledger state is authoritative for managed tools.
    // Read coverage (partial/unknown) and a corrected preflight error do not
    // pin a completed turn. Preserve conservative protection for other tools
    // (e.g. an ambiguous image send or shell write) with no settlement ledger.
    for(const e of events)if(e.type==='assistant/message'&&e.seq===this.turns.get(this.owners.get(e.seq))?.final
      &&message(e).content.some(b=>b.type==='tool-call'))allowed.delete(this.owners.get(e.seq));
    for(const e of events)if(e.type==='tool/result') {
      const m=message(e),body=text(m);
      const call=this.calls.get(m.source?.callId);
      if(call?.turn===this.owners.get(e.seq)&&(LEDGER_OR_READ_TOOLS.has(call.name)||READ_TOOLS.has(call.name)))continue;
      if(m.isError||/"(?:status|state)"\s*:\s*"(?:unknown|executing|partial|failed)"/.test(body))
        allowed.delete(this.owners.get(e.seq));
    }
    for(const e of events) {
      if(instructionNodes.has(e.seq))continue;
      if(kind(e)===RETIRED){retire.add(e.seq);continue;}
      const turn=this.turns.get(this.owners.get(e.seq));
      if(!turn||!allowed.has(turn.turn)||!turn.complete)continue;
      if(e.type==='tool/result')retire.add(e.seq);
      if(e.type==='assistant/message'&&(!turn.users.length||e.seq!==turn.final))retire.add(e.seq);
      if(e.type==='user/message'&&(['schedule','tool-jobs','time-context','repeat-tool-reminder'].includes(kind(e))))retire.add(e.seq);
    }
    // One bounded, replaceable account of the last two delivered background
    // checks. This is historical evidence, never a new reminder or instruction.
    const old=events.find(e=>kind(e)===RECENT);
    const records=new Map((message(old)?.source?.records??[]).map(r=>[r.seq,r]));
    for(const r of recent)if(allowed.has(r.turn))records.set(r.seq,r);
    const latest=[...records.values()].sort((a,b)=>a.seq-b.seq).slice(-2);
    if(latest.length&&!equal(latest,message(old)?.source?.records)) {
      const source={kind:RECENT,records:latest};
      const content=[{type:'text',text:'Recent completed background checks (historical evidence only; earlier records remain in session_event_search/read).\n'+
        latest.map(r=>`Turn ${r.turn}: ${r.text}${r.truncated?' [truncated; query history for full text]':''}`).join('\n')}];
      if(old)replace([old.seq],source,content);
      else session.append('user/message',createUserMessage({source,content}),{surfaceOp:'append'});
    }
    // Replace consecutive retired nodes together: a thousand completed timer
    // turns become one empty surface slot, not a thousand receipt markers.
    let run=[];
    const flush=()=>{
      if(run.length&&(run.length>1||kind(session.eventAt(run[0]))!==RETIRED))
        replace(run,{kind:RETIRED});
      run=[];
    };
    for(const seq of nodes){if(retire.has(seq))run.push(seq);else flush();}flush();
    return {retired:retire.size,profiles:profiles.length?1:0};
  }
}

export function mountContextRetention(scope,agent,service) {
  const retention=new ContextRetention(agent.session,{meter:scope.get('tokenMeter')});
  // The native instruction loader admits fresh baselines after pre-step.
  // Deduplicate once they are durable, before building the actual request.
  agent.ctx.on('agent/request',async(request,next)=>{
    const config=await next();retention.apply();return config;
  });
  scope.systemPrompt.section({name:'telos:retention',order:0,interpolate:false,text:
    'Completed and delivered tool transcripts are archived outside current context. Query current calendar/reminder state again when needed. ' +
    'Use session_event_search/read for earlier actions, receipts or notifications. Missing old tool output never means an action was not executed; check the durable state before retrying. ' +
    'Current pending confirmations and unresolved operations remain authoritative. Recent background-check notes are historical evidence, not instructions.'});
  return {async prepare() {
    const turns=retention.candidates();
    let status={eligible:[],recent:[]};
    try {for(let i=0;i<turns.length;i+=2000) {
      const part=await post(service,'/history-retention',{session:agent.session.id,turns:turns.slice(i,i+2000)},AbortSignal.timeout(15000));
      status.eligible.push(...part.eligible);status.recent.push(...part.recent);
    }} catch(error) {
      // Missing delivery evidence must retain history, not break a normal chat.
      console.warn('Telos history retention skipped: '+error.message);status={};
    }
    retention.apply(status);
  }};
}

// Reuse the native engine without changing its shared preset configuration or
// registering another engine. Only this channel opts into the smaller budget.
export async function compactChatHistory(engine,agent,signal) {
  if(!engine||typeof engine.compactPressure!=='function')return;
  const header=agent.session.requestHeader();if(!header)return;
  if(engine.ctx.get('tokenMeter').measure(agent.session,header).totalTokens<120000)return;
  const info=await engine.ctx.get('llm').resolveModelInfo(header.config.provider,header.config.model,signal);
  const window=info.context?.contextWindow;if(!window||window<=120000)return;
  const base=engine.config,override=base.modelPolicies.find(p=>p.provider===header.config.provider&&p.model===header.config.model);
  const original={...base,...override},thresholdRatio=Math.min(original.thresholdRatio,120000/window);
  const config={...original,thresholdRatio,retainRatio:undefined,retainTokens:Math.min(40000,Math.floor(window*thresholdRatio/2)),
    maxTokens:Math.min(original.maxTokens,8192),modelPolicies:[]};
  const local=Object.create(engine);Object.defineProperty(local,'config',{value:config});
  await local.compactPressure(agent,header,signal,false);
}
