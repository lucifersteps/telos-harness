// WeChat channel: native Agent API only. No model loop, copied history or retry queue.
import {createServer} from 'node:http';
import {fileURLToPath} from 'node:url';
import {createUserMessage} from '@deepseek-ai/dsh-llm';
import {admitEncodedImages} from '@deepseek-ai/dsh-attachment';
import {transcribeVoice} from './voice.mjs';
import {post} from './bridge.mjs';
import * as SkillFilesystem from '@deepseek-ai/dsh-skill-filesystem';
import SqliteSessionQuery from '@deepseek-ai/dsh-session-query-sqlite';
import {mountHistoryTools} from './history.mjs';
import {createScheduler,mountScheduleTools,scheduleDelivery,conversationTurns,isBackgroundMessage} from './schedule.mjs';
import BasicCompaction from '@deepseek-ai/dsh-compaction-basic';
import ToolResultPruner from '@deepseek-ai/dsh-compaction-tool-result-pruner';
import * as Apple from './apple.mjs';
import * as Maps from './maps.mjs';
import {mountUserMemory} from './memory.mjs';
import {mountDocumentTools} from './documents.mjs';
import {mountHostTools} from './host-tools.mjs';
export const name='telos-wechat';
export const inject=['agents','sessions','sessionQuery','sessionPersistence','storageDomain','systemPrompt','attachments','agentDefaultModel','telosBusiness','workingDirectory','tools'];
export async function apply(ctx) {
  const service=ctx.telosBusiness;
  const {prompt,scheduleNamespace}=await post(service,'/boot',{});
  const delivery=scheduleDelivery(ctx,service);
  const scheduler=createScheduler(ctx,service,scheduleNamespace,open);
  const managed=new Set(),turnEvents=new Map(),waiting=new Map();
  ctx.on('session/event',(session,event)=>{
    if(!managed.has(session))return;
    if(event.type==='turn/start')turnEvents.set(session,[]);
    const current=turnEvents.get(session);
    if(!current)return;
    current.push(event);
    if(event.type==='turn/end'){
      turnEvents.delete(session);
      const turn=conversationTurns(current)[0];
      for(const key of turn?.users??[]){
        const request=waiting.get(session.id+'\0'+key);if(request)request.turn=turn;
      }
      void ctx.sessions.flush(session).then(()=>delivery.enqueue(session.id,current))
        .catch(error=>console.error('Telos scheduled output flush: '+error.message));
    }
  });
  // Telos owns only this lazy, disposable index; host search settings stay intact.
  const history=ctx.isolate('sessionQuery');
  await history.plugin(SqliteSessionQuery,{path:':memory:',openAt:'first-search',readWindowMax:10});
  async function setup(scope,agent,memoryDirectory) {
    let businessContext;
    const presets=await mountHostTools(scope,agent);
    // Reuse the preset's isolated compaction hooks as well as CLI's provider.
    if(!scope.get('compaction')&&!presets?.serviceFor(agent,'compaction')) {
      const compression=scope.isolate('compaction').isolate('toolResultPruner');
      await compression.plugin(ToolResultPruner,{thresholdChars:8192,headChars:4096,tailChars:1024});
      await compression.plugin(BasicCompaction);
    }
    // Override the host's coding persona while retaining native tool guidance.
    // Native resume runs the same setup without rewriting conversation history.
    scope.systemPrompt.section({name:'deployment:persona-prefix',order:0,text:prompt,interpolate:false});
    scope.systemPrompt.section({name:'deployment:persona-suffix',order:0,text:'',interpolate:false});
    await mountUserMemory(scope,agent,memoryDirectory);
    await mountDocumentTools(scope,agent);
    mountHistoryTools(scope,agent,history.get('sessionQuery'));
    {
      await scope.plugin(SkillFilesystem,{customSkillDirs:[fileURLToPath(new URL('./document-skills/',import.meta.url)),...(service.tools?.maps===false?[]:[service.skillsDir])],includeDefaultRoots:true,watch:false});
      await mountScheduleTools(scope,scheduler.scope);
      await scope.plugin(Apple);
      if(service.tools?.maps!==false)await scope.plugin(Maps);
    }
    agent.ctx.on('agent/pre-step',async ({step,messages,signal},next)=>{
      if(step>service.maxSteps)throw new Error('本轮已达到步骤限额，保留会话等待继续。');
      if(messages.length)await post(service,'/prepare',{session:agent.session.id,
        messages:messages.map(m=>({id:m.id,hasMedia:m.content.some(b=>b.type!=='text'),
          scheduled:isBackgroundMessage(m),text:isBackgroundMessage(m)?m.content.filter(b=>b.type==='text').map(b=>b.text).join(''):undefined}))},signal);
      // DSH assembles sections BEFORE pre-step, but projects their context in next().
      // Fill the same section after confirmation so this request sees its receipt.
      const state=await post(service,'/context',{session:agent.session.id},signal);
      businessContext.text=JSON.stringify(state);
      return next();
    });
    agent.ctx.on('system-prompt/assemble',async (assembly,context,next)=>{
      businessContext={name:'telos:business',order:0,text:'',interpolate:false,required:true};
      assembly.contexts.push(businessContext);
      return next();
    });
    managed.add(agent.session);scope.effect(()=>()=>{managed.delete(agent.session);turnEvents.delete(agent.session);});
  }
  // A cache of native handles, including in-flight creation; never an input queue.
  const sessions=new Map();
  async function open(id,resume) {
    const {directory}=await post(service,'/memory-profile',{session:id});
    if(sessions.has(id)&&sessions.get(id).directory!==directory)throw new Error('Session user identity changed');
    if(!sessions.has(id)) {
      const agentOptions=service.modelSelection??ctx.agentDefaultModel.currentSelection();
      const configure=(scope,agent)=>setup(scope,agent,directory);
      const task=(resume?ctx.agents.resume({resumeSessionId:id,agentOptions,setup:configure}):
        ctx.agents.create({sessionId:id,meta:{cwd:directory},agentOptions,setup:configure}));
      sessions.set(id,{directory,task});task.catch(()=>sessions.delete(id));
    }
    return sessions.get(id).task;
  }
  ctx.effect(()=>async()=>{
    for(const {task} of sessions.values()){try {await (await task).dispose();} catch {}}
    sessions.clear();
    await delivery.drain();
  });
  await delivery.replay();
  await scheduler.start();
  const server=createServer(async(req,res)=>{
    if(req.headers.authorization!=='Bearer '+service.token){res.writeHead(403);res.end();return;}
    try {
      if(req.method!=='POST'||!['/steer','/transcribe'].includes(req.url))throw new Error('Unsupported channel operation');
      let raw='';for await(const chunk of req){raw+=chunk;if(raw.length>24*1024*1024)throw new Error('Input too large');}
      const data=JSON.parse(raw);
      if(req.url==='/transcribe'){
        const controller=new AbortController();
        const closed=()=>{if(!res.writableEnded)controller.abort();};res.on('close',closed);
        try {
          const result=await transcribeVoice(ctx,data,controller.signal);
          res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(result));
        } finally {res.off('close',closed);}
        return;
      }
      const {agent}=await open(data.session,data.resume);
      let content=data.content;
      const images=content.filter(b=>b.type==='image'&&b.data);
      if(images.length){
        const refs=await admitEncodedImages(ctx.attachments,images.map(b=>({data:b.data,mediaType:b.mimeType})));
        let index=0;content=content.map(b=>b.type==='image'&&b.data?{type:'image',attachment:refs[index++]}:b);
      }
      const events=[];
      const request={};const requestKey=data.session+'\0'+data.key;waiting.set(requestKey,request);
      const dispose=ctx.on('session/event',(session,event)=>{if(session===agent.session)events.push(event);});
      try {
        agent.steer({...createUserMessage({content,source:{kind:'user'}}),id:data.key});
        await agent.whenIdle();
        await ctx.parallel(agent.session,'session/flush',agent.session);
        // whenIdle may include a queued scheduled turn. Return only the turn
        // which actually consumed this request; background output has its own outbox.
        const turn=request.turn;
        const final=turn?.text||'';
        const finish=turn?.finish;
        res.writeHead(200,{'Content-Type':'application/json'});
        res.end(JSON.stringify({events,text:final,finish,keys:turn?.users??[data.key]}));
      } finally {dispose();waiting.delete(requestKey);}
    } catch(error){console.error('Telos channel: '+error.message);res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:String(error)}));}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  ctx.effect(()=>()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve);}));
  // Admit WeChat traffic only after all native providers/tools have mounted.
  const cancelReady=ctx.get('appReady').onReady(()=>{
    post(service,'/ready',{url:`http://127.0.0.1:${server.address().port}`}).catch(()=>ctx.get('appExit')(1));
  });
  ctx.effect(()=>cancelReady);
}
