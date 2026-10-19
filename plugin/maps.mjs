import {registerTools} from './bridge.mjs';
export const name='telos-maps';
export const inject=['tools','telosBusiness'];
export const apply=ctx=>registerTools(ctx,'maps');
