#!/usr/bin/env python3
"""
Records the live match the demo was presented on, minute by minute, for the static site's replay (static.js):
the simulator plays it again (the same seed, so the same match) into a throwaway ingest service, and what the
analysis room asks of it is kept after every minute of play.

    python3 deploy/pages/replay.py [--match EPL-20261027-2324-BHA-HUL]

Needs nothing running: the ingest service runs here, on a free port, with an empty database, on the match's
clock (time.time() is the moment being played, as it was when the match was live).

Writes recording/replay/<match>/:
  index.json        the steps ({"at": seconds after kick-off, on the match's clock, "file"}), the moments to jump to
  <sha1>.json       the analysis room's match at a step (analysis/match), with its line in the list (analysis/matches)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "recording" / "replay"
MATCH = "EPL-20261027-2324-BHA-HUL"
SITE, TOKEN, READ = "brighton", "replay-device-token", "replay-read-token"
# The moment being played: the ingest service caps what it records at "now"
CLOCK: dict[str, float | None] = {"now": None}
_wall = time.time
time.time = lambda: CLOCK["now"] if CLOCK["now"] is not None else _wall()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def env(port: int, db: Path) -> dict:
    tz = "Europe/London"
    season = "2026-08-21"
    for line in (ROOT / "iot" / ".env.example").read_text().splitlines():
        if line.startswith("SITE_TZ="):
            tz = line.split("=", 1)[1]
        if line.startswith("SEASON_START="):
            season = line.split("=", 1)[1]
    return {**os.environ, "INGEST_PORT": str(port), "INGEST_DB": str(db), "INGEST_SITE_TOKENS": f"{SITE}:{TOKEN}",
            "INGEST_READ_TOKEN": READ, "INGEST_DEFAULT_SITE": SITE, "SITE_TZ": tz, "SEASON_START": season,
            "ANALYSIS_DIR": str(ROOT / "iot" / "ingest" / "analysis"), "AGENT_URL": "", "NOTIFY_URL": "",
            "INFLUXDB_URL": "", "MQTT_HOST": "", "INGEST_URL": f"http://127.0.0.1:{port}", "INGEST_TOKEN": TOKEN,
            "SITE_ID": SITE, "SIMULATOR_SPEED": "3"}


def get(port: int, path: str) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Authorization": f"Bearer {READ}"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def wait(port: int) -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the ingest service didn't start")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--match", default=MATCH, help="a live match's id (EPL-<date>-<HHMM>-<home>-<away>)")
    args = parser.parse_args()
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        e = env(port, Path(tmp) / "ingest.db")
        os.environ.update(e)
        sys.path.insert(0, str(ROOT / "iot" / "ingest"))
        import ingest
        import sinks

        sinks.log = lambda *a, **k: None
        threading.Thread(target=ingest.main, daemon=True).start()
        wait(port)
        record(port, args.match)


def record(port: int, match_id: str) -> None:
    sys.path.insert(0, str(ROOT / "iot" / "scripts"))
    import simulate as sim

    from zoneinfo import ZoneInfo

    tz = ZoneInfo(os.environ["SITE_TZ"])
    stamp = datetime.strptime(match_id.split("-", 1)[1][:13], "%Y%m%d-%H%M").replace(tzinfo=tz)
    match = sim.live(tz, stamp.timestamp() + 1)
    if match.match_id != match_id:
        raise SystemExit(f"{match_id} isn't a live match the simulator plays (that slot is {match.match_id})")

    # The club's default plan only: a live match after the simulator's first gets its own when it's back-filled,
    # after full time, so the default is what the staff saw during it
    sim.call("PUT", f"/api/v1/plans/default?site_id={SITE}", {**sim.DEFAULT_PLAN, "by": "head-coach"}, READ)
    # Every minute of play on the match's clock, one step at half time, one at full time
    steps = [(1, k * 60) for k in range(sim.FIRST + 1)] + [("break", 0)] + [(2, k * 60) for k in range(1, sim.SECOND + 1)] + [("end", 0)]
    out = OUT / match_id
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    posted: set[str] = set()
    index = []
    for period, t in steps:
        if period == "break":
            now = match.wall(1, sim.FIRST * 60) + 60 / match.speed
        elif period == "end":
            now = match.end() + 1
        else:
            now = match.wall(period, t) + 0.5
        CLOCK["now"] = now
        sim.post("/api/v1/fixtures", [match.fixture(now)])
        sim.post("/api/v1/windows", match.window_docs(now))
        due = [d for d in match.play_docs(now) if d["play_id"] not in posted]
        if due:
            sim.post("/api/v1/plays", due)
            match.keyframes(due)
            posted |= {d["play_id"] for d in due}
        body = get(port, f"/api/v1/analysis/match?match_id={match_id}&site_id={SITE}")
        listed = next(m for m in get(port, "/api/v1/analysis/matches")["matches"] if m["match_id"] == match_id)
        body.pop("video_url", None)
        text = json.dumps({"listed": listed, "match": body}, ensure_ascii=False, separators=(",", ":"))
        name = hashlib.sha1(text.encode()).hexdigest()[:16] + ".json"
        (out / name).write_text(text)
        # On the match's own clock (no speed-up): seconds after kick-off
        index.append({"at": round((now - match.kickoff) * match.speed), "file": name})
        print(f"{listed['status']:>9} {body['match']['minute'] or '':>3}' {body['match']['score']}", flush=True)

    final = json.loads((out / index[-1]["file"]).read_text())["match"]
    index_doc = {"match_id": match_id, "site_id": SITE, "kickoff": final["match"]["kickoff"],
                 "length": index[-1]["at"], "first_half": sim.FIRST * 60, "second_half": (sim.FIRST + sim.BREAK) * 60,
                 "steps": dedupe(index), "moments": snapped(moments(final, sim), index)}
    (out / "index.json").write_text(json.dumps(index_doc, ensure_ascii=False, indent=1))
    kept = {s["file"] for s in index_doc["steps"]}
    for f in out.glob("*.json"):
        if f.name != "index.json" and f.name not in kept:
            f.unlink()
    size = sum(f.stat().st_size for f in out.glob("*.json"))
    print(f"{out}: {len(index_doc['steps'])} steps, {len(index_doc['moments'])} moments, {size / 1e6:.1f} MB")


def dedupe(index: list[dict]) -> list[dict]:
    """A step only where the analysis room's answer changes."""
    out = []
    for s in index:
        if not out or out[-1]["file"] != s["file"]:
            out.append(s)
    return out


def snapped(marks: list[dict], index: list[dict]) -> list[dict]:
    """Each moment at the first step that shows it (the minute after it happened)."""
    return [{**m, "at": next((s["at"] for s in index if s["at"] >= m["at"]), index[-1]["at"])} for m in marks]


def clock(sim, period: int, t: float) -> int:
    """Seconds after kick-off on the match's clock, half time included."""
    return round(t if period == 1 else (sim.FIRST + sim.BREAK) * 60 + t)


def moments(final: dict, sim) -> list[dict]:
    """What to jump to: the goals, the formation changes, each deviation as it opens, half and full time."""
    out = [{"at": 0, "label": "Kick-off"}]
    score = {"home": 0, "away": 0}
    codes = {final["match"]["team"]: final["match"]["us"]["code"]}
    codes[{"home": "away", "away": "home"}[final["match"]["team"]]] = final["match"]["opponent"]["code"]
    for p in sorted(final["plays"], key=lambda p: (p["period"], p["match_time_s"])):
        at = clock(sim, p["period"], p["match_time_s"]) + sim.analytics.PLAY_AFTER + 2
        if p["type"] == "goal":
            score[p["side"]] += 1
            out.append({"at": at, "label": f"{p['minute']}' Goal, {p['team']}: {score['home']}-{score['away']}"})
        elif p["type"] == "formation_change":
            out.append({"at": at, "label": f"{p['minute']}' {p['summary'].rstrip('.')}"})
    opened: dict[tuple, list[str]] = {}
    for d in final["deviations"]:
        opened.setdefault((d["period"], d["match_time_s"], d["minute"]), []).append(d["name"])
    for (period, t, minute), names in opened.items():
        out.append({"at": clock(sim, period, t), "label": f"{minute}' Off plan: {', '.join(names)}"})
    out.append({"at": sim.FIRST * 60 + 1, "label": "Half time"})
    out.append({"at": (sim.FIRST + sim.BREAK + sim.SECOND) * 60 + 1, "label": "Full time"})
    return sorted(out, key=lambda x: x["at"])

if __name__ == "__main__":
    main()
