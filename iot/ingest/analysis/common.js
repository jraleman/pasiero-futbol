// Shared by the analysis room (index.html), the plan editor (plan.html), the
// season page (season.html) and the reports (report.html): the staff's sign-in, the API, the league's crests and
// kits, the pitch, and how numbers and statuses are written.
"use strict";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
// A player, escaped: "#7 Tomas Calloway" (the synthetic season's fictional names, squads.json), or "#7"
const playerLabel = p => `#${esc(p.number ?? "?")}${p.name ? ` <span class="pname">${esc(p.name)}</span>` : ""}`;
const SVG = "http://www.w3.org/2000/svg";

// The theme's names (brand.js, from themes/<id>/theme.json by iot/scripts/theme.py): the header's wordmark,
// the title (or " · <name>" after a page's own), the mark's alt
for (const el of document.querySelectorAll("[data-brand]")) el.innerHTML = BRAND[el.dataset.brand];
if (document.querySelector("title[data-brand-suffix]")) document.title += ` · ${BRAND.name}`;
for (const img of document.querySelectorAll("img.mark")) img.alt = BRAND.mark;

// ── sign-in: the analyst's name goes on every call; the token stays in this tab ──
const store = {
  get(key) { try { return sessionStorage.getItem(key) || localStorage.getItem(key); } catch { return null; } },
  set(key, value, session) { try { (session ? sessionStorage : localStorage).setItem(key, value); } catch {} },
};
const auth = { token: store.get("analysis.token") || "", name: store.get("analysis.name") || "" };
let onSignedIn = () => {};

function signIn(error = "") {
  let dialog = $("signin");
  if (!dialog) {
    document.body.insertAdjacentHTML("beforeend", `
      <dialog id="signin"><form method="dialog" id="signin-form">
        <picture><source srcset="marks/mark.svg" media="(prefers-color-scheme: light)"><img class="mark" src="marks/mark-white.svg" alt="${esc(BRAND.mark)}" width="52" height="56"></picture>
        ${BRAND.greeting ? `<p class="greeting">${BRAND.greeting}</p>` : ""}
        <h2>${BRAND.name}</h2>
        <p>Sign in with your name: it goes on every call you make and every plan you save.</p>
        <label><span>Your name</span><input id="si-name" autocomplete="username" required maxlength="64"></label>
        <label><span>Staff token (INGEST_READ_TOKEN)</span><input id="si-token" type="password" autocomplete="current-password"></label>
        <p class="error" id="si-error"></p>
        <button>Sign in</button>
      </form></dialog>`);
    dialog = $("signin");
    $("signin-form").addEventListener("submit", e => {
      e.preventDefault();
      auth.name = $("si-name").value.trim();
      auth.token = $("si-token").value.trim();
      store.set("analysis.name", auth.name);
      store.set("analysis.token", auth.token, true);
      dialog.close();
      onSignedIn();
    });
    dialog.addEventListener("cancel", e => { if (!auth.name) e.preventDefault(); });
  }
  $("si-error").textContent = error;
  $("si-name").value = auth.name;
  $("si-token").value = "";
  if (!dialog.open) dialog.showModal();
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
               ...(options.body ? { "Content-Type": "application/json" } : {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) { signIn("That token isn't valid."); throw new Error("401"); }
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// A keyframe, fetched with the token (so it can't be a plain <img src>)
async function keyframe(site, playId) {
  const res = await fetch(`/api/v1/evidence/${encodeURIComponent(site)}/${encodeURIComponent(playId)}.jpg`,
                          { headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {} });
  if (!res.ok) throw new Error(res.status);
  return URL.createObjectURL(await res.blob());
}

// ── the league: crests and kits (league.json, shared with the simulator) ──
let league = null;
const leagueReady = fetch("league.json").then(r => r.ok ? r.json() : null).then(l => { league = l; return l; }).catch(() => null);
const club = code => league?.clubs?.[code] || null;
const crestImg = (code, cls = "") => club(code) && !club(code).dataset
  ? `<img class="${cls}" src="crests/${encodeURIComponent(code)}.png" alt="">`
  : `<span class="${cls} crest-none" aria-hidden="true">${esc(String(code || "").slice(0, 3))}</span>`;
const rgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
const inkOn = c => { const [r, g, b] = rgb(c); return 0.2126 * r + 0.7152 * g + 0.0722 * b > 150 ? "#0f1c14" : "#ffffff"; };
// The sides' shirt colours, the away side in its second when the two look alike (as simulate.py's kits())
function kits(home, away) {
  const h = club(home)?.kit || ["#e8efe9", "#0f1c14"], a = club(away)?.kit || ["#2a78d6", "#ffffff"];
  const near = Math.hypot(...rgb(h[0]).map((v, i) => v - rgb(a[0])[i])) < 120;
  const away_ = near ? a[1] : a[0];
  return { home: h[0], homeInk: inkOn(h[0]), away: away_, awayInk: inkOn(away_) };
}

// ── words ──
const STATUS = {
  on_plan: { label: "On plan", icon: "✓" }, drifting: { label: "Drifting", icon: "~" },
  off_plan: { label: "Off plan", icon: "✕" }, no_data: { label: "No data yet", icon: "–" },
};
const chip = status => `<span class="chip ${status}"><b aria-hidden="true">${STATUS[status].icon}</b>${STATUS[status].label}</span>`;
const MATCH_STATE = { scheduled: "Not started", live: "Live", half_time: "Half time", full_time: "Full time" };
const PERIOD = { 1: "First half", 2: "Second half", 3: "Extra time", 4: "Extra time" };
const PHASES = { in_possession: "In possession", out_of_possession: "Out of possession" };
const SCOPES = { match: "Match", first_half: "1st half", second_half: "2nd half", last_15: "Last 15'" };
const TYPE_NAMES = { goal: "Goal", shot: "Shot", save: "Save", set_piece: "Set piece", high_regain: "High regain",
                     counter_attack: "Counter-attack", build_up: "Build-up", press_broken: "Press played through", foul: "Foul won",
                     formation_change: "Formation change" };
// A glyph per play type, so the type never rests on colour (the team has the colour)
const GLYPHS = { goal: "●", shot: "◆", save: "◒", set_piece: "■", high_regain: "▲", counter_attack: "➤", build_up: "◗", press_broken: "✕",
                 foul: "✱", formation_change: "⬡" };
const ROLES = { shooter: "shot", assist: "assist", taker: "taker", goalkeeper: "keeper", regain: "won it", line_breaker: "broke the line",
                target: "target", first_contact: "first contact", fouler: "fouled", fouled: "was fouled" };
// How a play was found
const SOURCES = { tracking: "Found by the tracking", video: "Found by the video models", label: "On video, labelled" };
// Set pieces, in words
const SP_KINDS = { corner: "Corner", free_kick: "Free kick", throw_in: "Long throw" };
const SP_OUTCOMES = { goal: "Goal", shot: "Shot", cleared: "Cleared", retained: "Kept the ball" };
const FIRST_CONTACT = { attack: "won", defence: "lost", none: "none" };
const scoreText = m => `${m.home.code} ${m.score.home}–${m.score.away} ${m.away.code}`;
const when = iso => new Date(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

// ── the pitch: 105 × 68 m, x along the length, y from the far touchline ──
function el(name, attrs = {}, parent) {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}
function pitchLines(svg) {
  const g = el("g", { class: "lines" }, svg);
  el("rect", { x: 0, y: 0, width: 105, height: 68 }, g);
  el("line", { x1: 52.5, y1: 0, x2: 52.5, y2: 68 }, g);
  el("circle", { cx: 52.5, cy: 34, r: 9.15 }, g);
  for (const side of [0, 1]) {
    const x = d => side ? 105 - d : d;
    el("rect", { x: side ? 105 - 16.5 : 0, y: 34 - 20.16, width: 16.5, height: 40.32 }, g);
    el("rect", { x: side ? 105 - 5.5 : 0, y: 34 - 9.16, width: 5.5, height: 18.32 }, g);
    const dy = Math.sqrt(9.15 ** 2 - 5.5 ** 2);
    el("path", { d: `M ${x(16.5)} ${34 - dy} A 9.15 9.15 0 0 ${side ? 0 : 1} ${x(16.5)} ${34 + dy}` }, g);
    el("rect", { class: "net", x: side ? 105 : -2, y: 34 - 3.66, width: 2, height: 7.32 }, svg);
  }
  // The thirds, faintly: where final-third entries are counted
  for (const d of [35, 70]) el("line", { class: "third", x1: d, y1: 0, x2: d, y2: 68 }, svg);
}

// The formations the device reads, as analytics.py has them: [depth 0 (deepest) – 1, width 0 (left) – 1]
const FORMATIONS = {
  "4-4-2":[[0,0.1],[0,0.37],[0,0.63],[0,0.9],[0.5,0.1],[0.5,0.37],[0.5,0.63],[0.5,0.9],[1,0.37],[1,0.63]],
  "4-3-3":[[0,0.1],[0,0.37],[0,0.63],[0,0.9],[0.5,0.25],[0.45,0.5],[0.5,0.75],[1,0.08],[1,0.5],[1,0.92]],
  "4-2-3-1":[[0,0.1],[0,0.37],[0,0.63],[0,0.9],[0.35,0.37],[0.35,0.63],[0.72,0.1],[0.72,0.5],[0.72,0.9],[1,0.5]],
  "4-1-4-1":[[0,0.1],[0,0.37],[0,0.63],[0,0.9],[0.28,0.5],[0.62,0.08],[0.62,0.37],[0.62,0.63],[0.62,0.92],[1,0.5]],
  "3-5-2":[[0,0.22],[0,0.5],[0,0.78],[0.5,0.02],[0.45,0.3],[0.4,0.5],[0.45,0.7],[0.5,0.98],[1,0.37],[1,0.63]],
  "3-4-3":[[0,0.22],[0,0.5],[0,0.78],[0.5,0.05],[0.45,0.37],[0.45,0.63],[0.5,0.95],[1,0.12],[1,0.5],[1,0.88]],
  "5-3-2":[[0,0.02],[0,0.26],[0,0.5],[0,0.74],[0,0.98],[0.5,0.25],[0.5,0.5],[0.5,0.75],[1,0.37],[1,0.63]],
  "5-4-1":[[0,0.02],[0,0.26],[0,0.5],[0,0.74],[0,0.98],[0.55,0.1],[0.55,0.37],[0.55,0.63],[0.55,0.9],[1,0.5]],
  "3-2-5":[[0,0.2],[0,0.5],[0,0.8],[0.38,0.37],[0.38,0.63],[1,0.02],[0.92,0.28],[1,0.5],[0.92,0.72],[1,0.98]],
  "2-3-5":[[0,0.3],[0,0.7],[0.4,0.12],[0.35,0.5],[0.4,0.88],[1,0.02],[0.92,0.28],[1,0.5],[0.92,0.72],[1,0.98]],
};
