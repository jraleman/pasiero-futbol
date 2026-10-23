#!/usr/bin/env python3
"""
Demo club device for the ingest service (compose profile "demo"): Brighton's
match device, posting contract 0.1 fixtures, windows (with every player's
running and the set pieces), plays and heartbeats as a real one would, plus
the staff's game plans and the analyst's calls on past matches.

Every match is played out by the match engine (engine.py) from a seed
(site + match id) and measured by the device's analytics (analytics.py),
so it is the same on every run. Restarting re-sends the same ids, which the
ingest service counts as unchanged, and never touches a call.

  - History: SIMULATOR_HISTORY past matches, Saturdays at 15:00, home at
    the American Express Stadium and away in turn, against the Premier League.
    Each has a game plan (the club's default, or one made for the opponent),
    both benches make their changes, now and then one of ours tires (and
    may come off), and the analyst has already made a call on every
    deviation.
  - Live: a match is always being played at the Amex, sped up
    SIMULATOR_SPEED times (3: a match in about 44 minutes), then a short
    break, then the next. Its windows and plays arrive as their minute
    comes; its deviations wait for you (the Feed tab or the analysis page).

The live match tells a story about the game plan (3-2-5 in possession,
4-4-2 out of it, a high line, a hard press, the set pieces):
    19'      on plan: #10 wins it high, #9 scores                 1-0
    25'-45'  the line drops and the press fades: team length off
             plan by about 35', line height by 40', and in most
             matches PPDA, possession or the counter-press too
    41'      their counter plays through the press                1-1
    46'-70'  back on plan after the half-time talk (by 50'-60')
    58'      they change to a back three in possession
    63'      the near-post flick corner routine: #5 heads it in   2-1
    50'-65'  #7's legs go: his ground covered per minute falls, the
             running drop is off plan by 65'-70' (a key target)
    72'      #7 off, #19 on: the drop is back on plan by 80'
    70'-90'  5-4-1 out of possession to see it out, and the wide
             players drift inside: shape, width and left-channel
             deviations at 80'-85', which the analyst may well
             call expected

Every keyframe is a drawing of its play's snapshot from above (diagram.py),
uploaded the way a device uploads its camera's frame. Everything it posts is
marked "synthetic": true. Clubs, the competition and the ground are the
league's (league.json, shared with the analysis page); players are shirt
numbers.

    INGEST_URL                default http://ingest:8080
    INGEST_TOKEN              the site's write token (INGEST_SITE_TOKENS)
    INGEST_READ_TOKEN         the staff's token, for the plans and the calls
    SITE_ID                   default brighton
    SITE_TZ                   default UTC
    SIMULATOR_SPEED           how much faster than real time the live match runs (default 3)
    SIMULATOR_HISTORY         past matches to post on start (default 8)
    SIMULATOR_HOST            the simulated device's hostname
    LEAGUE_FILE               the clubs (default league.json next to this script, else ../ingest/analysis/league.json)
    simulate.py --once        history, the live match so far and one heartbeat, then exit (tests)
    simulate.py --schedule    when this and the next live match's moments arrive, in local time, then exit
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import analytics
import diagram
import engine
from engine import Script, Style

INGEST_URL = os.environ.get("INGEST_URL", "http://ingest:8080").rstrip("/")
TOKEN = os.environ.get("INGEST_TOKEN", "")
READ_TOKEN = os.environ.get("INGEST_READ_TOKEN", "")
SITE = os.environ.get("SITE_ID", "brighton")
SPEED = max(0.5, float(os.environ.get("SIMULATOR_SPEED") or 3))
HISTORY = int(os.environ.get("SIMULATOR_HISTORY") or 8)
HOST = os.environ.get("SIMULATOR_HOST") or "brighton-match-device-sim"
HEARTBEAT = 30
TICK = 2


def load_league() -> dict:
    here = Path(__file__).resolve().parent
    for path in (os.environ.get("LEAGUE_FILE"), here / "league.json", here.parent / "ingest/analysis/league.json"):
        if path and Path(path).is_file():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    raise SystemExit("league.json not found (LEAGUE_FILE)")


LEAGUE = load_league()
CLUB = LEAGUE["club"]
COMPETITION = LEAGUE["competition"]
SEASON_START = datetime.fromisoformat(LEAGUE["season"]["start"]).date()
CLUBS = {code: c for code, c in LEAGUE["clubs"].items() if not c.get("dataset")}
OPPONENTS = [code for code in CLUBS if code != CLUB]
GROUND = LEAGUE["sites"].get(SITE, {}).get("ground") or CLUBS[CLUB]["ground"]
ANALYSTS = ("analyst", "assistant-coach")
CAMERA = "tactical"

# A match on the live clock, in match minutes: the first half (45 + 3), half
# time, the second half (45 + 4), then a break until the next one
FIRST, BREAK, SECOND, AFTER = 48, 15, 49, 20
CYCLE = FIRST + BREAK + SECOND + AFTER
HALVES = (FIRST * 60, SECOND * 60)

# ── how the teams play ────────────────────────────────────────────────────────

# The Brighton way, as the club's default game plan describes it: build in a
# 3-2-5, defend in a 4-4-2 with a high line, press hard, attack down the left,
# go long when it's on, and make the set pieces count: strong in the air, long
# throws into the box, and a playbook of corner routines
BRIGHTON = Style(shape_in="3-2-5", shape_out="4-4-2", line_in=44, line_out=42, length_in=44, length_out=31,
                  width_in=58, width_out=40, press=0.72, passing=0.62, directness=0.5, left=2.6, centre=1.0,
                  right=0.7, short_gk=0.7, aerial=0.68, long_throws=True,
                  corners=(("near-post flick", 2.0), ("six-yard screen", 1.5), ("back post", 1.0), ("short corner", 0.5)))
# The same team when the plan slips
DROPPED = replace(BRIGHTON, line_out=33, line_in=36, length_out=37, press=0.05)
NARROW = replace(BRIGHTON, width_in=42, left=0.7, centre=2.0)
SEE_IT_OUT = replace(BRIGHTON, shape_out="5-4-1", line_out=34, press=0.45)
# Their opponents, each a little different
OPPONENT_STYLES = [
    Style("4-2-3-1", "4-4-2", 36, 30, 42, 32, 52, 40, 0.45, 0.5, 0.45, 1, 1.2, 1, 0.55),
    Style("4-3-3", "4-1-4-1", 40, 34, 44, 33, 56, 42, 0.55, 0.55, 0.4, 1, 1, 1.2, 0.7),
    Style("3-4-3", "5-4-1", 34, 26, 40, 30, 54, 44, 0.35, 0.45, 0.55, 1.2, 0.9, 1, 0.4),
    Style("4-4-2", "4-4-2", 33, 29, 38, 31, 50, 38, 0.4, 0.42, 0.6, 1, 1, 1, 0.3),
    Style("3-5-2", "5-3-2", 35, 27, 42, 30, 52, 42, 0.38, 0.5, 0.5, 1, 1.3, 1, 0.5),
]
# How each of them takes set pieces: strength in the air, and its corner routines
OPPONENT_SET_PIECES = [
    (0.5, (("back post", 1.0), ("penalty spot", 1.0))),
    (0.45, (("short corner", 1.0), ("penalty spot", 1.0))),
    (0.6, (("six-yard screen", 1.0), ("back post", 1.0))),
    (0.62, (("back post", 2.0), ("near-post flick", 1.0))),
    (0.55, (("penalty spot", 1.0), ("near-post flick", 1.0))),
]
OPPONENT_STYLES = [replace(style, aerial=aerial, corners=corners)
                   for style, (aerial, corners) in zip(OPPONENT_STYLES, OPPONENT_SET_PIECES)]
# The benches: who comes on, in order
BENCH = {"ours": (19, 20, 17, 14, 24), "theirs": (22, 18, 23, 15, 21)}
# Where a substitute's player came off: the places that tire first
SUBBED = (7, 11, 9, 10, 8, 2, 3)

# The club's default game plan: what every match is measured against unless the staff write it its own
DEFAULT_PLAN = {
    "title": "Brighton way: build in a 3-2-5, press high, defend in a 4-4-2",
    "formation": {"in_possession": "3-2-5", "out_of_possession": "4-4-2"},
    "targets": [
        {"metric": "ppda", "op": "<=", "value": 8, "key": True, "tolerance": 3},
        {"metric": "line_height_m", "op": ">=", "value": 38, "key": True},
        {"metric": "length_m", "op": "<=", "value": 35, "tolerance": 2},
        {"metric": "width_m", "op": ">=", "value": 48},
        {"metric": "possession_pct", "op": ">=", "value": 45},
        {"metric": "counterpress_pct", "op": ">=", "value": 24, "tolerance": 8},
        {"metric": "set_piece_first_contact_pct", "op": ">=", "value": 45, "tolerance": 10},
        {"metric": "set_piece_defence_pct", "op": ">=", "value": 60, "tolerance": 10},
        {"metric": "load_drop_pct", "op": "<=", "value": 18, "key": True, "tolerance": 3},
    ],
    "instructions": [
        "Press their centre-backs when they play back to the keeper: #9 and #10 go, #8 jumps to their 6.",
        "Back four step up together on every back pass.",
        "Left back inverts into midfield in possession; #11 holds the width on the left.",
        "First five seconds after we lose it: the nearest three press, no fouls in their half.",
        "Corners: near-post flick first, #5 attacks the near post, #9 screens the keeper. Long throws from #2 in their third.",
    ],
    "opponent_notes": "",
}


def plan_for(opponent: str, rng: random.Random) -> dict | None:
    """A match's own plan, made for the opponent (most matches), or None for the club's default."""
    if rng.random() < 0.25:
        return None
    plan = json.loads(json.dumps(DEFAULT_PLAN))
    name = CLUBS[opponent]["name"]
    side = rng.choice(("left", "right"))
    plan["title"] = f"{name}: press their build-up, attack their {'right' if side == 'left' else 'left'} back"
    plan["targets"].append({"metric": f"entries_{side}_pct", "op": ">=", "value": 32, "key": rng.random() < 0.5})
    plan["opponent_notes"] = (f"{name} build from the back through their holding midfielder; their "
                              f"{'right' if side == 'left' else 'left'} back gets forward and leaves space behind. "
                              f"Danger on the counter after their wide players win it.")
    return plan


# ── posting ───────────────────────────────────────────────────────────────────


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", file=sys.stderr, flush=True)


def call(method: str, path: str, body: object, token: str, kind: str = "application/json") -> dict | None:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(f"{INGEST_URL}{path}", data=data, method=method,
                                     headers={"Content-Type": kind, **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        log(f"{path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, OSError) as e:
        log(f"{path}: {e}")
    return None


def post(path: str, docs: list[dict]) -> None:
    for i in range(0, len(docs), 50):
        result = call("POST", path, docs[i:i + 50], TOKEN)
        if result and result.get("rejected"):
            log(f"{path}: {len(result['rejected'])} rejected, e.g. {result['rejected'][0]['error']}")


def rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def kits(home: str, away: str) -> dict:
    """
    Each side's (shirt, number) colours: the clubs' first colours, the away
    side in its second when the two would look alike (a change kit). The
    analysis page does the same (kits() in index.html).
    """
    h, (a, a2) = rgb(LEAGUE["clubs"][home]["kit"][0]), (rgb(c) for c in LEAGUE["clubs"][away]["kit"])
    if math.dist(h, a) < 120:
        a = a2
    ink = lambda c: (15, 28, 46) if 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] > 150 else (255, 255, 255)  # noqa: E731
    return {"home": (h, ink(h)), "away": (a, ink(a))}


def iso(t: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(t, tz).isoformat(timespec="milliseconds")


# ── a match ───────────────────────────────────────────────────────────────────


class Match:
    """One match: played out and measured at once, then posted as its moments come round on `clock`."""

    def __init__(self, match_id: str, home: str, away: str, kickoff: float, speed: float, matchday: int,
                 story: bool, tz: tzinfo, measure: bool = True) -> None:
        self.match_id, self.home, self.away, self.kickoff = match_id, home, away, kickoff
        self.speed, self.matchday, self.story, self.tz = speed, matchday, story, tz
        self.ours = "home" if home == CLUB else "away"
        rng = random.Random(f"{SITE}/{match_id}/plan")
        self.plan = plan_for(away if self.ours == "home" else home, rng) if not story else self.live_plan()
        # The live match's opponent builds in a 4-2-3-1 until 58', then a back three (style())
        self.opponent_style = OPPONENT_STYLES[0] if story else OPPONENT_STYLES[rng.randrange(len(OPPONENT_STYLES))]
        # The team attacks down the side its plan asks for (the default plan names none: down the left)
        right = any(t["metric"] == "entries_right_pct" for t in (self.plan or {}).get("targets", []))
        self.base = replace(BRIGHTON, left=BRIGHTON.right, right=BRIGHTON.left) if right else BRIGHTON
        self.rng = rng
        self.drifts = self.history_drifts(rng) if not story else {}
        started = time.monotonic()
        self.subs, self.fader = self.substitutions(random.Random(f"{SITE}/{match_id}/legs"))
        play = engine.play(f"{SITE}/{match_id}", self.style, self.scripts(), home_attacks="right",
                           halves=HALVES, score_random=not story, fatigue=self.legs, subs=self.subs)
        self.frames = play.frames
        self.goals = [(e["period"], e["t"], e["team"]) for e in play.events if e["kind"] == "shot" and e.get("outcome") == "goal"]
        if not measure:
            # The synthetic cameras only draw it (extras/config/go2rtc/pitch.py)
            return
        self.measured = analytics.Match(play.frames, play.events, {"home": {1}, "away": {1}}, "right", hz=engine.HZ)
        self.windows = self.measured.windows()
        self.plays = self.measured.plays(self.windows)
        log(f"{match_id}: played and measured in {time.monotonic() - started:.1f} s "
            f"({len(self.windows)} windows, {len(self.plays)} plays)")

    def live_plan(self) -> dict:
        opponent = self.away if self.ours == "home" else self.home
        plan = json.loads(json.dumps(DEFAULT_PLAN))
        plan["title"] = f"{CLUBS[opponent]['name']} at the Amex: press high, attack their right back"
        plan["targets"].append({"metric": "entries_left_pct", "op": ">=", "value": 32, "key": False})
        plan["opponent_notes"] = (f"{CLUBS[opponent]['name']} play out from the back with a double pivot; their right "
                                  "back pushes on and leaves space behind. They have looked at a back three in possession. "
                                  "At their corners they aim for the back post: #4 marks their #5.")
        return plan

    @staticmethod
    def history_drifts(rng: random.Random) -> dict:
        """For a past match: a stretch or two where something slipped. (period, from s, to s) → the slip."""
        slips = {}
        for _ in range(rng.choice((0, 1, 1, 2))):
            period = rng.choice((1, 2))
            start = rng.choice((600, 900, 1200, 1500, 1800))
            slips[(period, start, start + rng.choice((900, 1200)))] = rng.choice((DROPPED, NARROW, SEE_IT_OUT))
        return slips

    def substitutions(self, rng: random.Random) -> tuple[list[tuple], tuple | None]:
        """
        Both benches, and the player of ours whose legs go: (subs, fader). A fader is (place, from s, to s,
        how much is left) in the second half. In the live match it's #7, from 50' to 65' down to half, and he
        comes off at 72' for #19; in a past match, now and then someone, and he may stay on.
        """
        them = "away" if self.ours == "home" else "home"
        if self.story:
            subs = [(self.ours, 2, 1620, 7, 19), (self.ours, 2, 1980, 9, 20), (self.ours, 2, 2280, 11, 17),
                    (them, 2, 1200, 10, 22), (them, 2, 1800, 9, 18), (them, 2, 2100, 7, 23)]
            return subs, (7, 300, 1200, 0.42)
        fader = None
        if rng.random() < 0.4:
            start = rng.choice((300, 600, 900))
            fader = (rng.choice((2, 3, 7, 8, 11)), start, start + 900, rng.uniform(0.5, 0.6))
        subs = []
        for side, bench in ((self.ours, BENCH["ours"]), (them, BENCH["theirs"])):
            places = [p for p in SUBBED if not (side == self.ours and fader and p == fader[0])]
            changes = [(rng.uniform(900, 2400), place) for place in places[:rng.choice((3, 3, 4, 5))]]
            if side == self.ours and fader and rng.random() < 0.6:
                # The tired player comes off, a few minutes either side of his legs going for good
                changes[-1] = (fader[2] + rng.uniform(-120, 240), fader[0])
            subs += [(side, 2, round(at), place, on) for (at, place), on in zip(sorted(changes), bench)]
        return subs, fader

    def legs(self, side: str, n: int, period: int, t: float) -> float:
        """How much of a player's legs are left: a little less as the second half goes on, and the fader's fading."""
        base = 1.0 - (0.06 if period >= 2 else 0.0) * min(1.0, t / 2700)
        if side != self.ours or not self.fader or period != 2 or n != self.fader[0]:
            return base
        if any(s[0] == side and s[3] == n and t >= s[2] for s in self.subs):
            return base
        place, start, low_at, low = self.fader
        return base * (1.0 - (1.0 - low) * min(1.0, max(0.0, (t - start) / (low_at - start))))

    def style(self, side: str, period: int, t: float) -> Style:
        if side != self.ours:
            base = self.opponent_style
            # In the live match they change to a back three in possession at 58'
            if self.story and period == 2 and t >= 780:
                base = replace(base, shape_in="3-4-3")
            return base
        if self.story:
            if period == 1 and t >= 1500:
                return DROPPED
            if period == 2 and t >= 1500:
                return replace(SEE_IT_OUT, width_in=NARROW.width_in, left=NARROW.left, centre=NARROW.centre)
            return BRIGHTON
        for (p, lo, hi), slip in self.drifts.items():
            if p == period and lo <= t < hi:
                return replace(slip, left=self.base.left, right=self.base.right) if slip is not NARROW else slip
        return self.base

    def scripts(self) -> list[Script]:
        if not self.story:
            return []
        them = "away" if self.ours == "home" else "home"
        return [Script("high_regain_goal", self.ours, 1, 18 * 60 + 20, {"regain": 10, "scorer": 9}),
                Script("counter_goal", them, 1, 40 * 60 + 5),
                Script("corner_goal", self.ours, 2, 17 * 60 + 30, {"side": 0.5})]

    # ── the clock ─────────────────────────────────────────────────────────────

    def wall(self, period: int, t: float) -> float:
        """When match second t of a period happens on the wall clock."""
        before = 0 if period == 1 else FIRST * 60 + BREAK * 60
        return self.kickoff + (before + t) / self.speed

    def end(self) -> float:
        return self.kickoff + (FIRST + BREAK + SECOND) * 60 / self.speed

    def where(self, now: float) -> tuple[str, int, float]:
        """(status, period, seconds into it) at wall time now."""
        m = (now - self.kickoff) * self.speed / 60
        if m < 0:
            return "scheduled", 0, 0.0
        if m < FIRST:
            return "live", 1, m * 60
        if m < FIRST + BREAK:
            return "half_time", 1, FIRST * 60
        if m < FIRST + BREAK + SECOND:
            return "live", 2, (m - FIRST - BREAK) * 60
        return "full_time", 2, SECOND * 60

    def fixture(self, now: float) -> dict:
        status, period, t = self.where(now)
        score = {"home": 0, "away": 0}
        for p, at, team in self.goals:
            if (p, at) <= (period, t) and status != "scheduled":
                score[team] += 1
        return {
            "schema": "p4n4.team.fixture/0.1", "site_id": SITE, "match_id": self.match_id, "competition": COMPETITION,
            "matchday": self.matchday, "home": {"code": self.home, "name": CLUBS[self.home]["name"]},
            "away": {"code": self.away, "name": CLUBS[self.away]["name"]}, "team": self.ours,
            "venue": CLUBS[self.home]["ground"], "kickoff": iso(self.kickoff, self.tz), "status": status,
            "period": period, "score": score, "synthetic": True,
        }

    def window_docs(self, now: float) -> list[dict]:
        """The windows played by now: the finished ones, and the one being played (final: false)."""
        status, period, t = self.where(now)
        if status == "scheduled":
            return []
        out = []
        for w in self.windows:
            if (w["period"], w["end_s"]) <= (period, t) or status == "full_time":
                out.append(w)
            elif (w["period"], w["start_s"]) <= (period, t):
                partial = {"period": w["period"], "start_s": w["start_s"], "end_s": round(max(t, w["start_s"] + 1), 1),
                           "final": False, "teams": {side: self.measured.measure(side, w["period"], w["start_s"], t)
                                                     for side in ("home", "away")}}
                out.append(partial)
                break
        return [{"schema": "p4n4.team.window/0.1", "site_id": SITE, "match_id": self.match_id, "home_attacks": "right",
                 **w, "synthetic": True} for w in out]

    def play_docs(self, now: float) -> list[dict]:
        status, period, t = self.where(now)
        out = []
        stamp = datetime.fromtimestamp(self.kickoff, self.tz)
        for seq, p in enumerate(self.plays, 1):
            # A play is posted once the seconds after it have been seen
            if status != "full_time" and (p["period"], p["match_time_s"] + analytics.PLAY_AFTER + 2) > (period, t):
                continue
            out.append({"schema": "p4n4.team.play/0.1", "play_id": f"PL-{stamp:%Y%m%d}-{int(self.kickoff) % 1000:03d}{seq:03d}",
                        "site_id": SITE, "match_id": self.match_id, "camera_id": CAMERA,
                        "timestamp": iso(self.wall(p["period"], p["match_time_s"]), self.tz),
                        **{k: v for k, v in p.items() if v is not None},
                        "evidence": {"clip_uri": f"device://clips/{self.match_id}/{seq:03d}.mp4"}, "synthetic": True})
        return out

    def keyframes(self, docs: list[dict]) -> None:
        """The key plays' drawings, as their keyframes (the analysis page replays every play on its own)."""
        colours = kits(self.home, self.away)
        for doc in docs:
            if not key_play(doc, self.ours):
                continue
            image = diagram.render(doc, colours)
            if image:
                call("PUT", f"/api/v1/evidence/{doc['play_id']}", image, TOKEN, "image/png")


def key_play(doc: dict, ours: str) -> bool:
    """The plays the feed shows (the ingest service's own rule, store.key_play)."""
    d = doc.get("details") or {}
    return (doc["type"] in ("goal", "formation_change") or doc.get("source") in ("video", "label")
            or (doc["type"] == "shot" and (d.get("xg") or 0) >= 0.25)
            or (doc["type"] == "set_piece" and d.get("outcome") in ("goal", "shot"))
            or (doc["type"] == "high_regain" and d.get("led_to_shot"))
            or (doc["type"] == "counter_attack" and d.get("outcome") in ("goal", "shot"))
            or (doc["type"] == "press_broken" and doc["team"] == ours and d.get("led_to_shot")))


def matchday(kickoff: float, tz: tzinfo) -> int:
    return max(1, (datetime.fromtimestamp(kickoff, tz).date() - SEASON_START).days // 7 + 1)


def history(tz: tzinfo, now: float) -> list[Match]:
    """Past matches: Saturdays at 15:00, home and away in turn, newest last."""
    day = datetime.fromtimestamp(now, tz).replace(hour=15, minute=0, second=0, microsecond=0)
    kickoffs = []
    while len(kickoffs) < HISTORY:
        day -= timedelta(days=1)
        if day.weekday() == 5 and day.timestamp() + 3 * 3600 < now:
            kickoffs.append(day.timestamp())
    matches = []
    for kickoff in sorted(kickoffs):
        stamp = datetime.fromtimestamp(kickoff, tz)
        # Each date's fixture is fixed by its week of the season, so a past match keeps its id as days go by
        week = (stamp.date() - SEASON_START).days // 7
        opponent = OPPONENTS[week % len(OPPONENTS)]
        home, away = (CLUB, opponent) if week % 2 == 0 else (opponent, CLUB)
        matches.append(Match(f"EPL-{stamp:%Y%m%d}-{home}-{away}", home, away, kickoff, 1.0, matchday(kickoff, tz), False, tz))
    return matches


def live(tz: tzinfo, now: float, measure: bool = True) -> Match:
    """The match of the current cycle: at the Amex, against the league in turn."""
    length = CYCLE * 60 / SPEED
    n = int(now // length)
    kickoff = n * length
    opponent = OPPONENTS[n % len(OPPONENTS)]
    stamp = datetime.fromtimestamp(kickoff, tz)
    return Match(f"EPL-{stamp:%Y%m%d-%H%M}-{CLUB}-{opponent}", CLUB, opponent, kickoff, SPEED,
                 matchday(kickoff, tz), True, tz, measure)


def heartbeat(now: float, tz: tzinfo, rng: random.Random) -> dict:
    hour = datetime.fromtimestamp(now, tz).hour
    warm = 5 if 12 <= hour <= 18 else 0
    return {
        "schema": "p4n4.team.heartbeat/0.1", "site_id": SITE, "timestamp": iso(now, tz),
        "device": {"hostname": HOST, "model": "Jetson AGX Orin 64GB (simulated)", "cameras": 2},
        "metrics": {
            "temperature_cpu_c": round(54 + warm + rng.uniform(-1.5, 1.5), 1),
            "temperature_gpu_c": round(60 + warm + rng.uniform(-2, 2), 1),
            "cpu_utilization_pct": round(rng.uniform(35, 52), 1),
            "gpu_utilization_pct": round(rng.uniform(68, 86), 1),
            "memory_used_pct": round(rng.uniform(55, 64), 1),
            "disk_used_pct": 38.0,
        },
        "pipeline": {
            "tracking_fps": round(rng.uniform(24.6, 25.0), 1),
            "players_tracked": round(rng.uniform(21.6, 22.0), 1),
            "ball_visible_pct": round(rng.uniform(88, 96), 1),
            "latency_ms": round(rng.uniform(320, 540)),
        },
        "status": "healthy", "synthetic": True,
    }


def pending(match_id: str) -> list[dict]:
    request = urllib.request.Request(
        f"{INGEST_URL}/api/v1/analysis/match?match_id={match_id}&site_id={SITE}",
        headers={"Authorization": f"Bearer {READ_TOKEN}"} if READ_TOKEN else {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return [d for d in json.load(response)["deviations"] if d["status"] == "pending_review"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        log(f"deviations of {match_id}: {e}")
        return []


# What the analyst writes when they set a deviation aside, and when they flag one
EXPECTED = {"formation_out_of_possession": "Our call: a back five to see the lead out.",
            "possession_pct": "Fine: we were happy to let them have it once we led.",
            "width_m": "Wide players came inside on purpose for the overload.",
            "build_up_short_pct": "Wind behind us: keeper told to go long.",
            "load_drop_pct": "Planned: he was coming off at 70 anyway.",
            "set_piece_first_contact_pct": "Two short corners on purpose: no first contact to win."}
FLAGGED = {"line_height_m": "Back four not stepping up after back passes: talk at the break.",
           "ppda": "#9 and #10 not starting the press: their 6 had time all half.",
           "length_m": "Gap between the lines: #6 too deep.",
           "width_m": "#11 tucking in too early: left side empty.",
           "load_drop_pct": "Told the bench: sub warming up.",
           "set_piece_first_contact_pct": "Their #5 winning everything at the near post: switch #4 onto him.",
           "set_piece_defence_pct": "Losing the first ball at their corners: zonal line too deep."}


def decide(match: Match) -> None:
    """The analyst's calls on a past match: the key targets flagged, the rest flagged or set aside."""
    rng = random.Random(f"{SITE}/{match.match_id}/calls")
    for i, d in enumerate(pending(match.match_id)):
        expected = d["metric"] in EXPECTED and (not d["key"] or rng.random() < 0.3)
        action = "dismiss" if expected else "confirm"
        note = EXPECTED.get(d["metric"]) if expected else FLAGGED.get(d["metric"]) if rng.random() < 0.7 else None
        at = datetime.fromisoformat(d["time"]).timestamp() + rng.uniform(20, 150)
        call("POST", f"/api/v1/feed/{SITE}:{d['deviation_id']}/action",
             {"action": action, "by": ANALYSTS[i % 2], "note": note, "at": iso(at, timezone.utc)}, READ_TOKEN)


def backfill(match: Match, now: float) -> None:
    """Post a match as it stands at `now`: the fixture, its plan, its windows and plays."""
    post("/api/v1/fixtures", [match.fixture(now)])
    if match.plan:
        call("PUT", f"/api/v1/plans/{match.match_id}?site_id={SITE}", {**match.plan, "by": "head-coach"}, READ_TOKEN)
    post("/api/v1/windows", match.window_docs(now))
    plays = match.play_docs(now)
    post("/api/v1/plays", plays)
    match.keyframes(plays)


def schedule(now: float, tz: tzinfo) -> None:
    """The live match and the next one: kick-off, half time, the story's moments, in the host's local time."""
    length = CYCLE * 60 / SPEED
    at = lambda t: datetime.fromtimestamp(t).strftime("%H:%M:%S")  # noqa: E731
    for k in (0, 1):
        start = (int(now // length) + k) * length
        kickoff = datetime.fromtimestamp(start, tz)
        print(f"live match {k + 1}: kick-off {at(start)} ({kickoff:%H:%M} site time), "
              f"second half {at(start + (FIRST + BREAK) * 60 / SPEED)}, full time {at(start + (FIRST + BREAK + SECOND) * 60 / SPEED)}")
        for label, period, t in (("19' goal (high regain)", 1, 18 * 60 + 25), ("35' team length off plan", 1, 35 * 60),
                                 ("40' line height off plan (and PPDA, most matches)", 1, 40 * 60),
                                 ("41' their goal (counter)", 1, 40 * 60 + 15), ("half time", 1, FIRST * 60),
                                 ("58' their back three", 2, 13 * 60), ("63' corner goal", 2, 17 * 60 + 35),
                                 ("70' #7's running drop (65' in some matches)", 2, 25 * 60), ("72' #7 off, #19 on", 2, 27 * 60),
                                 ("80' 5-4-1 and the width", 2, 35 * 60)):
            before = 0 if period == 1 else (FIRST + BREAK) * 60
            print(f"  {at(start + (before + t) / SPEED)}  {label}")
        print()


def main() -> None:
    from zoneinfo import ZoneInfo

    tz_name = os.environ.get("SITE_TZ", "UTC") or "UTC"
    tz = timezone.utc if tz_name in ("UTC", "Z") else ZoneInfo(tz_name)
    if "--schedule" in sys.argv:
        return schedule(time.time(), tz)
    once = "--once" in sys.argv
    rng = random.Random(SITE)
    call("PUT", f"/api/v1/plans/default?site_id={SITE}", {**DEFAULT_PLAN, "by": "head-coach"}, READ_TOKEN)
    now = time.time()
    for match in history(tz, now):
        backfill(match, now)
        decide(match)
    log(f"posted {HISTORY} past matches")
    current = live(tz, now)
    backfill(current, now)
    post("/api/v1/heartbeat", [heartbeat(now, tz, rng)])
    log(f"live: {current.match_id}, {current.where(now)}, speed ×{SPEED:g}")
    if once:
        return
    posted = {d["play_id"] for d in current.play_docs(now)}
    last_fixture = json.dumps(current.fixture(now))
    last_window = None
    last_beat = now
    while True:
        time.sleep(TICK)
        now = time.time()
        if now >= current.kickoff + CYCLE * 60 / SPEED:
            backfill(current, now)
            current = live(tz, time.time())
            posted, last_window = set(), None
            log(f"live: {current.match_id} kicks off")
        fixture = current.fixture(now)
        if json.dumps(fixture) != last_fixture:
            post("/api/v1/fixtures", [fixture])
            last_fixture = json.dumps(fixture)
        # The window being played, about every 30 match seconds; a finished one at once
        windows = current.window_docs(now)
        if windows:
            state = (len(windows), windows[-1]["final"], int(windows[-1]["end_s"] // 30))
            if state != last_window:
                post("/api/v1/windows", windows[-2:] if last_window and state[0] > last_window[0] else windows[-1:])
                last_window = state
        due = [d for d in current.play_docs(now) if d["play_id"] not in posted]
        if due:
            post("/api/v1/plays", due)
            current.keyframes(due)
            posted |= {d["play_id"] for d in due}
            for d in due:
                if key_play(d, current.ours):
                    log(f"play {d['play_id']}: {d['type']} ({d['team']}) at {d['period']}H {d['match_time_s'] / 60:.1f}'")
        if now - last_beat >= HEARTBEAT:
            post("/api/v1/heartbeat", [heartbeat(now, tz, rng)])
            last_beat = now


if __name__ == "__main__":
    main()
