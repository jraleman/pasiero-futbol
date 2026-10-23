"""
SQLite store of the ingest service: the matches, what the club device
measured (5-minute windows, technical plays), the staff's game plans, the
deviations from them, and the deterministic queries behind the feed, the
analysis page, the reports and the agent API.

Every number the agent says comes from here: sums, shares and averages
over the stored windows, never from the model. Every sentence that
describes a play or a deviation is built from its own fields, so it says no
more than the device measured.

A deviation is opened when the last 15 minutes (three finished windows) of
one of the plan's targets are off the plan, and closed when they are back
on it. The plan's running drop is a deviation like any other: it names the
players whose ground covered per minute fell furthest from their first 30'. It is a reading, not a decision: it starts pending_review; an
analyst flags it for the bench (confirm) or sets it aside as expected
(dismiss), and can reopen it.

Times are stored twice: the document's own ISO string, and epoch seconds
for range queries. "Today" and weekdays are in the site's time zone (SITE_TZ).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, tzinfo

import plan as rules
from contract import PERIOD_START, TYPES, minute, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS fixtures (
    site_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    kickoff REAL NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    team TEXT NOT NULL,
    status TEXT NOT NULL,
    synthetic INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (site_id, match_id)
);

CREATE TABLE IF NOT EXISTS windows (
    site_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    period INTEGER NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    final INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (site_id, match_id, period, start_s)
);

CREATE TABLE IF NOT EXISTS plays (
    site_id TEXT NOT NULL,
    play_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    t REAL NOT NULL,
    period INTEGER NOT NULL,
    match_time_s REAL NOT NULL,
    type TEXT NOT NULL,
    team TEXT NOT NULL,
    key INTEGER NOT NULL,
    confidence REAL NOT NULL,
    synthetic INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (site_id, play_id)
);
CREATE INDEX IF NOT EXISTS plays_match ON plays (site_id, match_id, period, match_time_s);
CREATE INDEX IF NOT EXISTS plays_time ON plays (t);

-- The staff's game plans; match_id 'default' is the club's, for a match without its own
CREATE TABLE IF NOT EXISTS plans (
    site_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    doc TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (site_id, match_id)
);

CREATE TABLE IF NOT EXISTS deviations (
    site_id TEXT NOT NULL,
    deviation_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    priority TEXT NOT NULL,
    t REAL NOT NULL,
    period INTEGER NOT NULL,
    match_time_s REAL NOT NULL,
    doc TEXT NOT NULL,
    -- Back on the plan: when, and at what
    closed_period INTEGER,
    closed_time_s REAL,
    closed_value TEXT,
    -- The analyst's call; the device and the plan never touch these
    status TEXT NOT NULL DEFAULT 'pending_review',
    reviewed_by TEXT,
    reviewed_at REAL,
    note TEXT,
    PRIMARY KEY (site_id, deviation_id)
);
CREATE INDEX IF NOT EXISTS deviations_match ON deviations (site_id, match_id, metric);
CREATE INDEX IF NOT EXISTS deviations_time ON deviations (t);

-- The latest heartbeat of each device; the history goes to InfluxDB
CREATE TABLE IF NOT EXISTS nodes (
    site_id TEXT NOT NULL,
    hostname TEXT NOT NULL,
    t REAL NOT NULL,
    status TEXT NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (site_id, hostname)
);
"""

# What an analyst can do to a deviation, and the status it leaves
ACTIONS = {"confirm": "confirmed", "dismiss": "dismissed", "reopen": "pending_review"}
ACTION_LABELS = {"confirm": "Flag for the bench", "dismiss": "Expected", "reopen": "Reopen"}
TYPE_NAMES = {"goal": "Goal", "shot": "Shot", "save": "Save", "set_piece": "Set piece", "high_regain": "High regain",
              "counter_attack": "Counter-attack", "build_up": "Build-up", "press_broken": "Press played through",
              "foul": "Foul won", "formation_change": "Formation change"}
SOURCE_NAMES = {"tracking": "the tracking", "video": "the video models", "label": "a person's label"}
SWINGS = {"inswing": "inswinging", "outswing": "outswinging", "driven": "driven", "long_throw": "a long throw",
          "short": "played short", "direct": "a shot at goal"}
FIRST_CONTACTS = {"attack": "first contact ours", "defence": "first contact theirs", "none": "nobody got a touch"}
SAVES = {"catch": "catches it", "parry": "parries it", "punch": "punches it clear"}
PATTERNS = {"open_play": "from open play", "high_press": "after a high regain", "counter": "on the counter", "set_piece": "from a set piece", "penalty": "from the spot"}
SET_PIECES = {"corner": "Corner", "free_kick": "Free kick", "throw_in": "Long throw"}
DELIVERIES = {"near_post": "to the near post", "far_post": "to the far post", "six_yard": "into the six-yard box",
              "penalty_spot": "to the penalty spot", "edge": "to the edge of the box", "short": "played short"}
OUTCOMES = {"goal": "a goal", "shot": "a shot", "cleared": "cleared", "retained": "kept the ball", "none": "no shot"}
PHASE_NAMES = {"in_possession": "in possession", "out_of_possession": "out of possession"}
STATUS_NAMES = {"on_plan": "on plan", "drifting": "drifting", "off_plan": "off plan", "no_data": "no data yet"}
CAMERAS = {"tactical": "Tactical", "tv": "Broadcast (TV)"}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "<N>d (e.g. 7d, 30d)", "season",
           "a weekday (saturday)")
# A device that hasn't sent a heartbeat in this long counts as offline (3 missed beats)
NODE_TIMEOUT = 90
# The rolling stretch a deviation is judged on: the last three finished windows
ROLLING = 3
# A player's milestones in a match, tagged on the 5 minutes they were reached in: a top speed of SPEED_TAG_KMH or
# more (his first), and each of DISTANCE_TAGS_KM covered
SPEED_TAG_KMH, DISTANCE_TAGS_KM = 30.0, (5, 10)
HALF_TIME_S = 15 * 60


class PeriodError(ValueError):
    pass


def canonical(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def minute_of(period: int, match_time_s: float) -> float:
    """Minutes since kick-off as a number, stoppage time folded in (46.5 is early in the second half)."""
    return PERIOD_START[period] + match_time_s / 60


def key_play(doc: dict, ours: str) -> bool:
    """Whether a play goes to the feed: goals, big chances, set pieces that ended in a shot, regains and
    counters that did, a press of ours played through into a shot, formation changes, and every play found on
    the footage (by the video models or a person), which the Video tab shows as it plays."""
    d = doc.get("details") or {}
    kind = doc["type"]
    return (kind == "goal" or kind == "formation_change" or doc.get("source") in ("video", "label")
            or (kind == "shot" and (d.get("xg") or 0) >= 0.25)
            or (kind == "set_piece" and d.get("outcome") in ("goal", "shot"))
            or (kind == "high_regain" and d.get("led_to_shot"))
            or (kind == "counter_attack" and d.get("outcome") in ("goal", "shot"))
            or (kind == "press_broken" and doc["team"] == ours and d.get("led_to_shot")))


class Store:
    def __init__(self, path: str, tz: tzinfo, retention_days: int = 400, season_start: str | None = None,
                 club: str | None = None, squads: dict | None = None) -> None:
        self.tz = tz
        # Fictional names for the synthetic season's shirt numbers ({club code: {"7": name}}, squads.json)
        self.squads = squads or {}
        # The club's code (league.json): the season counts its own matches, not a dataset's or a scouting one
        self.club = club
        self.retention_days = retention_days
        self.season_start = season_start
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def player_name(self, fixture: dict, side: str, number: object) -> str | None:
        """A player's fictional name, on synthetic matches only: real players keep their numbers."""
        if number is None or not fixture.get("synthetic"):
            return None
        return self.squads.get(fixture[side]["code"], {}).get(str(number))

    def player_label(self, fixture: dict, side: str, number: object) -> str:
        """"#7 Tomas Calloway", or "#7" without a name."""
        name = self.player_name(fixture, side, number)
        return f"#{number} {name}" if name else f"#{number}"

    # ── writes ────────────────────────────────────────────────────────────────

    def _upsert(self, table: str, keys: dict, values: dict, doc: dict) -> str:
        """
        Insert or replace one row's document; created, updated or unchanged.
        Columns it doesn't name (a deviation's review) are left as they are.
        """
        text = canonical(doc)
        where = " AND ".join(f"{k} = ?" for k in keys)
        row = self.db.execute(f"SELECT doc FROM {table} WHERE {where}", tuple(keys.values())).fetchone()
        if row and row["doc"] == text:
            return "unchanged"
        columns = {**keys, **values, "doc": text, "received": time.time()}
        if row:
            sets = ", ".join(f"{k} = ?" for k in columns)
            self.db.execute(f"UPDATE {table} SET {sets} WHERE {where}", (*columns.values(), *keys.values()))
            return "updated"
        self.db.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                        tuple(columns.values()))
        return "created"

    def _recent(self, t: float) -> None:
        if t < time.time() - self.retention_days * 86400:
            raise ValueError(f"older than the retention period ({self.retention_days} days)")

    def _known(self, doc: dict) -> dict:
        fixture = self._fixture(doc["site_id"], doc["match_id"])
        if not fixture:
            raise ValueError(f"no fixture {doc['match_id']} for this site: post it to /api/v1/fixtures first")
        return fixture

    def put_fixture(self, doc: dict) -> str:
        kickoff = parse_time(doc["kickoff"], self.tz).timestamp()
        self._recent(kickoff)
        with self.lock:
            return self._upsert(
                "fixtures", {"site_id": doc["site_id"], "match_id": doc["match_id"]},
                {"kickoff": kickoff, "home": doc["home"]["code"], "away": doc["away"]["code"], "team": doc["team"],
                 "status": doc["status"], "synthetic": int(doc["synthetic"])}, doc)

    def put_window(self, doc: dict) -> str:
        with self.lock:
            self._known(doc)
            return self._upsert(
                "windows", {"site_id": doc["site_id"], "match_id": doc["match_id"], "period": doc["period"],
                            "start_s": float(doc["start_s"])},
                {"end_s": float(doc["end_s"]), "final": int(doc["final"])}, doc)

    def put_play(self, doc: dict) -> str:
        t = parse_time(doc["timestamp"], self.tz).timestamp()
        self._recent(t)
        with self.lock:
            fixture = self._known(doc)
            return self._upsert(
                "plays", {"site_id": doc["site_id"], "play_id": doc["play_id"]},
                {"match_id": doc["match_id"], "camera_id": doc["camera_id"], "t": t, "period": doc["period"],
                 "match_time_s": float(doc["match_time_s"]), "type": doc["type"], "team": doc["team"],
                 "key": int(key_play(doc, fixture["team"])), "confidence": float(doc["confidence"]),
                 "synthetic": int(doc["synthetic"])}, doc)

    def put_heartbeat(self, doc: dict) -> str:
        with self.lock:
            return self._upsert(
                "nodes", {"site_id": doc["site_id"], "hostname": doc["device"]["hostname"]},
                {"t": parse_time(doc["timestamp"], self.tz).timestamp(), "status": doc["status"]}, doc)

    def put_plan(self, site: str, match_id: str, doc: dict, by: str) -> dict:
        """A plan from the editor (plan.check()ed): the match's own, or the club's default."""
        with self.lock:
            if match_id != "default" and not self._fixture(site, match_id):
                raise KeyError(match_id)
            self.db.execute("INSERT OR REPLACE INTO plans (site_id, match_id, doc, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (site, match_id, canonical(doc), by, time.time()))
            return self.plan(site, match_id)

    def delete_plan(self, site: str, match_id: str) -> bool:
        with self.lock:
            return self.db.execute("DELETE FROM plans WHERE site_id = ? AND match_id = ?", (site, match_id)).rowcount > 0

    def act(self, site: str, deviation_id: str, action: str, by: str, note: str | None = None,
            now: float | None = None) -> tuple[dict, dict | None]:
        """
        An analyst's call on a deviation: confirm (flag it for the bench),
        dismiss (expected) or reopen. Returns (the deviation, the change for
        InfluxDB and MQTT or None). KeyError for an unknown one.
        """
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        status = ACTIONS[action]
        now = now if now is not None else time.time()
        with self.lock:
            row = self._deviation_row(site, deviation_id)
            if row is None:
                raise KeyError(deviation_id)
            now = min(max(now, row["t"]), time.time())
            if row["status"] == status:
                return self.deviation(site, deviation_id), None
            if status == "pending_review":
                self.db.execute("UPDATE deviations SET status = ?, reviewed_by = NULL, reviewed_at = NULL, note = NULL "
                                "WHERE site_id = ? AND deviation_id = ?", (status, site, deviation_id))
            else:
                self.db.execute("UPDATE deviations SET status = ?, reviewed_by = ?, reviewed_at = ?, note = ? "
                                "WHERE site_id = ? AND deviation_id = ?", (status, by, now, note, site, deviation_id))
            change = {"site_id": site, "deviation_id": deviation_id, "match_id": row["match_id"], "metric": row["metric"],
                      "decision": status, "previous": row["status"], "by": by, "at": now,
                      "review_s": round(now - row["t"], 1) if status != "pending_review" else None,
                      "synthetic": bool(self._fixture(site, row["match_id"])["synthetic"])}
            return self.deviation(site, deviation_id), change

    def purge(self, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - self.retention_days * 86400
        with self.lock:
            old = [r["match_id"] for r in self.db.execute("SELECT match_id FROM fixtures WHERE kickoff < ?", (cutoff,))]
            for table in ("windows", "plays", "deviations", "plans"):
                self.db.executemany(f"DELETE FROM {table} WHERE match_id = ?", [(m,) for m in old])
            self.db.execute("DELETE FROM fixtures WHERE kickoff < ?", (cutoff,))
            return len(old)

    # ── the game plan, against the windows ───────────────────────────────────

    def evaluate(self, site: str, match_id: str, now: float | None = None) -> list[tuple[str, dict]]:
        """
        After a window: open a deviation for each of the plan's targets whose
        last 15 minutes are off the plan, close those back on it. Returns
        [("opened" | "closed", the deviation)].
        """
        now = now if now is not None else time.time()
        with self.lock:
            fixture = self._fixture(site, match_id)
            plan = self.plan(site, match_id)
            if not fixture or not plan:
                return []
            windows = [w for w in self.windows(site, match_id) if w["final"]]
            if len(windows) < ROLLING:
                return []
            ours = fixture["team"]
            last = windows[-ROLLING:]
            at_period, at_s = last[-1]["period"], last[-1]["end_s"]
            out = []
            for target in rules.checks(plan["doc"]):
                metric = target["metric"]
                measured, state = rules.evaluate(target, last, ours, windows)
                open_row = self.db.execute(
                    "SELECT * FROM deviations WHERE site_id = ? AND match_id = ? AND metric = ? AND closed_period IS NULL "
                    "ORDER BY t DESC LIMIT 1", (site, match_id, metric)).fetchone()
                if state == "off_plan" and not open_row:
                    n = self.db.execute("SELECT COUNT(*) FROM deviations WHERE site_id = ? AND match_id = ? AND metric = ?",
                                        (site, match_id, metric)).fetchone()[0] + 1
                    deviation_id = f"{match_id}.{metric}.{n}"
                    match_value, _ = rules.evaluate(target, windows, ours, windows)
                    doc = {"target": target, "value": measured, "match_value": match_value,
                           "from": [last[0]["period"], last[0]["start_s"]], "to": [at_period, at_s],
                           "plan_title": plan["doc"].get("title")}
                    if metric == "load_drop_pct":
                        doc["players"] = [list(p) for p in rules.load_drops(last, ours, windows)[:3]]
                    self.db.execute(
                        "INSERT INTO deviations (site_id, deviation_id, match_id, metric, priority, t, period, match_time_s, doc) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (site, deviation_id, match_id, metric, "high" if target.get("key") else "normal",
                         min(now, self.wall_time(fixture, at_period, at_s)), at_period, at_s, canonical(doc)))
                    out.append(("opened", self.deviation(site, deviation_id)))
                elif state == "on_plan" and open_row:
                    self.db.execute("UPDATE deviations SET closed_period = ?, closed_time_s = ?, closed_value = ? "
                                    "WHERE site_id = ? AND deviation_id = ?",
                                    (at_period, at_s, json.dumps(measured), site, open_row["deviation_id"]))
                    out.append(("closed", self.deviation(site, open_row["deviation_id"])))
            return out

    def wall_time(self, fixture: dict, period: int, match_time_s: float) -> float:
        """When a match moment happened, on the clock: kick-off, the halves, 15 minutes between them."""
        kickoff = parse_time(fixture["kickoff"], self.tz).timestamp()
        return kickoff + PERIOD_START[period] * 60 + (HALF_TIME_S if period >= 2 else 0) + match_time_s

    def plan(self, site: str, match_id: str) -> dict | None:
        """
        The match's plan, else the club's default: {"doc", "match_id" (whose it is), "updated_by", "updated_at"}.
        A scouting match has none: neither side is ours.
        """
        fixture = self._fixture(site, match_id) if match_id != "default" else None
        if fixture and fixture.get("scouting"):
            return None
        for whose in (match_id, "default"):
            row = self.db.execute("SELECT * FROM plans WHERE site_id = ? AND match_id = ?", (site, whose)).fetchone()
            if row:
                return {"doc": json.loads(row["doc"]), "match_id": whose, "inherited": whose != match_id,
                        "updated_by": row["updated_by"], "updated_at": self.iso(row["updated_at"])}
        return None

    def adherence(self, site: str, match_id: str) -> dict:
        """
        Each of the plan's targets over the match: so far, per half, the
        last 15 minutes and every window, with its status.
        """
        fixture = self._fixture(site, match_id)
        plan = self.plan(site, match_id)
        windows = self.windows(site, match_id)
        if not plan:
            return {"plan": None, "targets": [], "counts": {}}
        ours = fixture["team"]
        first = [w for w in windows if w["period"] == 1]
        scopes = {"match": windows, "first_half": first, "second_half": [w for w in windows if w["period"] == 2],
                  "last_15": windows[-ROLLING:], "first_half_last_15": first[-ROLLING:]}
        targets = []
        for target in rules.checks(plan["doc"]):
            metric = target["metric"]
            row = {"metric": metric, "name": rules.name(metric), "unit": rules.unit(metric), "key": target.get("key", False),
                   "target": rules.target_text(target), "op": target.get("op"), "value": target.get("value", target.get("shape")),
                   "description": rules.METRICS[metric][5] if metric in rules.METRICS else
                   f"The formation the device reads {PHASE_NAMES[rules.SHAPES[metric][1]]}, against the plan's."}
            for scope, ws in scopes.items():
                measured, state = rules.evaluate(target, ws, ours, windows)
                row[scope] = {"value": measured, "text": rules.fmt(metric, measured), "status": state}
            row["windows"] = []
            for w in windows:
                measured, state = rules.evaluate(target, [w], ours, windows)
                row["windows"].append({"period": w["period"], "start_s": w["start_s"], "end_s": w["end_s"],
                                       "label": f"{minute(w['period'], w['start_s'])}'", "final": w["final"],
                                       "value": measured, "status": state})
            targets.append(row)
        counts = Counter(t["match"]["status"] for t in targets)
        judged = sum(counts[s] for s in ("on_plan", "drifting", "off_plan"))
        return {"plan": {**plan["doc"], "inherited": plan["inherited"], "updated_by": plan["updated_by"],
                         "updated_at": plan["updated_at"]},
                "targets": targets, "counts": dict(counts),
                "on_plan_share": round(counts["on_plan"] / judged, 3) if judged else None}

    # ── helpers ───────────────────────────────────────────────────────────────

    def sites(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT site_id FROM fixtures UNION SELECT site_id FROM nodes ORDER BY site_id")]

    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def iso(self, t: float | None) -> str | None:
        return None if t is None else self.local(t).isoformat(timespec="seconds")

    def period(self, period: str | None = None, start: str | None = None, end: str | None = None,
               now: float | None = None) -> tuple[float, float, dict]:
        """
        (start, end, description) of a period: today, yesterday, this_week,
        last_week, this_month, last_month, season (since SEASON_START), <N>d,
        a weekday, or from/to dates. Weeks start on Monday. Default: the season.
        """
        today = self.local(now if now is not None else time.time()).replace(hour=0, minute=0, second=0, microsecond=0)
        end_t = now if now is not None else time.time()
        name = (period or "").strip().casefold()
        if start or end:
            try:
                lo = self._bound(start, today - timedelta(days=29), False)
                hi = self._bound(end, None, True) if end else end_t
            except ValueError as e:
                raise PeriodError(f"from/to: {e}") from None
            name = "custom"
        elif name in ("", "season"):
            first = datetime.fromisoformat(self.season_start).replace(tzinfo=self.tz) if self.season_start else today - timedelta(days=365)
            name, lo, hi = "season", first.timestamp(), end_t
        elif name in ("last_30d", "30d"):
            name, lo, hi = "last_30d", (today - timedelta(days=29)).timestamp(), end_t
        elif name == "today":
            lo, hi = today.timestamp(), end_t
        elif name == "yesterday":
            lo, hi = (today - timedelta(days=1)).timestamp(), today.timestamp()
        elif name == "this_week":
            lo, hi = (today - timedelta(days=today.weekday())).timestamp(), end_t
        elif name == "last_week":
            monday = today - timedelta(days=today.weekday())
            lo, hi = (monday - timedelta(days=7)).timestamp(), monday.timestamp()
        elif name == "this_month":
            lo, hi = today.replace(day=1).timestamp(), end_t
        elif name == "last_month":
            first = today.replace(day=1)
            lo, hi = (first - timedelta(days=1)).replace(day=1).timestamp(), first.timestamp()
        elif name in WEEKDAYS:
            day = today - timedelta(days=(today.weekday() - WEEKDAYS.index(name)) % 7)
            lo, hi = day.timestamp(), min(end_t, (day + timedelta(days=1)).timestamp())
        elif name.endswith("d") and name[:-1].isdigit() and 0 < int(name[:-1]) <= 400:
            lo, hi = (today - timedelta(days=int(name[:-1]) - 1)).timestamp(), end_t
        else:
            raise PeriodError(f"unknown period {period!r}; use one of: {', '.join(PERIODS)}")
        if hi <= lo:
            raise PeriodError("the period ends before it starts")
        return lo, hi, {"name": name, "from": self.iso(lo), "to": self.iso(hi)}

    def _bound(self, value: str | None, default: datetime | None, upper: bool) -> float:
        if not value:
            return default.timestamp()
        if len(value) == 10:
            day = datetime.fromisoformat(value).replace(tzinfo=self.tz)
            return (day + timedelta(days=1)).timestamp() if upper else day.timestamp()
        return parse_time(value, self.tz).timestamp()

    def _fixture(self, site: str, match_id: str) -> dict | None:
        row = self.db.execute("SELECT doc FROM fixtures WHERE site_id = ? AND match_id = ?", (site, match_id)).fetchone()
        return json.loads(row["doc"]) if row else None

    def _play_row(self, site: str, play_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM plays WHERE site_id = ? AND play_id = ?", (site, play_id)).fetchone()

    def _deviation_row(self, site: str, deviation_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM deviations WHERE site_id = ? AND deviation_id = ?", (site, deviation_id)).fetchone()

    def windows(self, site: str, match_id: str) -> list[dict]:
        rows = self.db.execute("SELECT doc FROM windows WHERE site_id = ? AND match_id = ? ORDER BY period, start_s",
                               (site, match_id)).fetchall()
        return [json.loads(r["doc"]) for r in rows]

    def current_match(self, site: str, now: float | None = None) -> str | None:
        """
        The match being played (live or half time), else the latest of the club's own that has kicked off (not
        a scouting match, nor another team's), else the latest of any.
        """
        now = now if now is not None else time.time()
        row = self.db.execute("SELECT match_id FROM fixtures WHERE site_id = ? AND status IN ('live', 'half_time') "
                              "ORDER BY kickoff DESC LIMIT 1", (site,)).fetchone()
        if row:
            return row["match_id"]
        rows = self.db.execute("SELECT match_id, doc FROM fixtures WHERE site_id = ? AND kickoff <= ? ORDER BY kickoff DESC",
                               (site, now)).fetchall()
        own = next((r for r in rows if not json.loads(r["doc"]).get("scouting")
                    and (not self.club or json.loads(r["doc"])[json.loads(r["doc"])["team"]]["code"] == self.club)), None)
        row = own or (rows[0] if rows else None)
        return row["match_id"] if row else None

    def pick_match(self, site: str, match_id: str | None) -> str:
        match_id = (match_id or "").strip() or self.current_match(site)
        if not match_id or not self._fixture(site, match_id):
            raise PeriodError(f"no match {match_id!r} at site {site}" if match_id else f"no match at site {site} yet")
        return match_id

    def match_info(self, site: str, fixture: dict, windows: list[dict] | None = None) -> dict:
        ours = fixture["team"]
        them = "away" if ours == "home" else "home"
        windows = windows if windows is not None else self.windows(site, fixture["match_id"])
        last = windows[-1] if windows else None
        clock = None
        if fixture["status"] == "live" and last:
            clock = minute(last["period"], max(0, last["end_s"] - 1))
        return {
            "match_id": fixture["match_id"], "site_id": site, "competition": fixture["competition"],
            "matchday": fixture.get("matchday"), "venue": fixture.get("venue"),
            "home": fixture["home"], "away": fixture["away"], "team": ours,
            "us": fixture[ours], "opponent": fixture[them], "at_home": ours == "home",
            "kickoff": self.iso(parse_time(fixture["kickoff"], self.tz).timestamp()),
            "status": fixture["status"], "period": fixture["period"], "score": fixture["score"], "minute": clock,
            "result": self._result(fixture), "synthetic": bool(fixture["synthetic"]),
            "scouting": bool(fixture.get("scouting")),
        }

    @staticmethod
    def _result(fixture: dict) -> str | None:
        if fixture["status"] != "full_time":
            return None
        ours = fixture["team"]
        us, them = fixture["score"][ours], fixture["score"]["away" if ours == "home" else "home"]
        return "W" if us > them else "L" if us < them else "D"

    def score_text(self, fixture: dict) -> str:
        return f'{fixture["home"]["code"]} {fixture["score"]["home"]}-{fixture["score"]["away"]} {fixture["away"]["code"]}'

    # ── team data ─────────────────────────────────────────────────────────────

    def team_stats(self, windows: list[dict], side: str) -> dict:
        """One side's numbers over some windows."""
        them = "away" if side == "home" else "home"
        s = lambda key, who=side: sum(w["teams"][who][key] for w in windows)  # noqa: E731
        entries = Counter()
        for w in windows:
            entries.update(w["teams"][side]["final_third_entries"])
        total_entries = sum(entries.values())
        v = lambda metric: rules.value(metric, windows, side)  # noqa: E731
        rnd = lambda x, d=1: None if x is None else round(x, d)  # noqa: E731
        return {
            "possession_pct": rnd(v("possession_pct")),
            "passes": s("passes"), "passes_completed": s("passes_completed"),
            "pass_accuracy_pct": rnd(v("pass_accuracy_pct")),
            "ppda": rnd(v("ppda")),
            "field_tilt_pct": rnd(v("field_tilt_pct")),
            "shots": s("shots"), "shots_on_target": s("shots_on_target"), "xg": round(s("xg"), 2), "goals": s("goals"),
            "box_entries": s("box_entries"),
            "final_third_entries": total_entries,
            "entries_pct": {k: round(100 * entries[k] / total_entries) if total_entries else None for k in ("left", "centre", "right")},
            "turnovers": s("turnovers"), "regains": s("regains"), "regains_high": s("regains_high"),
            "counterpress_pct": rnd(v("counterpress_pct")),
            "corners": s("corners"), "free_kicks": s("free_kicks"),
            "goal_kicks": s("goal_kicks"), "goal_kicks_short": s("goal_kicks_short"),
            "line_height_m": rnd(v("line_height_m")), "width_m": rnd(v("width_m")), "length_m": rnd(v("length_m")),
            "distance_km": round(s("distance_m") / 1000, 1),
            "hsr_m": s("hsr_m"), "sprints": s("sprints"),
            "set_pieces": s("set_pieces"), "set_piece_shots": s("set_piece_shots"),
            "set_piece_xg": round(s("set_piece_xg"), 2),
            "set_piece_first_contact_pct": rnd(v("set_piece_first_contact_pct"), 0),
            **self.pass_quality(windows, side),
            "shape_in_possession": rules.shape(windows, side, "in_possession"),
            "shape_out_of_possession": rules.shape(windows, side, "out_of_possession"),
        }

    @staticmethod
    def pass_quality(windows: list[dict], side: str) -> dict:
        """Pass length, difficulty (0-100) and speed, the hard passes, the fastest ball and shot; None where not measured."""
        g = lambda key: sum(w["teams"][side].get(key, 0) for w in windows)  # noqa: E731
        top = lambda key: max((w["teams"][side].get(key, 0) for w in windows), default=0)  # noqa: E731
        rated, timed, hard = g("passes_rated"), g("passes_timed"), g("passes_hard")
        return {
            "pass_length_m": round(g("pass_distance_m") / rated, 1) if rated else None,
            "pass_difficulty": round(100 * g("pass_difficulty") / rated) if rated else None,
            "passes_hard": hard if rated else None, "passes_hard_completed": g("passes_hard_completed") if rated else None,
            "passes_hard_pct": round(100 * g("passes_hard_completed") / hard) if hard else None,
            "pass_speed_kmh": round(3.6 * g("pass_speed_ms") / timed, 1) if timed else None,
            "ball_speed_max_kmh": round(3.6 * top("ball_speed_max_ms"), 1) if top("ball_speed_max_ms") else None,
            "shot_speed_max_kmh": round(3.6 * top("shot_speed_max_ms"), 1) if top("shot_speed_max_ms") else None,
        }

    @staticmethod
    def positions(windows: list[dict], side: str, phase: str) -> list[list]:
        """Average positions per shirt number over some windows, weighted by frames ([number, x, y], the team's frame)."""
        acc: dict = defaultdict(lambda: [0.0, 0.0, 0])
        for w in windows:
            p = w["teams"][side][phase]
            for number, x, y in p.get("positions", []):
                a = acc[number]
                a[0] += x * p["frames"]
                a[1] += y * p["frames"]
                a[2] += p["frames"]
        return [[n, round(a[0] / a[2], 1), round(a[1] / a[2], 1)] for n, a in sorted(acc.items(), key=lambda kv: -kv[1][2])[:10]
                if a[2]]

    @staticmethod
    def network(windows: list[dict], side: str) -> list[list]:
        pairs = Counter()
        for w in windows:
            for a, b, n in w["teams"][side].get("pass_pairs", []):
                pairs[(a, b)] += n
        return [[a, b, n] for (a, b), n in pairs.most_common()]

    def scopes(self, windows: list[dict]) -> dict:
        return {"match": windows, "first_half": [w for w in windows if w["period"] == 1],
                "second_half": [w for w in windows if w["period"] == 2], "last_15": windows[-ROLLING:]}

    def shapes(self, windows: list[dict], fixture: dict) -> dict:
        """Per scope and side: the formation read in and out of possession, average positions, the pass network."""
        out = {}
        for scope, ws in self.scopes(windows).items():
            out[scope] = {}
            for side in ("home", "away"):
                out[scope][side] = {
                    phase: {"formation": rules.shape(ws, side, phase), "positions": self.positions(ws, side, phase)}
                    for phase in ("in_possession", "out_of_possession")}
                out[scope][side]["network"] = self.network(ws, side)
        return out

    # ── plays and deviations, described ──────────────────────────────────────

    def describe(self, doc: dict, fixture: dict) -> str:
        """One sentence about a play, from its own fields."""
        d = doc.get("details") or {}
        code = fixture[doc["team"]]["code"]
        other = fixture["away" if doc["team"] == "home" else "home"]["code"]

        def player(role: str) -> str | None:
            p = next((p for p in doc.get("players") or [] if p["role"] == role), None)
            return self.player_label(fixture, p["team"], p["number"]) if p and p.get("number") is not None else None

        # How hard it was struck, when the device measured it
        struck = f"{d['speed_kmh']:.0f} km/h" if d.get("speed_kmh") else None
        kind = doc["type"]
        if kind == "goal":
            who = player("shooter")
            assist = player("assist")
            how = ", ".join(x for x in ("a header" if d.get("body") == "head" else None,
                                        f"xG {d['xg']:.2f}" if d.get("xg") is not None else None, struck) if x)
            return (f"Goal for {code}{' by ' + who if who else ''} {PATTERNS.get(d.get('pattern'), '')}".rstrip()
                    + (f" ({how})" if how else "") + (f", assisted by {assist}" if assist else "") + ".")
        if kind == "shot":
            who = player("shooter") or "a player"
            outcome = {"saved": "saved", "off_target": "off target", "blocked": "blocked", "goal": "in"}.get(d.get("outcome"), "")
            big = (d.get("xg") or 0) >= 0.25
            return sentence(f"{'big chance for ' + code + ': ' if big else ''}{who} ({code}) "
                            f"{'heads' if d.get('body') == 'head' else 'shoots'}, {outcome} (xG {d.get('xg', 0):.2f}"
                            f"{', ' + struck if struck else ''}).")
        if kind == "set_piece":
            taker = player("taker")
            first = player("first_contact")
            how = ", ".join(x for x in (SWINGS.get(d.get("swing")) if d.get("swing") not in ("short", "long_throw") else None,
                                        DELIVERIES.get(d.get("delivery"))) if x)
            contact = FIRST_CONTACTS.get(d.get("first_contact"))
            if contact and first and d.get("first_contact") != "none":
                contact += f" ({first})"
            value = f" (xG {d['xg']:.2f})" if d.get("xg") else ""
            return (f"{SET_PIECES.get(d.get('kind'), 'Set piece')} for {code}{' (' + taker + ')' if taker else ''}"
                    f"{', “' + d['routine'] + '”' if d.get('routine') else ''}{', ' + how if how else ''}"
                    f"{'; ' + contact if contact else ''}: {OUTCOMES.get(d.get('outcome'), d.get('outcome', ''))}{value}.")
        if kind == "save":
            keeper = player("goalkeeper") or "the keeper"
            return sentence(f"{keeper} ({code}) {SAVES.get(d.get('kind'), 'saves')}"
                            + (f" (xG {d['xg']:.2f} against)" if d.get("xg") is not None else "") + ".")
        if kind == "foul" and d.get("whistled") is None and not d.get("area"):
            # From the video models: a contact and a player down; whether it was given, the video doesn't say
            fouled, fouler = player("fouled"), player("fouler")
            return (f"A challenge by {fouler + ' (' + other + ')' if fouler else other} and {fouled + ' (' + code + ')' if fouled else 'a ' + code + ' player'}"
                    " down: a likely foul, found on the video (whether it was given, the video doesn't say).")
        if kind == "foul":
            fouled, fouler = player("fouled"), player("fouler")
            where = {"box": "in the box", "final_third": f"{d['free_kick_x_m']:.0f} m from goal" if d.get("free_kick_x_m")
                     else "in their third", "middle_third": "in midfield", "own_third": "in their own third"}.get(d.get("area"), "")
            return (f"{code} win a free kick {where}{': ' + fouled if fouled else ''}"
                    f"{' fouled by ' + fouler + ' (' + other + ')' if fouler else ''}"
                    + ("" if d.get("whistled", True) else ", not given") + ".")
        if kind == "high_regain":
            who = player("regain")
            since = d.get("seconds_after_loss")
            return (f"{code} win it back {round(105 - d.get('x_m', 70))} m from goal{' (' + who + ')' if who else ''}"
                    + (f", {since:.0f} s after losing it: counter-press" if d.get("counterpress") and since is not None else "")
                    + (". It leads to a shot." if d.get("led_to_shot") else "."))
        if kind == "counter_attack":
            return (f"{code} counter from {d.get('from_x_m', 0):.0f} m out of their own goal: in the box in "
                    f"{d.get('duration_s', 0):.0f} s, {OUTCOMES.get(d.get('outcome'), 'no shot')}.")
        if kind == "build_up":
            return (f"{code} goal kick played {'short' if d.get('short') else 'long'}: "
                    + (f"out past halfway in {int(d.get('passes', 0))} passes." if d.get("progressed") else "lost before halfway."))
        if kind == "press_broken":
            who = player("line_breaker")
            return (f"{other} play through {code}'s press: from {d.get('from_x_m', 0):.0f} m out of their goal to "
                    f"{code}'s final third in {d.get('seconds', 0):.0f} s{' (' + who + ')' if who else ''}"
                    + (", and get a shot away." if d.get("led_to_shot") else "."))
        if kind == "formation_change":
            return f"{code} change shape {PHASE_NAMES.get(d.get('phase'), '')}: {d.get('from') or '?'} to {d.get('to')}."
        return TYPE_NAMES.get(kind, kind)

    def _play(self, row: sqlite3.Row, fixture: dict) -> dict:
        doc = json.loads(row["doc"])
        return {
            "play_id": row["play_id"], "minute": minute(row["period"], row["match_time_s"]), "period": row["period"],
            "match_time_s": row["match_time_s"], "type": row["type"], "type_name": TYPE_NAMES[row["type"]],
            "team": fixture[row["team"]]["code"], "side": row["team"], "ours": row["team"] == fixture["team"],
            "key": bool(row["key"]), "summary": self.describe(doc, fixture), "details": doc.get("details") or {},
            "camera_id": row["camera_id"], "confidence": round(row["confidence"], 2), "time": self.iso(row["t"]),
            "source": doc.get("source") or "tracking",
        }

    def plays(self, site: str, match_id: str, kind: str | None = None, side: str | None = None,
              period: int | None = None, key: bool | None = None, limit: int = 100) -> dict:
        if kind and kind not in TYPES:
            raise PeriodError(f"type must be one of {', '.join(TYPES)}")
        fixture = self._fixture(site, match_id)
        rows = self.db.execute("SELECT * FROM plays WHERE site_id = ? AND match_id = ? ORDER BY period, match_time_s, play_id",
                               (site, match_id)).fetchall()
        rows = [r for r in rows if (not kind or r["type"] == kind) and (not side or r["team"] == side)
                and (not period or r["period"] == period) and (key is None or bool(r["key"]) == key)]
        return {"match_id": match_id, "plays_total": len(rows), "plays": [self._play(r, fixture) for r in rows[-limit:]],
                "synthetic_data": bool(fixture["synthetic"])}

    def play(self, site: str, play_id: str, full: bool = False) -> dict:
        row = self._play_row(site, play_id)
        if row is None:
            raise KeyError(play_id)
        fixture = self._fixture(site, row["match_id"])
        doc = json.loads(row["doc"])
        result = {**self._play(row, fixture), "match_id": row["match_id"], "site_id": site,
                  "players": [{**p, "team_code": fixture[p["team"]]["code"],
                               "name": self.player_name(fixture, p["team"], p.get("number"))} for p in doc.get("players") or []],
                  "evidence": doc.get("evidence"), "synthetic_data": bool(row["synthetic"])}
        if full:
            result["snapshot"], result["track"] = doc.get("snapshot"), doc.get("track")
        return result

    def deviation(self, site: str, deviation_id: str) -> dict:
        row = self._deviation_row(site, deviation_id)
        if row is None:
            raise KeyError(deviation_id)
        fixture = self._fixture(site, row["match_id"])
        doc = json.loads(row["doc"])
        target, metric = doc["target"], row["metric"]
        (p0, s0), (p1, s1) = doc["from"], doc["to"]
        stretch = f"{minute(p0, s0)}'–{minute(p1, max(0, s1 - 1))}'"
        if metric == "load_drop_pct" and doc.get("players"):
            (number, drop, before, after), *rest = doc["players"]
            ours = fixture["team"]
            others = [f"{self.player_label(fixture, ours, n)} {d:.0f}%" for n, d, *_ in rest if d > 0]
            who = self.player_label(fixture, ours, number)
            title = f"{who} running {drop:.0f}% less than his first 30'"
            summary = (f"From {stretch}, {who} covered {after} m a minute against {before} in his first 30' "
                       f"({drop:.0f}% less; the plan allows {rules.fmt(metric, target['value'])})."
                       + (f" Next: {', '.join(others)}." if others else ""))
        elif metric in rules.SHAPES:
            phase = PHASE_NAMES[rules.SHAPES[metric][1]]
            title = f"Shape {phase}: {doc['value']}, plan {target['shape']}"
            summary = (f"From {stretch} the device read {doc['value']} {phase}; the plan is {target['shape']}. "
                       f"Over the match so far: mostly {doc['match_value'] or '?'}.")
        else:
            worse = "below" if target["op"] == ">=" else "above"
            title = f"{rules.name(metric)} {worse} plan"
            summary = (f"{rules.name(metric)}, {stretch}: {rules.fmt(metric, doc['value'])}; the plan asks "
                       f"{rules.target_text(target)}. Match so far: {rules.fmt(metric, doc['match_value'])}.")
        closed = None
        if row["closed_period"] is not None:
            closed_value = json.loads(row["closed_value"]) if row["closed_value"] else None
            closed = {"minute": minute(row["closed_period"], max(0, row["closed_time_s"] - 1)),
                      "value": rules.fmt(metric, closed_value), "period": row["closed_period"],
                      "match_time_s": row["closed_time_s"]}
        return {
            "deviation_id": deviation_id, "site_id": site, "match_id": row["match_id"], "metric": metric,
            "name": rules.name(metric), "title": title, "summary": summary, "what_to_check": rules.hint(metric),
            "minute": minute(row["period"], max(0, row["match_time_s"] - 1)), "period": row["period"],
            "match_time_s": row["match_time_s"], "stretch": stretch, "priority": row["priority"],
            "key": bool(target.get("key")), "target": rules.target_text(target),
            "value": rules.fmt(metric, doc["value"]), "match_value": rules.fmt(metric, doc["match_value"]),
            "team": fixture[fixture["team"]]["code"], "plan_title": doc.get("plan_title"),
            "open": closed is None, "back_on_plan": closed,
            "status": row["status"], "reviewed_by": row["reviewed_by"], "note": row["note"],
            "review_seconds": round(row["reviewed_at"] - row["t"]) if row["reviewed_at"] else None,
            "time": self.iso(row["t"]), "synthetic_data": bool(fixture["synthetic"]),
            "players": [{"number": n, "name": self.player_name(fixture, fixture["team"], n), "drop_pct": d,
                         "m_per_min_first_30": b, "m_per_min_now": a} for n, d, b, a in doc.get("players", [])],
        }

    def deviations(self, site: str, match_id: str, status: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT deviation_id, status FROM deviations WHERE site_id = ? AND match_id = ? "
                               "ORDER BY period, match_time_s, deviation_id", (site, match_id)).fetchall()
        return [self.deviation(site, r["deviation_id"]) for r in rows if not status or r["status"] == status]

    def season_fixtures(self, site: str, lo: float, hi: float) -> list[tuple[sqlite3.Row, dict]]:
        """Our matches that kicked off in a period: the club's own (not a scouting match, nor a dataset's)."""
        out = []
        for r in self.db.execute("SELECT * FROM fixtures WHERE site_id = ? AND kickoff >= ? AND kickoff < ? ORDER BY kickoff",
                                 (site, lo, hi)).fetchall():
            fixture = json.loads(r["doc"])
            if fixture.get("scouting") or (self.club and fixture[fixture["team"]]["code"] != self.club):
                continue
            out.append((r, fixture))
        return out

    # ── running ───────────────────────────────────────────────────────────────

    def physical(self, site: str, match_id: str) -> dict:
        """
        Every player's running in a match, per side: minutes, distance, high-speed running, sprints, top speed,
        ground covered per minute in each window, and (ours) the drop over the last 15' against his first 30'.
        """
        fixture = self._fixture(site, match_id)
        windows = self.windows(site, match_id)
        ours = fixture["team"]
        labels = [f"{minute(w['period'], w['start_s'])}'" for w in windows]
        drops = {n: (d, b, a) for n, d, b, a in rules.load_drops(windows[-ROLLING:], ours, windows)} if windows else {}
        teams = {}
        for side in ("home", "away"):
            players: dict[int, dict] = {}
            keepers: set[int] = set()
            for i, w in enumerate(windows):
                t = w["teams"][side]
                keepers |= set(t.get("keepers") or ())
                for number, seconds, distance, hsr, sprints, top in t.get("physical") or []:
                    p = players.setdefault(number, {"number": number, "seconds": 0, "distance_m": 0, "hsr_m": 0,
                                                    "sprints": 0, "top_speed_ms": 0.0, "per_window": [None] * len(windows),
                                                    "milestones": []})
                    stretch = f"{minute(w['period'], w['start_s'])}'–{minute(w['period'], max(0, w['end_s'] - 1))}'"
                    if top * 3.6 >= SPEED_TAG_KMH and not any(m["kind"] == "top_speed" for m in p["milestones"]):
                        p["milestones"].append({"kind": "top_speed", "kmh": round(top * 3.6, 1), "stretch": stretch,
                                                "text": f"{top * 3.6:.1f} km/h, {stretch}"})
                    for km in DISTANCE_TAGS_KM:
                        if p["distance_m"] < km * 1000 <= p["distance_m"] + distance:
                            p["milestones"].append({"kind": "distance", "km": km, "stretch": stretch,
                                                    "text": f"{km} km covered, {stretch}"})
                    p["seconds"] += seconds
                    p["distance_m"] += distance
                    p["hsr_m"] += hsr
                    p["sprints"] += sprints
                    p["top_speed_ms"] = max(p["top_speed_ms"], top)
                    p["per_window"][i] = round(60 * distance / seconds) if seconds >= 60 else None
            rows = []
            for p in sorted(players.values(), key=lambda p: (p["number"] not in keepers, -p["seconds"], p["number"])):
                minutes = p["seconds"] / 60
                row = {**p, "name": self.player_name(fixture, side, p["number"]), "minutes": round(minutes),
                       "keeper": p["number"] in keepers,
                       "distance_km": round(p["distance_m"] / 1000, 2),
                       "m_per_min": round(p["distance_m"] / minutes) if minutes else None,
                       "hsr_p90": round(p["hsr_m"] * 90 / minutes) if minutes >= 10 else None,
                       "top_speed_kmh": round(p["top_speed_ms"] * 3.6, 1)}
                if side == ours and p["number"] in drops:
                    d, before, after = drops[p["number"]]
                    row["drop_last_15"] = {"pct": d, "m_per_min_first_30": before, "m_per_min_now": after}
                rows.append(row)
            teams[side] = {
                "code": fixture[side]["code"], "ours": side == ours, "players": rows,
                "distance_km": round(sum(p["distance_m"] for p in rows) / 1000, 1),
                "hsr_m": sum(p["hsr_m"] for p in rows), "sprints": sum(p["sprints"] for p in rows),
            }
        return {"match_id": match_id, "windows": labels, "teams": teams, "synthetic_data": bool(fixture["synthetic"])}

    # ── set pieces ────────────────────────────────────────────────────────────

    @staticmethod
    def _attacking_right(doc: dict) -> bool:
        """Whether a play's team attacks towards x = 105 at its moment (the snapshot says where home attacks then)."""
        home_right = (doc.get("snapshot") or {}).get("home_attacks", "right") == "right"
        return home_right if doc["team"] == "home" else not home_right

    def _set_piece(self, row: sqlite3.Row, fixture: dict) -> dict:
        """One set piece, with its delivery's end in its team's frame (attacking right) for the maps."""
        doc = json.loads(row["doc"])
        d = doc.get("details") or {}
        end = None
        if d.get("end_x_m") is not None:
            x, y = d["end_x_m"], d["end_y_m"]
            end = [round(x, 1), round(y, 1)] if self._attacking_right(doc) else [round(105 - x, 1), round(68 - y, 1)]
        return {**self._play(row, fixture), "kind": d.get("kind"), "routine": d.get("routine"), "swing": d.get("swing"),
                "delivery": d.get("delivery"), "side_of_pitch": d.get("side"), "first_contact": d.get("first_contact"),
                "xg": d.get("xg") or 0.0, "outcome": d.get("outcome"), "end": end, "match_id": row["match_id"]}

    @staticmethod
    def _sum_set_pieces(rows: list[dict]) -> dict:
        n = len(rows)
        judged = [r for r in rows if r["first_contact"] in ("attack", "defence", "none")]
        won = sum(1 for r in judged if r["first_contact"] == "attack")
        return {"deliveries": n, "first_contacts": won,
                "first_contact_pct": round(100 * won / len(judged)) if judged else None,
                "shots": sum(1 for r in rows if r["outcome"] in ("shot", "goal")),
                "goals": sum(1 for r in rows if r["outcome"] == "goal"), "xg": round(sum(r["xg"] for r in rows), 2)}

    def _by(self, rows: list[dict], key: str) -> list[dict]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups[r.get(key) or "other"].append(r)
        return sorted(({key: k, **self._sum_set_pieces(g)} for k, g in groups.items()), key=lambda g: -g["deliveries"])

    def set_pieces(self, site: str, match_id: str) -> dict:
        """A match's set pieces, both sides: each delivery, and per side the totals, by routine and by kind."""
        fixture = self._fixture(site, match_id)
        rows = [self._set_piece(r, fixture) for r in self.db.execute(
            "SELECT * FROM plays WHERE site_id = ? AND match_id = ? AND type = 'set_piece' ORDER BY period, match_time_s",
            (site, match_id)).fetchall()]
        sides = {}
        for side in ("home", "away"):
            mine = [r for r in rows if r["side"] == side]
            sides[side] = {"code": fixture[side]["code"], "ours": side == fixture["team"], **self._sum_set_pieces(mine),
                           "by_routine": self._by(mine, "routine"), "by_kind": self._by(mine, "kind")}
        return {"match_id": match_id, "set_pieces": rows, "teams": sides, "synthetic_data": bool(fixture["synthetic"])}

    def set_piece_season(self, site: str, lo: float, hi: float, opponent: str | None = None) -> dict:
        """
        Set pieces over our matches in a period (or those against one opponent): ours and theirs, by routine,
        kind and delivery, each match's, and every delivery for the maps.
        """
        ours, theirs, matches = [], [], []
        for r, fixture in self.season_fixtures(site, lo, hi):
            them = "away" if fixture["team"] == "home" else "home"
            if opponent and fixture[them]["code"] != opponent.upper():
                continue
            rows = [self._set_piece(p, fixture) for p in self.db.execute(
                "SELECT * FROM plays WHERE site_id = ? AND match_id = ? AND type = 'set_piece' ORDER BY period, match_time_s",
                (site, r["match_id"])).fetchall()]
            for p in rows:
                p["opponent"] = fixture[them]["code"]
            mine, against = [p for p in rows if p["ours"]], [p for p in rows if not p["ours"]]
            ours += mine
            theirs += against
            matches.append({"match_id": r["match_id"], "kickoff": self.iso(r["kickoff"]), "opponent": fixture[them]["code"],
                            "for": self._sum_set_pieces(mine), "against": self._sum_set_pieces(against)})
        return {
            "matches": matches, "for": {**self._sum_set_pieces(ours), "by_routine": self._by(ours, "routine"),
                                        "by_kind": self._by(ours, "kind"), "by_delivery": self._by(ours, "delivery")},
            "against": {**self._sum_set_pieces(theirs), "by_kind": self._by(theirs, "kind"),
                        "by_delivery": self._by(theirs, "delivery"), "by_opponent": self._by(theirs, "opponent")},
            "deliveries": {"for": [{k: p[k] for k in ("end", "outcome", "first_contact", "kind", "routine", "match_id", "minute")}
                                   for p in ours if p["end"]],
                           "against": [{k: p[k] for k in ("end", "outcome", "first_contact", "kind", "opponent", "match_id", "minute")}
                                       for p in theirs if p["end"]]},
        }

    def physical_season(self, site: str, lo: float, hi: float) -> dict:
        """Our players' running over the matches in a period: per 90, and each match's team totals."""
        players: dict[int, dict] = {}
        matches = []
        for r, fixture in self.season_fixtures(site, lo, hi):
            ours = fixture["team"]
            them = "away" if ours == "home" else "home"
            run = self.physical(site, r["match_id"])
            team = run["teams"][ours]
            if not team["players"]:
                continue
            matches.append({"match_id": r["match_id"], "opponent": fixture[them]["code"], "kickoff": self.iso(r["kickoff"]),
                            "distance_km": team["distance_km"], "hsr_m": team["hsr_m"], "sprints": team["sprints"],
                            "opponent_hsr_m": run["teams"][them]["hsr_m"]})
            for p in team["players"]:
                a = players.setdefault(p["number"], {"number": p["number"], "keeper": p["keeper"], "matches": 0,
                                                     "seconds": 0, "distance_m": 0, "hsr_m": 0, "sprints": 0, "top_speed_ms": 0.0})
                a["matches"] += 1
                for k in ("seconds", "distance_m", "hsr_m", "sprints"):
                    a[k] += p[k]
                a["top_speed_ms"] = max(a["top_speed_ms"], p["top_speed_ms"])
        rows = []
        for a in sorted(players.values(), key=lambda a: (a["keeper"], -a["seconds"])):
            per90 = 90 * 60 / a["seconds"] if a["seconds"] >= 600 else None
            rows.append({"number": a["number"], "name": self.squads.get(self.club or "", {}).get(str(a["number"])),
                         "keeper": a["keeper"], "matches": a["matches"], "minutes": round(a["seconds"] / 60),
                         "distance_km_p90": round(a["distance_m"] * per90 / 1000, 2) if per90 else None,
                         "hsr_p90": round(a["hsr_m"] * per90) if per90 else None,
                         "sprints_p90": round(a["sprints"] * per90, 1) if per90 else None,
                         "top_speed_kmh": round(a["top_speed_ms"] * 3.6, 1)})
        return {"players": rows, "matches": matches}

    # ── the analysis page ─────────────────────────────────────────────────────

    def fixtures(self, site: str | None, limit: int = 40) -> list[dict]:
        query, args = "SELECT * FROM fixtures", ()
        if site:
            query, args = query + " WHERE site_id = ?", (site,)
        out = []
        for r in self.db.execute(query + " ORDER BY kickoff DESC LIMIT ?", (*args, limit)).fetchall():
            fixture = json.loads(r["doc"])
            pending = self.db.execute("SELECT COUNT(*) FROM deviations WHERE site_id = ? AND match_id = ? AND status = 'pending_review'",
                                      (r["site_id"], r["match_id"])).fetchone()[0]
            has_plan = self.plan(r["site_id"], r["match_id"])
            out.append({**self.match_info(r["site_id"], fixture, []), "pending": pending,
                        "plan": None if not has_plan else ("default" if has_plan["inherited"] else "own")})
        return out

    def analysis(self, site: str, match_id: str) -> dict:
        """Everything the analysis page shows for one match."""
        fixture = self._fixture(site, match_id)
        windows = self.windows(site, match_id)
        ours = fixture["team"]
        stats = {scope: {side: self.team_stats(ws, side) for side in ("home", "away")} if ws else None
                 for scope, ws in self.scopes(windows).items()}
        timeline = []
        for w in windows:
            row = {"period": w["period"], "start_s": w["start_s"], "end_s": w["end_s"], "final": w["final"],
                   "label": f"{minute(w['period'], w['start_s'])}'"}
            for side in ("home", "away"):
                t = w["teams"][side]
                row[side] = {"possession_s": t["possession_s"], "xg": t["xg"], "shots": t["shots"], "goals": t["goals"],
                             "line_height_m": t["out_of_possession"]["line_height_m"],
                             "width_m": t["in_possession"]["width_m"],
                             "formation_in": t["in_possession"]["formation"], "formation_out": t["out_of_possession"]["formation"]}
            timeline.append(row)
        return {
            "match": self.match_info(site, fixture, windows),
            "stats": stats, "timeline": timeline, "adherence": self.adherence(site, match_id),
            "deviations": self.deviations(site, match_id),
            "plays": self.plays(site, match_id)["plays"],
            "shapes": self.shapes(windows, fixture), "ours": ours,
            "physical": self.physical(site, match_id), "set_pieces": self.set_pieces(site, match_id),
        }

    # ── the agent API ─────────────────────────────────────────────────────────

    def match_summary(self, site: str, match_id: str) -> dict:
        """
        One match: who, the score, both teams' numbers, the plan's targets
        with their status (match so far and last 15'), the formations read,
        the deviations, and the key plays.
        """
        fixture = self._fixture(site, match_id)
        windows = self.windows(site, match_id)
        info = self.match_info(site, fixture, windows)
        ours, them = fixture["team"], "away" if fixture["team"] == "home" else "home"
        adherence = self.adherence(site, match_id)
        key = [p for p in self.plays(site, match_id, key=True)["plays"]]
        deviations = self.deviations(site, match_id)
        return {
            "match": info,
            "numbers": {"us": self.team_stats(windows, ours), "opponent": self.team_stats(windows, them)} if windows else None,
            "game_plan": None if not adherence["plan"] else {
                "title": adherence["plan"].get("title"), "inherited_default": adherence["plan"]["inherited"],
                "on_plan_share": adherence["on_plan_share"],
                "targets": [{"name": t["name"], "target": t["target"], "key": t["key"], "match": t["match"]["text"],
                             "match_status": STATUS_NAMES[t["match"]["status"]], "last_15": t["last_15"]["text"],
                             "last_15_status": STATUS_NAMES[t["last_15"]["status"]]} for t in adherence["targets"]],
            },
            "deviations": [{k: d[k] for k in ("deviation_id", "period", "minute", "title", "summary", "status", "reviewed_by", "note", "open")}
                           for d in deviations],
            "key_plays": [{k: p[k] for k in ("play_id", "period", "minute", "type", "team", "summary")} for p in key],
            "set_pieces": {fixture[side]["code"]: {k: v for k, v in t.items() if k in ("deliveries", "first_contact_pct", "shots", "goals", "xg")}
                           for side, t in self.set_pieces(site, match_id)["teams"].items()},
            "running_drops_last_15": [{"number": n, "name": self.player_name(fixture, ours, n), "drop_pct": d, "m_per_min_first_30": b, "m_per_min_now": a}
                                      for n, d, b, a in rules.load_drops(windows[-ROLLING:], ours, windows)[:3] if d > 0] if windows else [],
            "synthetic_data": bool(fixture["synthetic"]),
        }

    def formation(self, site: str, match_id: str) -> dict:
        fixture = self._fixture(site, match_id)
        windows = self.windows(site, match_id)
        out = {"match": self.match_info(site, fixture, windows), "teams": {}}
        changes = [p for p in self.plays(site, match_id, kind="formation_change")["plays"]]
        for side in ("home", "away"):
            code = fixture[side]["code"]
            team = {"ours": side == fixture["team"]}
            for scope, ws in self.scopes(windows).items():
                team[scope] = {phase: {"formation": rules.shape(ws, side, phase),
                                       "positions": [{"number": n, "depth_m": x, "across_m": y}
                                                     for n, x, y in self.positions(ws, side, phase)]}
                               for phase in ("in_possession", "out_of_possession")}
            team["changes"] = [{"minute": c["minute"], "summary": c["summary"]} for c in changes if c["team"] == code]
            out["teams"][code] = team
        out["synthetic_data"] = bool(fixture["synthetic"])
        return out

    def season(self, site: str, lo: float, hi: float) -> dict:
        """
        Our matches that kicked off in a period: each one's result, xG,
        possession, PPDA and how much of its plan it met; the record; and per
        plan metric, in how many matches it was met.
        """
        rows = [r for r, _ in self.season_fixtures(site, lo, hi)]
        matches, record, by_metric = [], Counter(), defaultdict(Counter)
        goals_for = goals_against = 0
        xg_for = xg_against = 0.0
        for r in rows:
            fixture = json.loads(r["doc"])
            windows = self.windows(site, r["match_id"])
            ours = fixture["team"]
            them = "away" if ours == "home" else "home"
            us, opp = (self.team_stats(windows, ours), self.team_stats(windows, them)) if windows else ({}, {})
            adherence = self.adherence(site, r["match_id"])
            for t in adherence["targets"]:
                by_metric[t["name"]][t["match"]["status"]] += 1
            result = self._result(fixture)
            if result:
                record[result] += 1
                goals_for += fixture["score"][ours]
                goals_against += fixture["score"][them]
                xg_for += us.get("xg") or 0
                xg_against += opp.get("xg") or 0
            matches.append({
                "match_id": r["match_id"], "kickoff": self.iso(r["kickoff"]), "opponent": fixture[them]["code"],
                "opponent_name": fixture[them]["name"], "venue": "home" if ours == "home" else "away",
                "score": f'{fixture["score"][ours]}-{fixture["score"][them]}', "result": result, "status": fixture["status"],
                "xg": us.get("xg"), "xg_against": opp.get("xg"), "possession_pct": us.get("possession_pct"),
                "ppda": us.get("ppda"), "line_height_m": us.get("line_height_m"),
                "set_piece_xg": us.get("set_piece_xg"), "set_piece_xg_against": opp.get("set_piece_xg"),
                "hsr_m": us.get("hsr_m"), "distance_km": us.get("distance_km"),
                "plan_on_share": adherence["on_plan_share"],
                "deviations": self.db.execute("SELECT COUNT(*) FROM deviations WHERE site_id = ? AND match_id = ?",
                                              (site, r["match_id"])).fetchone()[0],
            })
        played = sum(record.values())
        return {
            "matches": matches,
            "record": {"played": played, "won": record["W"], "drawn": record["D"], "lost": record["L"],
                       "points": 3 * record["W"] + record["D"], "goals_for": goals_for, "goals_against": goals_against,
                       "xg_for": round(xg_for, 2), "xg_against": round(xg_against, 2)},
            "plan_by_metric": {name: {"on_plan": c["on_plan"], "drifting": c["drifting"], "off_plan": c["off_plan"]}
                               for name, c in by_metric.items()},
            "synthetic_data": any(r["synthetic"] for r in rows),
        }

    def nodes(self, site: str | None, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        query, args = "SELECT * FROM nodes", ()
        if site:
            query, args = query + " WHERE site_id = ?", (site,)
        rows = []
        for r in self.db.execute(query + " ORDER BY site_id, hostname", args):
            doc = json.loads(r["doc"])
            age = now - r["received"]
            rows.append({"site_id": r["site_id"], "hostname": r["hostname"], "online": age <= NODE_TIMEOUT,
                         "status": r["status"] if age <= NODE_TIMEOUT else "offline", "last_heartbeat": self.iso(r["t"]),
                         "seconds_since": round(age), "device": doc.get("device"), "metrics": doc.get("metrics"),
                         "pipeline": doc.get("pipeline")})
        return {"nodes": rows}

    # ── the feed (p4n4-dashboard's Feed tab, through p4n4-api) ────────────────

    def feed_deviation(self, d: dict, fixture: dict) -> dict:
        actions = ("confirm", "dismiss") if d["status"] == "pending_review" else ("reopen",)
        facts = []
        if d["back_on_plan"]:
            facts.append(f"Back on plan at {d['back_on_plan']['minute']}' ({d['back_on_plan']['value']})")
        if d["reviewed_by"]:
            facts.append(f"{'Flagged for the bench' if d['status'] == 'confirmed' else 'Set aside as expected'} by {d['reviewed_by']}")
        if d["note"]:
            facts.append(f"“{d['note']}”")
        return {
            "id": f"{d['site_id']}:{d['deviation_id']}", "time": d["time"], "kind": "alert",
            "title": f"{d['title']} · {d['minute']}'",
            "subtitle": f"{self.score_text(fixture)} · last 15': {d['value']} · plan {d['target']}",
            "detail": f"{d['summary']} {d['what_to_check']}",
            "badge": f"{d['minute']}'", "tone": "review" if d["priority"] == "high" else "info", "status": d["status"],
            "actions": [{"id": k, "label": ACTION_LABELS[k]} for k in actions],
            "meta": {"camera_id": "tactical", "match_id": d["match_id"], "deviation_id": d["deviation_id"],
                     "metric": d["metric"], "priority": d["priority"], "team": d["team"]},
            "facts": facts, "synthetic": d["synthetic_data"],
        }

    def feed_play(self, row: sqlite3.Row, fixture: dict) -> dict:
        p = self._play(row, fixture)
        return {
            "id": f"{row['site_id']}:{row['play_id']}", "time": p["time"], "kind": "event",
            "title": f"{p['type_name']} · {p['minute']}' · {p['team']}",
            "subtitle": f"{self.score_text(fixture)} · {CAMERAS.get(row['camera_id'], row['camera_id'])}",
            "detail": p["summary"], "badge": f"{p['minute']}'",
            "tone": "info" if p["ours"] else "neutral", "status": None, "actions": [],
            "meta": {"camera_id": row["camera_id"], "match_id": row["match_id"], "play_id": row["play_id"],
                     "type": row["type"], "team": p["team"]},
            "facts": [], "synthetic": bool(row["synthetic"]),
        }

    def feed(self, limit: int = 50, before: float | None = None, site: str | None = None) -> dict:
        """Deviations and key plays, newest first; `before` (epoch seconds) is the previous page's next_before."""
        where, args = [], []
        if site:
            where.append("site_id = ?")
            args.append(site)
        if before is not None:
            where.append("t < ?")
            args.append(before)
        cond = " AND ".join(where)
        devs = self.db.execute("SELECT site_id, deviation_id, match_id, t FROM deviations" + (f" WHERE {cond}" if cond else "")
                               + " ORDER BY t DESC LIMIT ?", (*args, limit + 1)).fetchall()
        plays = self.db.execute("SELECT * FROM plays WHERE key = 1" + (f" AND {cond}" if cond else "")
                                + " ORDER BY t DESC LIMIT ?", (*args, limit + 1)).fetchall()
        merged = sorted([("d", r) for r in devs] + [("p", r) for r in plays], key=lambda x: -x[1]["t"])
        page = merged[:limit]
        fixtures = {}
        items = []
        for kind, r in page:
            key = (r["site_id"], r["match_id"])
            if key not in fixtures:
                fixtures[key] = self._fixture(*key)
            if kind == "d":
                items.append(self.feed_deviation(self.deviation(r["site_id"], r["deviation_id"]), fixtures[key]))
            else:
                items.append(self.feed_play(r, fixtures[key]))
        pending = self.db.execute("SELECT COUNT(*) FROM deviations WHERE status = 'pending_review'" + (" AND site_id = ?" if site else ""),
                                  (site,) if site else ()).fetchone()[0]
        return {"title": "Game plan and key plays", "items": items, "counts": {"pending": pending},
                "next_before": page[-1][1]["t"] if len(merged) > limit else None}

    def feed_action(self, item_id: str, action: str, by: str, note: str | None = None,
                    at: float | None = None) -> tuple[dict, dict | None]:
        site, _, deviation_id = item_id.partition(":")
        d, change = self.act(site, deviation_id, action, by, note, at)
        return self.feed_deviation(d, self._fixture(site, d["match_id"])), change
