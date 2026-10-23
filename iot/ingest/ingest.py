"""
Team ingest: the web side of the club device ↔ web contract 0.1 (schemas
p4n4.team.*/0.1), the game plans, the analyst's queue, and the agent API.

The club's match device posts the match it is measuring and what it
measured (outbound HTTPS, from wherever the team plays); this service checks
it against the contract, keeps it in SQLite, copies it to InfluxDB and MQTT
(sinks.py), rules it against the staff's game plan (plan.py), lists the
deviations and key plays for the analyst (the analysis page, and
p4n4-dashboard's Feed tab through p4n4-api), records each call, and answers
the agent's questions with numbers computed from what it stored (store.py).

    POST /api/v1/fixtures    the match and its live state (status, period, score)
    POST /api/v1/windows     5-minute windows: one, a list, or {"windows": [...]}
    POST /api/v1/plays       technical plays: one, a list, or {"plays": [...]}
    POST /api/v1/heartbeat   the device's health
    PUT  /api/v1/evidence/<play_id>   the play's keyframe, image/jpeg or image/png, up to 2 MB
        X-Site-Token: <the site's token> (or Authorization: Bearer <token>);
        a token only writes its own site_id.

    GET  /api/v1/plans/<match_id|default>    a match's game plan (else the club's default)
    PUT  /api/v1/plans/<match_id|default>    save one: {title, formation, targets, instructions, opponent_notes, by}
    DELETE /api/v1/plans/<match_id|default>
    GET  /api/v1/plans/catalog               what a plan can ask (metrics, formations)

    GET  /api/v1/feed                  limit, before: deviations and key plays, newest first (feed contract)
    POST /api/v1/feed/<site>:<id>/action     {"action": confirm|dismiss|reopen, "by", "note", "at"} (deviations)
    GET  /api/v1/feed/<site>:<id>/image      a key play's keyframe
    GET  /api/v1/analysis/matches      the latest matches
    GET  /api/v1/analysis/match        match_id: everything the analysis page shows
    GET  /api/v1/analysis/play         play_id: one play in full, with its replay
    GET  /api/v1/analysis/set_pieces   period | from, to; opponent: the season's set pieces (ours, theirs, the maps)
    GET  /api/v1/analysis/physical     period | from, to: our players' running per 90 over the season
    GET  /api/v1/analysis/recap        match_id, part (live, half, full), audience (fan, coach, analyst,
                                       broadcast), language, focus (a shirt number): the match's story,
                                       told by the agent's narrator (AGENT_URL's POST /api/recap)
    GET  /api/v1/evidence/<site>/<play_id>.jpg   a play's keyframe (JPEG or PNG, whichever it is)
    GET  /analysis/                    the analysis page, the plan editor and the reports (they ask for the read token)

    GET  /api/v1/agent/match     match_id (default: the match being played, else the latest)
    GET  /api/v1/agent/plan      match_id: the game plan, target by target
    GET  /api/v1/agent/formation match_id: the shapes read, and average positions
    GET  /api/v1/agent/plays     match_id; type, team (code), period, key; limit
    GET  /api/v1/agent/play      play_id
    GET  /api/v1/agent/set_pieces  match_id, or period | from, to (and opponent) for the season
    GET  /api/v1/agent/physical  match_id: every player's running, and the drops against his first 30'
    GET  /api/v1/agent/season    period | from, to
    GET  /api/v1/agent/nodes     each device's latest heartbeat
    GET  /api/v1/sites
        Authorization: Bearer <INGEST_READ_TOKEN> for everything (the
        staff's token: plans and calls need it), or a site's token for that
        site's reads. Every GET takes site_id; without it, INGEST_DEFAULT_SITE.

    GET  /health

Standard library only, so it runs on the stock python image.

    INGEST_PORT            Port to listen on (default 8080)
    INGEST_DB              SQLite file (default /data/ingest.db)
    INGEST_SITE_TOKENS     site_id:token pairs, comma-separated. Required: with none, every write is refused
    INGEST_READ_TOKEN      The staff's token: every read, the plans and the calls. Empty: none needed
    INGEST_DEFAULT_SITE    Site a read means when it names none (default: the only one)
    INGEST_RETENTION_DAYS  Days matches are kept (default 400)
    SEASON_START           First day of the season (YYYY-MM-DD), for the agent's "season"
    ANALYSIS_DIR           The analysis page's files (default /p4n4/analysis)
    ANALYSIS_PUBLIC_URL    The analysis page as a browser reaches it (feed items link there). Empty: no link
    ANALYSIS_VIDEO_URL     Where the page finds the cameras (go2rtc). Empty: port 1984 on its own host
    SITE_TZ                Site time zone, an IANA name or an offset (default UTC)
    INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET    Where to copy points (empty URL: don't)
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, MQTT_TOPIC_ROOT   Where to publish (empty host: don't)
    NOTIFY_URL             Where to post deviations, half time and full time (n8n). Empty: nowhere
    AGENT_URL              The match agent, for the analysis room's recaps (default http://p4n4-ollama:11434,
                           the ai layer's agent). Empty: no recaps
    NOTIFY_TOKEN           Sent as X-Notify-Token, so n8n knows the post is ours
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import contract
import plan as rules
import sinks
from store import ACTIONS, PeriodError, Store

PORT = int(os.environ.get("INGEST_PORT", "8080"))
DB = os.environ.get("INGEST_DB", "/data/ingest.db")
TZ = contract.site_tz(os.environ.get("SITE_TZ", "UTC"))
READ_TOKEN = os.environ.get("INGEST_READ_TOKEN", "").strip()
DEFAULT_SITE = os.environ.get("INGEST_DEFAULT_SITE", "").strip()
RETENTION = int(os.environ.get("INGEST_RETENTION_DAYS") or 400)
SEASON_START = os.environ.get("SEASON_START", "").strip() or None
PAGE_DIR = Path(os.environ.get("ANALYSIS_DIR", "/p4n4/analysis"))
VIDEO_URL = os.environ.get("ANALYSIS_VIDEO_URL", "").strip().rstrip("/")
PAGE_URL = os.environ.get("ANALYSIS_PUBLIC_URL", "").strip()
MAX_BODY = 16 * 1024 * 1024
MAX_EVIDENCE = 2 * 1024 * 1024
KEYFRAMES = {"image/jpeg": (b"\xff\xd8\xff", ".jpg"), "image/png": (b"\x89PNG\r\n\x1a\n", ".png")}
EVIDENCE_DIR = Path(os.environ.get("INGEST_EVIDENCE_DIR") or Path(DB).parent / "evidence")
MAX_BATCH = 500
PURGE_EVERY = 3600
# Deviations opened longer ago than this aren't sent to n8n: they are news no more
NOTIFY_MAX_AGE = 1800
AGENT_URL = os.environ.get("AGENT_URL", "http://p4n4-ollama:11434").strip().rstrip("/")
# A recap takes the narrator a few seconds on Azure, up to a minute or two on Ollama on a CPU
RECAP_TIMEOUT = 240
RECAP_PARAMS = ("match_id", "part", "audience", "language", "focus")


def parse_tokens(text: str) -> dict[str, str]:
    """INGEST_SITE_TOKENS → {token: site_id}."""
    tokens = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        site_id, _, token = pair.partition(":")
        if not token.strip() or not contract.ID.match(site_id.strip()):
            raise SystemExit(f"INGEST_SITE_TOKENS: expected site_id:token, got {pair!r}")
        tokens[token.strip()] = site_id.strip()
    return tokens


class Kind:
    """One POST endpoint: how to check and store its documents, and its id field."""

    def __init__(self, key, check, put, id_key):
        self.key, self.check, self.put, self.id_key = key, check, put, id_key


class Service:
    def __init__(self, store: Store, tokens: dict[str, str], read_token: str = "", default_site: str = "",
                 influx: sinks.Influx | None = None, mqtt: sinks.Mqtt | None = None,
                 notify: sinks.Notify | None = None) -> None:
        self.store, self.tokens, self.read_token = store, tokens, read_token
        self.default_site = default_site
        self.influx = influx or sinks.Influx()
        self.mqtt = mqtt or sinks.Mqtt()
        self.notify = notify or sinks.Notify()
        self.tz = store.tz
        self.outbox: queue.Queue = queue.Queue(maxsize=10_000)
        threading.Thread(target=self._drain, daemon=True).start()
        self.kinds = {
            "/api/v1/fixtures": Kind("fixtures", contract.check_fixture, store.put_fixture, "match_id"),
            "/api/v1/windows": Kind("windows", contract.check_window, store.put_window, "start_s"),
            "/api/v1/plays": Kind("plays", contract.check_play, store.put_play, "play_id"),
            "/api/v1/heartbeat": Kind("heartbeats", contract.check_heartbeat, store.put_heartbeat, "timestamp"),
        }

    def _drain(self) -> None:
        """Copies to InfluxDB, MQTT and n8n off the request thread, so a slow sink never delays a device."""
        while True:
            lines, messages, events = self.outbox.get()
            self.influx.write(lines)
            self.mqtt.publish(messages)
            self.notify.post(events)

    def _send(self, lines: list[str], messages: list, events: list | None = None) -> None:
        if not (lines or messages or events):
            return
        try:
            self.outbox.put_nowait((lines, messages, events or []))
        except queue.Full:
            sinks.log(f"outbox full: {len(lines)} points and {len(messages)} messages dropped")

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the outbox to empty (tests)."""
        deadline = time.monotonic() + timeout
        while not self.outbox.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    def purge(self) -> None:
        while True:
            deleted = self.store.purge()
            cutoff = time.time() - self.store.retention_days * 86400
            for frame in EVIDENCE_DIR.glob("*/*"):
                if frame.suffix in (".jpg", ".png") and frame.stat().st_mtime < cutoff:
                    frame.unlink(missing_ok=True)
            if deleted:
                sinks.log(f"retention: deleted {deleted} matches older than {self.store.retention_days} days")
            time.sleep(PURGE_EVERY)

    # ── auth ──────────────────────────────────────────────────────────────────

    def _match(self, token: str) -> str | None:
        found = None
        for known, site_id in self.tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                found = site_id
        return found

    def writer(self, headers) -> str | None:
        return self._match(_token(headers)) if self.tokens else None

    def reader(self, headers) -> tuple[bool, str | None]:
        """(allowed, the one site the token is limited to, or None for all)."""
        token = _token(headers)
        if not self.read_token:
            return True, None
        if token and hmac.compare_digest(self.read_token.encode(), token.encode()):
            return True, None
        site_id = self._match(token) if token else None
        return (site_id is not None), site_id

    def staff(self, headers) -> bool:
        """Plans and calls take the read token: the staff's (the analysis page's) or p4n4-api's (the Feed tab)."""
        token = _token(headers)
        return not self.read_token or bool(token and hmac.compare_digest(self.read_token.encode(), token.encode()))

    # ── writes ────────────────────────────────────────────────────────────────

    def ingest(self, path: str, body: object, site_id: str) -> tuple[int, dict]:
        kind = self.kinds[path]
        if isinstance(body, dict) and isinstance(body.get(kind.key), list):
            docs = body[kind.key]
        elif isinstance(body, list):
            docs = body
        else:
            docs = [body]
        if len(docs) > MAX_BATCH:
            return 413, {"error": f"at most {MAX_BATCH} {kind.key} per request"}
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        rejected, lines, messages, events = [], [], [], []
        for index, doc in enumerate(docs):
            error = kind.check(doc, self.tz)
            if not error and doc["site_id"] != site_id:
                error = f"this token writes site {site_id!r} only"
            if error:
                ref = doc.get(kind.id_key) if isinstance(doc, dict) else None
                rejected.append({"index": index, kind.id_key: ref if isinstance(ref, (str, int, float)) else None, "error": error})
                continue
            with self.store.lock:
                before = self.before(kind.key, doc)
                try:
                    outcome = kind.put(doc)
                except (sqlite3.Error, ValueError) as e:
                    rejected.append({"index": index, kind.id_key: doc.get(kind.id_key), "error": f"not stored: {e}"})
                    continue
                counts[outcome] += 1
                if outcome != "unchanged":
                    more_lines, more_messages, more_events = self.copies(kind.key, doc, before)
                    lines += more_lines
                    messages += more_messages
                    events += more_events
                    # The game plan, against each finished window as it lands (a batch is a match backfilled in order)
                    if kind.key == "windows" and doc["final"]:
                        more_lines, more_messages, more_events = self.deviations(doc["site_id"], doc["match_id"])
                        lines += more_lines
                        messages += more_messages
                        events += more_events
        self._send(lines, messages, events)
        accepted = sum(counts.values())
        if rejected:
            sinks.log(f"{kind.key} from {site_id}: {accepted} accepted, {len(rejected)} rejected: {rejected[0]['error']}")
        return (400 if rejected and not accepted else 200), {"accepted": accepted, **counts, "rejected": rejected}

    def before(self, key: str, doc: dict) -> dict | None:
        """What was stored before: a fixture's state, or whether a window was already final."""
        if key == "fixtures":
            return self.store._fixture(doc["site_id"], doc["match_id"])
        if key == "windows":
            row = self.store.db.execute("SELECT final FROM windows WHERE site_id = ? AND match_id = ? AND period = ? AND start_s = ?",
                                        (doc["site_id"], doc["match_id"], doc["period"], float(doc["start_s"]))).fetchone()
            return {"final": bool(row and row["final"])}
        return None

    def copies(self, key: str, doc: dict, before: dict | None) -> tuple[list, list, list]:
        site = doc["site_id"]
        if key == "fixtures":
            events = []
            if self.notify.enabled and before and before["status"] != doc["status"] and doc["status"] in ("half_time", "full_time"):
                event = doc["status"].replace("_", "-")
                events.append((event, {"event": doc["status"], "match": self.store.match_info(site, doc)}))
            return sinks.fixture_lines(doc), [(sinks.topic(site, "fixture"), doc)], events
        if key == "windows":
            messages = [(sinks.topic(site, "window"), sinks.window_message(doc))]
            if not doc["final"] or (before and before["final"]):
                return [], messages, []
            fixture = self.store._fixture(site, doc["match_id"])
            t = min(time.time(), self.store.wall_time(fixture, doc["period"], doc["end_s"]))
            lines = sinks.window_lines(doc, fixture, t)
            plan = self.store.plan(site, doc["match_id"])
            if plan:
                checks = []
                for target in rules.checks(plan["doc"]):
                    measured, state = rules.evaluate(target, [doc], fixture["team"])
                    checks.append({"metric": target["metric"], "key": bool(target.get("key")), "status": state,
                                   "value": measured, "target": float(target["value"]) if "value" in target else None})
                lines += sinks.check_lines(site, doc["match_id"], checks, round(contract.PERIOD_START[doc["period"]] + doc["start_s"] / 60, 1), t)
            return lines, messages, []
        if key == "plays":
            fixture = self.store._fixture(site, doc["match_id"])
            return (sinks.play_lines(doc, self.tz, fixture), [(sinks.topic(site, "play", doc["type"]), sinks.play_message(doc))], [])
        return sinks.heartbeat_lines(doc, self.tz), [(sinks.topic(site, "heartbeat"), doc)], []

    def deviations(self, site: str, match_id: str) -> tuple[list, list, list]:
        lines, messages, events = [], [], []
        fixture = self.store._fixture(site, match_id)
        for what, d in self.store.evaluate(site, match_id):
            messages.append((sinks.topic(site, "deviation"), {"event": what, **d}))
            if what == "opened":
                t = contract.parse_time(d["time"], self.tz).timestamp()
                lines += sinks.deviation_lines(d, t)
                live = fixture["status"] in ("live", "half_time")
                if self.notify.enabled and live and time.time() - t <= NOTIFY_MAX_AGE:
                    events.append(("deviation", {"event": "deviation", "deviation": d,
                                                 "match": self.store.match_info(site, fixture)}))
                sinks.log(f"deviation: {d['deviation_id']} at {d['minute']}' ({d['value']}, plan {d['target']})")
        return lines, messages, events

    def put_plan(self, site: str, match_id: str, body: object) -> tuple[int, dict]:
        try:
            doc = rules.check(body)
        except rules.PlanError as e:
            return 400, {"error": str(e)}
        by = str((body or {}).get("by") or "staff").strip()[:64] or "staff"
        try:
            saved = self.store.put_plan(site, match_id, doc, by)
        except KeyError:
            return 404, {"error": f"no match {match_id} at site {site}"}
        self._send([], [(sinks.topic(site, "plan"), {"match_id": match_id, **doc, "by": by})])
        sinks.log(f"plan: {match_id} saved by {by} ({len(doc['targets'])} targets)")
        return 200, {"match_id": match_id, **saved}

    def put_evidence(self, site: str, play_id: str, data: bytes, kind: str) -> tuple[int, dict]:
        kind = kind.split(";")[0].strip()
        if kind not in KEYFRAMES or not data.startswith(KEYFRAMES[kind][0]):
            return 400, {"error": "a keyframe must be image/jpeg or image/png"}
        if len(data) > MAX_EVIDENCE:
            return 413, {"error": f"a keyframe is at most {MAX_EVIDENCE // 1024 // 1024} MB"}
        if not contract.PLAY_ID.match(play_id):
            return 400, {"error": "play_id must look like PL-20261017-000123"}
        with self.store.lock:
            if self.store._play_row(site, play_id) is None:
                return 404, {"error": f"no play {play_id} at site {site}: post the play first"}
        path = EVIDENCE_DIR / site / f"{play_id}{KEYFRAMES[kind][1]}"
        path.parent.mkdir(parents=True, exist_ok=True)
        for _, suffix in KEYFRAMES.values():
            path.with_suffix(suffix).unlink(missing_ok=True)
        path.write_bytes(data)
        return 200, {"stored": f"{site}/{path.name}", "bytes": len(data)}

    @staticmethod
    def evidence_path(site: str, play_id: str) -> Path | None:
        if not contract.ID.match(site) or not contract.PLAY_ID.match(play_id):
            return None
        return next((p for p in (EVIDENCE_DIR / site / f"{play_id}{s}" for _, s in KEYFRAMES.values()) if p.is_file()), None)

    @classmethod
    def evidence(cls, site: str, play_id: str) -> tuple[bytes, str] | None:
        path = cls.evidence_path(site, play_id)
        if not path:
            return None
        return path.read_bytes(), next(kind for kind, (_, suffix) in KEYFRAMES.items() if suffix == path.suffix)

    def act(self, item_id: str, body: object) -> tuple[int, dict]:
        """An analyst's call on deviation <site>:<deviation_id>, from the Feed tab or the analysis page."""
        if not isinstance(body, dict) or body.get("action") not in ACTIONS:
            return 400, {"error": f"action must be one of {', '.join(ACTIONS)}"}
        by = str(body.get("by") or "anonymous").strip()[:64] or "anonymous"
        note = body.get("note")
        note = str(note).strip()[:500] or None if note is not None else None
        try:
            at = contract.parse_time(body["at"], self.tz).timestamp() if body.get("at") else None
        except (TypeError, ValueError):
            return 400, {"error": "at must be an ISO 8601 date-time"}
        try:
            with self.store.lock:
                item, change = self.store.feed_action(item_id, body["action"], by, note, at)
        except KeyError:
            return 404, {"error": f"no deviation {item_id} (plays have no actions)"}
        if change:
            message = {k: change[k] for k in ("deviation_id", "match_id", "metric", "decision", "previous", "by", "review_s", "synthetic")}
            self._send(sinks.review_lines(change), [(sinks.topic(change["site_id"], "review"), message)])
            sinks.log(f"call: {item_id} {change['previous']} → {change['decision']} by {by}")
        return 200, {"action": body["action"], "item": self.decorate(item)}

    def decorate(self, item: dict) -> dict:
        """A feed item with its keyframe ("image": true, a play's) and its link to the analysis page."""
        site, _, ref = item["id"].partition(":")
        if contract.PLAY_ID.match(ref) and self.evidence_path(site, ref):
            item["image"] = True
        if PAGE_URL:
            hash_ = urllib.parse.quote(f'{site}/{item["meta"]["match_id"]}/{ref}', safe="/:")
            item["link"] = {"label": "Open in the analysis room", "url": f"{PAGE_URL}#{hash_}"}
        return item

    # ── recaps ────────────────────────────────────────────────────────────────

    def recap(self, params: dict, limit: str | None) -> tuple[int, dict]:
        """
        A match's story from the agent's narrator (POST <AGENT_URL>/api/recap), as (status, body). The agent reads
        the match back from this service, so this runs outside the store's lock.
        """
        if not AGENT_URL:
            return 503, {"error": "no agent for recaps (AGENT_URL is empty)"}
        with self.store.lock:
            site = self.pick_site(params, limit)
            if params.get("match_id") and not self.store._fixture(site, params["match_id"]):
                return 400, {"error": f"no match {params['match_id']!r} at site {site}"}
        body = json.dumps({k: params[k] for k in RECAP_PARAMS if params.get(k)}).encode()
        request = urllib.request.Request(f"{AGENT_URL}/api/recap", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=RECAP_TIMEOUT) as response:
                return 200, json.load(response)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except ValueError:
                return 502, {"error": f"the agent answered {e.code}"}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 502, {"error": f"the agent is unreachable at {AGENT_URL}: {getattr(e, 'reason', e)}"}

    # ── reads ─────────────────────────────────────────────────────────────────

    def pick_site(self, params: dict, limit: str | None) -> str:
        wanted = (params.get("site_id") or "").strip()
        if limit:
            if wanted and wanted != limit:
                raise PermissionError(f"this token reads site {limit!r} only")
            return limit
        if wanted or self.default_site:
            return wanted or self.default_site
        known = sorted(set(self.store.sites()) | set(self.tokens.values()))
        if len(known) == 1:
            return known[0]
        raise PeriodError(f"pass site_id, one of: {', '.join(known) or '(no sites yet)'}")

    def query(self, path: str, params: dict, limit: str | None) -> dict | None:
        s = self.store
        if path == "/api/v1/sites":
            return {"sites": [x for x in s.sites() if not limit or x == limit]}
        if path == "/api/v1/plans/catalog":
            return rules.catalog()
        if path == "/api/v1/feed":
            before = float(params["before"]) if params.get("before") else None
            page = s.feed(max(1, min(200, int(params.get("limit") or 50))), before, limit)
            page["items"] = [self.decorate(i) for i in page["items"]]
            return page
        if path == "/api/v1/agent/nodes":
            return s.nodes(limit or params.get("site_id") or None)
        if path == "/api/v1/analysis/matches":
            return {"matches": s.fixtures(limit or params.get("site_id") or None), "video_url": VIDEO_URL or None}
        site = self.pick_site(params, limit)
        if path.startswith("/api/v1/plans/"):
            match_id = urllib.parse.unquote(path[len("/api/v1/plans/"):])
            found = s.plan(site, match_id)
            return {"match_id": match_id, "site_id": site, **(found or {"doc": None})}
        if path in ("/api/v1/agent/play", "/api/v1/analysis/play"):
            try:
                result = s.play(site, (params.get("play_id") or "").strip(), full=path == "/api/v1/analysis/play")
            except KeyError:
                raise PeriodError(f"no play {params.get('play_id')!r} at site {site}") from None
            if path == "/api/v1/analysis/play":
                result["keyframe"] = self.evidence_path(site, result["play_id"]) is not None
            return result
        if path == "/api/v1/agent/season":
            lo, hi, period = s.period(params.get("period"), params.get("from"), params.get("to"))
            return {"period": period, "site_id": site, **s.season(site, lo, hi)}
        if path in ("/api/v1/analysis/set_pieces", "/api/v1/analysis/physical") or (
                path == "/api/v1/agent/set_pieces" and not params.get("match_id")
                and any(params.get(k) for k in ("period", "from", "to", "opponent"))):
            lo, hi, period = s.period(params.get("period"), params.get("from"), params.get("to"))
            if path == "/api/v1/analysis/physical":
                return {"period": period, "site_id": site, **s.physical_season(site, lo, hi)}
            return {"period": period, "site_id": site, "opponent": (params.get("opponent") or "").upper() or None,
                    **s.set_piece_season(site, lo, hi, params.get("opponent") or None)}
        if path in ("/api/v1/agent/match", "/api/v1/agent/plan", "/api/v1/agent/formation", "/api/v1/agent/plays",
                    "/api/v1/analysis/match", "/api/v1/agent/set_pieces", "/api/v1/agent/physical"):
            match_id = s.pick_match(site, params.get("match_id"))
            if path == "/api/v1/agent/set_pieces":
                return {"match": s.match_info(site, s._fixture(site, match_id)), **s.set_pieces(site, match_id)}
            if path == "/api/v1/agent/physical":
                return {"match": s.match_info(site, s._fixture(site, match_id)), **s.physical(site, match_id)}
            if path == "/api/v1/agent/match":
                return s.match_summary(site, match_id)
            if path == "/api/v1/agent/plan":
                found = s.adherence(site, match_id)
                return {"match": s.match_info(site, s._fixture(site, match_id)), **found}
            if path == "/api/v1/agent/formation":
                return s.formation(site, match_id)
            if path == "/api/v1/analysis/match":
                return {**s.analysis(site, match_id), "video_url": VIDEO_URL or None}
            fixture = s._fixture(site, match_id)
            team = (params.get("team") or "").strip().upper()
            side = next((x for x in ("home", "away") if fixture[x]["code"] == team), None)
            if team and not side:
                raise PeriodError(f"team must be {fixture['home']['code']} or {fixture['away']['code']}")
            period = params.get("period")
            key = params.get("key")
            return s.plays(site, match_id, params.get("type") or None, side, int(period) if period else None,
                           None if key in (None, "") else key in ("1", "true", "yes"),
                           max(1, min(100, int(params.get("limit") or 50))))
        return None

    @staticmethod
    def static(path: str) -> tuple[bytes, str] | None:
        """A file of the analysis page, or None. Never outside PAGE_DIR."""
        name = path[len("/analysis"):].lstrip("/") or "index.html"
        target = (PAGE_DIR / name).resolve()
        if PAGE_DIR.resolve() not in target.parents or not target.is_file():
            return None
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return target.read_bytes(), kind + ("; charset=utf-8" if kind.startswith("text/") else "")


def club_code() -> str | None:
    """The club's code, from the league the analysis page shows (league.json "club"); None without one."""
    try:
        return json.loads((PAGE_DIR / "league.json").read_text(encoding="utf-8")).get("club")
    except (OSError, ValueError):
        return None


def squads() -> dict:
    """The synthetic season's fictional names per club (squads.json, iot/scripts/squads.py); {} without one."""
    try:
        return json.loads((PAGE_DIR / "squads.json").read_text(encoding="utf-8")).get("clubs") or {}
    except (OSError, ValueError):
        return {}


def _token(headers) -> str:
    token = (headers.get("X-Site-Token") or "").strip()
    auth = (headers.get("Authorization") or "").strip()
    if not token and auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
    return token


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "team-ingest/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:
            pass  # the feed polls every few seconds; calls and refusals are logged on their own

        def _send(self, status: int, data: dict) -> None:
            self._raw(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _raw(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if kind.startswith("text/html"):
                self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> object:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError(f"body over {MAX_BODY // 1024 // 1024} MB")
            return json.loads(self.rfile.read(length) or b"null")

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/health":
                return self._send(200, {"status": "ok", "sites": service.store.sites(),
                                        "retention_days": service.store.retention_days,
                                        "influxdb": service.influx.enabled, "mqtt": service.mqtt.enabled})
            if path == "/analysis" and not url.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", "/analysis/")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            if path == "/analysis" or path.startswith("/analysis/"):
                found = service.static(url.path)
                return self._raw(200, *found) if found else self._send(404, {"error": "not found"})
            allowed, limit = service.reader(self.headers)
            if not allowed:
                return self._send(401, {"error": "Authorization: Bearer <read token> required"})
            if path.startswith("/api/v1/feed/") and path.endswith("/image"):
                site, _, ref = urllib.parse.unquote(path[len("/api/v1/feed/"):-len("/image")]).partition(":")
                if limit and site != limit:
                    return self._send(403, {"error": f"this token reads site {limit!r} only"})
                data = service.evidence(site, ref)
                return self._raw(200, *data) if data else self._send(404, {"error": "no keyframe"})
            if path.startswith("/api/v1/evidence/") and path.endswith(".jpg"):
                site, _, ref = path[len("/api/v1/evidence/"):-len(".jpg")].partition("/")
                if limit and site != limit:
                    return self._send(403, {"error": f"this token reads site {limit!r} only"})
                data = service.evidence(site, ref)
                return self._raw(200, *data) if data else self._send(404, {"error": "no keyframe"})
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            if path == "/api/v1/analysis/recap":
                try:
                    return self._send(*service.recap(params, limit))
                except PermissionError as e:
                    return self._send(403, {"error": str(e)})
                except (PeriodError, ValueError) as e:
                    return self._send(400, {"error": str(e)})
            try:
                with service.store.lock:
                    result = service.query(path, params, limit)
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except (PeriodError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            if result is None:
                return self._send(404, {"error": f"no endpoint {path}"})
            self._send(200, result)

        def do_PUT(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            path = url.path.rstrip("/")
            if path.startswith("/api/v1/plans/"):
                return self._plan(url, path, "PUT")
            if not path.startswith("/api/v1/evidence/"):
                return self._send(404, {"error": f"no endpoint {path}"})
            site_id = service.writer(self.headers)
            if site_id is None:
                return self._send(401, {"error": "X-Site-Token: <site token> required"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_EVIDENCE:
                return self._send(413, {"error": f"a keyframe is at most {MAX_EVIDENCE // 1024 // 1024} MB"})
            data = self.rfile.read(length)
            self._send(*service.put_evidence(site_id, path[len("/api/v1/evidence/"):], data,
                                             self.headers.get("Content-Type") or ""))

        def do_DELETE(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            path = url.path.rstrip("/")
            if path.startswith("/api/v1/plans/"):
                return self._plan(url, path, "DELETE")
            self._send(404, {"error": f"no endpoint {path}"})

        def _plan(self, url, path: str, method: str) -> None:
            if not service.staff(self.headers):
                return self._send(401, {"error": "Authorization: Bearer <read token> required"})
            match_id = urllib.parse.unquote(path[len("/api/v1/plans/"):])
            if match_id != "default" and not contract.ID.match(match_id):
                return self._send(400, {"error": "a plan is for a match id, or default"})
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                site = service.pick_site(params, None)
            except PeriodError as e:
                return self._send(400, {"error": str(e)})
            if method == "DELETE":
                with service.store.lock:
                    gone = service.store.delete_plan(site, match_id)
                return self._send(200 if gone else 404, {"deleted": gone, "match_id": match_id})
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._send(400, {"error": f"invalid JSON: {e}"})
            with service.store.lock:
                self._send(*service.put_plan(site, match_id, body))

        def do_POST(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            path = url.path.rstrip("/")
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._send(400, {"error": f"invalid JSON: {e}"})
            if path.startswith("/api/v1/feed/") and path.endswith("/action"):
                if not service.staff(self.headers):
                    return self._send(401, {"error": "Authorization: Bearer <read token> required"})
                return self._send(*service.act(urllib.parse.unquote(path[len("/api/v1/feed/"):-len("/action")]), body))
            if path not in service.kinds:
                return self._send(404, {"error": f"no endpoint {path}"})
            if not service.tokens:
                return self._send(503, {"error": "no site tokens configured (INGEST_SITE_TOKENS)"})
            site_id = service.writer(self.headers)
            if site_id is None:
                return self._send(401, {"error": "X-Site-Token: <site token> required"})
            self._send(*service.ingest(path, body, site_id))

    return Handler


def main() -> None:
    tokens = parse_tokens(os.environ.get("INGEST_SITE_TOKENS", ""))
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB, TZ, RETENTION, SEASON_START, club_code(), squads())
    service = Service(store, tokens, READ_TOKEN, DEFAULT_SITE)
    threading.Thread(target=service.purge, daemon=True).start()
    sinks.log(f"ingest on :{PORT}, {DB}, matches kept {RETENTION} days, "
              f"sites with a token: {', '.join(sorted(set(tokens.values()))) or 'none'}")
    if not tokens:
        sinks.log("WARNING: INGEST_SITE_TOKENS is empty, so every write is refused")
    if not READ_TOKEN:
        sinks.log("WARNING: INGEST_READ_TOKEN is empty, so anyone who reaches this port can read and change plans")
    sinks.log(f"influxdb: {service.influx.url or 'off'}, mqtt: {service.mqtt.host or 'off'}, "
              f"notify: {service.notify.url or 'off'}, analysis page: /analysis/")
    ThreadingHTTPServer(("0.0.0.0", PORT), handler(service)).serve_forever()


if __name__ == "__main__":
    main()
