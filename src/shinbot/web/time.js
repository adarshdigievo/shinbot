export function displayZone(preference) {
  try { return new Intl.DateTimeFormat('en', {timeZone:preference || undefined}).resolvedOptions().timeZone; }
  catch { return Intl.DateTimeFormat().resolvedOptions().timeZone; }
}

export function formatTime(value, zone, options={dateStyle:'medium', timeStyle:'short'}) {
  if (value === null || value === undefined || value === '') return '—';
  const instant = new Date(typeof value === 'number' ? value*1000 : value);
  return Number.isNaN(instant.valueOf()) ? '—' : new Intl.DateTimeFormat(undefined, {...options,timeZone:displayZone(zone)}).format(instant);
}

export function messageTimestamp(value, zone) {
  if (value === null || value === undefined || value === '') return null;
  const instant = new Date(typeof value === 'number' ? value * 1000 : value);
  if (!Number.isFinite(instant.valueOf())) return null;
  return {iso:instant.toISOString(), label:formatTime(value, zone), title:formatTime(value, zone, {dateStyle:'full', timeStyle:'long'})};
}

export function calendarDay(value, zone) {
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone:displayZone(zone),year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(value);
  const part = name => parts.find(p=>p.type===name).value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}

export function dayStart(day, zone) {
  // Find the first instant of this local date, including days whose midnight is skipped by DST.
  const noon = Date.parse(day+'T12:00:00Z')/1000;
  let low = noon-48*3600, high = noon+48*3600;
  while (low<high) {
    const middle = Math.floor((low+high)/2);
    if (calendarDay(new Date(middle*1000),zone)<day) low=middle+1; else high=middle;
  }
  return new Date(low*1000);
}

export function addDays(day, count) {
  const date=new Date(day+'T12:00:00Z');date.setUTCDate(date.getUTCDate()+count);
  return date.toISOString().slice(0,10);
}
