"""
Offline checks for the club device's analytics (iot/scripts/analytics.py)
and the demo's match engine (engine.py): formations, xG, possession,
windows, plays, and the live match's story against its game plan. No Docker.

    python3 tests/test_analytics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "iot/ingest"), str(ROOT / "iot/scripts")]
import analytics  # noqa: E402
import contract  # noqa: E402
import engine  # noqa: E402
import plan as rules  # noqa: E402
import simulate  # noqa: E402
from store import Store  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {str(detail)[:400]}"))
    failed |= not ok


print("== formations")
for name, template in analytics.FORMATIONS.items():
    read, _ = analytics.formation([(18 + d * 30, 9 + w * 50) for d, w in template])
    check(f"{name} reads as itself", read == name, read)
# A 4-4-2 with noise, squashed and shifted, still reads as a 4-4-2
noisy = [(30 + d * 22 + (i % 3 - 1) * 0.8, 14 + w * 40 + (i % 2) * 0.9) for i, (d, w) in enumerate(analytics.FORMATIONS["4-4-2"])]
check("a 4-4-2 with noise, at another depth and width", analytics.formation(noisy)[0] == "4-4-2", analytics.formation(noisy))
check("not ten players: no formation", analytics.formation([(1, 1)] * 9)[0] is None)
check("the assignment is optimal", analytics.hungarian([[4, 1, 3], [2, 0, 5], [3, 2, 2]]) == [1, 0, 2])

print("== xG and geometry")
check("from the penalty spot about 0.33", 0.3 <= analytics.xg(94, 34) <= 0.36, analytics.xg(94, 34))
check("from 25 m about 0.05", analytics.xg(80, 34) < 0.08, analytics.xg(80, 34))
check("from 6 m about 0.7", analytics.xg(99, 34) > 0.6, analytics.xg(99, 34))
check("a header is worth less", analytics.xg(94, 34, header=True) < analytics.xg(94, 34))
check("ends change at half time", analytics.attacks_right("right", 1, "home") and not analytics.attacks_right("right", 2, "home")
      and analytics.attacks_right("right", 2, "away"))
check("normalising flips both axes", analytics.norm(10, 20, False) == (95, 48))
check("channels from the attacker's view", analytics.channel(5) == "left" and analytics.channel(34) == "centre" and analytics.channel(60) == "right")

print("== a scripted mini-match")
# Home passes three times, loses it, away shoots and scores; positions: 22 players standing still
players = [("home", n, 30 + n, 10 + 4 * n) for n in range(1, 12)] + [("away", n, 70 - n, 10 + 4 * n) for n in range(1, 12)]
frames = [(1, i / 5, (50.0, 34.0), players) for i in range(0, 300 * 5)]
events = [
    {"period": 1, "t": 1.0, "team": "home", "number": 4, "kind": "pass", "x": 30, "y": 30, "end": (45, 30), "success": True, "to": 6},
    {"period": 1, "t": 3.0, "team": "home", "number": 6, "kind": "pass", "x": 45, "y": 30, "end": (75, 10), "success": True, "to": 11},
    {"period": 1, "t": 5.0, "team": "home", "number": 11, "kind": "pass", "x": 75, "y": 10, "end": (90, 30), "success": False},
    {"period": 1, "t": 5.5, "team": "away", "number": 4, "kind": "interception", "x": 88, "y": 30, "success": True},
    {"period": 1, "t": 9.0, "team": "away", "number": 9, "kind": "shot", "x": 12, "y": 34, "outcome": "goal", "success": True},
]
m = analytics.Match(frames, events, {"home": {1}, "away": {1}}, "right", hz=5)
w = m.windows()
check("one 5-minute window", len(w) == 1 and w[0]["final"], len(w))
home, away = w[0]["teams"]["home"], w[0]["teams"]["away"]
check("passes and completions", home["passes"] == 3 and home["passes_completed"] == 2, (home["passes"], home["passes_completed"]))
check("an entry into the final third, down the left", home["final_third_entries"] == {"left": 1, "centre": 0, "right": 0}, home["final_third_entries"])
check("a turnover and a regain", home["turnovers"] == 1 and away["regains"] == 1, (home["turnovers"], away["regains"]))
check("the goal and its xG", away["goals"] == 1 and away["xg"] > 0.2, (away["goals"], away["xg"]))
check("possession time is split", home["possession_s"] > 0 and away["possession_s"] > home["possession_s"], (home["possession_s"], away["possession_s"]))
plays = m.plays(w)
goal = next((p for p in plays if p["type"] == "goal"), None)
check("the goal is a play, after a regain in their own half: a counter", goal and goal["details"]["pattern"] == "counter", goal)
check("plays carry their snapshot and replay", goal and goal["snapshot"] and len(goal["track"]["frames"]) > 10)
check("pass quality: three rated passes, 76 m in all", home["passes_rated"] == 3 and home["pass_distance_m"] == 76,
      (home["passes_rated"], home["pass_distance_m"]))
easy, forward = m.pass_difficulty(events[0]), m.pass_difficulty(events[1])
check("a long pass forward into the final third is harder than a short one sideways", 0 <= easy < forward <= 1, (easy, forward))
check("no ball speed from a ball that doesn't move, and none sent", home["passes_timed"] == 0 and home["ball_speed_max_ms"] == 0,
      (home["passes_timed"], home["ball_speed_max_ms"]))
moving = [(1, i / 5, (50.0 + 4 * max(0, i - 5), 34.0), players) for i in range(0, 40)]
kick = {"period": 1, "t": 1.0, "team": "home", "number": 4, "kind": "pass", "x": 50, "y": 34, "end": (80, 34), "success": True}
check("ball speed measured from the ball's track: 4 m a frame is 20 m/s",
      analytics.Match(moving, [kick], {}, "right", hz=5).ball_speed(kick) == 20.0)
check("the device's own reading comes first", m.ball_speed({**kick, "speed_ms": 27.5}) == 27.5)

print("== the engine and the device's analytics")
match = simulate.live(simulate.timezone.utc, 1_800_000_000.0)
check("the live match is the same every time", simulate.live(simulate.timezone.utc, 1_800_000_000.0, measure=False).frames[500] == match.frames[500])
check("18 windows, all valid under the contract", len(match.windows) == 18 and not any(
    contract.check_window({"schema": contract.WINDOW, "site_id": "brighton", "match_id": "x", "home_attacks": "right", **x, "synthetic": True})
    for x in match.windows), [contract.check_window({"schema": contract.WINDOW, "site_id": "brighton", "match_id": "x", "home_attacks": "right", **x, "synthetic": True}) for x in match.windows][:1])
docs = match.play_docs(match.end() + 60)
check("every play valid under the contract", docs and not any(contract.check_play(d) for d in docs), next((contract.check_play(d) for d in docs if contract.check_play(d)), ""))
goals = [(p["period"], round(p["match_time_s"] / 60), p["team"], p["details"]["pattern"]) for p in match.plays if p["type"] == "goal"]
check("the story's goals: 19' high press, 41' their counter, 63' a set piece",
      [(g[0], g[2], g[3]) for g in goals] == [(1, "home", "high_press"), (1, "away", "counter"), (2, "home", "set_piece")], goals)
check("the final score is 2-1", match.fixture(match.end() + 60)["score"] == {"home": 2, "away": 1})
shapes = {(x["period"], x["start_s"]): x["teams"]["home"]["out_of_possession"]["formation"] for x in match.windows}
late = [shapes[(2, s)] for s in (1500, 1800, 2100, 2400)]
check("the plan's 4-4-2 out of possession, then a 5-4-1 to see it out (most of the last 20')",
      shapes[(1, 0)] == "4-4-2" and late.count("5-4-1") >= 3, shapes)
changes = [(p["team"], p["details"]["to"]) for p in match.plays if p["type"] == "formation_change"]
check("their back three at 58' and our 5-4-1 are formation changes", ("away", "3-4-3") in changes and ("home", "5-4-1") in changes, changes)

every = [x["teams"][side] for x in match.windows for side in ("home", "away")]
rated = sum(t["passes_rated"] for t in every)
length, hardness = sum(t["pass_distance_m"] for t in every) / rated, 100 * sum(t["pass_difficulty"] for t in every) / rated
check("passes average 12-25 m, difficulty 15-60 of 100", 12 <= length <= 25 and 15 <= hardness <= 60, (round(length, 1), round(hardness)))
hard = sum(t["passes_hard"] for t in every)
check("some passes are hard, and fewer of them are completed", 0 < hard < rated / 3 and
      sum(t["passes_hard_completed"] for t in every) / hard < sum(t["passes_completed"] for t in every) / sum(t["passes"] for t in every))
kmh = 3.6 * sum(t["pass_speed_ms"] for t in every) / sum(t["passes_timed"] for t in every)
check("passes travel 45-80 km/h on average", 45 <= kmh <= 80, round(kmh))
shots = [p["details"].get("speed_kmh") for p in match.plays if p["type"] in ("goal", "shot")]
check("every shot and goal says how hard it was struck, 35-130 km/h", shots and all(v and 35 <= v <= 130 for v in shots), shots)

print("== running")
run = {}
for x in match.windows:
    for n, sec, dist, hsr, spr, top in x["teams"]["home"]["physical"]:
        a = run.setdefault(n, [0, 0, 0, 0, 0.0])
        a[0] += sec; a[1] += dist; a[2] += hsr; a[3] += spr; a[4] = max(a[4], top)
starters = {n: a for n, a in run.items() if a[0] > 80 * 60 and n != 1}
km = sorted(round(a[1] / a[0] * 5400 / 1000, 1) for a in starters.values())
check("outfield starters cover 8-14 km per 90", starters and 8 <= km[0] and km[-1] <= 14, km)
hsr90 = sorted(round(a[2] / a[0] * 5400) for a in starters.values())
check("and run 150-2,000 m over 5.5 m/s per 90", 150 <= hsr90[0] and hsr90[-1] <= 2000, hsr90)
check("nobody faster than 10 m/s", max(a[4] for a in run.values()) <= 10.0, max(a[4] for a in run.values()))
check("#7 comes off at 72' for #19", 7 in run and 19 in run and run[7][0] < 75 * 60 and 15 * 60 <= run[19][0] <= 22 * 60, (run.get(7), run.get(19)))
w1 = match.windows[0]["teams"]["home"]
check("a window's distance is its players'", w1["distance_m"] == sum(p[2] for p in w1["physical"]), (w1["distance_m"], sum(p[2] for p in w1["physical"])))
drops = rules.load_drops([x for x in match.windows if x["period"] == 2][2:5], "home", match.windows)
check("by 70', #7 covers the least ground against his first 30'", drops and drops[0][0] == 7 and drops[0][1] > 21, drops[:3])

print("== set pieces")
sp = [p for p in match.plays if p["type"] == "set_piece"]
corner = next((p for p in sp if p["period"] == 2 and abs(p["match_time_s"] - 17.5 * 60) < 30), None)
check("the 63' corner is the near-post flick, first contact ours, a goal",
      corner and corner["details"].get("routine") == "near-post flick" and corner["details"]["first_contact"] == "attack"
      and corner["details"]["outcome"] == "goal", corner and corner["details"])
check("every set piece says who won the first contact", sp and all(p["details"]["first_contact"] in ("attack", "defence", "none") for p in sp))
ours = [p for p in sp if p["team"] == "home"]
check("ours are the club's routines", ours and all(p["details"].get("routine") in engine.ROUTINES or p["details"].get("routine") == "direct free kick"
                                                for p in ours), [p["details"].get("routine") for p in ours])
total = sum(x["teams"]["home"]["set_pieces"] for x in match.windows)
check("the windows count the same deliveries", total == len(ours), (total, len(ours)))

print("== the live match against its plan")
store = Store(":memory:", simulate.timezone.utc)
store.put_fixture(match.fixture(match.end() + 60))
store.put_plan("brighton", match.match_id, rules.check(match.plan), "test")
opened = []
for doc in match.window_docs(match.end() + 60):
    store.put_window(doc)
    opened += [(what, d["metric"], d["back_on_plan"]["minute"] if what == "closed" else d["minute"])
               for what, d in store.evaluate("brighton", match.match_id, now=4e9)]
first_half = {metric for what, metric, minute in opened if what == "opened" and int(minute.split("+")[0]) <= 45}
# Every live match drops its line before half time; the press (PPDA, counter-press, possession) fades in most
check("the line drops and the team stretches before half time", {"line_height_m", "length_m"} <= first_half, opened)
closed = {metric for what, metric, minute in opened if what == "closed" and 46 <= int(minute.split("+")[0]) <= 70}
check("back on the plan after the break", "line_height_m" in closed, opened)
late = {metric for what, metric, minute in opened if what == "opened" and int(minute.split("+")[0]) >= 75}
check("the width and the 5-4-1 late on", {"width_m", "formation_out_of_possession"} <= late, opened)
load = [(what, minute) for what, metric, minute in opened if metric == "load_drop_pct"]
check("#7's running drop opens at 65'-70' and is back on plan by 80', after his substitution",
      len(load) == 2 and load[0][0] == "opened" and 65 <= int(load[0][1]) <= 70 and load[1][0] == "closed" and int(load[1][1]) <= 80, load)
dev = next((d for d in store.deviations("brighton", match.match_id) if d["metric"] == "load_drop_pct"), None)
check("the deviation names #7", dev and dev["title"].startswith("#7 running") and dev["players"][0]["number"] == 7, dev and dev["title"])
run = store.physical("brighton", match.match_id)["teams"]["home"]["players"]
tags = [(p["number"], m["kind"]) for p in run for m in p["milestones"]]
check("milestones: 5 and 10 km for the starters who play on, 30 km/h for someone",
      ((2, "distance") in tags or (3, "distance") in tags) and any(k == "top_speed" for _, k in tags)
      and all(sum(1 for m in p["milestones"] if m["kind"] == "top_speed") <= 1 for p in run), tags)

print("== synthetic names")
import squads  # noqa: E402
names = squads.build()["clubs"]
check("every club has 30 names, no surname twice", len(names) == 20 and all(
    len(v) == 30 and len({n.split()[-1] for n in v.values()}) == 30 for v in names.values()))
check("the same names every run", squads.build() == squads.build())
named = Store(":memory:", simulate.timezone.utc, squads=names)
fixture = match.fixture(match.end() + 60)
named.put_fixture(fixture)
named.put_plan("brighton", match.match_id, rules.check(match.plan), "test")
for doc in match.window_docs(match.end() + 60):
    named.put_window(doc)
    named.evaluate("brighton", match.match_id, now=4e9)
dev = next((d for d in named.deviations("brighton", match.match_id) if d["metric"] == "load_drop_pct"), None)
seven = names[fixture["home"]["code"]]["7"]
check("the deviation names #7 by his name too", dev and dev["title"].startswith(f"#7 {seven} running") and dev["players"][0]["name"] == seven,
      dev and dev["title"])
real = Store(":memory:", simulate.timezone.utc, squads=names)
real.put_fixture({**fixture, "synthetic": False})
check("a real match's players keep their numbers only", real.player_name(real._fixture("brighton", match.match_id), "home", 7) is None)

sys.exit(1 if failed else 0)
