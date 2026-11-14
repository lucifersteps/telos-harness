import {createActivityCheck} from './activity-check.mjs';
import {createPlanCheck} from './plan-check.mjs';
import {MENTOR_PROMPT,allowsHabitRead} from './prompt.mjs';
export const id='goal_execution';
export const dependencies=['personalPlan','activity_history'];
export const instructions="Every active proactive check (heartbeat, fixed reminder, and followup) must call activity_history in THIS turn using a relevant explicit time range and timeline/summary/detail, and wait for the result before notify_user. When observation_enabled=true, attempt all three observed sources (aw, screen_time, health) over the full observation_window, in one default-source call or separate calls covering that same interval. Subsequent evidence queries may use any source combination and relevant interval, including health alone; they never invalidate completed observation reads. Without independent observation, any observed source can satisfy the activity prerequisite. A status-only or calendar-only query, old tool result, or claim of having checked is insufficient. If the read fails or data is partial/missing, acknowledge uncertainty and continue with a degraded notification when required; never invent inactivity or retry indefinitely. Read current goals AND today's action items and calendar using personalPlan query/detail where available, then assess plan alignment and goal contribution separately. A monthly-goal read is not a daily-plan read. For claims about today's calendar, query the entire local day, including the remaining hours; a narrow activity query cannot establish that the whole day is free. This prerequisite applies regardless of custom event prompts; it does not change scheduling, followup send conditions, or notification frequency.\nBefore notify_user also complete this turn's goal/action/calendar reads under policy.context.query_requirements and read their results in a subsequent model step. Do not treat a goal-only read as an action read. Complete pagination; a settled source failure permits unknown degradation. Diagnostic priority is plan_missing, then execution_gap if a plan exists, otherwise on_track when supported; use unknown for insufficient evidence. In notify_user, assessment must be exactly one of the four strings: plan_missing, execution_gap, on_track, unknown. Send only that label, without a colon, explanation or reasoning suffix; for example assessment=execution_gap. Keep reasoning out of this field. Be direct about the concrete problem and action needed, without shaming, device/time reports or invented urgency. These evidence and diagnostic rules apply even to legacy custom prompts asking for a generic check report.\n仅在当前主动检查且本策略启用时适用；notification_required 决定本轮是否必发。普通聊天不受本策略查询前置条件限制。\n"+MENTOR_PROMPT;
export function create({now=Date.now}={}) {
  const activity=createActivityCheck(now),plan=createPlanCheck();
  let available={},event=null,observation=false;
  return {
    bind(current,context,capabilities){
      event=current;available=capabilities;
      observation=context.observation_enabled===true;
      activity.bind(current,observation?current?.observation_window:null);plan.bind(current,context.query_requirements,capabilities.personalPlan!==false);
    },
    reset(){event=null;activity.reset();plan.reset();},
    execute(name,args,exec,run){
      if(name==='activity_history')return activity.execute(args,exec,run);
      if(name==='personalPlan')return plan.execute(args,exec,run);
      return run();
    },
    beforeNotify(args){
      if(available.activity_history!==false)activity.assertRead();
      plan.assertRead();
      if(observation&&(typeof args.observation!=='string'||!args.observation.trim()||args.observation.length>1000))
        throw new Error('TELOS_OBSERVATION_REQUIRED: 请提供 observation，基于 observation_window 单独概括此前行为，信息不足就说明；不要把判断或建议写进观察。');
      if(!['plan_missing','execution_gap','on_track','unknown'].includes(args.assessment))
        throw new Error('TELOS_ASSESSMENT_REQUIRED: 当前策略要求 assessment=plan_missing / execution_gap / on_track / unknown。');
      if((available.activity_history===false||Object.values(plan.status()).includes('unavailable'))&&args.assessment!=='unknown')
        throw new Error('TELOS_EVIDENCE_UNKNOWN: 目标或计划来源未配置、不可读或已缺失，或行为工具不可用，本轮只能 assessment=unknown；不能据此批评或表扬。');
    },
    allowsRestrictedRead(name,args){return allowsHabitRead(event,name,args);},
    context(){return {query_checks:plan.status(),...(observation?{activity_check:activity.status()}:{})};}
  };
}
