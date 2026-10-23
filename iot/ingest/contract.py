"""
The club device ↔ web contract, version 0.1, as checks on the JSON the
ingest service accepts.

The club's match device (a tactical camera with its tracker, or a tracking
provider's feed through it) posts the match it is measuring (a fixture,
with the live state the analyst keys in), what it measured every 5 minutes
(a window: both teams' data and shape), the technical plays it found (a
play: a goal, a set piece, a high regain …, with its key moment and the
seconds around it), and its own health (a heartbeat). The staff write the
game plan (a plan), from the plan editor. Standard library only: each check
returns the reason a document is invalid, or None.

Pitch coordinates are metres on a 105 × 68 m pitch: x along the length from
the left goal line (as the main camera sees it), y across from the far
touchline. A window's average positions are in the team's own frame
instead (attacking right: x from its own goal line, a small y its left).
Players are identified by team (home, away) and shirt number, never by name.

A window also carries each player's running (distance, high-speed running
over 5.5 m/s, sprints over 7 m/s, top speed) and the team's set pieces
(deliveries into the box, the shots and xG after them, first contacts won).
A play says where it was found ("source"): by the tracking's analytics, by
the video models, or labelled by a person.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

FIXTURE = "p4n4.team.fixture/0.1"
WINDOW = "p4n4.team.window/0.1"
PLAY = "p4n4.team.play/0.1"
HEARTBEAT = "p4n4.team.heartbeat/0.1"
PLAN = "p4n4.team.plan/0.1"

# What the device finds. The order is the display order everywhere (and the
# order of the categorical colours, see scripts/build_dashboard.py)
TYPES = ("goal", "shot", "save", "set_piece", "high_regain", "counter_attack", "build_up", "press_broken", "foul",
         "formation_change")
TEAMS = ("home", "away")
ROLES = ("shooter", "assist", "taker", "goalkeeper", "regain", "line_breaker", "target", "first_contact", "fouler", "fouled")
STATUSES = ("scheduled", "live", "half_time", "full_time")
HEALTH = ("healthy", "degraded", "error")
# How a play was found: the tracking's analytics, the video models, or a person's label
SOURCES = ("tracking", "video", "label")
PHASES = ("in_possession", "out_of_possession")
# Per type: the details a device may send, and what each may be
DETAILS = {
    "goal": {"xg": "number", "pattern": ("open_play", "high_press", "counter", "set_piece", "penalty"), "body": ("foot", "head"),
             "speed_kmh": "number"},
    "shot": {"xg": "number", "outcome": ("saved", "off_target", "blocked", "goal"), "body": ("foot", "head"),
             "speed_kmh": "number"},
    "set_piece": {"kind": ("corner", "free_kick", "throw_in"), "outcome": ("goal", "shot", "cleared", "retained"),
                  "delivery": ("near_post", "far_post", "six_yard", "penalty_spot", "edge", "short"),
                  "side": ("left", "right", "centre"), "swing": ("inswing", "outswing", "driven", "long_throw", "short", "direct"),
                  "routine": "label", "first_contact": ("attack", "defence", "none"), "xg": "number",
                  "end_x_m": "number", "end_y_m": "number"},
    "save": {"xg": "number", "kind": ("catch", "parry", "punch"), "body": ("hands", "feet")},
    "foul": {"area": ("box", "final_third", "middle_third", "own_third"), "whistled": "bool", "free_kick_x_m": "number"},
    "high_regain": {"x_m": "number", "seconds_after_loss": "number", "counterpress": "bool", "led_to_shot": "bool"},
    "counter_attack": {"duration_s": "number", "outcome": ("goal", "shot", "none"), "from_x_m": "number"},
    "build_up": {"short": "bool", "progressed": "bool", "passes": "number"},
    "press_broken": {"seconds": "number", "from_x_m": "number", "led_to_shot": "bool"},
    "formation_change": {"phase": PHASES, "from": "text", "to": "text"},
}
# A window's numbers per team; final_third_entries is {left, centre, right}
COUNTS = ("possession_s", "passes", "passes_completed", "passes_own60", "passes_final_third", "def_actions",
          "def_actions_high", "opp_passes_own60", "box_entries", "shots", "shots_on_target", "xg", "goals",
          "turnovers", "regains", "regains_high", "counterpress_regains", "corners", "free_kicks", "goal_kicks",
          "goal_kicks_short", "distance_m", "hsr_m", "sprints", "set_pieces", "set_piece_shots", "set_piece_xg",
          "set_piece_first_contacts")
# Pass quality and ball speed (optional, a device that measures them sends them all): passes with an end point
# (rated), their length and difficulty (0-1 each) summed, the hard ones (0.6 and over) and how many were completed,
# passes with a ball speed (timed) and the speeds summed, the fastest ball and shot (m/s)
PASS_QUALITY = ("passes_rated", "pass_distance_m", "pass_difficulty", "passes_hard", "passes_hard_completed",
                "passes_timed", "pass_speed_ms", "ball_speed_max_ms", "shot_speed_max_ms")
# A player's running in a window: [number, seconds on the pitch, distance m, high-speed running m (over 5.5 m/s),
# sprints (efforts over 7 m/s), top speed m/s]
PHYSICAL = 6
HSR_MS, SPRINT_MS = 5.5, 7.0
SHAPE = ("line_height_m", "width_m", "length_m", "formation_fit")
# Periods: 1 and 2 (45 min), 3 and 4 (extra time, 15 min)
PERIOD_START = {1: 0, 2: 45, 3: 90, 4: 105}
PERIOD_LENGTH = {1: 45, 2: 45, 3: 15, 4: 15}
PITCH = (105.0, 68.0)
MARGIN = 6.0
MAX_TRACK_FRAMES = 300

PLAY_ID = re.compile(r"^PL-[0-9]{8}-[0-9]{6}$")
# Site, camera, match and host ids end up in MQTT topics and InfluxDB tags
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
TEAM_CODE = re.compile(r"^[A-Z0-9]{2,5}$")
SHAPE_NAME = re.compile(r"^[1-6](-[1-6]){1,4}$")
EVIDENCE = ("image_uri", "clip_uri")


def parse_time(value: object, tz: tzinfo = timezone.utc) -> datetime:
    """An ISO 8601 date-time; one without an offset is in the site's time zone."""
    if not isinstance(value, str) or not value:
        raise ValueError("expected an ISO 8601 date-time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def minute(period: int, match_time_s: float) -> str:
    """The match minute as it's written: 1' is the first minute, 45+2 the second of stoppage time."""
    played = int(match_time_s // 60) + 1
    base, length = PERIOD_START[period], PERIOD_LENGTH[period]
    return f"{base + length}+{played - length}" if played > length else str(base + played)


def _id(doc: dict, key: str, required: bool = True) -> str | None:
    value = doc.get(key)
    if value is None:
        return f"missing {key}" if required else None
    if not isinstance(value, str) or not ID.match(value):
        return f"{key} must be 1-64 letters, digits, '.', '_', ':' or '-'"
    return None


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _whole(value: object, low: int, high: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _time(doc: dict, key: str, tz: tzinfo) -> str | None:
    if key not in doc:
        return f"missing {key}"
    try:
        parse_time(doc[key], tz)
    except (TypeError, ValueError):
        return f"{key} must be an ISO 8601 date-time"
    return None


def _text(doc: dict, key: str, limit: int, required: bool = False) -> str | None:
    value = doc.get(key)
    if value is None:
        return f"missing {key}" if required else None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return f"{key} must be a text of 1-{limit} characters"
    return None


def _first(*errors: str | None) -> str | None:
    return next((e for e in errors if e), None)


def _point(value: object) -> bool:
    """[x, y] in metres, on the pitch or just off it."""
    return (isinstance(value, list) and len(value) == 2 and all(_is_number(v) for v in value)
            and -MARGIN <= value[0] <= PITCH[0] + MARGIN and -MARGIN <= value[1] <= PITCH[1] + MARGIN)


def _shirt(value: object) -> bool:
    """A shirt number (some datasets number past 99), or null when it wasn't read."""
    return value is None or _whole(value, 0, 999)


def _positions(value: object) -> str | None:
    """[[team, number, x, y], ...]: where each player is. team is home, away or ref."""
    if not isinstance(value, list) or len(value) > 40:
        return "must be a list of at most 40 [team, number, x, y]"
    for p in value:
        if not (isinstance(p, list) and len(p) in (4, 5) and p[0] in (*TEAMS, "ref") and _shirt(p[1])
                and _point(p[2:4]) and (len(p) == 4 or _whole(p[4], 0, 10**9))):
            return "each player must be [home|away|ref, number 0-999 or null, x, y] in metres on the pitch"
    return None


def _snapshot(value: object) -> str | None:
    if not isinstance(value, dict):
        return "snapshot must be an object"
    if value.get("home_attacks") not in ("left", "right"):
        return "snapshot.home_attacks must be left or right"
    if value.get("ball") is not None and not _point(value["ball"]):
        return "snapshot.ball must be [x, y] in metres on the pitch"
    error = _positions(value.get("players"))
    return f"snapshot.players {error}" if error else None


def _track(value: object) -> str | None:
    """
    The seconds around the key moment, for the replay:
    {"hz": 5, "t0": -6.0, "frames": [{"ball": [x, y] | null, "players": [...]}, ...]}.
    Frame i is at t0 + i / hz seconds from the key moment.
    """
    if not isinstance(value, dict):
        return "track must be an object"
    if not _is_number(value.get("hz")) or not 1 <= value["hz"] <= 30:
        return "track.hz must be a number from 1 to 30"
    if not _is_number(value.get("t0")) or not -30 <= value["t0"] <= 0:
        return "track.t0 must be a number of seconds from -30 to 0"
    frames = value.get("frames")
    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_TRACK_FRAMES:
        return f"track.frames must be a list of 1-{MAX_TRACK_FRAMES} frames"
    for frame in frames:
        if not isinstance(frame, dict) or (frame.get("ball") is not None and not _point(frame["ball"])):
            return "each track frame must be {ball: [x, y] or null, players: [...]}"
        error = _positions(frame.get("players"))
        if error:
            return f"track frame players {error}"
    return None


def _details(kind: str, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return "details must be an object"
    allowed = DETAILS[kind]
    for key, v in value.items():
        rule = allowed.get(key)
        if rule is None:
            return f"details.{key} is not a {kind} detail (expected {', '.join(allowed) or 'none'})"
        if v is None:
            continue
        if rule == "number" and not _is_number(v):
            return f"details.{key} must be a number"
        if rule == "bool" and not isinstance(v, bool):
            return f"details.{key} must be true or false"
        if rule == "text" and not (isinstance(v, str) and SHAPE_NAME.match(v)):
            return f"details.{key} must be a formation such as 4-4-2"
        if rule == "label" and not (isinstance(v, str) and 0 < len(v.strip()) <= 40):
            return f"details.{key} must be a text of 1-40 characters"
        if isinstance(rule, tuple) and v not in rule:
            return f"details.{key} must be one of {', '.join(rule)}"
    return None


def _team(value: object) -> str | None:
    if not isinstance(value, dict) or not TEAM_CODE.match(str(value.get("code", ""))):
        return "must be {code: 2-5 capitals or digits, name}"
    if not isinstance(value.get("name"), str) or not 0 < len(value["name"]) <= 64:
        return "name must be a text of 1-64 characters"
    return None


def check_fixture(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    """
    The match the club's device is measuring, and its live state as the
    analyst keys it in: status, period and score (the referee's, never a
    model's). team says which side is ours, the club the analysis is for.
    """
    if not isinstance(doc, dict):
        return "a fixture must be a JSON object"
    if doc.get("schema") != FIXTURE:
        return f"schema must be {FIXTURE}"
    error = _first(_id(doc, "site_id"), _id(doc, "match_id"), _time(doc, "kickoff", tz),
                   _text(doc, "competition", 80, True), _text(doc, "venue", 80))
    if error:
        return error
    for side in TEAMS:
        error = _team(doc.get(side))
        if error:
            return f"{side} {error}"
    if doc["home"]["code"] == doc["away"]["code"]:
        return "home and away must be different teams"
    if doc.get("team") not in TEAMS:
        return "team must be home or away: the side the analysis is for"
    if doc.get("matchday") is not None and not _whole(doc["matchday"], 1, 99):
        return "matchday must be a whole number from 1 to 99"
    if doc.get("status") not in STATUSES:
        return f"status must be one of {', '.join(STATUSES)}"
    if not _whole(doc.get("period"), 0, 4):
        return "period must be 0 (before kick-off) to 4"
    score = doc.get("score")
    if not isinstance(score, dict) or not all(_whole(score.get(side), 0, 99) for side in TEAMS):
        return "score must be {home: goals, away: goals}"
    if not isinstance(doc.get("synthetic"), bool):
        return "synthetic must be true or false"
    if doc.get("scouting") is not None and not isinstance(doc["scouting"], bool):
        return "scouting must be true or false: a match filmed for scouting, neither side ours"
    return None


def _physical(value: object, where: str) -> str | None:
    """[[number, seconds, distance_m, hsr_m, sprints, top_speed_ms], ...]: each player's running in the window."""
    if not isinstance(value, list) or len(value) > 18:
        return f"{where} must be a list of at most 18 players"
    for p in value:
        if not (isinstance(p, list) and len(p) == PHYSICAL and _shirt(p[0]) and p[0] is not None
                and all(_is_number(v) and v >= 0 for v in p[1:]) and p[1] <= 3600 and p[5] <= 13):
            return (f"each of {where} must be [number, seconds 0-3600, distance m, high-speed m, sprints, "
                    "top speed m/s 0-13]")
    return None


def _shape(value: object, where: str) -> str | None:
    if not isinstance(value, dict):
        return f"{where} must be an object"
    if not _whole(value.get("frames"), 0, 10**6):
        return f"{where}.frames must be a whole number"
    for key in SHAPE:
        if value.get(key) is not None and not _is_number(value[key]):
            return f"{where}.{key} must be a number or null"
    if value.get("formation") is not None and not (isinstance(value["formation"], str) and SHAPE_NAME.match(value["formation"])):
        return f"{where}.formation must be a formation such as 4-4-2, or null"
    positions = value.get("positions", [])
    if not isinstance(positions, list) or len(positions) > 11 or not all(
            isinstance(p, list) and len(p) == 3 and _shirt(p[0]) and _is_number(p[1]) and _is_number(p[2])
            and -MARGIN <= p[1] <= PITCH[0] + MARGIN and -MARGIN <= p[2] <= PITCH[1] + MARGIN for p in positions):
        return f"{where}.positions must be at most 11 [number, x, y] in the team's frame"
    return None


def check_window(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    """
    Five minutes of one period, measured: per team its counts, its shape in
    and out of possession, and its pass network. A window still being
    played is "final": false, and the device re-sends it as it fills.
    """
    if not isinstance(doc, dict):
        return "a window must be a JSON object"
    if doc.get("schema") != WINDOW:
        return f"schema must be {WINDOW}"
    error = _first(_id(doc, "site_id"), _id(doc, "match_id"))
    if error:
        return error
    if not _whole(doc.get("period"), 1, 4):
        return "period must be 1, 2 (halves) or 3, 4 (extra time)"
    if not (_is_number(doc.get("start_s")) and _is_number(doc.get("end_s")) and 0 <= doc["start_s"] < doc["end_s"] <= 3600):
        return "start_s and end_s must be seconds since the period's kick-off, start before end"
    if not isinstance(doc.get("final"), bool):
        return "final must be true or false"
    if doc.get("home_attacks") not in ("left", "right"):
        return "home_attacks must be left or right (in the first period)"
    teams = doc.get("teams")
    if not isinstance(teams, dict) or set(teams) != set(TEAMS):
        return "teams must be {home: {...}, away: {...}}"
    for side in TEAMS:
        t = teams[side]
        if not isinstance(t, dict):
            return f"teams.{side} must be an object"
        for key in COUNTS:
            if not _is_number(t.get(key)) or t[key] < 0:
                return f"teams.{side}.{key} must be a number, 0 or more"
        for key in PASS_QUALITY:
            if key in t and (not _is_number(t[key]) or t[key] < 0):
                return f"teams.{side}.{key} must be a number, 0 or more"
        entries = t.get("final_third_entries")
        if not isinstance(entries, dict) or set(entries) != {"left", "centre", "right"} or not all(
                _whole(v, 0, 10**4) for v in entries.values()):
            return f"teams.{side}.final_third_entries must be {{left, centre, right}} counts"
        for phase in PHASES:
            error = _shape(t.get(phase), f"teams.{side}.{phase}")
            if error:
                return error
        if t.get("physical") is not None and (error := _physical(t["physical"], f"teams.{side}.physical")):
            return error
        keepers = t.get("keepers", [])
        if not isinstance(keepers, list) or len(keepers) > 3 or not all(_whole(k, 0, 999) for k in keepers):
            return f"teams.{side}.keepers must be the goalkeepers' shirt numbers"
        pairs = t.get("pass_pairs", [])
        if not isinstance(pairs, list) or len(pairs) > 400 or not all(
                isinstance(p, list) and len(p) == 3 and _shirt(p[0]) and _shirt(p[1]) and _whole(p[2], 1, 10**4) for p in pairs):
            return f"teams.{side}.pass_pairs must be [[from, to, passes], ...]"
    if not isinstance(doc.get("synthetic"), bool):
        return "synthetic must be true or false"
    return None


def check_play(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    """One technical play the device found, with its key moment and the seconds around it."""
    if not isinstance(doc, dict):
        return "a play must be a JSON object"
    if doc.get("schema") != PLAY:
        return f"schema must be {PLAY}"
    error = _first(_id(doc, "site_id"), _id(doc, "match_id"), _id(doc, "camera_id"), _time(doc, "timestamp", tz))
    if error:
        return error
    if not PLAY_ID.match(str(doc.get("play_id", ""))):
        return "play_id must look like PL-20261017-000123"
    if doc.get("type") not in TYPES:
        return f"type must be one of {', '.join(TYPES)}"
    if not _whole(doc.get("period"), 1, 4):
        return "period must be 1, 2 (halves) or 3, 4 (extra time)"
    if not _is_number(doc.get("match_time_s")) or not 0 <= doc["match_time_s"] <= 3600:
        return "match_time_s must be the seconds since the period's kick-off, 0 to 3600"
    if not _is_number(doc.get("confidence")) or not 0 <= doc["confidence"] <= 1:
        return "confidence must be a number from 0 to 1"
    if doc.get("team") not in TEAMS:
        return "team must be home or away"
    players = doc.get("players", [])
    if not isinstance(players, list) or len(players) > 6:
        return "players must be a list of at most 6"
    for p in players:
        if not (isinstance(p, dict) and p.get("team") in TEAMS and _shirt(p.get("number")) and p.get("role") in ROLES):
            return f"each player must be {{team: home|away, number, role: {'|'.join(ROLES)}}}"
    error = _details(doc["type"], doc.get("details"))
    if error:
        return error
    if doc.get("snapshot") is not None and (error := _snapshot(doc["snapshot"])):
        return error
    if doc.get("track") is not None and (error := _track(doc["track"])):
        return error
    if doc.get("source") is not None and doc["source"] not in SOURCES:
        return f"source must be one of {', '.join(SOURCES)}"
    evidence = doc.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) - set(EVIDENCE):
            return f"evidence must be an object with {', '.join(EVIDENCE)}"
        if any(not isinstance(v, str) or len(v) > 512 for v in evidence.values()):
            return "evidence URIs must be texts of at most 512 characters"
    if not isinstance(doc.get("synthetic"), bool):
        return "synthetic must be true or false"
    return None


def check_heartbeat(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    if not isinstance(doc, dict):
        return "a heartbeat must be a JSON object"
    if doc.get("schema") != HEARTBEAT:
        return f"schema must be {HEARTBEAT}"
    error = _first(_id(doc, "site_id"), _time(doc, "timestamp", tz))
    if error:
        return error
    for key in ("device", "metrics", "pipeline"):
        if not isinstance(doc.get(key), dict):
            return f"{key} must be an object"
    if _id(doc["device"], "hostname"):
        return "device.hostname must be 1-64 letters, digits, '.', '_', ':' or '-'"
    for section in ("metrics", "pipeline"):
        for key, value in doc[section].items():
            if not _is_number(value):
                return f"{section}.{key} must be a number"
    if doc.get("status") not in HEALTH:
        return f"status must be one of {', '.join(HEALTH)}"
    return None


def site_tz(value: str) -> tzinfo:
    """SITE_TZ: an IANA name (Europe/London), or a fixed offset (-04:00) for images without time-zone data."""
    value = (value or "").strip()
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", value)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    if value in ("", "UTC", "Z"):
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(value)
