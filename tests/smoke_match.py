"""
A match for the smoke test, posted the way the club's device would: run with
the simulator's image and scripts (python - <match_id> <status> < tests/smoke_match.py).

    status live        the fixture, a plan nobody can meet (possession ≥ 99%, a key
                       instruction; width ≥ 95 m, not key) and the first 15 minutes,
                       three finished windows: two deviations open, one of them high priority
    status half_time   the same fixture at half time
    status full_time   at full time
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/scripts")
import analytics  # noqa: E402
import engine  # noqa: E402
import simulate  # noqa: E402

MATCH, STATUS = sys.argv[1], sys.argv[2]
URL, SITE = os.environ["INGEST_URL"], os.environ["SITE_ID"]


def call(method, path, body, token):
    request = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method=method,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


fixture = {"schema": "p4n4.team.fixture/0.1", "site_id": SITE, "match_id": MATCH, "competition": "PL smoke",
           "home": {"code": "BHA", "name": "Brighton & Hove Albion"}, "away": {"code": "ARS", "name": "Arsenal"}, "team": "home",
           "venue": "American Express Stadium", "kickoff": simulate.iso(__import__("time").time() - 1200, simulate.timezone.utc),
           "status": STATUS, "period": 1, "score": {"home": 1, "away": 0}, "synthetic": True}
print(call("POST", "/api/v1/fixtures", fixture, os.environ["INGEST_TOKEN"]))
if STATUS == "live":
    plan = {"title": "Smoke: a plan nobody meets", "formation": {"out_of_possession": "4-4-2"}, "by": "smoke",
            "targets": [{"metric": "possession_pct", "op": ">=", "value": 99, "key": True},
                        {"metric": "width_m", "op": ">=", "value": 95, "key": False}]}
    print(call("PUT", f"/api/v1/plans/{MATCH}?site_id={SITE}", plan, os.environ["INGEST_READ_TOKEN"])["match_id"])
    play = engine.play("smoke", lambda side, p, t: simulate.BRIGHTON if side == "home" else simulate.OPPONENT_STYLES[0],
                       halves=(960, 60))
    measured = analytics.Match(play.frames, play.events, {"home": {1}, "away": {1}}, "right", hz=engine.HZ)
    windows = [{"schema": "p4n4.team.window/0.1", "site_id": SITE, "match_id": MATCH, "home_attacks": "right", **w,
                "synthetic": True} for w in measured.windows(upto=(1, 905))]
    print(call("POST", "/api/v1/windows", windows, os.environ["INGEST_TOKEN"]))
