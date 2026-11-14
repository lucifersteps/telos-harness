import {registerTools} from './bridge.mjs';
export const name='telos-activity';
export const inject=['tools','telosBusiness'];
export const apply=(ctx,config={})=>registerTools(ctx,'activity',undefined,config.execute);
