// Read a session copy on stdin; never open or mutate a live DSH session.
const fs=require('node:fs');
const {Session}=require(process.env.DSH_NATIVE_ROOT+'/dsh-session/lib/index.js');
const {CurrentState,STATE_KIND}=require('../plugin/current-state.mjs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const session=Session.create('detached-context-audit',input.events);
const beforeNodes=[...session.surface.nodes];
const lastRuntime=beforeNodes.map(seq=>session.eventAt(seq)).findLast(e=>e.type==='user/message'&&e.data.source.kind==='runtime-context');
const state=new CurrentState(session,{now:()=>input.at});
const assembly={contexts:lastRuntime.data.source.sections.map(s=>({...s})),sections:[{name:'habit-coach:method',text:'verified-current-method'}]};
state.capture(assembly);state.commit();
const changed=beforeNodes.filter(seq=>!session.surface.nodes.includes(seq));
const added=session.surface.nodes.filter(seq=>!beforeNodes.includes(seq));
const texts=seqs=>seqs.flatMap(seq=>session.eventAt(seq).data.content??[]).filter(b=>b.type==='text').map(b=>b.text);
const restored=Session.create('detached-context-audit',session.snapshotEvents());
process.stdout.write(JSON.stringify({removed_texts:texts(changed),added_texts:texts(added),
  replaced_nodes:changed.length,new_state_nodes:added.filter(seq=>session.eventAt(seq).data.source.kind===STATE_KIND).length,
  nonruntime_removed:changed.filter(seq=>session.eventAt(seq).data.source.kind!=='runtime-context').length,
  restart_equal:JSON.stringify(restored.surface.nodes)===JSON.stringify(session.surface.nodes)}));
