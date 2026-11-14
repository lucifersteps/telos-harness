import test from 'node:test';
import assert from 'node:assert/strict';
import {startHealthReceiver} from '../plugin/health-receiver.mjs';

test('disabled receiver has no processes or disposal hooks',async()=>{
 const states=[];
 const result=await startHealthReceiver({effect(){throw new Error('Must not allocate');}}, {}, {},s=>states.push(s));
 assert.equal(result,undefined);assert.deepEqual(states,[{phase:'disabled'}]);
});
test('missing installation reports failure without taking down Telos',async()=>{
 const effects=[],states=[];
 await startHealthReceiver({effect(fn){effects.push(fn());}}, {pkg:'/missing'},
  {health_receiver:{enabled:true,root:'/nonexistent-telos-health-test'}},s=>states.push(s));
 assert.equal(states.at(-1).phase,'failed');
 await Promise.all(effects.map(fn=>fn()));
 assert.equal(states.at(-1).phase,'stopped');
});
