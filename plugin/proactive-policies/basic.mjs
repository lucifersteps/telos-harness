export const id='basic';
export const dependencies=[];
export const instructions='当前为基础主动联系策略：结合本轮提醒目的和近期对话决定联系内容，不要求固定业务工具或目标诊断标签。notification_required=true 时发送一次；否则没有值得联系的具体事项可以静默。不沿用之前策略的强制查询、分类或督促口吻。';
export function create(){return {
 bind(){},reset(){},execute(_name,_args,_exec,run){return run();},beforeNotify(){},
 allowsRestrictedRead(){return false;},context(){return {};}
};}
