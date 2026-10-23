// ---- GET /webhook/brighton/report?match_id=…&part=half|full → which match ----
// All optional: without them, the match being played (or the latest), and the match report.
const query = $input.first().json.query || {};
return [{ json: { site_id: query.site_id || '', match_id: query.match_id || '', part: query.part === 'half' ? 'half' : 'full' } }];
