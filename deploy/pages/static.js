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

  // ── the replay: the live match the demo was presented on, as it was played (replay.py) ──
  // Its clock is shared by every page of the analysis room: {start (ms), speed, paused, at (s, when paused)}
  const SPEEDS = [3, 10, 30], SPEED = 10;
  let replayIndex;
  const snapshots = new Map();
  async function replay() {
    if (replayIndex === undefined) replayIndex = await recorded("replay/index.json").catch(() => null);
    return replayIndex;
  }
  function clock() {
    const c = local.get("replay");
    if (c.start === undefined) { const fresh = { start: Date.now(), speed: SPEED, paused: false, at: 0 }; local.set("replay", fresh); return fresh; }
    return c;
  }
  function replayAt(idx) {
    const c = clock(), t = c.paused ? c.at : (Date.now() - c.start) / 1000 * c.speed;
    return Math.max(0, Math.min(idx.length, t));
  }
  function setReplay(t, change = {}) {
    const c = { ...clock(), ...change };
    c.at = t;
    c.start = Date.now() - t / c.speed * 1000;
    local.set("replay", c);
  }
  const stepOf = (idx, t) => idx.steps.reduce((i, s, k) => (s.at <= t ? k : i), 0);
  async function snapshot(idx, t = replayAt(idx)) {
    const file = idx.steps[stepOf(idx, t)].file;
    if (!snapshots.has(file)) snapshots.set(file, recorded(`replay/${file}`));
    return snapshots.get(file);
  }

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
    const idx = await replay();
    if (idx && path === "analysis/match" && params.get("match_id") === idx.match_id) {
      lastSite = idx.site_id;
      return json(withCalls(structuredClone((await snapshot(idx)).match)));
    }
    const body_ = await recorded(`${key(path, params)}.json`);
    if (path === "analysis/matches" && body_ && idx) {
      // The replayed match as it stands, and while it's played, none of the matches after it
      const t = replayAt(idx), now = (await snapshot(idx, t)).listed;
      body_.matches = body_.matches
        .filter(m => t >= idx.length || m.scouting || m.kickoff <= idx.kickoff)
        .map(m => (m.match_id === idx.match_id ? structuredClone(now) : m));
    }
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
    const idx = await replay();
    if (idx && id === idx.match_id && replayAt(idx) < idx.length)
      return json({ error: "this match's stories were written at full time: ask again when the replay gets there" }, 404);
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

  // ── the replay's bar (the analysis room's own page): the minute, pause, speed, the moments, start again ──
  function minuteOf(idx, t) {
    if (t >= idx.length) return "Full time";
    if (t >= idx.first_half && t < idx.second_half) return "Half time";
    const m = t < idx.first_half ? Math.floor(t / 60) + 1 : Math.floor((t - idx.second_half) / 60) + 46;
    const end = t < idx.first_half ? 45 : 90;
    return m > end ? `${end}+${m - end}'` : `${m}'`;
  }
  const html = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const BAR_CSS = `
#replay { position: fixed; z-index: 50; left: 50%; bottom: 14px; transform: translateX(-50%); width: min(760px, calc(100vw - 32px));
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; padding: 8px 12px; box-sizing: border-box;
  background: var(--panel); color: var(--ink); border: 1px solid var(--line-2); border-radius: 12px; box-shadow: 0 6px 24px rgba(0,0,0,.35); font-size: 13px; }
#replay .tag { border-color: var(--accent); color: var(--accent); }
#replay b { font-family: var(--mono); font-variant-numeric: tabular-nums; min-width: 4.5em; }
#replay input[type=range] { flex: 1 1 160px; accent-color: var(--accent); min-width: 0; }
#replay button, #replay select { background: var(--raise, transparent); border: 1px solid var(--line-2); border-radius: 6px; padding: 4px 9px; cursor: pointer; }
#replay button:focus-visible, #replay select:focus-visible, #replay input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
#replay #rp-jump { max-width: 11em; }
#replay-toasts { position: fixed; z-index: 51; right: 16px; bottom: 84px; display: grid; gap: 8px; width: min(340px, calc(100vw - 32px)); }
#replay-toasts div { background: var(--panel); color: var(--ink); border: 1px solid var(--line-2); border-left: 4px solid var(--accent);
  border-radius: 8px; padding: 9px 12px; font-size: 13px; box-shadow: 0 6px 24px rgba(0,0,0,.35); }
#replay-toasts small { display: block; color: var(--muted); margin-bottom: 2px; }
#replay-toasts a { color: var(--accent); }
body { padding-bottom: 90px; }
@media (max-width: 560px) { #replay { bottom: 8px; } #replay-toasts { bottom: 120px; } #replay-toasts div + div { display: none; } }`;

  // A toast; `open` is a link, or what clicking "Open" does
  function toast(where, text, open) {
    const box = document.getElementById("replay-toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.innerHTML = `<small>${html(where)}</small>${html(text)}${open ? ` <a href="${typeof open === "string" ? html(open) : "#"}">Open</a>` : ""}`;
    if (typeof open === "function") el.querySelector("a").onclick = e => { e.preventDefault(); open(); };
    box.prepend(el);
    while (box.children.length > 3) box.lastChild.remove();
    setTimeout(() => el.remove(), 9000);
  }

  async function bar() {
    const idx = await replay();
    if (!idx || !document.getElementById("story") || typeof refresh !== "function") return;
    const style = document.createElement("style");
    style.textContent = BAR_CSS;
    document.head.append(style);
    const el = document.createElement("div");
    el.id = "replay";
    el.setAttribute("role", "region");
    el.setAttribute("aria-label", "Replay of the live match");
    const [, home, away] = idx.match_id.match(/-([A-Z]{3})-([A-Z]{3})$/) || [];
    el.innerHTML = `<span class="tag" title="The match the demo was presented on, as it was played">Replay · ${html(home)} v ${html(away)}</span>
      <b id="rp-min" aria-live="off"></b>
      <button id="rp-play" type="button"></button>
      <input id="rp-scrub" type="range" min="0" max="${idx.length}" step="10" aria-label="Match time">
      <select id="rp-speed" aria-label="Replay speed">${SPEEDS.map(s => `<option value="${s}">×${s}</option>`).join("")}</select>
      <select id="rp-jump" aria-label="Jump to a moment"><option value="">Jump to…</option>${idx.moments.map(m => `<option value="${m.at}">${html(m.label)}</option>`).join("")}</select>
      <button id="rp-again" type="button" title="Start again from kick-off" aria-label="Start again">↺</button>`;
    document.body.append(el);
    const toasts = document.createElement("div");
    toasts.id = "replay-toasts";
    toasts.setAttribute("role", "status");
    document.body.append(toasts);
    const $ = id => document.getElementById(id);
    const hash = `#${idx.site_id}/${idx.match_id}`;
    let last = replayAt(idx), lastStep = stepOf(idx, last), seen = null, busy = false;
    // The analysis room's own state (index.html): the deviation, in the list and selected
    const show = id => async () => {
      state.matchId = idx.match_id;
      state.devFilter = "all";
      await refresh();
      state.selectedDev = id;
      renderDevs();
      document.querySelector(`[data-dev="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" });
    };

    async function update(jumped) {
      if (busy) return;
      busy = true;
      try { await tick(jumped); } finally { busy = false; }
    }
    async function tick(jumped) {
      const c = clock(), t = replayAt(idx), step = stepOf(idx, t);
      $("rp-min").textContent = minuteOf(idx, t);
      $("rp-play").textContent = c.paused ? "▶ Play" : t >= idx.length ? "↺ Again" : "❚❚ Pause";
      if (document.activeElement !== $("rp-scrub")) $("rp-scrub").value = t;
      $("rp-speed").value = c.speed;
      if (step !== lastStep) {
        const snap = await snapshot(idx, t);
        const open = new Set(snap.match.deviations.map(d => d.deviation_id));
        // Played on (not a jump): what n8n sent the staff's phones and inboxes in that stretch
        if (!jumped && step > lastStep && seen) {
          for (const d of snap.match.deviations)
            if (!seen.has(d.deviation_id) && d.priority === "high")
              toast(`To the bench's phones · ${d.minute}'`, d.title, show(d.deviation_id));
          if (last < idx.first_half + 60 && t >= idx.first_half + 60) toast("To the staff's inbox", "The half-time report", `report.html?part=half${hash}`);
          if (last < idx.length && t >= idx.length) toast("To the coaches' inbox", "The match report", `report.html?part=full${hash}`);
        }
        seen = open;
        lastStep = step;
        refresh();
      }
      last = t;
    }
    snapshot(idx, last).then(s => { seen = new Set(s.match.deviations.map(d => d.deviation_id)); });
    function jump(t) {
      setReplay(t);
      update(true);
    }
    $("rp-play").onclick = () => {
      const c = clock(), t = replayAt(idx);
      if (t >= idx.length && !c.paused) return again();
      setReplay(t, { paused: !c.paused });
      update(true);
    };
    $("rp-scrub").oninput = e => { $("rp-min").textContent = minuteOf(idx, +e.target.value); };
    $("rp-scrub").onchange = e => jump(+e.target.value);
    $("rp-speed").onchange = e => { setReplay(replayAt(idx), { speed: +e.target.value }); update(true); };
    $("rp-jump").onchange = e => { if (e.target.value !== "") jump(+e.target.value); e.target.value = ""; };
    function again() {
      // The calls made on this match go with it
      const calls = local.get("calls");
      for (const k of Object.keys(calls)) if (k.startsWith(`${idx.site_id}:${idx.match_id}.`)) delete calls[k];
      local.set("calls", calls);
      setReplay(0, { paused: false });
      update(true);
    }
    $("rp-again").onclick = again;
    update(true);
    setInterval(() => update(false), 1000);
    if (!local.get("replay").welcomed) {
      local.set("replay", { ...clock(), welcomed: true });
      toast("Replay", `${home} v ${away}, the live match the demo was presented on, as it was played: ×${clock().speed}, or jump to a moment below.`);
    }
  }
  document.addEventListener("DOMContentLoaded", () => setTimeout(bar, 0));

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
