"""
Where the ingest service copies what it stores: InfluxDB, for Grafana and
the agent's telemetry tools, and MQTT, for anything else on p4n4-net that
wants to react (n8n telling the staff, a bench tablet, a scoreboard).

SQLite stays the record. Both copies are best effort: a failed write is
logged and dropped, and never fails the device's request.

InfluxDB (bucket INFLUXDB_BUCKET):
    team_window,site_id,match_id,team=<club code>,side,ours,period
        minute, possession_pct, passes, passes_completed, ppda, xg, shots, goals,
        line_height_m, width_m, length_m, regains_high, counterpress_regains,
        turnovers, distance_m, hsr_m, sprints, set_pieces, set_piece_shots,
        set_piece_xg, set_piece_first_contacts, formation_in="4-3-3", formation_out="4-4-2"
                                                          one point per finished window and side
    player_run,site_id,match_id,team=<club code>,side,ours,number
        minute, seconds, distance_m, hsr_m, sprints, top_speed_ms
                                                          one point per player in a finished window
    plan_check,site_id,match_id,metric,key,status
        value (or shape="4-4-2"), target, minute        one per finished window and target of the plan
    team_play,site_id,match_id,type,team=<club code>,ours
        count=1i, minute, xg, play_id="…"                 at the play's time
    plan_deviation,site_id,match_id,metric,priority,team
        count=1i, minute, deviation_id="…"                when a deviation opens
    deviation_review,site_id,match_id,metric,decision,reviewer
        count=1i (-1i: a reopen, undoing the decision), review_s, deviation_id="…"
    team_fixture,site_id,match_id home="BHA",away="ARS",status,period,score_home,score_away
    sensor_data,device=<hostname>,sensor=<metric> value=<number>
        every number of a heartbeat, in the p4n4 schema, so the agent's
        list_sensors / get_stats / get_history tools see the device's health
    team_node,site_id,device status="healthy",up=1i

Window points are timed at the window's end on the match clock (kick-off,
the halves, 15 minutes of half time), or now if that is later, so a sped-up
demo match never writes into the future.

Notifications (NOTIFY_URL, e.g. n8n's http://n8n:5678/webhook/brighton), JSON
with an X-Notify-Token header (NOTIFY_TOKEN):
    POST <NOTIFY_URL>/deviation    {"event": "deviation", "deviation": {...}, "match": {...}}
                                   a deviation opening while the match is on
    POST <NOTIFY_URL>/half-time    {"event": "half_time", "match": {...}}
    POST <NOTIFY_URL>/full-time    {"event": "full_time", "match": {...}}

MQTT (QoS 0, not retained, JSON), under MQTT_TOPIC_ROOT (default brighton):
    brighton/<site_id>/window          a window, without its positions and pass network (with each player's running)
    brighton/<site_id>/play/<type>     a play, without its track (the replay)
    brighton/<site_id>/deviation       a deviation opening or closing
    brighton/<site_id>/review          an analyst's call on one
    brighton/<site_id>/plan            a game plan saved
    brighton/<site_id>/fixture         the match and its live state
    brighton/<site_id>/heartbeat
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import tzinfo

from contract import PERIOD_START, parse_time

# First level of every MQTT topic (MQTT_TOPIC_ROOT)
TOPIC_ROOT = os.environ.get("MQTT_TOPIC_ROOT", "brighton").strip("/") or "brighton"


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", file=sys.stderr, flush=True)


# ── InfluxDB line protocol ────────────────────────────────────────────────────


def _escape_tag(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _field(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def line(measurement: str, tags: dict, fields: dict, ns: int) -> str:
    tag_text = "".join(f",{k}={_escape_tag(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    field_text = ",".join(f"{k}={_field(v)}" for k, v in fields.items() if v is not None)
    return f"{measurement}{tag_text} {field_text} {ns}"


def _ns(iso: str, tz: tzinfo, unique: str = "") -> int:
    """Nanoseconds of a timestamp, with an id's sequence number below the millisecond so two points never collide."""
    ns = int(parse_time(iso, tz).timestamp() * 1000) * 1_000_000
    digits = unique.rsplit("-", 1)[-1]
    return ns + (int(digits) % 1_000_000 if digits.isdigit() else 0)


def window_lines(doc: dict, fixture: dict, t: float) -> list[str]:
    """A finished window, one point per side, at t (epoch seconds)."""
    lines = []
    total = sum(doc["teams"][s]["possession_s"] for s in ("home", "away")) or 1
    for side in ("home", "away"):
        team = doc["teams"][side]
        other = doc["teams"]["away" if side == "home" else "home"]
        tags = {"site_id": doc["site_id"], "match_id": doc["match_id"], "team": fixture[side]["code"], "side": side,
                "ours": str(side == fixture["team"]).lower(), "period": str(doc["period"])}
        fields = {
            "minute": round(PERIOD_START[doc["period"]] + doc["start_s"] / 60, 1),
            "possession_pct": round(100 * team["possession_s"] / total, 1),
            "passes": int(team["passes"]), "passes_completed": int(team["passes_completed"]),
            "ppda": round(other["opp_passes_own60"] / team["def_actions_high"], 2) if team["def_actions_high"] else None,
            "xg": float(team["xg"]), "shots": int(team["shots"]), "goals": int(team["goals"]),
            "line_height_m": team["out_of_possession"]["line_height_m"], "width_m": team["in_possession"]["width_m"],
            "length_m": team["out_of_possession"]["length_m"], "regains_high": int(team["regains_high"]),
            "counterpress_regains": int(team["counterpress_regains"]), "turnovers": int(team["turnovers"]),
            "distance_m": float(team["distance_m"]), "hsr_m": float(team["hsr_m"]), "sprints": int(team["sprints"]),
            "set_pieces": int(team["set_pieces"]), "set_piece_shots": int(team["set_piece_shots"]),
            "set_piece_xg": float(team["set_piece_xg"]), "set_piece_first_contacts": int(team["set_piece_first_contacts"]),
            "formation_in": team["in_possession"]["formation"], "formation_out": team["out_of_possession"]["formation"],
        }
        fields = {k: (float(v) if isinstance(v, (int, float)) and k in ("line_height_m", "width_m", "length_m") else v)
                  for k, v in fields.items()}
        lines.append(line("team_window", tags, fields, int(t * 1e9) + (1 if side == "away" else 0)))
        for i, (number, seconds, distance, hsr, sprints, top) in enumerate(team.get("physical") or []):
            lines.append(line("player_run", {**{k: tags[k] for k in ("site_id", "match_id", "team", "side", "ours")},
                                             "number": str(number)},
                              {"minute": fields["minute"], "seconds": float(seconds), "distance_m": float(distance),
                               "hsr_m": float(hsr), "sprints": int(sprints), "top_speed_ms": float(top)},
                              int(t * 1e9) + 2 + i + (100 if side == "away" else 0)))
    return lines


def check_lines(site: str, match_id: str, checks: list[dict], minute: float, t: float) -> list[str]:
    """Each target of the plan in one finished window: [{"metric", "key", "status", "value", "target"}]."""
    out = []
    for i, c in enumerate(checks):
        tags = {"site_id": site, "match_id": match_id, "metric": c["metric"], "key": str(c["key"]).lower(), "status": c["status"]}
        value = c["value"]
        fields = {"minute": minute, "target": c.get("target"),
                  **({"shape": value} if isinstance(value, str) else {"value": float(value) if value is not None else None}),
                  "on_plan": 1 if c["status"] == "on_plan" else 0}
        out.append(line("plan_check", tags, fields, int(t * 1e9) + i))
    return out


def play_lines(doc: dict, tz: tzinfo, fixture: dict) -> list[str]:
    tags = {"site_id": doc["site_id"], "match_id": doc["match_id"], "type": doc["type"],
            "team": fixture[doc["team"]]["code"], "ours": str(doc["team"] == fixture["team"]).lower()}
    fields = {"count": 1, "minute": round(PERIOD_START[doc["period"]] + doc["match_time_s"] / 60, 2),
              "xg": (doc.get("details") or {}).get("xg"), "play_id": doc["play_id"]}
    return [line("team_play", tags, fields, _ns(doc["timestamp"], tz, doc["play_id"]))]


def deviation_lines(d: dict, t: float) -> list[str]:
    tags = {"site_id": d["site_id"], "match_id": d["match_id"], "metric": d["metric"], "priority": d["priority"], "team": d["team"]}
    fields = {"count": 1, "minute": round(PERIOD_START[d["period"]] + d["match_time_s"] / 60, 1), "deviation_id": d["deviation_id"]}
    return [line("plan_deviation", tags, fields, int(t * 1e9))]


def review_lines(change: dict) -> list[str]:
    """A call on a deviation, or (reopened) the undoing of the one before it."""
    reopened = change["decision"] == "pending_review"
    if reopened and change["previous"] == "pending_review":
        return []
    tags = {"site_id": change["site_id"], "match_id": change["match_id"], "metric": change["metric"],
            "decision": change["previous"] if reopened else change["decision"], "reviewer": change["by"]}
    fields = {"count": -1 if reopened else 1, "review_s": None if reopened else change["review_s"],
              "deviation_id": change["deviation_id"]}
    return [line("deviation_review", tags, fields, int(change["at"] * 1e9))]


def fixture_lines(doc: dict, now: float | None = None) -> list[str]:
    fields = {"home": doc["home"]["code"], "away": doc["away"]["code"], "status": doc["status"], "period": doc["period"],
              "score_home": doc["score"]["home"], "score_away": doc["score"]["away"], "ours": doc[doc["team"]]["code"]}
    return [line("team_fixture", {"site_id": doc["site_id"], "match_id": doc["match_id"]}, fields,
                 int((now if now is not None else time.time()) * 1e9))]


def heartbeat_lines(doc: dict, tz: tzinfo) -> list[str]:
    ns = _ns(doc["timestamp"], tz)
    host = doc["device"]["hostname"]
    numbers = {**doc["metrics"], **doc["pipeline"]}
    lines = [line("sensor_data", {"device": host, "sensor": key}, {"value": float(value)}, ns)
             for key, value in sorted(numbers.items())]
    lines.append(line("team_node", {"site_id": doc["site_id"], "device": host},
                      {"status": doc["status"], "up": 1 if doc["status"] == "healthy" else 0}, ns))
    return lines


def window_message(doc: dict) -> dict:
    """A window as MQTT gets it: the numbers and shapes, without positions and pass networks."""
    teams = {}
    for side, t in doc["teams"].items():
        teams[side] = {k: v for k, v in t.items() if k != "pass_pairs"}
        for phase in ("in_possession", "out_of_possession"):
            teams[side][phase] = {k: v for k, v in t[phase].items() if k != "positions"}
    return {**doc, "teams": teams}


def play_message(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "track"}


class Notify:
    """
    Posts events to a webhook (n8n's) so it can tell people: best effort,
    like the other copies. A failure is logged at most once every 5 minutes,
    so a stack without n8n doesn't fill the log.
    """

    QUIET = 300

    def __init__(self) -> None:
        self.url = os.environ.get("NOTIFY_URL", "").strip().rstrip("/")
        self.token = os.environ.get("NOTIFY_TOKEN", "")
        self.failed_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def post(self, events: list[tuple[str, dict]]) -> None:
        if not self.enabled:
            return
        for path, body in events:
            request = urllib.request.Request(
                f"{self.url}/{path}", data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                headers={"Content-Type": "application/json", **({"X-Notify-Token": self.token} if self.token else {})},
            )
            try:
                with urllib.request.urlopen(request, timeout=10):
                    pass
            except (urllib.error.URLError, OSError) as e:
                if time.monotonic() - self.failed_at > self.QUIET:
                    self.failed_at = time.monotonic()
                    log(f"notify: {path} not delivered to {self.url}: {e}")


class Influx:
    def __init__(self) -> None:
        self.url = os.environ.get("INFLUXDB_URL", "").rstrip("/")
        self.token = os.environ.get("INFLUXDB_TOKEN", "")
        self.org = os.environ.get("INFLUXDB_ORG", "")
        self.bucket = os.environ.get("INFLUXDB_BUCKET", "raw_telemetry")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def write(self, lines: list[str]) -> None:
        if not self.enabled or not lines:
            return
        query = urllib.parse.urlencode({"org": self.org, "bucket": self.bucket, "precision": "ns"})
        request = urllib.request.Request(
            f"{self.url}/api/v2/write?{query}",
            data="\n".join(lines).encode(),
            headers={"Authorization": f"Token {self.token}", "Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except (urllib.error.URLError, OSError) as e:
            detail = e.read().decode(errors="replace")[:200] if isinstance(e, urllib.error.HTTPError) else e
            log(f"influxdb: {len(lines)} points dropped: {detail}")


# ── MQTT 3.1.1, publish only, QoS 0 ───────────────────────────────────────────


def _string(value: str) -> bytes:
    data = value.encode()
    return struct.pack("!H", len(data)) + data


def _packet(kind: int, body: bytes) -> bytes:
    length, encoded = len(body), b""
    while True:
        byte, length = length % 128, length // 128
        encoded += bytes([byte | (0x80 if length else 0)])
        if not length:
            return bytes([kind]) + encoded + body


class Mqtt:
    """
    Enough of MQTT to publish: one connection per batch, so a broker restart
    costs nothing to recover from. Batches are small (a device posts every
    few seconds), so the connection overhead doesn't matter.
    """

    def __init__(self) -> None:
        self.host = os.environ.get("MQTT_HOST", "")
        self.port = int(os.environ.get("MQTT_PORT") or 1883)
        self.user = os.environ.get("MQTT_USER", "")
        self.password = os.environ.get("MQTT_PASSWORD", "")

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def publish(self, messages: list[tuple[str, dict]]) -> None:
        if not self.enabled or not messages:
            return
        flags, payload = 0x02, _string(f"team-ingest-{os.getpid()}-{time.monotonic_ns() % 1_000_000}")
        if self.user:
            flags |= 0x80
            payload += _string(self.user)
            if self.password:
                flags |= 0x40
                payload += _string(self.password)
        connect = _string("MQTT") + bytes([4, flags]) + struct.pack("!H", 30) + payload
        try:
            with socket.create_connection((self.host, self.port), timeout=5) as sock:
                sock.sendall(_packet(0x10, connect))
                ack = sock.recv(4)
                if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
                    raise OSError(f"connection refused (CONNACK {ack.hex() or 'none'})")
                for topic, doc in messages:
                    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
                    sock.sendall(_packet(0x30, _string(topic) + body))
                sock.sendall(_packet(0xE0, b""))
        except OSError as e:
            log(f"mqtt: {len(messages)} messages dropped: {e}")


def topic(site_id: str, *parts: str) -> str:
    return "/".join((TOPIC_ROOT, site_id, *parts))
