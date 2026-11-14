// Per-turn evidence coverage, not a persisted diagnosis or another scheduler.
const parts=['goals','actions','calendar'];
const labels={goals:'目标清单',actions:'行动提醒事项（含逾期和无日期事项）',calendar:'完整当日日程'};
const allowed=new Set(['action','kind','calendar_ids','include_completed','include_overdue','offset','limit','start','end']);
const zoned=s=>typeof s==='string'&&/(Z|[+-]\d{2}:\d{2})$/i.test(s)&&Number.isFinite(Date.parse(s));
function covers(actual,expected) {
  if(actual!==undefined&&(!Array.isArray(actual)||actual.some(x=>typeof x!=='string'||!x)))return false;
  return !actual?.length||expected.length>0&&expected.every(id=>actual.includes(id));
}
function classify(args,requirements) {
  if(args.action!=='query'||Object.keys(args).some(k=>!allowed.has(k)))return [];
  if(args.include_overdue!==undefined&&(args.kind!=='reminder'||args.include_overdue!==true))return [];
  if(args.offset!==undefined&&(!Number.isInteger(args.offset)||args.offset<0))return [];
  if(args.limit!==undefined&&(!Number.isInteger(args.limit)||args.limit<1||args.limit>500))return [];
  if(args.kind==='reminder'&&args.include_completed===true&&!args.start&&!args.end) {
    return [['goals',requirements.goal_list_ids],['actions',requirements.action_list_ids]]
      .filter(([,ids])=>covers(args.calendar_ids,ids)).map(([key])=>key);
  }
  if(args.kind==='event'&&covers(args.calendar_ids,[])&&zoned(args.start)&&zoned(args.end)&&
    Date.parse(args.start)<=Date.parse(requirements.day_start)&&Date.parse(args.end)>=Date.parse(requirements.day_end))return ['calendar'];
  return [];
}
export function createPlanCheck() {
  let turn=null;
  return {
    bind(event,requirements,available=true) {
      if(!event){turn=null;return;}
      const identity=JSON.stringify([requirements,available]);
      if(turn?.id!==event.id||turn?.input!==event.input_id||turn?.identity!==identity)
        turn={id:event.id,input:event.input_id,identity,requirements,available,reads:[]};
      else for(const read of turn.reads)read.reviewed=true;
    },
    reset(){turn=null;},
    async execute(args,exec,run) {
      const owner=turn;
      const keys=owner?.requirements?classify(args,owner.requirements):[];
      let result,outcome='failed';
      try {result=await run();outcome=result?.is_error||result?.isError||['error','failed','unavailable'].includes(result?.status)?'failed':'returned';return result;}
      finally {
        if(keys.length&&turn===owner&&!exec.signal?.aborted) {
          const {offset=0,...query}=args;
          const signature=JSON.stringify(Object.fromEntries(Object.entries(query).filter(([k])=>k!=='limit').sort(([a],[b])=>a.localeCompare(b))));
          // Malformed responses permit uncertainty, never evidence of an empty plan.
          if(outcome==='returned'&&(!Array.isArray(result?.items)||!Number.isInteger(result?.total)||result.total<0||result.offset!==offset))outcome='failed';
          const missingSources=keys.filter(key=>{
            if(key==='calendar')return false;
            const ids=owner.requirements[key==='goals'?'goal_list_ids':'action_list_ids'];
            return key==='goals'&&!ids.length||outcome==='returned'&&!ids.every(id=>result.calendars?.some(c=>c.id===id));
          });
          owner.reads.push({keys,signature,offset,total:result?.total,count:result?.items?.length??0,
            outcome,missingSources,call_id:exec.callId,reviewed:false});
        }
      }
    },
    status() {
      // A trusted host setting can disable the tool entirely. No impossible
      // query loop: only an uncertainty-aware notification is allowed then.
      if(turn?.available===false)return Object.fromEntries(parts.map(k=>[k,'unavailable']));
      return Object.fromEntries(parts.map(key=>{
        const reads=(turn?.reads??[]).filter(r=>r.reviewed&&r.keys.includes(key));
        if(reads.some(r=>r.outcome==='failed'||r.missingSources.includes(key)))return [key,'unavailable'];
        for(const signature of new Set(reads.map(r=>r.signature))) {
          const pages=reads.filter(r=>r.signature===signature).sort((a,b)=>a.offset-b.offset);
          let end=0;
          const total=Math.max(...pages.map(r=>r.total));
          for(const p of pages){if(p.offset>end)break;end=Math.max(end,p.offset+p.count);}
          if(pages[0]?.offset===0&&end>=total)return [key,'read'];
        }
        return [key,reads.length?'needs_pagination':'missing'];
      }));
    },
    assertRead() {
      const missing=Object.entries(this.status()).filter(([,state])=>!['read','unavailable'].includes(state));
      if(missing.length)throw new Error('TELOS_PLAN_REQUIRED: 本轮发送前仍需读取：'+missing.map(([key,state])=>labels[key]+(state==='needs_pagination'?'（补齐分页）':'')).join('、')+
        '。按可信上下文 policy.context.query_requirements 查询：提醒事项用 kind=reminder, include_completed=true，不加日期/关键词/标签筛选，以覆盖逾期、无日期事项；目标按 goal_list_ids，行动按 action_list_ids（空数组表示所有可读清单）。一次不筛清单的完整查询可覆盖两者。日程用 kind=event 覆盖 day_start 至 day_end，不筛日历。返回 total 大于已读条数时继续 offset 分页。等待结果进入下一模型步骤再发送；真实读取失败允许未知降级，不反复重试。');
    }
  };
}
