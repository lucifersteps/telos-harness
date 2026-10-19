// WeChat channel: native Agent API only. No model loop, copied history or retry queue.
import {createServer} from 'node:http';
import {createUserMessage} from '@deepseek-ai/dsh-llm';
import {admitEncodedImages} from '@deepseek-ai/dsh-attachment';
import {transcribeVoice} from './voice.mjs';
import {post} from './bridge.mjs';
export const name='telos-wechat';
export const inject=['agents','systemPrompt','attachments','telosBusiness','speechToText'];
export async function apply(ctx) {
  const service=ctx.telosBusiness;
  const {prompt}=await post(service,'/boot',{});
  ctx.systemPrompt.section({name:'telos:rules',order:0,text:prompt,complete:true,interpolate:false});
  ctx.on('agent/created',({agent})=>{
    agent.ctx.on('agent/pre-step',async ({step,messages,signal},next)=>{
      if(step>service.maxSteps)throw new Error('本轮已达到步骤限额，保留会话等待继续。');
      if(messages.length)await post(service,'/prepare',{session:agent.session.id,
        messages:messages.map(m=>({id:m.id,hasMedia:m.content.some(b=>b.type!=='text')}))},signal);
      return next();
    });
    agent.ctx.on('system-prompt/assemble',async (assembly,context,next)=>{
      const state=await post(service,'/context',{session:agent.session.id},context.signal);
      assembly.contexts.push({name:'telos:business',order:0,text:JSON.stringify(state),interpolate:false,required:true});
      return next();
    });
  });
  // A cache of native handles, including in-flight creation; never an input queue.
  const sessions=new Map();
  async function open(id,resume) {
    if(!sessions.has(id)) {
      const agentOptions={provider:'deepseek-official',model:service.model,reasoningEffort:'off'};
      const task=resume?ctx.agents.resume({resumeSessionId:id,agentOptions}):
        ctx.agents.create({sessionId:id,meta:{cwd:process.cwd()},agentOptions});
      sessions.set(id,task);task.catch(()=>sessions.delete(id));
    }
    return sessions.get(id);
  }
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
    } catch(error){res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({error:String(error)}));}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  ctx.on('dispose',()=>new Promise(resolve=>{server.closeAllConnections();server.close(resolve);}));
  // Admit WeChat traffic only after all native providers/tools have mounted.
  const cancelReady=ctx.get('appReady').onReady(()=>{
    post(service,'/ready',{url:`http://127.0.0.1:${server.address().port}`}).catch(()=>ctx.get('appExit')(1));
  });
  ctx.on('dispose',cancelReady);
}
