// Optional host capability: Telos never installs, selects or downloads a speech model.
export function voiceStatus(ctx) {
  return ctx.get('speechToText')?.snapshot()??{available:false,providers:[]};
}
export async function transcribeVoice(ctx,data,signal) {
  if(typeof data.audio!=='string'||data.audio.length>6*1024*1024)throw new Error('语音内容为空或过大。');
  const audio=Buffer.from(data.audio,'base64');
  // Native provider owns readiness checks, cold-start waiting and its bounded queue.
  const speech=ctx.get('speechToText');
  if(!speech)throw new Error('请在 DSH 中启用语音识别服务；也可以发送文字。');
  const spec=speech.resolve({audio});
  const result=await speech.transcribe(spec,AbortSignal.any([signal,AbortSignal.timeout(150000)]));
  if(!result.text?.trim())throw new Error('没有识别到清晰语音，请重新发送或改用文字。');
  return {text:result.text};
}
