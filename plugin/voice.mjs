// Speech recognition and model preparation belong entirely to the native DSH service.
export async function prepareVoice(ctx) {
  const signal=AbortSignal.timeout(30*60*1000);let started=false,last='';
  for await(const state of ctx.speechToText.follow(signal)) {
    const provider=state.providers.find(p=>p.id===state.selection.providerId);
    if(!provider)continue;
    const phase=provider.preparation.phase;
    if(phase==='ready'||phase==='standby'){console.log('DSH 本地语音识别已就绪。');return;}
    if(!started){started=true;ctx.speechToText.prepare(provider.id);continue;}
    const label=[phase,provider.preparation.resource||'',provider.preparation.totalBytes?
      Math.floor(100*provider.preparation.completedBytes/provider.preparation.totalBytes)+'%':''].filter(Boolean).join(' ');
    if(label!==last){console.error('DSH 语音准备：'+label);last=label;}
    if(phase==='failed'||phase==='cancelled')throw new Error('DSH 语音准备未完成：'+(provider.preparation.message||phase));
  }
  throw new Error('DSH 语音准备超时；可再次运行 --telos-prepare-voice 继续准备。');
}
export async function transcribeVoice(ctx,data,signal) {
  if(typeof data.audio!=='string'||data.audio.length>6*1024*1024)throw new Error('语音内容为空或过大。');
  const audio=Buffer.from(data.audio,'base64');
  // Native provider owns readiness checks, cold-start waiting and its bounded queue.
  const spec=ctx.speechToText.resolve({audio});
  const result=await ctx.speechToText.transcribe(spec,AbortSignal.any([signal,AbortSignal.timeout(150000)]));
  if(!result.text?.trim())throw new Error('没有识别到清晰语音，请重新发送或改用文字。');
  return {text:result.text};
}
