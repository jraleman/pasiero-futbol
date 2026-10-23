"""
Offline checks for the agent's tools (ai/agent/local_tools.py) against a real
ingest service (in-process, on a free port) holding one of the demo season's
past matches: the tools pick the match, the play, the target; the numbers are
the ingest service's. No Docker, no model.

    python3 tests/test_local_tools.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import types
from datetime import timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "iot/ingest"), str(ROOT / "iot/scripts"), str(ROOT / "ai/agent")]
import ingest  # noqa: E402
import plan as rules  # noqa: E402
import simulate  # noqa: E402
import sinks  # noqa: E402
from store import Store  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {str(detail)[:400]}"))
    failed |= not ok


tmp = tempfile.TemporaryDirectory()
store = Store(f"{tmp.name}/tools.db", timezone.utc, 400, "2026-03-07")
service = ingest.Service(store, {"site-token": simulate.SITE}, "read-token", simulate.SITE)
service.influx.url = service.mqtt.host = service.notify.url = ""
server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.handler(service))
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ.update(INGEST_URL=f"http://127.0.0.1:{server.server_port}", INGEST_READ_TOKEN="read-token")
import local_tools  # noqa: E402

match = simulate.history(timezone.utc, time.time())[-1]
service.ingest("/api/v1/fixtures", match.fixture(time.time()), simulate.SITE)
store.put_plan(simulate.SITE, match.match_id, rules.check(match.plan or simulate.DEFAULT_PLAN), "coach")
service.ingest("/api/v1/windows", match.window_docs(time.time()), simulate.SITE)
service.ingest("/api/v1/plays", match.play_docs(time.time()), simulate.SITE)

agent = types.SimpleNamespace(ToolError=type("ToolError", (Exception,), {}))
tools = {d["function"]["name"]: (d, f) for d, f in local_tools.tools(agent)}
check("ten tools, each with a label", len(tools) == 10 and set(tools) <= set(local_tools.LABELS))
call = lambda name, **args: tools[name][1](args)  # noqa: E731

r = call("get_match_summary")
check("the match being played (or the latest), without asking", r["match"]["match_id"] == match.match_id, r["match"])
check("both teams' numbers, the plan, the deviations, the key plays",
      r["numbers"]["us"]["passes"] > 100 and r["game_plan"]["targets"] and "deviations" in r and r["key_plays"])
check("small enough for a small model", len(json.dumps(r)) < 8000, len(json.dumps(r)))
check("what's flagged for the bench, in so many words", r["flagged_for_the_bench"], r.get("flagged_for_the_bench"))
check("what's still to call, and every call in words", r["still_to_call"]
      and all(d["call"] in ("not called yet", "flagged for the bench", "expected") for d in r["deviations"]), r["still_to_call"])
r = call("get_game_plan")
check("the plan, target by target, every 5 minutes on one line", r["targets"] and r["targets"][0]["every_5_minutes"].count("'") == 18, r["targets"][:1])
check("…and small", len(json.dumps(r)) < 8000, len(json.dumps(r)))
r = call("get_game_plan", target="line height")
check("one target by its name", len(r["targets"]) == 1 and r["targets"][0]["name"] == "Defensive line height" and r["targets"][0]["description"], r["targets"])
r = call("get_game_plan", target="free kicks scored")
check("a target the plan doesn't have: the whole plan, with a note (an error makes a small model give up)",
      len(r["targets"]) > 3 and "no target like" in r["note"], r.get("note"))
r = call("get_formation", team="us")
check("'us' is Brighton", list(r["teams"]) == ["BHA"] and r["teams"]["BHA"]["match"]["out_of_possession"], r["teams"].keys())
r = call("get_formation")
check("both teams' shapes and average positions", len(r["teams"]) == 2 and len(r["teams"]["BHA"]["average_positions_match"]["in_possession"]) == 10)
r = call("list_plays", type="corners", team="Brighton")
check("'corners' are set pieces, 'Brighton' is BHA", r["plays"] and all(p["type"] == "set_piece" and p["team"] == "BHA" for p in r["plays"]), r["plays"][:2])
r = call("list_plays", key_only=True)
check("key plays only", r["plays"] and all(p["key"] for p in r["plays"]))
r = call("get_play", type="our last goal")
check("'our last goal' takes one call", r["type"] == "goal" and r["picked"].startswith("the latest goal") and r["players"], r)
try:
    call("list_plays", period=7)
    check("a half that doesn't exist is a tool error", False)
except agent.ToolError:
    check("a half that doesn't exist is a tool error", True)
r = call("get_set_pieces")
check("set pieces: both teams' deliveries, first contacts, xG, our routines", len(r["teams"]) == 2 and r["teams"]["BHA"]["by_routine"]
      and r["set_pieces"], r["teams"])
check("…and small", len(json.dumps(r)) < 8000, len(json.dumps(r)))
r = call("get_set_pieces", period="season")
check("the season's set pieces, ours and theirs", r["ours"]["deliveries"] and "theirs" in r and r["matches"], r["ours"])
r = call("get_running")
check("running: ours by default, every player, and the biggest drops against their first 30'", r["team"] == "BHA"
      and len(r["players"]) >= 11 and r["biggest_drops"], r["players"][:2])
check("…and small", len(json.dumps(r)) < 8000, len(json.dumps(r)))
r = call("get_running", team=match.away if match.ours == "home" else match.home)
check("theirs on request", r["team"] != "BHA", r["team"])
r = call("get_running")
check("running less than usual: the three biggest drops, as sentences, not one per player",
      0 < len(r["biggest_drops"]) <= 3 and "less than in his first 30'" in r["biggest_drops"][0]
      and not any("drop_last_15_vs_first_30_pct" in p for p in r["players"]), r["biggest_drops"])
check("'all', 'latest' and the like mean the current match",
      call("get_game_plan", opponent="all", target="press")["match"]["match_id"] == match.match_id
      and call("get_match_summary", match_id="latest")["match"]["match_id"] == match.match_id)
check("'press' finds the press's targets", {t["name"] for t in call("get_game_plan", target="press")["targets"]}
      >= {"Press intensity (PPDA)"})
try:
    call("get_play", match_id="latest")
    check("a play without its id or type is a tool error", False)
except agent.ToolError as e:
    check("a play without its id or type is a tool error, asking for the type", "type" in str(e), e)
try:
    call("get_match_summary", match_id="EPL-20990101-BHA-XXX")
    check("a made-up match is a tool error", False)
except agent.ToolError as e:
    check("a made-up match is a tool error, saying how to ask instead", "leave match_id out" in str(e), e)
r = call("get_season_summary", period="2026-10-20")
check("a period the service doesn't know is the season, said so", r["period"] == "season" and "whole season" in r["note"], r.get("note"))
r = call("get_season_summary")
check("the season: the record (xG as totals) and each match", r["record"]["played"] == 1 and "xg_for_total" in r["record"]
      and r["matches"][0]["opponent_name"] and r["targets_by_matches"], r["record"])
opponent = match.away if match.home == simulate.CLUB else match.home
name = simulate.CLUBS[opponent]["name"]
r = call("get_match_summary", opponent=name)
check("a match by its opponent's name", r["match"]["match_id"] == match.match_id, r["match"])
check("…or code", call("get_game_plan", opponent=opponent)["match"]["match_id"] == match.match_id)
try:
    call("get_running", opponent="Nobody FC")
    check("an opponent not played is a tool error", False)
except agent.ToolError as e:
    check("an opponent not played is a tool error, naming those we've played", name in str(e), e)
r = call("get_briefing")
check("the briefing: options already worded, running drops, what's going well, the analyst's calls",
      {"options", "running_drops", "going_well", "analyst_calls"} <= set(r)
      and all(isinstance(o, str) and "the plan's" in o and (" below " in o or " above " in o or " not " in o) for o in r["options"]), r)
check("…an option carries the plan's own instruction when one speaks to it",
      local_tools.instruction_for("line_height_m", ["Back four step up together on every back pass."]) is not None
      and local_tools.instruction_for("ppda", ["Back four step up."]) is None)
check("…and small", len(json.dumps(r)) < 4000, len(json.dumps(r)))
coach = local_tools.follow_ups("How did our set pieces go?", ["get_set_pieces"], "The person asking uses the dashboard's viewer view.")
check("follow-ups: from the tool used, naming the opponent", coach and any(name in q for q in coach) and len(coach) <= 3, coach)
analyst = local_tools.follow_ups("How did our set pieces go?", ["get_set_pieces"], "The person asking uses the dashboard's power user view.")
check("…the analysts' first in their views", analyst[0] in local_tools.NEXT_ANALYST["get_set_pieces"], analyst)
check("…never the question just asked, none without a tool",
      "What should we tell the players?" not in local_tools.follow_ups("what should we tell the players", ["get_running"], "")
      and local_tools.follow_ups("hi", [], "") == [])
prompt = local_tools.SYSTEM_PROMPT
check("the prompt is the project's own, without the sensor tools", local_tools.BASE_TOOLS is False and "sensor" not in prompt)
check("the prompt keeps it to measured numbers and shirt numbers, options not decisions",
      "Never guess a name" in prompt and "never work out your own" in prompt and "Options, never decisions" in prompt
      and "No tables, no headings, no LaTeX" in prompt)
server.shutdown()
sys.exit(1 if failed else 0)
