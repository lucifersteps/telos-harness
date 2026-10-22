// Harness owns lifecycle, model loop, history and credential persistence.
import {randomBytes} from 'node:crypto';
import {readFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join,resolve} from 'node:path';
import {resolveDshHome,expandHomePath} from '@deepseek-ai/dsh-home-paths';
import * as WeChat from './index.mjs';
import {component} from './component.mjs';
import {voiceStatus} from './voice.mjs';
import {mountDesktop} from './desktop.mjs';
export const name='telos-bootstrap';
export const inject=['cmdlineArgs'];
export function requireHost(ctx) {
  const missing=['agents','agentDefaultModel','credentials','skills','attachments'].filter(name=>!ctx.get(name));
  if(missing.length)throw new Error('DSH 当前配置缺少 '+missing.join(', ')+'；请使用包含 dsh-base 的宿主配置，Telos 不补装通用服务。');
}
const commands={'--telos-setup':'setup','--telos-login':'login','--telos-authorize':'authorize','--telos-status':'status'};
export function commandFor(profile,args) {
  if(profile==='desktop')return 'desktop'; // Desktop owns its host/port and UI arguments.
  if(!args.length)return 'serve';
  if(args.length===1&&commands[args[0]])return commands[args[0]];
  if(args.length===2&&args[0]==='--telos-direct-write'&&['on','off'].includes(args[1]))return 'configure';
  if(args.length===2&&['--telos-persona','--telos-persona-file'].includes(args[0]))return 'configure';
  throw new Error('支持 --telos-setup、--telos-login、--telos-authorize、--telos-status、--telos-direct-write on|off、--telos-persona default|liman、--telos-persona-file 文件路径；无参数启动微信通道。');
}
export async function readConfig(paths) {
  try {
    const config=JSON.parse(await readFile(join(paths.root,'config.json'),'utf8'));
    if(config.version!==1)throw new Error('不支持此配置版本；原文件未修改。');
    return config;
  } catch(error) {if(error.code==='ENOENT')return null;throw error;}
}
export async function runtime(ctx,paths,config,onFailure) {
  const token=randomBytes(24).toString('hex');
  requireHost(ctx);
  const child=component(ctx,paths,'serve',{TELOS_BUSINESS_TOKEN:token});
  let timer;
  const ready=await Promise.race([child.ready,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Telos 本机组件启动超时。')),30000);})]).finally(()=>clearTimeout(timer));
  if(ready.protocol!==1)throw new Error('Telos 本机组件协议不兼容。');
  child.done.then(()=>{if(!child.stopping)onFailure(new Error('Telos 本机组件已停止。'));},error=>{if(!child.stopping)onFailure(error);});
  ctx.provide('telosBusiness',{url:ready.url,token,maxSteps:config.max_steps,skillsDir:join(paths.pkg,'skills'),tools:config.tools,modelSelection:config.model_selection});
  await ctx.plugin(WeChat);
}
async function run(ctx) {
  const home=resolveDshHome();
  const paths={home,pkg:fileURLToPath(new URL('.',import.meta.url)),root:resolve(expandHomePath(process.env.TELOS_HOME||join(home,'telos')))};
  const desktop=ctx.get('profileContext')?.name==='desktop';
  try {
    await mkdir(paths.root,{recursive:true,mode:0o700});
    const command=commandFor(desktop?'desktop':'cli',ctx.cmdlineArgs.get());
    if(desktop){await mountDesktop(ctx,paths,{readConfig,runtime});return;}
    const config=await readConfig(paths);
    if(command==='serve') {
      if(!config)throw new Error('请先运行 --telos-setup 完成首次配置。');
      await runtime(ctx,paths,config,()=>ctx.get('appExit')(1));return;
    }
    const args=ctx.cmdlineArgs.get();
    const input=command==='configure'?(args[0]==='--telos-direct-write'?{direct_write:args[1]==='on'}:args[0]==='--telos-persona'?{persona:args[1]}:{persona:'custom',persona_file:args[1]}):undefined;
    const result=await component(ctx,paths,command,{},input).done;
    if(command==='setup') {
      console.log('Telos 配置已保存；模型、凭据和语音请在 DSH 配置。接着运行 --telos-login 和 --telos-authorize。');
    } else {
      if(command==='status') {
        result.voice=voiceStatus(ctx);
        result.model=config?.model_selection??ctx.get('agentDefaultModel')?.currentSelection()??null;
      }
      console.log(JSON.stringify(result));
    }
    ctx.get('appExit')(0);
  } catch(error) {
    console.error('Telos: '+error.message);
    // Plugin configuration failures must never take down the desktop Host.
    if(!desktop)ctx.get('appExit')(1);
  }
}

export function apply(ctx) {
  // Host services may initialize asynchronously. Validate after DSH readiness.
  const off=ctx.get('appReady').onReady(()=>run(ctx));
  ctx.effect(()=>off);
}
