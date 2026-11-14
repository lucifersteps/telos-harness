// Offline audit: operate only on a detached copy; print aggregate counts.
const fs=require('node:fs');
const {Session,decodeSeqRanges}=require(process.env.DSH_NATIVE_ROOT+'/dsh-session/lib/index.js');
const {TokenMeter}=require(process.env.DSH_NATIVE_ROOT+'/dsh-token-meter/lib/index.js');
const {ContextRetention,profileBaseline}=require('../plugin/context-retention.mjs');
const input=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
for(const e of input.events)if(e.sourceEventSeqs)e.sourceEventSeqs=decodeSeqRanges(e.sourceEventSeqs,e.seq);
const s=Session.create(input.session,input.events),meter={estimateMessage:m=>TokenMeter.prototype.estimateMessage(m)};
const r=new ContextRetention(s,{meter});
if(process.argv[3]==='candidates'){fs.writeFileSync(process.argv[4],JSON.stringify({session:input.session,turns:r.candidates()}),{mode:0o600});process.exit();}
const counts=()=>{const rows=s.surface.nodes.map(seq=>s.eventAt(seq));return {nodes:rows.length,profiles:rows.filter(profileBaseline).length,
 toolResults:rows.filter(e=>e.type==='tool/result').length,estimatedMessageTokens:rows.reduce((n,e)=>n+meter.estimateMessage(s.deriveEventMessage(e)),0)};};
const before=counts(),humans=s.surface.nodes.filter(seq=>{const e=s.eventAt(seq);return e.type==='user/message'&&e.data.source.kind==='user';});
const systems=s.surface.nodes.filter(seq=>s.eventAt(seq).type==='system/message');
r.apply(JSON.parse(fs.readFileSync(process.argv[3],'utf8')));
const after=counts(),restored=Session.create(input.session,JSON.parse(JSON.stringify(s.snapshotEvents())));
const pairIds=()=>s.surface.nodes.flatMap(seq=>{const e=s.eventAt(seq),m=s.deriveEventMessage(e);return m.content.filter(b=>b.type==='tool-call').map(b=>b.id);});
const calls=new Set(pairIds());let orphanResults=0;
for(const seq of s.surface.nodes){const e=s.eventAt(seq);if(e.type==='tool/result'&&!calls.has(e.data.message.source.callId))orphanResults++;}
process.stdout.write(JSON.stringify({before,after,humanInputsPreserved:humans.every(seq=>s.surface.nodes.includes(seq)),
 systemNodesPreserved:systems.every(seq=>s.surface.nodes.includes(seq)),restartEqual:JSON.stringify(restored.surface.nodes)===JSON.stringify(s.surface.nodes),orphanResults},null,2)+'\n');
