// ---- a deviation from the plan → the staff ---------------------------------
// The ingest service posts every deviation that opens while the match is on.
// Only the priorities in NOTIFY_PRIORITY (default high: the plan's key
// targets) go out; the rest wait in the analysis room.
const request = $input.first().json;
if (!fromIngest(request)) return [];
const { deviation: d, match } = request.body || {};
if (!d || !match) return [];
const wanted = setting('NOTIFY_PRIORITY', 'high').split(',').map((s) => s.trim());
if (!wanted.includes('all') && !wanted.includes(d.priority)) return [];
const to = setting('STAFF_EMAIL_TO', '');

const subject = `${d.minute}' ${d.title} — ${scoreOf(match)}`;
const link = roomLink(d.site_id, d.match_id, d.deviation_id);
const text = [
    `${d.minute}' · ${d.title}${d.key ? ' (key instruction)' : ''}`,
    '',
    d.summary,
    `What to check: ${d.what_to_check}`,
    '',
    `Plan: ${d.plan_title || ''}`,
    `Open it in the analysis room: ${link}`,
    ...(d.synthetic_data ? ['', 'Demo data (synthetic).'] : []),
].join('\n');

const body = `
<p style="margin:0 0 10px"><b style="font-size:19px">${esc(d.title)}</b>
  <span style="font-size:19px;color:${MUTED};margin-left:8px;font-family:'IBM Plex Mono',monospace">${esc(d.minute)}'</span>
  ${d.key ? ' <span style="color:' + CLUB + ';font-weight:600">★ key instruction</span>' : ''}</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="font-size:14px;margin-bottom:12px">
<tr><td style="color:${MUTED};padding:2px 14px 2px 0">${esc(d.stretch)}</td><td style="font-family:'IBM Plex Mono',monospace">${esc(d.value)} ${chip('off_plan')}</td></tr>
<tr><td style="color:${MUTED};padding:2px 14px 2px 0">The plan asks</td><td style="font-family:'IBM Plex Mono',monospace">${esc(d.target)}</td></tr>
<tr><td style="color:${MUTED};padding:2px 14px 2px 0">Match so far</td><td style="font-family:'IBM Plex Mono',monospace">${esc(d.match_value)}</td></tr>
</table>
<p style="margin:0 0 16px;color:#39503f">${esc(d.what_to_check)}</p>
${button(link, 'Open in the analysis room')}`;

return [{ json: { to, subject, text, html: page('Off the plan', match, body), deviation_id: d.deviation_id, priority: d.priority } }];
