// Read-only capability ceiling for unattended execution. Policies can restrict
// these permissions, never add writes or choose a recipient.
const plain=new Set(['notify_user','schedule_next_check','session_event_read','session_event_search','web_fetch','web_search','read','read_image','glob','grep','skill','baidu_maps']);
export function allowsProactiveRead(exec) {
 if(plain.has(exec.name))return true;
 if(exec.name==='personalPlan')return ['query','detail'].includes(exec.arguments?.action);
 if(exec.name==='user_memory')return exec.arguments?.action==='read';
 return false;
}
