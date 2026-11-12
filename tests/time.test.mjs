import assert from 'node:assert/strict';
import test from 'node:test';
import {formatTime, messageTimestamp, calendarDay, dayStart, addDays} from '../src/shinbot/web/time.js';

test('display timezone changes the day without changing the UTC instant',()=>{
  const instant=new Date('2026-10-26T01:00:00Z');
  assert.equal(calendarDay(instant,'Asia/Kolkata'),'2026-10-26');
  assert.equal(calendarDay(instant,'America/Los_Angeles'),'2026-10-25');
  assert.match(formatTime(instant.toISOString(),'Asia/Kolkata',{hour:'2-digit',minute:'2-digit',hourCycle:'h23'}),/06:30/);
  assert.notEqual(formatTime(instant.valueOf()/1000,'UTC'),formatTime(instant.valueOf()/1000,'Asia/Kolkata'));
  assert.equal(formatTime(null,'UTC'),'—');
  assert.doesNotThrow(()=>formatTime(0,'invalid-zone'));
});

test('calendar ranges follow selected timezone across DST gaps and folds',()=>{
  assert.equal(dayStart('2026-03-08','America/New_York').toISOString(),'2026-03-08T05:00:00.000Z');
  assert.equal((dayStart('2026-03-09','America/New_York')-dayStart('2026-03-08','America/New_York'))/3600000,23);
  assert.equal((dayStart('2026-11-02','America/New_York')-dayStart('2026-11-01','America/New_York'))/3600000,25);
  assert.equal(dayStart('2026-10-26','Asia/Kolkata').toISOString(),'2026-10-25T18:30:00.000Z');
  assert.equal(addDays('2026-12-31',1),'2027-01-01');
});


test('message dates keep their UTC instant while displaying the chosen timezone',()=>{
  const instant=Date.parse('2026-10-28T23:45:00Z')/1000;
  const india=messageTimestamp(instant,'Asia/Kolkata'),utc=messageTimestamp(instant,'UTC');
  assert.equal(india.iso,'2026-10-28T23:45:00.000Z');
  assert.equal(india.iso,utc.iso);
  assert.match(india.label,/29/);
  assert.match(utc.label,/28/);
  assert.notEqual(india.label,utc.label);
  for(const missing of [undefined,null,'',Infinity,NaN,'bad-date'])assert.equal(messageTimestamp(missing,'UTC'),null);
});
