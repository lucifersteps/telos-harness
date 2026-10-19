import {registerTools} from './bridge.mjs';
export const name='telos-maps';
export const inject=['tools','telosBusiness','credentials'];
export const apply=ctx=>registerTools(ctx,'maps',async()=>({map_token:(await ctx.credentials.resolve('BAIDU_MAP_AUTH_TOKEN'))?.value||''}));
