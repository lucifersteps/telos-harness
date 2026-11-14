import test from 'node:test';
import assert from 'node:assert/strict';
import {allowsHabitRead,MENTOR_PROMPT} from '../plugin/mentor.mjs';
import {proactiveContextView} from '../plugin/runtime-context.mjs';

test('midnight permits only habit reads; ordinary proactive checks cannot inspect habits',()=>{
  for(const event of [null,{}, {habit_reminder_allowed:false}]) {
    assert.equal(allowsHabitRead(event,'habit_context'),false);
    assert.equal(allowsHabitRead(event,'habit_tasks',{action:'status'}),false);
  }
  const event={habit_reminder_allowed:true};
  assert.equal(allowsHabitRead(event,'habit_context'),true);
  for(const action of ['list','status'])assert.equal(allowsHabitRead(event,'habit_tasks',{action}),true);
  for(const action of ['complete','miss','pause','resume','notes','append_note',undefined])
    assert.equal(allowsHabitRead(event,'habit_tasks',{action}),false);
  assert.equal(allowsHabitRead(event,'habit_streaks',{action:'inspect'}),false);
});

test('model sees delivery history, future checks and trusted midnight flag without personal seed facts',()=>{
  const context={settings:{},events:[{kind:'fixed',due:1,expires:2,state:'running',habit_reminder_allowed:true}],recent:[],
    recent_notifications:[{state:'sent',message:'已经问过进展'}],pending_checks:[{kind:'fixed',due:3,prompt:'中午跟进'}]};
  const view=proactiveContextView(context);
  assert.equal(view.events[0].habit_reminder_allowed,true);
  assert.deepEqual(view.recent_notifications,context.recent_notifications);
  assert.deepEqual(view.pending_checks,context.pending_checks);
  assert.ok(!MENTOR_PROMPT.includes('2026-11-05'));
  assert.ok(!MENTOR_PROMPT.includes('博士论文'));
});
