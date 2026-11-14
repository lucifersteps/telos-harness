// Diagnose a dependency-waiting bootstrap after the host finishes mounting.
// This observer never owns channel startup, sessions or business components.
import {requireHost} from './bootstrap.mjs';
export const name='telos-host-check';
export const inject=['cmdlineArgs'];
export function apply(ctx) {
  const check=()=>{
    try {requireHost(ctx);}
    catch(error) {
      console.error('Telos: '+error.message);
      if(ctx.get('profileContext')?.name!=='desktop')ctx.get('appExit')(1);
    }
  };
  const ready=ctx.get('appReady');
  if(ready)ctx.effect(()=>ready.onReady(check));
  else check();
}
