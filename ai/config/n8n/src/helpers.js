// ---- helpers ---------------------------------------------------------------
// Settings come from the container environment (ai/.env)
const setting = (name, fallback) => {
    const value = $env[name];
    return value === undefined || value === '' ? fallback : String(value);
};
const ZONE = setting('TZ', 'Europe/London');
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Whether a webhook call carries the ingest service's NOTIFY_TOKEN
const fromIngest = (request) => {
    const token = setting('NOTIFY_TOKEN', '');
    return !token || (request.headers || {})['x-notify-token'] === token;
};

// Colours: the Premier League's purple and its gradient for the brand; the plan's statuses
// in the status colours, always with their icons (as on the analysis page)
const CLUB = '#37003c', NAVY = '#37003c', INK = '#1f0024', MUTED = '#725d79', LINE = '#e3d9e6';
// The league's gradient, as table cells (email clients don't draw gradients)
const GRADIENT = ['#04f5ff', '#00ff85', '#e90052', '#963cff'];
const STRIPES = `<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>${
  GRADIENT.map((c) => `<td height="6" style="height:6px;font-size:0;line-height:0;background:${c}">&nbsp;</td>`).join('')}</tr></table>`;
const STATUS = { on_plan: ['#0b8a0b', '✓ on plan'], drifting: ['#b7791f', '~ drifting'], off_plan: ['#c42f2f', '✕ off plan'],
                 no_data: [MUTED, '– no data'] };
const CALLS = { pending_review: 'Not called yet', confirmed: 'Flagged for the bench', dismissed: 'Expected' };

const score = (m) => m.score || `${m.home.code} ${m.score.home}-${m.score.away} ${m.away.code}`;
const scoreOf = (m) => `${m.home.code} ${m.score.home}-${m.score.away} ${m.away.code}`;
const when = (iso) => DateTime.fromISO(iso).setZone(ZONE).toFormat("ccc d LLL, HH:mm");
const roomUrl = () => setting('ANALYSIS_URL', 'http://localhost:8090/analysis/');
// The analysis room, opened on one deviation or play
const roomLink = (site, matchId, ref) => roomUrl() + '#' + [site, matchId, ref].filter(Boolean).map(encodeURIComponent).join('/');
const reportLink = (site, matchId, part) => roomUrl() + `report.html?part=${part}#` + [site, matchId].map(encodeURIComponent).join('/');
const chip = (status) => `<span style="display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid ${STATUS[status][0]};`
    + `color:${STATUS[status][0]};font-size:12px;font-weight:600;white-space:nowrap">${esc(STATUS[status][1])}</span>`;
const button = (href, text) => `<a href="${esc(href)}" style="display:inline-block;background:${CLUB};color:#fff;text-decoration:none;`
    + `font-weight:600;padding:10px 18px;border-radius:6px">${esc(text)}</a>`;
// An email: the band with the brand and the match, then the body
const page = (title, match, body) => `<!doctype html><html><body style="margin:0;background:#f7f4f8;font-family:Barlow,'Segoe UI',Arial,sans-serif;color:${INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f4f8"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" width="660" cellpadding="0" cellspacing="0" style="max-width:660px;width:100%;background:#fff;border:1px solid ${LINE};border-radius:8px;overflow:hidden">
<tr><td style="padding:0">${STRIPES}</td></tr>
<tr><td style="background:${NAVY};color:#fff;padding:13px 22px;font-size:13px;font-weight:600">Match Intelligence · ${esc(title)}</td></tr>
<tr><td style="padding:18px 22px 4px;font-size:26px;font-weight:800;letter-spacing:.01em">${esc(match.home.code)}
  <span style="background:${CLUB};color:#fff;padding:2px 10px;border-radius:4px;margin:0 6px">${match.score.home} – ${match.score.away}</span>${esc(match.away.code)}</td></tr>
<tr><td style="padding:0 22px 14px;color:${MUTED};font-size:13px">${esc(match.home.name)} v ${esc(match.away.name)} · ${esc(match.competition)}`
    + `${match.matchday ? ' · Matchday ' + match.matchday : ''}${match.venue ? ' · ' + esc(match.venue) : ''}</td></tr>
<tr><td style="padding:0 22px 22px">${body}</td></tr>
<tr><td style="padding:14px 22px;border-top:1px solid ${LINE};color:${MUTED};font-size:12px">Measured by the club's match device every 5 minutes; the plan and the calls are the staff's.${match.synthetic ? ' Demo data (synthetic).' : ''}</td></tr>
</table></td></tr></table></body></html>`;
