// Transitional send prerequisite, scoped to one native proactive turn. No new
// persisted lifecycle or scheduler state; raw evidence stays in tool history.
const observedSources=['aw','screen_time','health'];
function qualifyingSources(args,now) {
  if(!['summary','timeline','detail'].includes(args.view??'timeline'))return false;
  const zoned=value=>typeof value==='string'&&/(Z|[+-]\d{2}:\d{2})$/i.test(value)&&Number.isFinite(Date.parse(value));
  if(!zoned(args.start)||!zoned(args.end))return false;
  const start=Date.parse(args.start),end=Date.parse(args.end);
  if(start>=now||end<=start||end-start>31*86400000)return false;
  // Omitted sources use the tool's default: all three observed sources.
  // Source selection is never a tool restriction; it only contributes evidence.
  const sources=args.sources??observedSources;
  if(!Array.isArray(sources)||!sources.length||
    !sources.every(s=>[...observedSources,'calendar'].includes(s)))return false;
  return observedSources.filter(s=>sources.includes(s));
}

export function createActivityCheck(now=Date.now) {
  let turn=null;
  const status=()=>{
    if(!turn?.window)return null;
    const completed=new Set(turn.reads.filter(r=>r.reviewed).flatMap(r=>r.sources));
    const settled=new Set(turn.reads.flatMap(r=>r.sources));
    const missing=observedSources.filter(s=>!completed.has(s));
    const unread=observedSources.filter(s=>!settled.has(s));
    return {state:missing.length?(unread.length?'read_required':'awaiting_review'):'ready',
      observation_window:turn.window,missing_sources:missing,
      ...(unread.length?{next_call:{name:'activity_history',arguments:{view:'summary',
        start:turn.window.start,end:turn.window.end,sources:unread}},
        instruction:'请按 next_call 查询完整区间并阅读结果后再 notify_user。起点是上次成功投递，不是上次检查；失败或未送达的检查不会推进起点。最近半小时/一小时的查询不能替代。查询失败可如实降级通知，无需反复重试同一失败来源。'}:{})};
  };
  return {
    status,
    bind(event,window=null) {
      if(!event){turn=null;return;}
      if(turn?.eventId!==event.id||turn?.inputId!==event.input_id)
        turn={eventId:event.id,inputId:event.input_id,reads:[],window};
      else for(const read of turn.reads)read.reviewed=true; // Next model step receives completed results.
    },
    reset(){turn=null;},
    async execute(args,exec,run) {
      const owner=turn;
      const sources=qualifyingSources(args,now());
      const qualifies=owner&&sources?.length&&(!owner.window||
        Date.parse(args.start)<=Date.parse(owner.window.start)+5000&&Date.parse(args.end)>=Date.parse(owner.window.end)-5000);
      let outcome='failed',result;
      try {
        result=await run();
        outcome=result?.is_error||['failed','error'].includes(result?.status)?'failed':result?.status??'returned';
      } finally {
        // A pending/cancelled read, human interruption, or a previous turn cannot
        // unlock delivery. A settled source/transport failure permits degradation.
        if(qualifies&&turn===owner&&!exec.signal?.aborted)
          owner.reads.push({call_id:exec.callId,sources,view:args.view??'timeline',start:args.start,end:args.end,outcome,reviewed:false});
      }
      // Preserve the requested query and its facts. Explain a coverage mismatch
      // immediately, rather than making the model guess after a rejected send.
      const check=turn===owner&&!exec.signal?.aborted?status():null;
      return check&&result&&typeof result==='object'&&!Array.isArray(result)
        ?{...result,proactive_observation:check}:result;
    },
    assertRead() {
      const completed=new Set(turn?.reads.filter(r=>r.reviewed).flatMap(r=>r.sources)??[]);
      const missing=turn?.window?observedSources.filter(s=>!completed.has(s)):[];
      if(!completed.size||missing.length)throw new Error('TELOS_ACTIVITY_REQUIRED: '+
        (turn?.window?JSON.stringify(status())+'\n':'')+
        '发送前必须在本轮调用 activity_history，使用 timeline/summary/detail 和带时区的 start/end，并等待结果进入下一步模型判断。'+
        (turn?.window?`独立观察要求 aw、screen_time、health 都尝试查询完整 observation_window；尚缺：${missing.join(', ')}。省略 sources 可一次查询全部来源，也可分开查询同一完整区间。`:'至少查询一种实际行为来源（aw、screen_time 或 health）。')+
        '仅 status、仅日程或历史结果不算。已完成或已失败的查询不因后续补查失效；补查允许任意来源组合和相关时段。查询失败可说明无法核实并降级发送，不要重复失败查询。');
    }
  };
}
