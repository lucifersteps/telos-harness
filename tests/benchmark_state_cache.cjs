// Explicit live benchmark: synthetic data, no business tools or message delivery.
// Uses the installed native provider and its account-token validation; secrets
// are read only, stay in memory, and are never included in the report.
const fs=require('node:fs');
const {createRequire}=require('node:module');
const root=process.env.DSH_NATIVE_ROOT;
if(!root||process.env.TELOS_LIVE_CACHE_TEST!=='1')throw new Error('Explicit native root and live opt-in required');
const native=p=>require(root+'/'+p+'/lib/index.js');
const {Context}=native('cordis');
const {Session}=native('dsh-session');
const {createSystemMessage,createUserMessage}=native('dsh-llm');
const {CurrentState}=require('../plugin/current-state.mjs');
const {stableContext}=require('../plugin/runtime-context.mjs');
const yaml=createRequire(root+'/dsh-credentials-local/lib/index.js')('yaml');
const document=yaml.parse(fs.readFileSync(require('node:path').join(process.env.DSH_HOME??require('node:path').join(require('node:os').homedir(),'.dsh'),'.credentials.yaml'),'utf8'));
const platform=native('dsh-deepseek-account-platform').default;
const account={closed:false,inferenceOrigin:'https://api.deepseek.com',origin:'https://platform.deepseek.com',
  ctx:{credentials:{readRecord:async key=>document.records?.[key]}}};
const ctx=new Context(),fibers=[];
const add=async(p,c)=>{const f=ctx.plugin(p,c);fibers.push(f);await f;};
const report=[];
function build(replace) {
  const s=Session.create('synthetic-state-cache-'+(replace?'replace':'append'));
  s.append('system/message',{turn:1,step:1,message:createSystemMessage('Synthetic benchmark. Reply only OK. Do not use tools. State is fictional.\n'+
    Array.from({length:40},(_,i)=>`Rule ${i}: current state is authoritative; older state is historical evidence only.`).join('\n'))},{surfaceOp:'append'});
  const manager=replace?new CurrentState(s,{now:()=> '2026-11-04T00:00:00Z'}):null;
  const update=index=>{
    const value={habit:{id:'fictional-reading',name:'虚构阅读习惯',lifecycle:'active'},
      plan:{accepted:true,version:1,plan:{minimum_action:'阅读一页',cue:'早餐后',notes:'这是用于测量上下文的虚构说明。'.repeat(80)}},
      habit_day:{date:'2026-11-04',check:index},unresolved_operations:[],completion_targets:[]};
    const section={name:'habit-coach:current',text:stableContext(value)};
    if(manager){manager.capture({contexts:[section],sections:[]});manager.commit();}
    else s.append('user/message',createUserMessage({source:{kind:'runtime-context',sections:[section]},content:[{type:'text',text:'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n'+section.text}]}),{surfaceOp:'append'});
  };
  for(let i=0;i<16;i++){
    update(i);s.append('user/message',createUserMessage({source:{kind:'user'},content:[{type:'text',text:`Synthetic observation ${i}. No action required.`}]}),{surfaceOp:'append'});
  }
  return {s,update};
}
async function run() {
  await add(native('dsh-llm').default);
  ctx.provide('deepseekAccount',{resolveToken:url=>platform.prototype.resolveToken.call(account,url)});
  await add(native('dsh-llm-deepseek-account'),{});
  for(const replace of [false,true]) {
    const {s,update}=build(replace);
    for(const stage of ['initial','repeat','state_changed','continuation']) {
      if(stage==='state_changed')update(16);
      if(stage==='continuation')s.append('user/message',createUserMessage({source:{kind:'user'},content:[{type:'text',text:'Continue. Reply only OK.'}]}),{surfaceOp:'append'});
      const start=Date.now();let usage,output='',finish;
      for await(const chunk of await ctx.llm.stream({provider:'deepseek-account',model:'deepseek-flash',reasoningEffort:'off',maxTokens:32,
        messages:s.deriveMessages(),sessionId:s.id,signal:AbortSignal.timeout(60000)})) {
        if(chunk.usage)usage=chunk.usage;
        if(chunk.type==='finish')finish=chunk;
        if(chunk.type==='text-delta')output+=chunk.text??chunk.delta??'';
      }
      if(!usage)throw new Error('No usage: '+JSON.stringify(finish?.failure??finish?.reason??'unknown'));
      const input=(usage.inputTokens??0)+(usage.cacheReadTokens??0)+(usage.cacheWriteTokens??0);
      const row={mode:replace?'replacement':'append',stage,input,cache_read:usage.cacheReadTokens??0,
        uncached:usage.inputTokens??0,cache_hit_pct:Math.round((usage.cacheReadTokens??0)/input*10000)/100,
        output:usage.outputTokens,elapsed_ms:Date.now()-start};
      report.push(row);console.log(JSON.stringify(row));
      fs.writeFileSync(process.env.TELOS_CACHE_REPORT,JSON.stringify({synthetic:true,provider:'deepseek-account',model:'deepseek-flash',rows:report},null,2),{mode:0o600});
    }
  }
}
run().catch(e=>{console.error(e.code??e.message);process.exitCode=1;}).finally(async()=>{for(const f of fibers.reverse())await f.dispose();});
