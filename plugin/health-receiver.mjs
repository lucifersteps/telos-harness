// This scope owns the receiver. It is never launched by status/configure calls.
import {spawn} from 'node:child_process';
import {join,isAbsolute} from 'node:path';

export async function startHealthReceiver(ctx,paths,config,onState=()=>{}) {
  const options=config.health_receiver??{enabled:false};
  if(!options.enabled){onState({phase:'disabled'});return;}
  if(typeof options.root!=='string'||!isAbsolute(options.root)){
    onState({phase:'failed',error:'请配置健康接收端目录。'});return;
  }
  onState({phase:'starting'});
  const env={...process.env};
  for(const key of Object.keys(env))if(/(?:API_KEY|AUTH_TOKEN)$/.test(key)||key.startsWith('TELOS_'))delete env[key];
  const child=spawn(join(options.root,'.venv/bin/python'),[join(paths.pkg,'health-receiver.py'),'--root',options.root],
    {cwd:options.root,env,detached:true,stdio:['pipe','pipe','pipe']});
  let stopping=false,buffer='',settle;
  const ready=new Promise(resolve=>{settle=resolve;});
  child.stdin.on('error',()=>{});
  child.stderr.on('data',()=>{}); // Errors are reported without forwarding paths/credentials.
  child.stdout.on('data',chunk=>{
    buffer+=chunk;
    while(buffer.includes('\n')){
      const end=buffer.indexOf('\n'),line=buffer.slice(0,end);buffer=buffer.slice(end+1);
      try{
        const value=JSON.parse(line);
        if(['starting','running','failed','stopped'].includes(value.phase)){
          onState({phase:value.phase,...(value.error?{error:value.error}:{})});
          if(value.phase!=='starting')settle();
        }
      }catch{}
    }
  });
  const done=new Promise(resolve=>{
    child.once('error',()=>{onState({phase:'failed',error:'健康接收端无法启动，请检查本机安装。'});settle();resolve();});
    child.once('close',code=>{if(!stopping&&code!==0)onState({phase:'failed',error:'健康接收端已停止，请重启 Telos 后重试。'});settle();resolve();});
  });
  const stop=async()=>{
    stopping=true;
    child.stdin.end(); // EOF also works if the host itself is killed.
    const timer=setTimeout(()=>{try{if(child.pid)process.kill(-child.pid,'SIGKILL');}catch{}},7000);
    try{await done;}finally{clearTimeout(timer);onState({phase:'stopped'});}
  };
  ctx.effect(()=>stop);
  const timeout=setTimeout(()=>{void stop();settle();},20000);
  await ready;clearTimeout(timeout);
  return {stop,done};
}
