// WeChat channel: native Agent API only. No model loop, copied history or retry queue.
import {createServer} from 'node:http';
import {createUserMessage} from '@deepseek-ai/dsh-llm';
import {admitEncodedImages} from '@deepseek-ai/dsh-attachment';
import {transcribeVoice} from './voice.mjs';
import {post} from './bridge.mjs';
import * as SkillFilesystem from '@deepseek-ai/dsh-skill-filesystem';
import * as SkillTool from '@deepseek-ai/dsh-tool-skill';
import * as TodoTool from '@deepseek-ai/dsh-tool-todo';
import BasicCompaction from '@deepseek-ai/dsh-compaction-basic';
import ToolResultPruner from '@deepseek-ai/dsh-compaction-tool-result-pruner';
import * as Apple from './apple.mjs';
import * as Maps from './maps.mjs';
export const name='telos-wechat';
export const inject=['agents','systemPrompt','attachments','agentDefaultModel','telosBusiness'];
export async function apply(ctx) {
  const service=ctx.telosBusiness;
  const {prompt}=await post(service,'/boot',{});
  async function setup(scope,agent) {
    let businessContext;
    // Desktop moves compaction out of dsh-base into UI presets. WeChat creates
    // agents directly, so mount the same native backend in this agent's scope.
    // CLI already supplies one; keep its policy and avoid duplicate hooks.
    if(!scope.get('compaction')) {
      const compression=scope.isolate('compaction').isolate('toolResultPruner');
      await compression.plugin(ToolResultPruner,{thresholdChars:8192,headChars:4096,tailChars:1024});
      await compression.plugin(BasicCompaction);
    }
    // Complete, composed rules + selected persona belong only to this WeChat scope.
    // Native resume runs the same setup, without rewriting history or business state.
    scope.systemPrompt.section({name:'telos:rules',order:0,text:prompt,complete:true,interpolate:false});
    {
      await scope.plugin(SkillFilesystem,{customSkillDirs:service.tools?.maps===false?[]:[service.skillsDir],includeDefaultRoots:true,watch:false});
      await scope.plugin(SkillTool);
      await scope.plugin(TodoTool,{allowParallelInProgress:false});
      await scope.plugin(Apple);
      if(service.tools?.maps!==false)await scope.plugin(Maps);
    }
    scope.tools.restrict({allow:[]});
    agent.ctx.on('agent/pre-step',async ({step,messages,signal},next)=>{
      if(step>service.maxSteps)throw new Error('本轮已达到步骤限额，保留会话等待继续。');
      if(messages.length)await post(service,'/prepare',{session:agent.session.id,
        messages:messages.map(m=>({id:m.id,hasMedia:m.content.some(b=>b.type!=='text')}))},signal);
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
  }
  // A cache of native handles, including in-flight creation; never an input queue.
  const sessions=new Map();
  async function open(id,resume) {
    if(!sessions.has(id)) {
      const agentOptions=service.modelSelection??ctx.agentDefaultModel.currentSelection();
      const task=(resume?ctx.agents.resume({resumeSessionId:id,agentOptions,setup}):
        ctx.agents.create({sessionId:id,meta:{cwd:process.cwd()},agentOptions,setup}));
      sessions.set(id,task);task.catch(()=>sessions.delete(id));
    }
    return sessions.get(id);
  }
  ctx.effect(()=>async()=>{
    for(const task of sessions.values()){try {await (await task).dispose();} catch {}}
    sessions.clear();
  });
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
      const dispose=ctx.on('session/event',(session,event)=>{if(session===agent.session)events.push(event);});
      try {
        agent.steer({...createUserMessage({content,source:{kind:'user'}}),id:data.key});
        await agent.whenIdle();
        await ctx.parallel(agent.session,'session/flush',agent.session);
        const last=events.findLast(e=>e.type==='assistant/message');
        const final=last?.data.message.content.filter(b=>b.type==='text').map(b=>b.text).join('')||'';
        const finish=events.findLast(e=>e.type==='turn/end')?.data;
        res.writeHead(200,{'Content-Type':'application/json'});
        res.end(JSON.stringify({events,text:final,finish,keys:[data.key,...events.filter(e=>e.type==='user/message').map(e=>e.data.id)]}));
      } finally {dispose();}
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
