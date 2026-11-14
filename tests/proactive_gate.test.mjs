import test from 'node:test';
import assert from 'node:assert/strict';
import {createProactive} from '../plugin/proactive.mjs';

test('native scheduler idle tracking waits for admission, flush, and actual execution',async()=>{
  const finished=Promise.withResolvers(),persisted=Promise.withResolvers(),enqueued=Promise.withResolvers();
  let started=false,idleReads=0;
  const agent={session:{id:'chat'},inbox:{nextTurn:[],nextStep:[]},status:'idle',
    followup(){started=true;this.status='running';enqueued.resolve();},
    whenIdle(){idleReads++;assert.ok(started);return finished.promise;}};
  const gate=createProactive({},{}).gate({flush:()=>persisted.promise});
  const wrapped=gate.agent(agent);
  wrapped.followup({id:'input',source:{kind:'tool-jobs'},content:[]});
  const idle=wrapped.whenIdle();let settled=false;idle.then(()=>{settled=true;});
  await Promise.resolve();assert.equal(idleReads,0);assert.equal(started,false);
  const flush=gate.sessions.flush(agent.session);
  await enqueued.promise;assert.equal(started,true);assert.equal(idleReads,0);
  assert.equal(wrapped.status,'running');assert.equal(wrapped.inbox,agent.inbox);
  persisted.resolve(true);assert.equal(await flush,true);
  await Promise.resolve();assert.equal(idleReads,1);assert.equal(settled,false);
  finished.resolve();await idle;assert.equal(settled,true);
});
