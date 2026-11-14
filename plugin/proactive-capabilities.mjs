// Read-only capability ceiling for unattended execution. Policies can restrict
// these permissions, never add writes or choose a recipient.
const plain=new Set(['notify_user','schedule_next_check','session_event_read','session_event_search','web_fetch','web_search','read','read_image','glob','grep','skill','baidu_maps','activity_history']);
export function allowsProactiveRead(exec,policy) {
 if(plain.has(exec.name))return true;
 if(exec.name==='personalPlan')return ['query','detail'].includes(exec.arguments?.action);
 if(exec.name==='user_memory')return exec.arguments?.action==='read';
 if(exec.name==='habit_context'||exec.name==='habit_tasks')
   return (exec.name==='habit_context'||['list','status'].includes(exec.arguments?.action))&&policy.allowsRestrictedRead(exec.name,exec.arguments);
 return /^mcp__.*__(health_activity_history|health_detail|heartbeat|get_summary|get_data_status)$/.test(exec.name);
}
