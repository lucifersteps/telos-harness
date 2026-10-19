import test from 'node:test';
import assert from 'node:assert/strict';
import {prepareVoice,transcribeVoice} from '../plugin/voice.mjs';
const state=phase=>({selection:{providerId:'local'},providers:[{id:'local',preparation:{phase}}]});
test('prepared service returns without restarting native preparation',async()=>{
 let calls=0;await prepareVoice({speechToText:{async *follow(){yield state('standby');},prepare(){calls++;}}});assert.equal(calls,0);
});
test('preparation delegates once and follows native progress',async()=>{
 let calls=0;await prepareVoice({speechToText:{async *follow(){yield state('unprepared');yield state('checking');yield state('ready');},prepare(){calls++;}}});assert.equal(calls,1);
});
test('native preparation failures propagate',async()=>{
 await assert.rejects(prepareVoice({speechToText:{async *follow(){yield state('unprepared');yield state('failed');},prepare(){}}}),/未完成/);
});
test('audio is delegated to the selected native provider',async()=>{
 let bytes;const ctx={speechToText:{snapshot:()=>state('standby'),resolve(spec){bytes=spec.audio;return spec;},async transcribe(spec,signal){assert.ok(signal instanceof AbortSignal);return {text:'测试会议'};}}};
 assert.deepEqual(await transcribeVoice(ctx,{audio:Buffer.from('wave').toString('base64')},new AbortController().signal),{text:'测试会议'});assert.equal(bytes.toString(),'wave');
});
test('unprepared, empty and failed recognition never produce fabricated transcripts',async()=>{
 const ctx={speechToText:{resolve:s=>s,async transcribe(){throw new Error('Prepare the local speech provider before recording');}}};
 await assert.rejects(transcribeVoice(ctx,{audio:'YQ=='},new AbortController().signal),/Prepare/);
 ctx.speechToText={snapshot:()=>state('ready'),resolve:s=>s,async transcribe(){return {text:'  '};}};
 await assert.rejects(transcribeVoice(ctx,{audio:'YQ=='},new AbortController().signal),/没有识别/);
 ctx.speechToText.transcribe=async()=>{throw new Error('native failure');};
 await assert.rejects(transcribeVoice(ctx,{audio:'YQ=='},new AbortController().signal),/native failure/);
});

test('a waking provider receives the next audio through its own native queue',async()=>{
 let calls=0;const ctx={speechToText:{snapshot(){throw new Error('Telos must not gate native queue admission');},resolve:s=>s,async transcribe(){calls++;return {text:'第二条语音'};}}};
 const outputs=await Promise.all([1,2].map(()=>transcribeVoice(ctx,{audio:'YQ=='},new AbortController().signal)));
 assert.equal(calls,2);assert.equal(outputs[1].text,'第二条语音');
});
