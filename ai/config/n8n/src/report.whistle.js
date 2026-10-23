// ---- half time or full time → which match and which report -----------------
// The ingest service posts here when a match's status becomes half_time or
// full_time. The report waits (next node), so the analyst can make the last calls.
const request = $input.first().json;
if (!fromIngest(request)) return [];
const match = (request.body || {}).match;
if (!match || !match.match_id) return [];
const part = (request.body || {}).event === 'half_time' ? 'half' : 'full';
const delay = Number(setting(part === 'half' ? 'HALFTIME_DELAY' : 'REPORT_DELAY', part === 'half' ? '60' : '300'));
return [{ json: { site_id: match.site_id, match_id: match.match_id, part, delay: delay >= 0 ? delay : 0 } }];
