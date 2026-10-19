// Harness owns the component lifecycle. No second model runtime or message queue.
import {spawn} from 'node:child_process';
import {randomBytes} from 'node:crypto';
import {readFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join,resolve} from 'node:path';
import {resolveDshHome,expandHomePath} from '@deepseek-ai/dsh-home-paths';
import * as DeepSeek from '@deepseek-ai/dsh-llm-deepseek-api-key';
import {prepareVoice} from './voice.mjs';
import * as SkillFilesystem from '@deepseek-ai/dsh-skill-filesystem';
export const name='telos-bootstrap';
export const inject=['cmdlineArgs','credentials','llm','skills','speechToText'];
export async function apply(ctx) {
  try {await start(ctx);} catch(error) {console.error('Telos: '+error.message);ctx.get('appExit')(1);}
}
async function start(ctx) {
  const pkg=fileURLToPath(new URL('.',import.meta.url));
  const home=resolveDshHome();
  const root=resolve(expandHomePath(process.env.TELOS_HOME||join(home,'telos')));
  const args=ctx.cmdlineArgs.get();
  const commands={'--telos-setup':'setup','--telos-login':'login','--telos-authorize':'authorize','--telos-status':'status','--telos-prepare-voice':'prepare-voice'};
  if(args.length && (args.length!==1||!commands[args[0]])) {
    console.error('Telos: 支持 --telos-setup、--telos-login、--telos-authorize、--telos-status、--telos-prepare-voice；无参数启动微信通道。');
    ctx.get('appExit')(2);return;
  }
  const command=commands[args[0]]||'serve';
  if(command==='prepare-voice'){await prepareVoice(ctx);ctx.get('appExit')(0);return;}
  await mkdir(root,{recursive:true,mode:0o700});
  let config;
  try {config=JSON.parse(await readFile(join(root,'config.json'),'utf8'));}
  catch(error) {if(error.code!=='ENOENT')throw error;}
  if(command==='serve'&&!config)throw new Error('请先运行 dsh --profile sdk-minimal --telos-setup 完成首次配置。');
  if(config&&config.version!==1)throw new Error('不支持此配置版本；原文件未修改。');
  const token=randomBytes(24).toString('hex');
  const mapKey=await ctx.credentials.resolve('BAIDU_MAP_AUTH_TOKEN');
  const env={...process.env,TELOS_BUSINESS_TOKEN:token};
  if(mapKey)env.BAIDU_MAP_AUTH_TOKEN=mapKey.value;
  // Setup may reuse native credentials without asking the user to type them again.
  if(command==='setup')for(const ref of ['DEEPSEEK_API_KEY','BAIDU_MAP_AUTH_TOKEN']) {
    const hit=await ctx.credentials.resolve(ref);if(hit)env[ref]=hit.value;
  }
  const child=spawn(join(pkg,'bin/telos-component'),[command,'--home',root,'--harness-home',home],
    {cwd:root,env,stdio:[command==='setup'?'inherit':'pipe','pipe','inherit']});
  let stopping=false;
  const exited=new Promise(resolve=>child.once('exit',(code,signal)=>resolve({code,signal})));
  ctx.on('dispose',async()=>{
    stopping=true;
    if(child.exitCode===null&&child.signalCode===null){
      child.kill('SIGTERM');
      const timer=setTimeout(()=>child.kill('SIGKILL'),8000);
      await exited;clearTimeout(timer);
    }
  });
  child.once('error',()=>ctx.get('appExit')(1));
  if(command!=='serve') {
    let output='';for await(const chunk of child.stdout)output+=chunk;
    const {code}=await exited;
    if(code===0){
      const result=JSON.parse(output);
      if(command==='setup') {
        for(const [ref,value] of Object.entries(result.credentials))await ctx.credentials.set(ref,value);
        if(!await ctx.credentials.resolve('DEEPSEEK_API_KEY'))throw new Error('尚未配置 DeepSeek API Key，请再次运行 --telos-setup。');
        console.log('Telos 配置已保存；凭据已持久化到 Harness。接着运行 --telos-login 和 --telos-authorize。');
      } else {
        if(command==='status')result.voice=ctx.speechToText.snapshot();
        if(command==='status')result.credentials={model:!!await ctx.credentials.resolve('DEEPSEEK_API_KEY'),maps:!!mapKey};
        console.log(JSON.stringify(result));
      }
    }
    ctx.get('appExit')(code??1);return;
  }
  const ready=await new Promise((resolve,reject)=>{
    let output='';const timer=setTimeout(()=>reject(new Error('Telos 本机组件启动超时。')),30000);
    child.once('error',reject);child.once('exit',()=>{clearTimeout(timer);reject(new Error('Telos 本机组件启动失败。'));});
    child.stdout.on('data',chunk=>{output+=chunk;if(output.includes('\n')){clearTimeout(timer);try{resolve(JSON.parse(output.split('\n')[0]));}catch(error){reject(error);}}});
  });
  if(ready.protocol!==1)throw new Error('Telos 本机组件协议不兼容。');
  // Stop the host on component failure; the process manager decides whether to restart.
  exited.then(()=>{if(!stopping)ctx.get('appExit')(1);});
  ctx.plugin(DeepSeek,{baseURL:config.base_url,thinking:'disabled',streamIdleTimeoutMs:180000});
  ctx.plugin(SkillFilesystem,{customSkillDirs:[join(pkg,'skills')],includeDefaultRoots:false,watch:false});
  ctx.provide('telosBusiness',{url:ready.url,token,model:config.model,maxSteps:config.max_steps});
}
