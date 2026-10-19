// Local authenticated transport to the existing Python business connectors.
export async function post(service,path,data,signal) {
  const response=await fetch(service.url+path,{
    method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+service.token},
    body:JSON.stringify(data),signal});
  if(!response.ok)throw new Error(await response.text());
  return response.json();
}
export async function registerTools(ctx,family,authenticate=async()=>({})) {
  const {tools}=await post(ctx.telosBusiness,'/boot',{family});
  for(const schema of tools)ctx.tools.register({
    ...schema,
    output:{schema:{type:'object',additionalProperties:true},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
    execute:async(args,exec)=>post(ctx.telosBusiness,'/tool',{session:exec.agent.session.id,id:exec.callId,name:schema.name,args,...await authenticate()},exec.signal)
  });
}
