// The analysis room without the ingest service, for the static site (build.sh puts it before common.js): the
// API's answers come from data/ (record.py's recording, by key()), and what the staff do here (calls on
// deviations, notes, plans) stays in this browser and shows on every page.
"use strict";
(() => {
  const realFetch = window.fetch.bind(window);
  const API = /\/api\/v1\/(.*)$/;
  const local = {
    get(name) { try { return JSON.parse(localStorage.getItem(`static.${name}`)) || {}; } catch { return {}; } },
    set(name, value) { try { localStorage.setItem(`static.${name}`, JSON.stringify(value)); } catch {} },
  };

  // record.py's key(): the path after /api/v1/, its query sorted, anything but [A-Za-z0-9.-] as _
  function key(path, params) {
    const q = [...params].sort(([a, x], [b, y]) => (a < b ? -1 : a > b ? 1 : x < y ? -1 : 1)).map(([k, v]) => `${k}=${v}`).join("&");
    return (path + (q ? `@${q}` : "")).replace(/[^A-Za-z0-9.-]/g, "_");
  }
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const missing = () => json({ error: "Not in the demo's recording." }, 404);
  async function recorded(file) {
    const res = await realFetch(`data/${file}`);
    return res.ok ? res.json() : null;
  }
  let stories = null;
  let lastSite = "brighton";  // the site of the last match served, for a page that has none (a report without a link)

  // ── what the staff did here, over the recording ──
  const callKey = (site, id) => `${site}:${id}`;
  function withCalls(match) {
    const calls = local.get("calls");
    for (const d of match.deviations || []) {
      const c = calls[callKey(d.site_id, d.deviation_id)];
      if (c) Object.assign(d, c);
    }
    return match;
  }
  function pendingOf(site, matchId, recordedPending) {
    const calls = local.get("calls"), changed = Object.entries(calls).filter(([k]) => k.startsWith(`${site}:${matchId}.`));
    return changed.reduce((n, [, c]) => n + (c.status === "pending_review" ? 1 : 0) - (c.was === "pending_review" ? 1 : 0), recordedPending);
  }

  async function answer(method, path, params, body) {
    const site = params.get("site_id") || "brighton";
    if (method === "POST" && /^feed\/[^/]+\/action$/.test(path)) {
      const ref = decodeURIComponent(path.split("/")[1]);
      const status = { confirm: "confirmed", dismiss: "dismissed", reopen: "pending_review" }[body.action];
      if (!status) return json({ error: "No such action." }, 400);
      const calls = local.get("calls"), [s, id] = [ref.slice(0, ref.indexOf(":")), ref.slice(ref.indexOf(":") + 1)];
      const match = await recorded(`${key("analysis/match", new URLSearchParams({ match_id: id.split(".")[0], site_id: s }))}.json`);
      const was = calls[ref]?.was ?? match?.deviations.find(d => d.deviation_id === id)?.status ?? "pending_review";
      calls[ref] = { status, was, reviewed_by: status === "pending_review" ? null : body.by, note: body.note ?? null };
      local.set("calls", calls);
      return json({ ok: true, status });
    }
    if (/^plans\/[^/]+$/.test(path) && method !== "GET") {
      const which = decodeURIComponent(path.split("/")[1]), plans = local.get("plans");
      plans[`${site}/${which}`] = method === "DELETE" ? { removed: true }
        : { doc: body, updated_by: body.by, updated_at: new Date().toISOString() };
      local.set("plans", plans);
      return json({ ok: true });
    }
    if (method !== "GET") return json({ error: "The demo only reads." }, 405);

    if (path === "analysis/recap") return story(params);
    if (/^plans\/[^/]+$/.test(path) && path !== "plans/catalog") {
      const which = decodeURIComponent(path.split("/")[1]), plans = local.get("plans");
      const mine = plans[`${site}/${which}`];
      if (mine && !mine.removed) return json({ ...mine, inherited: false });
      if (mine?.removed || !(await recorded(`${key(path, params)}.json`))) {
        const fallback = which === "default" ? null : plans[`${site}/default`];
        if (fallback && !fallback.removed) return json({ ...fallback, inherited: true });
        if (which === "default" && mine?.removed) return json({ doc: null });
      }
    }
    const body_ = await recorded(`${key(path, params)}.json`);
    if (path === "analysis/match") {
      if (body_) { lastSite = body_.match.site_id; return json(withCalls(body_)); }
      // A link to a match that isn't in the recording (a report without one): the newest that is
      const list = await recorded(`${key("analysis/matches", [])}.json`);
      const m = list?.matches.find(x => !x.scouting);
      const newest = m && await recorded(`${key("analysis/match", new URLSearchParams({ match_id: m.match_id, site_id: m.site_id }))}.json`);
      if (newest) lastSite = newest.match.site_id;
      return newest ? json(withCalls(newest)) : missing();
    }
    if (path === "analysis/matches" && body_) {
      for (const m of body_.matches) if (m.pending !== undefined) m.pending = Math.max(0, pendingOf(m.site_id, m.match_id, m.pending));
      return json(body_);
    }
    return body_ ? json(body_) : missing();
  }

  // The narrator's story: the one recorded for these settings, else the closest one recorded for the match
  async function story(params) {
    stories ??= (await recorded("stories.json")) || [];
    const id = params.get("match_id"), want = { audience: params.get("audience"), language: params.get("language"), focus: params.get("focus") || "" };
    const mine = stories.filter(s => s.match_id === id);
    if (!mine.length) return json({ error: "No story was recorded for this match" }, 404);
    const score = s => (s.audience === want.audience) * 4 + (s.language === want.language) * 2 + (s.focus === want.focus);
    const best = mine.reduce((a, b) => (score(b) > score(a) ? b : a));
    const r = await recorded(`stories/${best.key}.json`);
    if (!r) return missing();
    if (score(best) < 7) {
      const names = { fan: "a fan", coach: "the coaching staff", analyst: "an analyst", broadcast: "broadcast captions" };
      r.recap += `\n(Recorded for ${names[best.audience]}, ${new Intl.DisplayNames(["en"], { type: "language" }).of(best.language)}` +
        `${best.focus ? `, following #${best.focus}` : ""}: the closest to what you asked.)`;
    }
    return json(r);
  }

  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const m = url.pathname.match(API);
    if (!m) return realFetch(input, options);
    const ev = m[1].match(/^evidence\/([^/]*)\/(.+)\.jpg$/);
    if (ev) return realFetch(`data/evidence_${decodeURIComponent(ev[1]) || lastSite}_${decodeURIComponent(ev[2])}.jpg`.replace(/[^A-Za-z0-9._/-]/g, "_"));
    const method = (options.method || "GET").toUpperCase();
    const body = options.body ? JSON.parse(options.body) : {};
    await new Promise(r => setTimeout(r, 60 + Math.random() * 120));
    return answer(method, m[1], url.searchParams, body);
  };
})();
