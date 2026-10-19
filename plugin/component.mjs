// One owned component process; all business state remains in the existing store.
import {spawn} from 'node:child_process';
import {join} from 'node:path';
export function component(ctx, paths, command, env={}, input, onLine) {
  const environment={...process.env,...env};
  for(const key of Object.keys(environment))if(/(?:API_KEY|AUTH_TOKEN)$/.test(key))delete environment[key];
  const child=spawn(join(paths.pkg,'bin/telos-component'),[command,'--home',paths.root,'--harness-home',paths.home],
    {cwd:paths.root,env:environment,stdio:[command==='setup'?'inherit':'pipe','pipe','pipe']});
  let buffer='',error='',last,stopping=false;
  let resolveReady,rejectReady;
  const ready=new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;});
  ready.catch(()=>{});
  child.stdout.on('data',chunk=>{
    buffer+=chunk;
    while(buffer.includes('\n')) {
      const end=buffer.indexOf('\n'),line=buffer.slice(0,end);buffer=buffer.slice(end+1);
      try {last=JSON.parse(line);onLine?.(last);if(last.protocol===1)resolveReady(last);} catch {}
    }
  });
  child.stderr.on('data',chunk=>{error=(error+chunk).slice(-8192);if(['serve','setup','login','authorize'].includes(command))process.stderr.write(chunk);});
  const done=new Promise((resolve,reject)=>{
    child.once('error',reject);
    child.once('close',code=>code===0?resolve(last):reject(new Error(error.trim()||`Telos 本机组件退出（${code}）。`)));
  });
  done.then(()=>rejectReady(new Error('Telos 本机组件已退出。')),rejectReady);
  const stop=async()=>{
    stopping=true;
    if(child.exitCode===null&&child.signalCode===null) {
      child.kill('SIGTERM');const timer=setTimeout(()=>child.kill('SIGKILL'),8000);
      try {await done;} catch {} finally {clearTimeout(timer);}
    }
  };
  const off=ctx.effect(()=>stop);
  done.finally(off).catch(()=>{});
  if(input!==undefined)child.stdin.end(JSON.stringify(input));
  return {done,ready,stop,get stopping(){return stopping;}};
}
