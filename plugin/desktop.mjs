// Native Desktop configuration surface; no additional server or Agent state machine.
import {component} from './component.mjs';
import {voiceStatus} from './voice.mjs';
import {modelCatalog,validateSelection,addModel,testModel} from './models.mjs';
import {getDshRuntimeVersion} from '@deepseek-ai/dsh-app-boot';
export async function mountDesktop(ctx,paths,{readConfig,runtime}) {
  const dshVersion=getDshRuntimeVersion();
  let fork=null,busy='',error='',qr=null,closed=false;
  const fail=reason=>{if(!closed)error=reason.message;};
  async function stop() {
    const current=fork;fork=null;
    if(current)await current.dispose();
  }
  ctx.effect(()=>()=>{closed=true;});
  async function start() {
    if(fork)return;
    const config=await readConfig(paths);
    if(!config)throw new Error('请先保存 Telos 配置。');
    let ready,failed;
    const boot=new Promise((resolve,reject)=>{ready=resolve;failed=reject;});
    fork=ctx.plugin({name:'telos-runtime',async apply(scope){
      try {await runtime(scope,paths,config,e=>{fail(e);void stop();});ready();}
      catch(e){failed(e);}
    }});
    try {await boot;} catch(e){await stop();throw e;}
  }
  async function status() {
    let result={};
    try {result=await component(ctx,paths,'status').done;} catch(e){fail(e);}
    let config;
    try {config=await readConfig(paths);} catch(e){fail(e);}
    return {...result,config:config?{direct_write:config.direct_write??true,max_steps:config.max_steps,persona:config.persona??'default',persona_file:config.persona_file??'',model_selection:config.model_selection??null,tools:{apple:true,maps:true,...config.tools}}:null,
      model:config?.model_selection??ctx.get('agentDefaultModel')?.currentSelection()??null,
      defaultModel:ctx.get('agentDefaultModel')?.currentSelection()??null,
      running:!!fork,busy,error,qr,voice:voiceStatus(ctx),dshVersion};
  }
  async function execute(action,data) {
    if(action==='apply'){
      await validateSelection(ctx,data.model_selection);
      const wasRunning=!!fork;await stop();
      try {await execute('save',{...data,action:'save'});}
      finally {if(wasRunning)await start();}
      return;
    }
    if(action==='stop'){await stop();return;}
    if(action==='start'){
      // Starting with edited fields must not silently use the previous persona.
      if(!fork&&Object.keys(data).some(key=>key!=='action'))await execute('save',{...data,action:'save'});
      await start();return;
    }
    if(fork)throw new Error('请先停止 Telos，再修改配置或重新登录；已有草稿和回执会保留。');
    if(action==='save') {
      if(Object.keys(data).some(key=>!['action','max_steps','persona','persona_file','model_selection','tools','direct_write'].includes(key)))throw new Error('凭据请通过 DSH 原生服务保存。');
      const {action:_,...options}=data;
      if('model_selection' in options)await validateSelection(ctx,options.model_selection);
      await component(ctx,paths,'configure',{},options).done;
      return;
    }
    if(action==='login') {
      qr=null;
      await component(ctx,paths,'login-json',{},undefined,value=>{if(value.event==='qr')qr=value.image_url;}).done;
      qr=null;return;
    }
    if(action==='authorize'){await component(ctx,paths,'authorize').done;return;}
    throw new Error('不支持的配置操作。');
  }
  // DSH Connection applies its existing owner authentication and Host/Origin checks.
  ctx.inject(['connection'],scope=>{
    scope.connection.fetch.register({path:'/api/telos/models',methods:['GET','POST'],requestBody:'buffered',fetch:async request=>{
      try {
        if(request.method==='GET')return Response.json(await modelCatalog(ctx));
        const raw=await request.text();if(raw.length>4096)throw new Error('配置内容过大。');
        const data=JSON.parse(raw);
        if(data.action==='add')return Response.json(await addModel(ctx,data.config));
        if(data.action==='test'){
          const selection=data.selection??ctx.get('agentDefaultModel')?.currentSelection();
          await validateSelection(ctx,selection);return Response.json(await testModel(ctx,selection));
        }
        throw new Error('不支持的模型操作。');
      }catch {return Response.json({error:'DSH 未能完成模型操作，请检查模型配置、登录或凭据。'},{status:400});}
    }});
    scope.connection.fetch.register({path:'/api/telos/status',methods:['GET'],requestBody:'buffered',
      fetch:async()=>Response.json(await status(),{headers:{'cache-control':'no-store'}})});
    scope.connection.fetch.register({path:'/api/telos/action',methods:['POST'],requestBody:'buffered',fetch:async request=>{
      if(busy)return Response.json({error:'上一个配置操作尚未结束。'},{status:409});
      try {
        const raw=await request.text();if(raw.length>16384)throw new Error('配置内容过大。');
        const data=JSON.parse(raw);
        if(!data||!['save','apply','login','authorize','start','stop'].includes(data.action))throw new Error('不支持的配置操作。');
        busy=data.action;error='';
        // QR login and system authorization may wait for the user. Keep the native page responsive.
        void execute(data.action,data).catch(fail).finally(()=>{busy='';});
        return Response.json({accepted:true});
      } catch(e){return Response.json({error:e.message},{status:400});}
    }});
  });
  try {
    const config=await readConfig(paths);
    if(config?.wechat?.enabled)await start();
  } catch(e){fail(e);}
}
