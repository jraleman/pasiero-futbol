// ---- the half-time or match report → the staff -----------------------------
// From the ingest service's agent API (Read match, Read plan): the score, the
// plan's targets with their status, the deviations and the analyst's calls,
// both teams' numbers (set pieces and running too), who is running less than
// in his first 30', and the key plays. Emailed as HTML, with a CSV of every
// target in every 5-minute window. The narrator agent's story of the half or
// the match goes first when the agent wrote one (Narrator's story).
const which = $('Which report').first().json;
const summary = $('Read match').first().json;
const plan = $('Read plan').first().json;
const match = summary.match;
const half = which.part === 'half';
const title = half ? 'Half-time report' : 'Match report';
const subject = `${title}: ${scoreOf(match)} · ${DateTime.fromISO(match.kickoff).setZone(ZONE).toFormat('d LLL yyyy')}`;
const cols = half ? [['first_half', '1st half'], ['first_half_last_15', "Last 15' of the half"]] : [['first_half', '1st half'], ['second_half', '2nd half'], ['match', 'Match']];
const td = (v, style = '') => `<td style="padding:5px 8px;border-bottom:1px solid ${LINE};vertical-align:top;${style}">${v}</td>`;
const th = (v, align = 'left') => `<th align="${align}" style="padding:5px 8px;border-bottom:2px solid ${LINE};color:${MUTED};font-weight:600;font-size:12px">${esc(v)}</th>`;
const targets = plan.targets || [];
const planTable = targets.length ? `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">
<tr>${th('Target')}${th('Plan')}${cols.map(([, l]) => th(l)).join('')}</tr>
${targets.map((t) => '<tr>' + td((t.key ? '★ ' : '') + esc(t.name)) + td(esc(t.target), "font-family:'IBM Plex Mono',monospace")
    + cols.map(([k]) => td(`<span style="font-family:'IBM Plex Mono',monospace">${esc(t[k].text)}</span> ${chip(t[k].status)}`, 'white-space:nowrap')).join('') + '</tr>').join('')}
</table>` : `<p style="color:${MUTED}">No game plan for this match.</p>`;
const devs = (summary.deviations || []).filter((d) => !half || d.period === 1);
const devList = devs.length ? `<ol style="margin:0;padding-left:20px;font-size:13.5px">${devs.map((d) => `<li style="margin-bottom:5px"><b>${esc(d.minute)}'</b> ${esc(d.title)}`
    + ` · <span style="color:${d.status === 'confirmed' ? CLUB : MUTED};font-weight:600">${esc(CALLS[d.status] || d.status)}</span>`
    + `${d.note ? ` · <i>“${esc(d.note)}”</i>` : ''}${d.open ? '' : ' · back on plan'}</li>`).join('')}</ol>`
    : `<p style="color:${MUTED};margin:0">No deviations from the plan${half ? ' in the first half' : ''}.</p>`;
const us = summary.numbers ? summary.numbers.us : null, them = summary.numbers ? summary.numbers.opponent : null;
const pct = (v) => (v === null || v === undefined ? '–' : Math.round(v) + '%');
const num = (v, d = 1) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));
const rows = us ? [['Possession', pct(us.possession_pct), pct(them.possession_pct)], ['Pass completion', pct(us.pass_accuracy_pct), pct(them.pass_accuracy_pct)],
    ['PPDA', num(us.ppda), num(them.ppda)], ['Shots (on target)', `${us.shots} (${us.shots_on_target})`, `${them.shots} (${them.shots_on_target})`],
    ['xG', num(us.xg, 2), num(them.xg, 2)], ['High regains', us.regains_high, them.regains_high],
    ['Set pieces into the box (first contact)', `${us.set_pieces} (${pct(us.set_piece_first_contact_pct)})`, `${them.set_pieces} (${pct(them.set_piece_first_contact_pct)})`],
    ['Set-piece xG', num(us.set_piece_xg, 2), num(them.set_piece_xg, 2)],
    ['High-speed running', `${us.hsr_m} m`, `${them.hsr_m} m`], ['Sprints', us.sprints, them.sprints],
    ['Line height', num(us.line_height_m) + ' m', num(them.line_height_m) + ' m'], ['Shape in / out of possession',
    `${us.shape_in_possession || '–'} / ${us.shape_out_of_possession || '–'}`, `${them.shape_in_possession || '–'} / ${them.shape_out_of_possession || '–'}`]] : [];
const numbers = rows.length ? `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;border-collapse:collapse">
<tr>${th('')}${th(match.us.code, 'right')}${th(match.opponent.code, 'right')}</tr>
${rows.map(([k, a, b]) => '<tr>' + td(esc(k)) + td(esc(a), "text-align:right;font-family:'IBM Plex Mono',monospace") + td(esc(b), "text-align:right;font-family:'IBM Plex Mono',monospace") + '</tr>').join('')}</table>` : '';
const plays = (summary.key_plays || []).filter((p) => !half || p.period === 1).slice(-10);
const drops = (summary.running_drops_last_15 || []).filter((d) => d.drop_pct >= 10);
const running = drops.length ? `<p style="margin:10px 0 0;font-size:13.5px"><b>Running, last 15'${half ? ' of the half' : ''}:</b> `
    + drops.map((d) => `#${esc(d.number)}${d.name ? ' ' + esc(d.name) : ''} ${Math.round(d.drop_pct)}% less ground a minute than in his first 30' (${esc(d.m_per_min_now)} m against ${esc(d.m_per_min_first_30)})`).join('; ') + '.</p>' : '';
const share = plan.on_plan_share === null || plan.on_plan_share === undefined ? '–' : Math.round(plan.on_plan_share * 100) + '%';
let story = null;
try {
    const told = $("Narrator's story").first().json;
    if (told && told.recap) story = told;
} catch (e) { story = null; }
const storyBlock = story ? `<h3 style="margin:0 0 8px;font-size:16px">The story of the ${half ? 'half' : 'match'}</h3>
<div dir="auto" style="font-size:14px;line-height:1.55">${story.recap.split(/\n+/).filter((x) => x.trim()).map((x) => `<p style="margin:0 0 8px">${esc(x)}</p>`).join('')}</div>
<p style="margin:0 0 18px;color:${MUTED};font-size:12px">Written by ${story.backend === 'azure' ? 'the narrator agent on Azure OpenAI' : 'the offline model'}, from the match device's numbers.</p>` : '';

const body = `
<p style="margin:0 0 4px;font-size:13px;color:${MUTED}">${esc(when(match.kickoff))}${half ? ' · at half time' : ''}</p>
${storyBlock}
<p style="margin:0 0 14px;font-size:16px"><b>${esc((plan.plan && plan.plan.title) || 'Game plan')}</b> · <b>${esc(share)}</b> of targets on plan so far</p>
${planTable}
<h3 style="margin:20px 0 8px;font-size:16px">For the ${half ? 'half-time talk' : 'debrief'}</h3>
${devList}
<h3 style="margin:20px 0 8px;font-size:16px">The numbers${half ? ' at half time' : ''}</h3>
${numbers}
${running}
${plays.length ? `<h3 style="margin:20px 0 8px;font-size:16px">Key plays</h3><ul style="margin:0;padding-left:20px;font-size:13.5px">${plays.map((p) => `<li><b>${esc(p.minute)}'</b> ${esc(p.summary)}</li>`).join('')}</ul>` : ''}
<p style="margin:20px 0 0">${button(reportLink(summary.match.site_id || which.site_id, match.match_id, which.part), 'Open the printable ' + title.toLowerCase())}</p>
<p style="margin:12px 0 0;color:${MUTED};font-size:12px">Every target in every 5 minutes is in the attached CSV. Generated by p4n4, ${esc(DateTime.now().setZone(ZONE).toFormat('yyyy-MM-dd HH:mm'))}.</p>`;

const csvCell = (v) => (v === null || v === undefined ? '' : /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v));
const csv = [['target', 'plan', 'key', 'window', 'status', 'value']];
for (const t of plan.targets || []) {
    for (const w of t.windows || []) csv.push([t.name, t.target, t.key ? 'yes' : '', w.label, w.status, w.value]);
}
const csvText = '﻿' + csv.map((r) => r.map(csvCell).join(',')).join('\n') + '\n';
const name = (half ? 'half-time-' : 'match-report-') + match.match_id;
const to = setting(half ? 'STAFF_EMAIL_TO' : 'REPORT_EMAIL_TO', '');
return [{
    json: { to, subject, html: page(title, { ...match, synthetic: summary.synthetic_data }, body), match_id: match.match_id, part: which.part,
            score: scoreOf(match), on_plan_share: plan.on_plan_share, deviations: devs.length, file: name + '.csv' },
    binary: { csv: { data: Buffer.from(csvText, 'utf8').toString('base64'), mimeType: 'text/csv', fileName: name + '.csv' } },
}];
