// A configuration view over native DSH services. No model client or credential store.
import {randomUUID} from 'node:crypto';
import {createUserMessage,BlockAssembler} from '@deepseek-ai/dsh-llm';

export async function modelCatalog(ctx) {
  const llm=ctx.get('llm'),settings=ctx.get('settings');
  if(!llm)return {providers:[],customAvailable:false};
  const descriptions=settings?.describe({redactSecrets:true})??[];
  const directory=llm.listConfigurableProviders();
  const providers=new Map(llm.listProviders().map(p=>[p.id,{id:p.id,name:p.name}]));
  for(const p of directory)if(!providers.has(p.provider))providers.set(p.provider,{id:p.provider,name:p.displayName});
  const available=await Promise.all([...providers.values()].map(async p=>{
    const entry=directory.find(d=>d.provider===p.id),descriptor=descriptions.find(d=>d.ns===entry?.settingsNs);
    const config=entry?.settingsPath.reduce((v,k)=>v?.[k],descriptor?.value);
    let models=[];
    try {models=(await llm.listModels(p.id)).map(m=>({id:m.id,name:m.name||m.id,inputModalities:m.inputModalities}));}catch{}
    return {...p,models,credentialRef:typeof config?.apiKeyEnv==='string'?config.apiKeyEnv:null};
  }));
  // Dormant native routes have no usable configuration; add those through DSH below.
  return {customAvailable:descriptions.some(d=>d.ns==='llm-pi-ai'),providers:available.filter(p=>p.models.length||p.credentialRef||p.id==='deepseek-account')};
}

export async function validateSelection(ctx,selection) {
  if(selection==null)return;
  if(typeof selection!=='object'||Array.isArray(selection)||Object.keys(selection).some(k=>!['provider','model','reasoningEffort'].includes(k))||
    !['provider','model'].every(k=>typeof selection[k]==='string'&&selection[k].trim())||
    Object.values(selection).some(v=>typeof v!=='string'||!v.trim()||v.length>256))throw new Error('请选择有效的 DSH 模型。');
  if(!ctx.get('llm'))throw new Error('宿主未提供 DSH 模型服务。');
  try {await ctx.get('llm').resolveCallConfig(selection,AbortSignal.timeout(15000));}
  catch {throw new Error('DSH 无法使用所选模型，请检查模型名称、凭据和连接。');}
}

export async function addModel(ctx,data) {
  if(Object.keys(data).some(k=>!['name','baseURL','model','api'].includes(k)))throw new Error('API Key 必须通过 DSH 凭据服务保存。');
  if(!['name','baseURL','model'].every(k=>typeof data[k]==='string'&&data[k].trim()&&data[k].length<=256))throw new Error('请填写名称、API 地址和模型 ID。');
  const url=new URL(data.baseURL);
  if(!['https:','http:'].includes(url.protocol)||url.username||url.password||url.search||url.hash)throw new Error('API 地址不能包含账号、密码或查询参数。');
  if(!['openai-completions','openai-responses','anthropic-messages'].includes(data.api))throw new Error('不支持该 API 协议。');
  const settings=ctx.get('settings'),descriptor=settings?.describe().find(d=>d.ns==='llm-pi-ai');
  if(!descriptor)throw new Error('宿主未启用 DSH 自定义模型服务。');
  const provider='telos-api-'+randomUUID().slice(0,8),credentialRef=provider.toUpperCase().replaceAll('-','_')+'_API_KEY';
  await settings.mutate('llm-pi-ai',[{op:'set',path:['providers',provider],value:{displayName:data.name.trim(),baseURL:url.href.replace(/\/$/,''),api:data.api,apiKeyEnv:credentialRef,models:[{id:data.model.trim()}]}}],descriptor.revision);
  return {provider,model:data.model.trim(),credentialRef};
}

export async function testModel(ctx,selection) {
  const llm=ctx.get('llm'),signal=AbortSignal.timeout(30000);
  try {
    const call=await llm.prepareCall({...selection,maxTokens:64},signal);
    const assembler=new BlockAssembler();
    for await(const chunk of call.stream({...call.config,signal,system:'Reply briefly.',
      messages:[createUserMessage({content:[{type:'text',text:'请只回复 OK。'}],source:{kind:'user'}})],purpose:'telos-connection-test'}))assembler.push(chunk);
    if(!['stop','max-tokens'].includes(assembler.finish?.kind))throw new Error();
    return {message:'模型连接成功（已完成一次简短调用）。'};
  }catch {throw new Error('模型连接未通过，请检查 DSH 登录、凭据、模型名称和网络。');}
}
