#!/usr/bin/env python3
"""
Records the running stack for the static site (build.sh): what the analysis room, the dashboard, Mailpit and
Grafana show of the club's latest matches, into deploy/pages/recording/ (gitignored).

    python3 deploy/pages/record.py [--matches 8] [--only analysis,stories,feed,answers,mail,grafana]

Record after a match's full time and before the next kick-off, with the simulator stopped
(`docker stop p4n4-simulator` on the device, `docker start` after): the assistant answers about the
latest match, so it should be the newest finished one. Each part resumes: what's recorded is kept, unless
--fresh. The model writes the stories and the answers: read them (recording/stories/, answers.json)
before building, and delete one to record it again.

    INGEST_URL     the ingest service (default http://192.168.100.2:8090)
    INGEST_TOKEN   the staff's token (default demo-read-token)
    P4N4_API       p4n4-api (default http://127.0.0.1:8000), signed in as P4N4_USER / P4N4_PASSWORD
    MAILPIT_URL    default http://192.168.100.2:8025
    GRAFANA_URL    default http://192.168.100.2:3000 (anonymous viewers on)
    NODE_PATH      where Node finds Playwright, for Grafana's screenshots (grafana.mjs)

Writes:
  analysis/<key>.json, analysis/<key>.jpg   the ingest API's answers, by static.js's key (the path and its query)
  stories/<key>.json, stories.json          the narrator's stories, and which were asked
  dashboard/{project,home,vision,stacks,agents,feed}.json, dashboard/feed/<id>.jpg
  answers.json                              the assistant's answers to the brand's suggestions
  mail/messages.json, mail/<id>.html        the emails about the recorded matches
  grafana/<match>.<theme>.jpg               the Match analysis board, per match, dark and light
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "recording"
INGEST = os.environ.get("INGEST_URL", "http://192.168.100.2:8090").rstrip("/")
TOKEN = os.environ.get("INGEST_TOKEN", "demo-read-token")
API = os.environ.get("P4N4_API", "http://127.0.0.1:8000").rstrip("/")
MAILPIT = os.environ.get("MAILPIT_URL", "http://192.168.100.2:8025").rstrip("/")
GRAFANA = os.environ.get("GRAFANA_URL", "http://192.168.100.2:3000").rstrip("/")
PERIODS = ("season", "30d", "this_month")
AUDIENCES = ("fan", "coach", "analyst", "broadcast")
# The stories asked of the newest matches: (audience, language, follow), the whole match each
LATEST_STORIES = [(a, "en", "") for a in AUDIENCES] + [("fan", "es", "7"), ("coach", "es", "")]
OLDER_STORIES = [("fan", "en", ""), ("broadcast", "en", "")]
LATEST = 3  # matches with every audience's story
# Words of a model that slipped out of plain text (LaTeX, code): asked again
SLIPS = re.compile(r"\$|\\\(|\\\[|\\text|\\frac|```")


def log(*a):
    print(*a, flush=True)


def key(path: str) -> str:
    """static.js's key(): the API path after /api/v1/, its query sorted, anything but [A-Za-z0-9.-] as _."""
    u = urllib.parse.urlsplit(path)
    q = "&".join(f"{k}={v}" for k, v in sorted(urllib.parse.parse_qsl(u.query)))
    return re.sub(r"[^A-Za-z0-9.-]", "_", u.path.split("/api/v1/", 1)[1] + ("@" + q if q else ""))


def get(url: str, headers: dict | None = None, timeout: float = 30) -> bytes | None:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read()
    except urllib.error.HTTPError as e:
        if e.code in (204, 404):
            return None
        raise


def ingest(path: str, timeout: float = 30) -> bytes | None:
    return get(INGEST + path, {"Authorization": f"Bearer {TOKEN}"}, timeout)


def save(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.write_bytes if isinstance(data, bytes) else path.write_text)(data)


def keep(path: str, *, timeout: float = 30) -> dict | None:
    """The ingest API's answer to [path], recorded under its key; None when it has none."""
    file = OUT / "analysis" / f"{key(path)}.json"
    if file.is_file():
        return json.loads(file.read_text())
    body = ingest(path, timeout)
    if body is None:
        return None
    save(file, body)
    return json.loads(body)


def q(**params) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


# ── the analysis room ─────────────────────────────────────────────────────────

def chosen(count: int) -> list[dict]:
    """The club's newest `count` finished matches, and the scouting ones (the footage's)."""
    listing = json.loads(ingest("/api/v1/analysis/matches"))
    own = [m for m in listing["matches"] if not m.get("scouting") and m["status"] == "full_time"][:count]
    scouting = [m for m in listing["matches"] if m.get("scouting")]
    picked = own + scouting
    save(OUT / "analysis" / f"{key('/api/v1/analysis/matches')}.json",
         json.dumps({**listing, "matches": picked, "video_url": None}, ensure_ascii=False))
    return picked


def analysis(matches: list[dict]) -> None:
    keep("/api/v1/plans/catalog")
    keep(f"/api/v1/plans/default?{q(site_id=matches[0]['site_id'])}")
    for m in matches:
        site, mid = m["site_id"], m["match_id"]
        d = keep(f"/api/v1/analysis/match?{q(match_id=mid, site_id=site)}")
        keep(f"/api/v1/plans/{mid}?{q(site_id=site)}")
        frames = 0
        for p in d["plays"]:
            keep(f"/api/v1/analysis/play?{q(play_id=p['play_id'], site_id=site)}")
            jpg = OUT / "analysis" / f"evidence_{site}_{p['play_id']}.jpg"
            if not jpg.exists() and (img := ingest(f"/api/v1/evidence/{site}/{p['play_id']}.jpg")):
                save(jpg, img)
            frames += jpg.exists()
        log(f"  {mid}: {len(d['plays'])} plays, {frames} keyframes, {len(d['deviations'])} deviations")
    for period in PERIODS:
        sp = keep(f"/api/v1/analysis/set_pieces?{q(period=period)}")
        keep(f"/api/v1/analysis/physical?{q(period=period)}")
        for opponent in sorted({x["opponent"] for x in sp.get("matches", [])}):
            keep(f"/api/v1/analysis/set_pieces?{q(period=period, opponent=opponent)}")
    log(f"analysis: {len(list((OUT / 'analysis').iterdir()))} files")


def stories(matches: list[dict]) -> None:
    own = [m for m in matches if not m.get("scouting")]
    asked = []
    for i, m in enumerate(own):
        for audience, language, focus in LATEST_STORIES if i < LATEST else OLDER_STORIES:
            path = f"/api/v1/analysis/recap?{q(match_id=m['match_id'], site_id=m['site_id'], part='full', audience=audience, language=language, focus=focus)}"
            file = OUT / "stories" / f"{key(path)}.json"
            asked.append({"match_id": m["match_id"], "audience": audience, "language": language, "focus": focus,
                          "part": "full", "key": key(path)})
            if file.is_file():
                continue
            for attempt in range(3):
                started = time.time()
                body = ingest(path, timeout=600)
                text = json.loads(body).get("recap", "") if body else ""
                if text and not SLIPS.search(text):
                    break
                log(f"  {m['match_id']} {audience}/{language}{'/#' + focus if focus else ''}: asked again")
            save(file, body)
            log(f"  {m['match_id']} {audience}/{language}{'/#' + focus if focus else ''}: {time.time() - started:.0f} s")
    save(OUT / "stories.json", json.dumps(asked, indent=1))


# ── the dashboard ─────────────────────────────────────────────────────────────

def signed_in() -> dict:
    body = json.dumps({"username": os.environ.get("P4N4_USER", "admin"),
                       "password": os.environ.get("P4N4_PASSWORD", "p4n4")}).encode()
    req = urllib.request.Request(API + "/api/v1/auth/token", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return {"Authorization": "Bearer " + json.loads(res.read())["access_token"]}


def dashboard(matches: list[dict]) -> None:
    auth = signed_in()
    out = OUT / "dashboard"
    for name, path in {"project": "/api/v1/project", "home": "/api/v1/project/home", "vision": "/api/v1/project/vision",
                       "stacks": "/api/v1/stacks", "agents": "/api/v1/agents/config"}.items():
        save(out / f"{name}.json", get(API + path, auth, 60) or b"null")
    ids = {m["match_id"] for m in matches}
    items, before = [], None
    while True:
        page = json.loads(get(API + "/api/v1/feed?" + q(limit=200, before=before), auth))
        items += [i for i in page["items"] if (i.get("meta") or {}).get("match_id") in ids]
        before = page.get("next_before")
        if not before or not page["items"]:
            break
    pictures = 0
    for i in items:
        jpg = out / "feed" / (re.sub("[:/]", "_", i["id"]) + ".jpg")
        if i.get("image") and not jpg.exists():
            if img := get(API + f"/api/v1/feed/{urllib.parse.quote(i['id'], safe='')}/image", auth):
                save(jpg, img)
        pictures += jpg.exists()
    save(out / "feed.json", json.dumps({"title": page["title"], "items": items}, ensure_ascii=False))
    log(f"feed: {len(items)} items, {pictures} pictures")


def answers() -> None:
    """The assistant's answers to the brand's suggestions, each asked in the view that suggests it."""
    brand = json.loads((ROOT / "theme" / "brand.json").read_text())["assistant"]
    asked = [(qn, "admin") for qn in brand["suggestions"]]
    for view, questions in brand.get("suggestionsByView", {}).items():
        asked += [(qn, view) for qn in questions]
    asked.append(("How are we doing, and what should we tell the players?", "normie"))
    file = OUT / "answers.json"
    done = json.loads(file.read_text()) if file.is_file() else []
    model = json.loads((OUT / "dashboard" / "agents.json").read_text())["model"]
    auth = signed_in()
    views = {"admin": "admin", "power": "power user", "normie": "viewer"}
    # The match they're about: the newest recorded (with no live match, the agents may ask which)
    m = next(m for m in json.loads((OUT / "analysis" / "analysis_matches.json").read_text())["matches"] if not m.get("scouting"))
    about = (f" They are asking about the latest match, {m['home']['code']} {m['score']['home']}-{m['score']['away']} "
             f"{m['away']['code']} ({m['match_id']}, site {m['site_id']}), at full time.")
    for question, view in dict(asked).items():
        if any(question in a["q"] for a in done):
            continue
        for attempt in range(3):
            started = time.time()
            body = json.dumps({"model": model, "stream": True, "include_status": True, "messages": [
                {"role": "system", "content": f"The person asking uses the dashboard's {views[view]} view.{about}"},
                {"role": "user", "content": question}]}).encode()
            req = urllib.request.Request(API + "/api/v1/agents/chat", body, {**auth, "Content-Type": "application/json"})
            steps, text = [], ""
            with urllib.request.urlopen(req, timeout=900) as res:
                for line in res:
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if (step := chunk.get("step")) and step.get("state") == "running":
                        steps.append([step["tool"], step.get("label") or {}])
                    text += (chunk.get("message") or {}).get("content") or ""
            if text.strip() and not SLIPS.search(text):
                break
            log(f"  {question!r}: asked again")
        done.append({"q": [question], "view": view, "steps": steps, "text": text.strip()})
        save(file, json.dumps(done, ensure_ascii=False, indent=1))
        log(f"  {question!r} ({view}): {time.time() - started:.0f} s, {len(steps)} steps")


# ── the emails and Grafana ────────────────────────────────────────────────────

def mail(matches: list[dict]) -> None:
    scores = [f"{m['home']['code']} {m['score']['home']}-{m['score']['away']} {m['away']['code']}" for m in matches]
    codes = [m["opponent"]["code"] for m in matches if not m.get("scouting")]
    listing = json.loads(get(MAILPIT + "/api/v1/messages?limit=1000"))["messages"]
    # A match's emails name it by its score at the time: BHA 1-0 HUL, then 1-1, 2-1
    kept = [m for m in listing if any(re.search(rf"BHA \d+-\d+ {c}\b", m["Subject"]) for c in codes)]
    # Emails of an older match against the same opponent: not after the oldest recorded kick-off
    oldest = min(m["kickoff"] for m in matches if not m.get("scouting"))
    kept = [m for m in kept if m["Created"] >= oldest[:19]]
    rows = []
    for m in kept:
        msg = json.loads(get(MAILPIT + f"/api/v1/message/{m['ID']}"))
        save(OUT / "mail" / f"{m['ID']}.html", msg.get("HTML") or f"<pre>{msg.get('Text', '')}</pre>")
        rows.append({"id": m["ID"], "at": m["Created"], "subject": m["Subject"],
                     "from": m["From"]["Address"], "to": [t["Address"] for t in m["To"]]})
    save(OUT / "mail" / "messages.json", json.dumps(rows, ensure_ascii=False, indent=1))
    log(f"mail: {len(rows)} emails ({', '.join(scores[:3])}, …)")


def grafana(matches: list[dict]) -> None:
    own = [m["match_id"] for m in matches if not m.get("scouting")]
    subprocess.run(["node", str(HERE / "grafana.mjs"), GRAFANA, str(OUT / "grafana"), *own], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--matches", type=int, default=8)
    parser.add_argument("--only", default="analysis,stories,feed,answers,mail,grafana")
    parser.add_argument("--fresh", action="store_true", help="drop what's recorded first")
    args = parser.parse_args()
    if args.fresh:
        shutil.rmtree(OUT, ignore_errors=True)
    parts = set(args.only.split(","))
    matches = chosen(args.matches)
    log(f"{len(matches)} matches: {', '.join(m['match_id'] for m in matches)}")
    if live := [m for m in json.loads(ingest("/api/v1/analysis/matches"))["matches"] if m["status"] in ("live", "half_time")]:
        log(f"warning: {live[0]['match_id']} is being played: the assistant answers about it (stop the simulator)")
    if "analysis" in parts:
        analysis(matches)
    if "stories" in parts:
        stories(matches)
    if "feed" in parts:
        dashboard(matches)
    if "answers" in parts:
        answers()
    if "mail" in parts:
        mail(matches)
    if "grafana" in parts:
        grafana(matches)
    save(OUT / "recorded.json", json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                            "matches": [m["match_id"] for m in matches]}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
