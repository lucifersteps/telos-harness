import {registerTools} from './bridge.mjs';
export const name='telos-apple';
export const inject=['tools','telosBusiness'];
export const apply=ctx=>registerTools(ctx,'apple');
