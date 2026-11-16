// Model-facing views only. Full records remain in the business service and
// tool handlers; DSH continues to own assembly, history and compaction.
export function stableContext(value) {
  return JSON.stringify(value,(_key,item)=>item&&typeof item==='object'&&!Array.isArray(item)
    ?Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]])):item);
}

export function businessContextView(state) {
  // This is the preparation timestamp, not an expiry or an observation time.
  // The host already supplies current time. Preserve receipts and confirmations.
  const {time,...current}=state;
  return current;
}

export function proactiveContextView(context) {
  const {settings}=context;
  const events=context.events.map(event=>({
    kind:event.kind,due:event.due,expires:event.expires,state:event.state,
    ...(event.budget_notice?{budget_notice:true}:{}),
    // Keep the current task's instructions explicit, even when they match preferences.
    prompt:event.prompt??(event.kind==='heartbeat'?settings.preferences:undefined),
    notification_required:event.notification_required??(event.kind==='heartbeat'||event.kind==='fixed'),
    ...(event.message?{message:event.message}:{}),...(event.detail?{detail:event.budget_notice?'额度收尾提醒':event.detail}:{})
  }));
  return {events,settings,recent:context.recent,
    ...(context.recent_budget_notices?{recent_budget_notices:context.recent_budget_notices}:{}),
    ...(context.recent_notifications?{recent_notifications:context.recent_notifications}:{}),
    ...(context.pending_checks?{pending_checks:context.pending_checks}:{})};
}

// Put the current task beside its native trigger, not only in an earlier state
// node. Keep the native identity/source so admission and delivery still bind it.
export function proactiveInput(message,event) {
  if(!event||message.id!==event.input_id||message.source?.kind!=='schedule')return message;
  const required=event.notification_required??(event.kind==='heartbeat'||event.kind==='fixed');
  const instruction='[TELOS CURRENT CHECK]\n'+
    (event.budget_notice
      ?'这是微信额度收尾提醒，只生成一个简短气泡并调用 notify_user(message)，不需要额外查询；发送后结束。\n'
      :required
      ?'这是本轮必须执行的主动通知任务。请先完成下方 prompt 要求的实时查询，再调用 notify_user 发送一条简短消息，最后结束本轮。没有新变化或查询失败也要简短如实通知。不能用“先不打扰”、普通最终文本或 schedule_next_check 代替本次 notify_user。此前静默记录及其他检查的条件不适用于本轮。\n'
      :'这是当前主动检查，按下方 prompt 和近期对话判断；需要通知时调用 notify_user，否则可静默。\n')+
    '本轮仅提供基础主动联系，不沿用历史目标督促、行为健康分析或诊断要求。事实不明时保留未知。\n'+
    stableContext({kind:event.kind,due:event.due,expires:event.expires,prompt:event.prompt});
  return {...message,content:[...message.content,{type:'text',text:instruction}]};
}
