"""
Offline checks for the ingest service (iot/ingest): the contract's rules,
the game plan's rules, upserts that never touch a call, deviations opening
and closing, the analyst's calls, the feed, the agent API's numbers, the
sinks and the HTTP layer (tokens, plans, the analysis room's files), against
one of the demo season's matches. No Docker.

    python3 tests/test_ingest.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "iot/ingest"), str(ROOT / "iot/scripts")]
import contract  # noqa: E402
import ingest  # noqa: E402
import plan as rules  # noqa: E402
import simulate  # noqa: E402
import sinks  # noqa: E402
from store import Store, key_play  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {str(detail)[:400]}"))
    failed |= not ok


UTC = timezone.utc
NOW = time.time()
# A past match of the demo season, played and measured as the simulator does
match = simulate.history(UTC, NOW)[-1] if simulate.HISTORY else None
fixture = match.fixture(NOW)
windows = match.window_docs(NOW)
plays = match.play_docs(NOW)
SITE = simulate.SITE

print("== the contract")
check("the fixture is valid", contract.check_fixture(fixture) is None, contract.check_fixture(fixture))
check("so is every window", all(contract.check_window(w) is None for w in windows))
check("and every play", all(contract.check_play(p) is None for p in plays))
bad = copy.deepcopy(fixture)
bad["team"] = "both"
check("a fixture must say which side is ours", "team must be" in (contract.check_fixture(bad) or ""))
bad = copy.deepcopy(windows[0])
bad["teams"]["home"]["in_possession"]["formation"] = "4-4-2-1-1-1"
check("a formation must look like one", "formation" in (contract.check_window(bad) or ""))
bad = copy.deepcopy(windows[0])
bad["end_s"] = bad["start_s"]
check("a window ends after it starts", "start_s" in (contract.check_window(bad) or ""))
bad = copy.deepcopy(plays[0])
bad["details"] = {"made_up": 1}
check("a play's details are its type's", "not a" in (contract.check_play(bad) or ""))
check("match minutes as they're written", contract.minute(1, 2830) == "45+3" and contract.minute(2, 0) == "46")

print("== the game plan's rules")
try:
    rules.check({"targets": [{"metric": "magic", "value": 1}]})
    check("an unknown metric is refused", False)
except rules.PlanError:
    check("an unknown metric is refused", True)
try:
    rules.check({"targets": [{"metric": "ppda", "value": 9}, {"metric": "ppda", "value": 8}]})
    check("a metric only once", False)
except rules.PlanError:
    check("a metric only once", True)
p = rules.check({"title": "t", "formation": {"in_possession": "3-2-5", "out_of_possession": ""}, "targets": [{"metric": "ppda", "value": 9}]})
check("a target takes its metric's direction", p["targets"][0]["op"] == "<=" and p["formation"] == {"in_possession": "3-2-5"}, p)
t = {"metric": "line_height_m", "op": ">=", "value": 38}
check("on plan, drifting, off plan", [rules.status(t, v) for v in (38.0, 36.0, 34.0, None)] == ["on_plan", "drifting", "off_plan", "no_data"])
check("ruled at the precision people read", rules.evaluate({"metric": "possession_pct", "op": ">=", "value": 52},
      [{**windows[0], "teams": {**windows[0]["teams"], "home": {**windows[0]["teams"]["home"], "possession_s": 51.9},
                                "away": {**windows[0]["teams"]["away"], "possession_s": 48.1}}}], "home")[1] == "on_plan")
check("a shape target compares formations", rules.evaluate({"metric": "formation_in_possession", "shape": "3-2-5"}, windows, match.ours)[1] == "on_plan")
check("PPDA over a window is their passes per our defensive action", rules.value("ppda", windows, match.ours) is not None)

print("== the store")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"{tmp}/test.db", UTC, 400, "2026-03-07")
    try:
        store.put_window(windows[0])
        check("a window needs its fixture first", False)
    except ValueError:
        check("a window needs its fixture first", True)
    store.put_fixture(fixture)
    check("a plan for the match", store.put_plan(SITE, match.match_id, rules.check(simulate.DEFAULT_PLAN), "coach")["inherited"] is False)
    check("windows stored", all(store.put_window(w) == "created" for w in windows))
    check("a re-sent window is unchanged", store.put_window(windows[0]) == "unchanged")
    for p_ in plays:
        store.put_play(p_)
    check("key plays are marked", any(key_play(p_, fixture["team"]) for p_ in plays))
    # Deviations, window by window, as the ingest service runs them
    store2 = Store(":memory:", UTC)
    store2.put_fixture(fixture)
    store2.put_plan(SITE, match.match_id, rules.check({"targets": [{"metric": "possession_pct", "op": ">=", "value": 99, "key": True}]}), "t")
    events = []
    for w in windows:
        store2.put_window(w)
        events += store2.evaluate(SITE, match.match_id, now=4e9)
    check("an impossible target opens one deviation, after three windows", [e[0] for e in events] == ["opened"]
          and events[0][1]["minute"] == "15", [(e[0], e[1]["minute"]) for e in events])
    d = events[0][1]
    check("high priority for a key target", d["priority"] == "high" and d["key"])
    check("it says what was measured, against what", "plan asks ≥ 99%" in d["summary"], d["summary"])
    item, change = store2.act(SITE, d["deviation_id"], "confirm", "analyst", "talk at the break")
    check("flagged for the bench, with a note", item["status"] == "confirmed" and item["note"] == "talk at the break" and change["previous"] == "pending_review")
    check("the same call again changes nothing", store2.act(SITE, d["deviation_id"], "confirm", "analyst")[1] is None)
    item, change = store2.act(SITE, d["deviation_id"], "reopen", "analyst")
    check("reopened", item["status"] == "pending_review" and item["reviewed_by"] is None)
    store2.put_window(windows[0])
    check("a device re-sending a window never touches a call", store2.deviation(SITE, d["deviation_id"])["status"] == "pending_review")
    # Back on plan: a target that's met closes its deviation
    store2.put_plan(SITE, match.match_id, rules.check({"targets": [{"metric": "possession_pct", "op": ">=", "value": 1, "key": True}]}), "t")
    closed = store2.evaluate(SITE, match.match_id, now=4e9)
    check("a target met again closes its deviation", closed and closed[0][0] == "closed" and closed[0][1]["back_on_plan"], closed)
    # The club's default
    store.delete_plan(SITE, match.match_id)
    check("without its own plan, the match uses the default", store.plan(SITE, match.match_id) is None)
    store.put_plan(SITE, "default", rules.check(simulate.DEFAULT_PLAN), "coach")
    check("the club's default, inherited", store.plan(SITE, match.match_id)["inherited"])
    a = store.adherence(SITE, match.match_id)
    check("adherence: every target over the match, the halves, the last 15' and every window",
          all({"match", "first_half", "second_half", "last_15", "windows"} <= set(t_) for t_ in a["targets"])
          and all(len(t_["windows"]) == 18 for t_ in a["targets"]), a["targets"][0].keys())
    s = store.match_summary(SITE, match.match_id)
    check("the summary: both teams' numbers, the plan, the key plays", s["numbers"]["us"]["passes"] > 100 and s["game_plan"] and s["key_plays"])
    season = store.season(SITE, *store.period("season")[:2])
    check("the season: the record and the plan per metric", season["record"]["played"] == 1 and season["plan_by_metric"], season["record"])
    f = store.formation(SITE, match.match_id)
    check("the shapes read, with average positions", f["teams"]["BHA"]["match"]["out_of_possession"]["formation"] == "4-4-2"
          and len(f["teams"]["BHA"]["match"]["in_possession"]["positions"]) == 10, f["teams"]["BHA"]["match"])
    feed = store.feed(50)
    check("the feed: key plays, as events", feed["items"] and all(i["kind"] == "event" for i in feed["items"]), feed["items"][:1])
    # Running and set pieces
    run = store.physical(SITE, match.match_id)
    us_run = run["teams"][fixture["team"]]
    check("running: every player, minutes, km, high-speed metres, sprints, top speed, m a minute every 5'",
          len(us_run["players"]) >= 11 and all(len(p_["per_window"]) == 18 for p_ in us_run["players"])
          and us_run["players"][0]["keeper"], us_run["players"][:1])
    check("and ours with the drop against his first 30'", any("drop_last_15" in p_ for p_ in us_run["players"]))
    sp = store.set_pieces(SITE, match.match_id)
    check("set pieces: each delivery, both sides' totals and our routines",
          sp["set_pieces"] and sp["teams"][fixture["team"]]["by_routine"] and all(p_["first_contact"] for p_ in sp["set_pieces"]), sp["teams"])
    check("a delivery's end in its team's frame (the attacking end)", all(p_["end"] is None or p_["end"][0] > 52.5 for p_ in sp["set_pieces"]),
          [p_["end"] for p_ in sp["set_pieces"]][:5])
    lo, hi = store.period("season")[:2]
    check("the season's set pieces, ours and theirs", store.set_piece_season(SITE, lo, hi)["for"]["deliveries"] == len(
        [p_ for p_ in sp["set_pieces"] if p_["ours"]]))
    check("and the squad's running per 90", store.physical_season(SITE, lo, hi)["players"][0]["distance_km_p90"])
    # A scouting match: neither side ours, no plan, not in the season; plays found on video go to the feed
    scout = {**fixture, "match_id": "SCOUT-KICKABOUT-1", "home": {"code": "BIB", "name": "Bibs"},
             "away": {"code": "SKN", "name": "Skins"}, "scouting": True, "synthetic": False}
    check("a scouting fixture is valid", contract.check_fixture(scout) is None, contract.check_fixture(scout))
    store.put_fixture(scout)
    check("no plan for a scouting match, the club's default included", store.plan(SITE, "SCOUT-KICKABOUT-1") is None)
    check("and it isn't in the season", store.season(SITE, lo, hi)["record"]["played"] == 1)
    foul = {"schema": contract.PLAY, "play_id": "PL-20261019-311801", "site_id": SITE, "match_id": "SCOUT-KICKABOUT-1",
            "camera_id": "kickabout", "timestamp": fixture["kickoff"], "period": 1, "match_time_s": 25.0, "type": "foul",
            "team": "home", "players": [{"team": "away", "number": None, "role": "fouler"}], "details": {}, "confidence": 0.4,
            "source": "video", "synthetic": False}
    check("a foul the video models found is a valid play", contract.check_play(foul) is None, contract.check_play(foul))
    store.put_play(foul)
    check("and a key one: the feed shows it", store.play(SITE, foul["play_id"])["key"] and store.play(SITE, foul["play_id"])["source"] == "video")
    # A club code keeps another team's match out of the season
    club_store = Store(":memory:", UTC, 400, "2026-03-07", "ARS")
    club_store.put_fixture(fixture)
    check("the season counts the club's own matches only", club_store.season(SITE, lo, hi)["record"]["played"] == 0)

print("== the sinks")
lines = sinks.window_lines(windows[0], fixture, NOW)
team_lines = [x for x in lines if x.startswith("team_window,")]
check("a window is a point per side", len(team_lines) == 2 and "ours=" in team_lines[0] and "set_piece_xg=" in team_lines[0], team_lines[:1])
players = [x for x in lines if x.startswith("player_run,")]
check("and a point per player, with his running", len(players) >= 22 and "number=" in players[0] and "hsr_m=" in players[0], players[:1])
check("formations go as strings", 'formation_in="3-2-5"' in "".join(lines), lines[0][:300])
check("MQTT gets windows without their pass network", "pass_pairs" not in json.dumps(sinks.window_message(windows[0])))
check("and plays without their track", "track" not in sinks.play_message(plays[0]))

print("== HTTP")
with tempfile.TemporaryDirectory() as tmp:
    ingest.EVIDENCE_DIR = Path(tmp) / "evidence"
    ingest.PAGE_DIR = ROOT / "iot/ingest/analysis"
    store = Store(f"{tmp}/http.db", UTC)
    service = ingest.Service(store, {"site-token": SITE, "other-token": "other"}, "read-token", SITE,
                             influx=sinks.Influx(), mqtt=sinks.Mqtt(), notify=sinks.Notify())
    service.influx.url = service.mqtt.host = service.notify.url = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.handler(service))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def http(method, path, body=None, token="", kind="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": kind, **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers["Content-Type"].startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    check("writes need a device token", http("POST", "/api/v1/fixtures", fixture)[0] == 401)
    check("a token writes only its own club", http("POST", "/api/v1/fixtures", fixture, "other-token")[0] == 400)
    check("the fixture", http("POST", "/api/v1/fixtures", fixture, "site-token")[1]["created"] == 1)
    check("a device can't write a plan", http("PUT", f"/api/v1/plans/{match.match_id}", simulate.DEFAULT_PLAN, "site-token")[0] == 401)
    status, body = http("PUT", f"/api/v1/plans/{match.match_id}", {**simulate.DEFAULT_PLAN, "by": "coach"}, "read-token")
    check("the staff save a plan", status == 200 and body["updated_by"] == "coach", body)
    check("a plan for an unknown match is refused", http("PUT", "/api/v1/plans/NOPE", simulate.DEFAULT_PLAN, "read-token")[0] == 404)
    status, body = http("POST", "/api/v1/windows", windows, "site-token")
    check("windows in one batch", body["created"] == 18, body)
    status, body = http("POST", "/api/v1/plays", plays, "site-token")
    check("plays in one batch", body["created"] == len(plays), body)
    goal = next(p_ for p_ in plays if p_["type"] == "goal")
    check("a keyframe for a play", http("PUT", f"/api/v1/evidence/{goal['play_id']}", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "site-token", "image/png")[0] == 200)
    check("not a picture: refused", http("PUT", f"/api/v1/evidence/{goal['play_id']}", b"hello", "site-token", "image/png")[0] == 400)
    check("reads need a token", http("GET", "/api/v1/feed")[0] == 401)
    status, body = http("GET", "/api/v1/feed?limit=100", token="read-token")
    check("the feed has the deviations to call", body["counts"]["pending"] >= 1 and any(i["kind"] == "alert" for i in body["items"]), body.get("counts"))
    goal_item = next(i for i in body["items"] if i["id"].endswith(goal["play_id"]))
    check("a key play has its picture", goal_item.get("image") is True)
    dev = next(i for i in body["items"] if i["kind"] == "alert")
    check("a deviation's actions: flag, expected", [a["id"] for a in dev["actions"]] == ["confirm", "dismiss"])
    check("a device can't make a call", http("POST", f"/api/v1/feed/{dev['id']}/action", {"action": "confirm"}, "site-token")[0] == 401)
    status, body = http("POST", f"/api/v1/feed/{dev['id']}/action", {"action": "dismiss", "by": "analyst"}, "read-token")
    check("a call through the feed", status == 200 and body["item"]["status"] == "dismissed", body)
    check("plays have no actions", http("POST", f"/api/v1/feed/{goal_item['id']}/action", {"action": "confirm"}, "read-token")[0] == 404)
    status, body = http("GET", f"/api/v1/analysis/match?match_id={match.match_id}", token="read-token")
    check("everything the analysis room shows", status == 200 and {"stats", "timeline", "adherence", "deviations", "plays", "shapes",
                                                                    "physical", "set_pieces"} <= set(body))
    status, body = http("GET", "/api/v1/analysis/set_pieces?period=season", token="read-token")
    check("the season's set pieces", status == 200 and {"for", "against", "matches", "deliveries"} <= set(body), body)
    status, body = http("GET", "/api/v1/analysis/physical?period=season", token="read-token")
    check("the squad's running", status == 200 and body["players"], body)
    for path in ("set_pieces", "physical"):
        status, body = http("GET", f"/api/v1/agent/{path}?match_id={match.match_id}", token="read-token")
        check(f"the agent's {path.replace('_', ' ')}", status == 200 and body["match"]["match_id"] == match.match_id, body)
    status, body = http("GET", "/api/v1/plans/catalog", token="read-token")
    check("the plan editor's catalog", status == 200 and len(body["metrics"]) >= 10 and "4-4-2" in body["formations"])
    # Recaps: the ingest service forwards to the agent's narrator, which reads the match back from it (so the
    # forwarding must not hold the store's lock: a stub agent here does the same)
    from http.server import BaseHTTPRequestHandler

    class StubAgent(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            asked = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            read_back = http("GET", f"/api/v1/agent/match?match_id={match.match_id}", token="read-token")[0]
            status, payload = (400, {"error": "audience must be one of fan, coach"}) if asked.get("audience") == "nobody" \
                else (200, {"recap": "A story.", "asked": asked, "read_back": read_back, "backend": "azure"})
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubAgent)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    ingest.AGENT_URL = f"http://127.0.0.1:{stub.server_port}"
    check("recaps need the staff's token", http("GET", "/api/v1/analysis/recap")[0] == 401)
    status, body = http("GET", f"/api/v1/analysis/recap?match_id={match.match_id}&audience=fan&language=es&focus=7&junk=1",
                        token="read-token")
    check("a recap: forwarded to the agent, which reads the match back (no deadlock)", status == 200 and body["recap"] == "A story."
          and body["read_back"] == 200 and body["asked"] == {"match_id": match.match_id, "audience": "fan", "language": "es",
                                                              "focus": "7"}, (status, body))
    status, body = http("GET", "/api/v1/analysis/recap?audience=nobody", token="read-token")
    check("the agent's refusal comes back as it is", status == 400 and "audience" in body["error"], (status, body))
    check("a match this site doesn't have: refused here", http("GET", "/api/v1/analysis/recap?match_id=NOPE", token="read-token")[0] == 400)
    stub.shutdown()
    stub.server_close()
    status, body = http("GET", "/api/v1/analysis/recap", token="read-token")
    check("the agent down: 502, saying so", status == 502 and "unreachable" in body["error"], (status, body))
    ingest.AGENT_URL = ""
    check("no agent set: 503", http("GET", "/api/v1/analysis/recap", token="read-token")[0] == 503)
    status, page = http("GET", "/analysis/")
    check("the analysis room is served", status == 200 and b'<script src="brand.js">' in page)
    check("nothing outside it", http("GET", "/analysis/../ingest.py")[0] == 404)
    check("another club's token sees none of this one", http("GET", "/api/v1/agent/match?site_id=brighton", token="other-token")[0] == 403)
    server.shutdown()

sys.exit(1 if failed else 0)
