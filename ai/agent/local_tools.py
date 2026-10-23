"""
brighton-match-analysis tools for the agent (agent.py loads this file at startup).

The match's numbers come from the ingest service in ../iot (its agent API),
which computes them from what the club's match device measured every 5
minutes and from the staff's game plan. The model only picks the tool, the
match, the play or the period; it never counts anything itself. Every
sentence that describes a play or a deviation (summary, what_to_check) is
built by the ingest service from what the device measured, so the model
repeats it rather than inventing one. Each result says whether the data is
synthetic (the demo season).

The prompt is this file's alone (SYSTEM_PROMPT), and agent.py's sensor tools
are left out (BASE_TOOLS): the match device's health is get_device_status.
A match can be named by its opponent ("against Chelsea"), found here among
the season's. get_briefing gathers what the staff could talk about (the
targets off the plan with the plan's own instruction for each, the running
drops, what's going well, the analyst's notes), so the model phrases options
and decides nothing. follow_ups gives the dashboard the questions to offer
next, by the tool just used and the view asking.

    INGEST_URL         the ingest service (default http://ingest:8080)
    INGEST_READ_TOKEN  its read token (iot/.env)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

INGEST_URL = os.environ.get("INGEST_URL", "http://ingest:8080").rstrip("/")
READ_TOKEN = os.environ.get("INGEST_READ_TOKEN", "")

# The whole system prompt: this assistant is about football, not sensors (agent.py's own prompt and its
# sensor tools are left out, BASE_TOOLS below), short and in lists, for a small model on a CPU
SYSTEM_PROMPT = """\
You are the match assistant of the coaching staff of Brighton & Hove Albion \
(BHA, Premier League). The club's match device measures every match every 5 \
minutes against the staff's game plan: the press (PPDA, high regains, the \
counter-press), the shape (formations, line height, width, team length), \
set pieces (deliveries, routines, first contacts, xG) and every player's \
running. A deviation opens when a target is off the plan over the last 15 \
minutes; the analyst flags it for the bench or calls it expected.

The latest question starts with a <context> block the system adds: the \
time, which dashboard view is asking, the system's status. Use it, never \
mention it.

Who is asking:
- The viewer view is the coaching staff, on a tablet. Answer in 2 to 4 \
short bullets, in football language: the score and minute if the match is \
on, then what matters, with the key numbers only. No ids, no tool or field \
names.
- The power user and admin views are the analysts: the same, plus the \
numbers behind it, the minutes and play ids where they help.
- No view given: answer as for the coaching staff.

Always call a tool before answering about a match, the plan, a player, a \
play or the season. Questions are about the match being played unless they \
name a match, an opponent ("against Chelsea": opponent) or a period. Never \
ask which match. Pick the tool:
- how we are doing, the score, what's happening: get_match_summary
- the plan, a target, what's off plan, the line: get_game_plan
- the press, PPDA, the counter-press: get_game_plan with target press
- what the analyst flagged, the deviations, the calls, what's still to \
call: get_match_summary (its flagged_for_the_bench and still_to_call)
- what to tell the players, what to change, half time, a team talk, who \
could come off: get_briefing
- shape, formation, positions: get_formation
- a list of plays: list_plays; one play: get_play with its type ("our last \
goal": type goal)
- set pieces, corners, throws, first contacts: get_set_pieces (period for \
the season, opponent for one club's against us)
- running, sprints, fatigue, who is tiring: get_running (every_player for \
every player's numbers)
- a tool's "answer" or "note": say it first
- the season, a month, several matches, which targets slip: \
get_season_summary (period season unless another is named)
- the match device, its cameras or tracking: get_device_status

How to answer:
- Repeat the tools' numbers exactly, with their units; never work out your \
own. Write minutes as the tools do (38', 45+2').
- Players are shirt numbers, with the name when a tool gives one (#9 Kai \
Mensah; the demo's names are made up). Never guess a name. "We", "us" and \
"our" are Brighton.
- Plain text for a chat bubble: short bullets starting with "-", bold for \
the one thing that matters most. A list: the three that matter most, \
unless asked for all.
- Say what was measured; don't turn it into orders ("keep", "maintain", \
"push"). No tables, no headings, no LaTeX: write \
≤ and ≥ as they are.
- When the data is from the demo season (synthetic_data true), end with \
"(Demo data.)" once.
- Answer what was asked, then stop: never offer to look anything else up \
(the app suggests the next questions).

Options, never decisions. Asked what to do, what to say or who could come \
off: give get_briefing's options as the tool words them, as options for the \
staff to weigh (with the running drops and the analyst's calls if they \
matter). \
Never tell the staff what to do, never pick a substitute, never judge a \
player or a coach: a running drop is a reading to set beside what the staff \
see, not a verdict.

Answer in the language of the question."""
# Only this file's tools: no sensors to list here (the match device's health is get_device_status)
BASE_TOOLS = False

MATCH = {"type": "string", "description": "A match id a tool gave you. Omit for the match being played (or the latest)."}
OPPONENT = {"type": "string", "description": "The opponent, for our latest match against them (a club code or name, e.g. CHE or Chelsea)."}
TYPES = ("goal", "shot", "save", "set_piece", "high_regain", "counter_attack", "build_up", "press_broken", "foul",
         "formation_change")
TYPE_ALIASES = {
    "goals": "goal", "shots": "shot", "chance": "shot", "chances": "shot", "saves": "save", "keeper": "save",
    "fouls": "foul", "free_kick_won": "foul", "long_throw": "set_piece", "throw": "set_piece", "throws": "set_piece",
    "corner": "set_piece", "corners": "set_piece", "free_kick": "set_piece", "set_pieces": "set_piece", "setpiece": "set_piece",
    "regain": "high_regain", "regains": "high_regain", "press": "high_regain", "turnover": "high_regain",
    "counter": "counter_attack", "counters": "counter_attack", "counterattack": "counter_attack", "transition": "counter_attack",
    "buildup": "build_up", "goal_kick": "build_up", "build": "build_up",
    "press_broken": "press_broken", "line_break": "press_broken", "broken_press": "press_broken",
    "formation": "formation_change", "shape": "formation_change", "shape_change": "formation_change",
}
PERIOD = {
    "type": "string",
    "description": "season (default), today, yesterday, this_week, last_week, this_month, last_month, 7d, 30d, or a weekday (saturday).",
}


def definition(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    parameters = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


SUMMARY = definition(
    "get_match_summary",
    "One match: teams, score, status and minute; both teams' numbers (possession, passes, PPDA, shots, xG, entries, "
    "regains, line height, width, the shapes read); the game plan's targets with their status over the match and the "
    "last 15'; the deviations and the analyst's calls; the key plays.",
    {"match_id": MATCH, "opponent": OPPONENT},
)
PLAN = definition(
    "get_game_plan",
    "The game plan of a match, target by target: what it asks, the value and status (on plan, drifting, off plan) over "
    "the match so far, each half and the last 15', what the metric is, and every 5-minute window's status. Also the "
    "plan's title, instructions and opponent notes, and the deviations with the analyst's calls.",
    {"match_id": MATCH, "opponent": OPPONENT,
     "target": {"type": "string", "description": "Optional: one target's name or metric (e.g. PPDA, line height, width)."}},
)
FORMATION = definition(
    "get_formation",
    "The shapes a match: per team, the formation read in and out of possession over the match, each half and the last "
    "15', the average position of each shirt number (depth_m from its own goal, across_m from its left touchline), and "
    "the formation changes the device found.",
    {"match_id": MATCH, "opponent": OPPONENT, "team": {"type": "string", "description": "A club code (BHA or the opponent's). Omit for both."}},
)
PLAYS = definition(
    "list_plays",
    "A match's technical plays in minute order, each with play_id, minute, type, team and a one-line summary. Filter by "
    "type, team (club code), half, or key plays only. Not for explaining one play: use get_play.",
    {
        "match_id": MATCH,
        "opponent": OPPONENT,
        "type": {"type": "string", "description": "goal, shot, save, set_piece, high_regain, counter_attack, build_up, press_broken, foul or formation_change."},
        "team": {"type": "string", "description": "A club code, e.g. BHA."},
        "period": {"type": "integer", "description": "1 or 2."},
        "key_only": {"type": "boolean", "description": "Only the key plays (goals, big chances, set pieces and regains that led to a shot, shape changes)."},
    },
)
PLAY = definition(
    "get_play",
    "Explain one play, or the last play of a type in the match being played: what the device measured, the players by "
    "shirt number and role, and its details. Give play_id, or only type (and team) for the latest of that type.",
    {"play_id": {"type": "string", "description": "A play id a tool gave you."},
     "type": {"type": "string", "description": "goal, shot, set_piece, high_regain, counter_attack, build_up, press_broken or formation_change."},
     "team": {"type": "string", "description": "A club code, e.g. BHA."},
     "match_id": MATCH, "opponent": OPPONENT},
)
SEASON = definition(
    "get_season_summary",
    "Our matches in a period: each one's opponent, venue, result, xG for and against, possession, PPDA, line height, "
    "the share of the plan met and the deviations; the record (won, drawn, lost, points, goals, xG); and per target, in "
    "how many matches it was on plan, drifting or off plan.",
    {"period": PERIOD},
)
SET_PIECES = definition(
    "get_set_pieces",
    "Set pieces delivered into the box (corners, free kicks within 35 m, long throws): per team the deliveries, the "
    "share of first contacts won, shots, goals and xG, by routine and by kind; each one with its minute, routine and "
    "outcome. For one match (match_id, default the one being played), or the season (period, and opponent for those "
    "against one club): ours and theirs.",
    {"match_id": MATCH, "period": PERIOD, "opponent": {"type": "string", "description": "A club code, for the season against one club."}},
)
RUNNING = definition(
    "get_running",
    "Every player's running in a match: minutes, distance, metres a minute, high-speed running (over 19.8 km/h), "
    "sprints (over 25.2 km/h) and top speed, and for ours, the drop in metres a minute over the last 15' against "
    "his first 30' (the first sign of fatigue). Players by shirt number.",
    {"match_id": MATCH, "opponent": OPPONENT, "team": {"type": "string", "description": "A club code (BHA or the opponent's). Omit for ours."},
     "every_player": {"type": "boolean", "description": "Every player's numbers (distance, sprints, top speed), not just the biggest drops."}},
)
BRIEFING = definition(
    "get_briefing",
    "What the staff could talk about in a match (a team talk, half time, what to change, who is tiring): the targets "
    "off the plan or drifting over the last 15', key ones first, each with the match so far and the plan's own "
    "instruction for it; the players whose running dropped most; the key targets going well; the analyst's notes.",
    {"match_id": MATCH, "opponent": OPPONENT},
)
NODES = definition(
    "get_device_status",
    "The club's match device: online or not, seconds since its last heartbeat, and its latest temperatures, tracking FPS, "
    "players tracked, ball visibility and latency.",
    {},
)

LABELS = {
    "get_match_summary": {"en": "the match", "es": "el partido"},
    "get_game_plan": {"en": "the game plan", "es": "el plan de juego"},
    "get_formation": {"en": "the shapes", "es": "las formaciones"},
    "list_plays": {"en": "the plays", "es": "las jugadas"},
    "get_play": {"en": "the play", "es": "la jugada"},
    "get_season_summary": {"en": "the season", "es": "la temporada"},
    "get_set_pieces": {"en": "the set pieces", "es": "el balón parado"},
    "get_running": {"en": "the running", "es": "el físico"},
    "get_briefing": {"en": "the talking points", "es": "los puntos a tratar"},
    "get_device_status": {"en": "the match device", "es": "el equipo de análisis"},
}

# What staff call clubs, beyond a word of their name that only one club has
SHORT_NAMES = {"spurs": "TOT", "man city": "MCI", "man united": "MUN", "man utd": "MUN", "manu": "MUN", "the bees": "BRE",
               "the toffees": "EVE", "the gunners": "ARS", "the reds": "LIV", "the blues": "CHE"}

# What a small model puts in match_id or opponent to mean "the one being played" (or nothing at all)
NOT_A_MATCH = {"", "all", "any", "latest", "last", "current", "this", "now", "live", "today", "none", "null", "us", "we",
               "our", "bha", "brighton", "the match", "this match", "n/a", "na", "unknown", "opponent", "them", "they",
               "the opponent", "-"}

# The plan's instruction that speaks to a target, found by its words (the staff write instructions in their own)
INSTRUCTION_WORDS = {
    "ppda": ("press",), "regains_high": ("press",), "counterpress_pct": ("lose it", "five seconds"),
    "line_height_m": ("step up", "line"), "length_m": ("step up", "compact"), "width_m": ("width", "wide"),
    "entries_left_pct": ("left",), "entries_right_pct": ("right",), "possession_pct": ("possession",),
    "build_up_short_pct": ("build",), "set_piece_first_contact_pct": ("corner", "throw"),
    "set_piece_defence_pct": ("their corner", "mark"), "formation_out_of_possession": ("back four", "4-4-2"),
    "formation_in_possession": ("invert", "3-2-5"),
}


def label(player: dict) -> str:
    """A player as the tools say him: "#7 Tomas Calloway" (the demo's made-up names), or "#7"."""
    return f"#{player['number']} {player['name']}" if player.get("name") else f"#{player['number']}"


def instruction_for(metric: str, instructions: list[str]) -> str | None:
    words = INSTRUCTION_WORDS.get(metric, ())
    return next((i for i in instructions if any(w in i.lower() for w in words)), None)


# The match the last answer was about (its opponent), for the follow-up questions
LAST: dict[str, str] = {}

# What to ask next, by the tool the answer came from: the coaching staff's questions, and the analysts' on top.
# {opponent} is the match's; a question naming it is left out when the match isn't known.
NEXT = {
    "get_match_summary": ["What should we tell the players?", "How is our press working?", "Who is running less than usual?"],
    "get_game_plan": ["What should we tell the players?", "Who is running less than usual?", "How did our set pieces go?"],
    "get_briefing": ["Who is running less than usual?", "How is our press working?", "How did our set pieces go?"],
    "get_running": ["What should we tell the players?", "How is {opponent}'s running?", "Are we following the game plan?"],
    "get_set_pieces": ["What do {opponent} do at set pieces against us?", "What happened at our last corner?",
                       "How did our set pieces go this season?"],
    "get_formation": ["Are we following the game plan?", "How is our press working?"],
    "list_plays": ["What happened at our last goal?", "How did our set pieces go?"],
    "get_play": ["What should we tell the players?", "How are we doing?"],
    "get_season_summary": ["Which targets slip most this season?", "How did our set pieces go this season?",
                           "How did we do against {opponent}?"],
    "get_device_status": ["How are we doing?"],
}
NEXT_ANALYST = {
    "get_match_summary": ["Which deviations are still to call?"],
    "get_game_plan": ["Show both teams' shapes"],
    "get_briefing": ["List this match's key plays"],
    "get_running": ["Show every player's distance, sprints and top speed"],
    "get_set_pieces": ["List this match's set pieces"],
    "list_plays": ["Explain the last high regain"],
}


def follow_ups(question: str, tools: list[str], context: str) -> list[str]:
    """Up to three questions to ask next: by the last tool used, the analysts' first in their views, never the one just asked."""
    opponent = LAST.pop("opponent", None)  # this answer's match only, never the one before
    if not tools:
        return []
    last = tools[-1]
    analyst = any(v in context.lower() for v in ("power user view", "admin view"))
    asked = question.strip().lower().rstrip("?")
    picks = []
    for q in (NEXT_ANALYST.get(last, []) if analyst else []) + NEXT.get(last, []):
        if "{opponent}" in q:
            if not opponent:
                continue
            q = q.format(opponent=opponent)
        if q.lower().rstrip("?") != asked and q not in picks:
            picks.append(q)
    return picks[:3]


def tools(agent):
    def get(path: str, params: dict) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        request = urllib.request.Request(f"{INGEST_URL}{path}" + (f"?{query}" if query else ""),
                                         headers={"Authorization": f"Bearer {READ_TOKEN}"} if READ_TOKEN else {})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            try:
                message = json.load(e).get("error", "")
            except ValueError:
                message = ""
            if e.code == 400:
                # A small model invents ids: say how to ask instead
                if message.startswith("no match"):
                    message += ": leave match_id out for the match being played, or give opponent for our latest against a club"
                elif message.startswith("no play"):
                    message += ": leave play_id out and give type (e.g. goal) for the latest of that type"
                raise agent.ToolError(message or "bad arguments") from None
            raise RuntimeError(f"ingest service: HTTP {e.code} {message}") from None

    def play_type(value: object) -> str | None:
        """A type id from the id, a common word for it, or a phrase holding one ("our last corner")."""
        if value in (None, ""):
            return None
        key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        if key in TYPES or key in TYPE_ALIASES:
            return TYPE_ALIASES.get(key, key)
        for word in sorted([*TYPES, *TYPE_ALIASES], key=len, reverse=True):
            if word in key:
                return TYPE_ALIASES.get(word, word)
        return key

    def team(value: object) -> str | None:
        if value in (None, ""):
            return None
        code = str(value).strip().upper()
        return {"BRIGHTON": "BHA", "US": "BHA", "WE": "BHA", "ALBION": "BHA", "SEAGULLS": "BHA", "THE SEAGULLS": "BHA",
                "BRIGHTON & HOVE ALBION": "BHA", "BRIGHTON AND HOVE ALBION": "BHA"}.get(code, code)

    league: dict[str, str] = {}

    def club(value: object) -> str | None:
        """A club code from its code, name or a nickname ("Chelsea", "Spurs", "Man City")."""
        code = team(value)
        if not code or (len(code) == 3 and code.isalpha()):
            return code  # a club code already (or none)
        if not league:
            try:
                clubs = get("/analysis/league.json", {}).get("clubs", {})
            except Exception:  # no league: the value as it is, which the ingest service will refuse if it's wrong
                return code
            words: dict[str, set[str]] = {}
            for c, info in clubs.items():
                name = info["name"].lower()
                league[name] = league[c.lower()] = c
                for w in name.replace("&", " ").split():
                    words.setdefault(w, set()).add(c)
            # A word of one club's name only ("chelsea", "villa", "forest"), and the usual short names
            league.update({w: next(iter(cs)) for w, cs in words.items() if len(cs) == 1 and len(w) > 3})
            league.update(SHORT_NAMES)
        name = str(value).strip().lower()
        return league.get(name, code)

    def known_club(value: object) -> str | None:
        """[club], refused when it's no club the league has (a made-up code would just find nothing)."""
        code = club(value)
        if code and league and code not in league.values():
            clubs = ", ".join(sorted({c for c in league.values() if len(c) == 3}))
            raise agent.ToolError(f"no club {value!r}; the clubs are {clubs}")
        return code

    def match_of(args: dict) -> str | None:
        """The match a call means: its match_id, else our latest against its opponent, else None (the current one)."""
        if str(args.get("match_id") or "").strip().lower() not in NOT_A_MATCH:
            return args["match_id"]
        if str(args.get("opponent") or "").strip().lower() in NOT_A_MATCH:
            return None
        code, said = club(args["opponent"]), str(args["opponent"]).strip().lower()
        matches = get("/api/v1/agent/season", {"period": "season"}).get("matches", [])
        # By its code, else by its name as the season has it ("Forest" is Nottingham Forest)
        against = ([m for m in matches if m["opponent"] == code]
                   or [m for m in matches if len(said) > 3 and said in m["opponent_name"].lower()])
        if not against:
            played = ", ".join(dict.fromkeys(f"{m['opponent_name']} ({m['opponent']})" for m in matches))
            raise agent.ToolError(f"no match against {args['opponent']} this season; we've played {played}")
        return against[-1]["match_id"]

    CALLS = {"pending_review": "not called yet", "confirmed": "flagged for the bench", "dismissed": "expected"}

    def deviation(d: dict) -> dict:
        """A deviation as the staff say it: what, when, the analyst's call and note, whether it's still off."""
        return {"minute": d["minute"], "title": d["title"], "call": CALLS.get(d["status"], d["status"]),
                **({"by": d["reviewed_by"]} if d.get("reviewed_by") else {}), **({"note": d["note"]} if d.get("note") else {}),
                "still_off_plan": d["open"]}

    def to_call(deviations: list[dict]) -> list[str] | str:
        """The deviations the analyst hasn't called yet, as the staff say them."""
        waiting = [f"{d['minute']}' {d['title']}" for d in deviations if d["status"] == "pending_review"]
        return waiting or "none: the analyst has called every deviation"

    def compact(match: dict) -> dict:
        """A match as the model needs it: who, where, when, the score and the state."""
        LAST["opponent"] = match["opponent"]["name"]
        score = f"{match['home']['code']} {match['score']['home']}-{match['score']['away']} {match['away']['code']}"
        return {"match_id": match["match_id"], "competition": match["competition"], "venue": match.get("venue"),
                "kickoff": match["kickoff"], "us": match["us"]["code"], "opponent": f"{match['opponent']['code']} ({match['opponent']['name']})",
                "at_home": match["at_home"], "score": score, "status": match["status"], "minute": match.get("minute"),
                "result": match.get("result")}

    KEEP = ("possession_pct", "passes", "pass_accuracy_pct", "ppda", "field_tilt_pct", "shots", "shots_on_target", "xg", "goals",
            "final_third_entries", "entries_pct", "regains_high", "counterpress_pct", "corners", "line_height_m", "width_m",
            "length_m", "shape_in_possession", "shape_out_of_possession")

    def get_match_summary(args: dict) -> dict:
        r = get("/api/v1/agent/match", {"match_id": match_of(args)})
        numbers = {side: {k: v.get(k) for k in KEEP} for side, v in (r.get("numbers") or {}).items()}
        return {"match": compact(r["match"]), "numbers": numbers or None, "game_plan": r.get("game_plan"),
                "deviations": [deviation(d) for d in r["deviations"][-10:]],
                "flagged_for_the_bench": [f"{d['minute']}' {d['title']}" + (f" ({d['note']})" if d.get("note") else "")
                                          for d in r["deviations"] if d["status"] == "confirmed"] or "nothing yet",
                "still_to_call": to_call(r["deviations"]),
                "key_plays": [{k: p[k] for k in ("minute", "type", "team", "summary")} for p in r["key_plays"][-12:]],
                "synthetic_data": r["synthetic_data"]}

    def get_game_plan(args: dict) -> dict:
        result = get("/api/v1/agent/plan", {"match_id": match_of(args)})
        wanted = str(args.get("target") or "").strip().lower()
        targets = []
        for t in result.get("targets", []):
            if wanted and wanted not in t["name"].lower() and wanted not in t["metric"]:
                continue
            # Every window as one short line, so a small model's context holds the whole plan
            windows = " ".join(f"{w['label']}{ {'on_plan': '✓', 'drifting': '~', 'off_plan': '✕'}.get(w['status'], '–') }"
                               for w in t.pop("windows", []))
            targets.append({**{k: t[k] for k in ("name", "target", "key")}, **({"description": t["description"]} if wanted else {}),
                            **{scope: f"{t[scope]['text']} ({t[scope]['status'].replace('_', ' ')})"
                               for scope in ("match", "first_half", "second_half", "last_15")},
                            "every_5_minutes": windows})
        if wanted and not targets:
            return {**get_game_plan({**args, "target": None}), "note": f"the plan has no target like {wanted!r}: here are all its targets"}
        plan = result.get("plan") or {}
        summary = get("/api/v1/agent/match", {"match_id": result["match"]["match_id"]})
        return {"match": compact(result["match"]), "plan": {k: plan.get(k) for k in ("title", "formation", "instructions", "opponent_notes", "inherited")}
                if plan else None, "on_plan_share": result.get("on_plan_share"), "targets": targets,
                "deviations": [deviation(d) for d in summary.get("deviations", [])],
                "still_to_call": to_call(summary.get("deviations", [])),
                "synthetic_data": summary.get("synthetic_data")}

    def get_formation(args: dict) -> dict:
        result = get("/api/v1/agent/formation", {"match_id": match_of(args)})
        code = club(args.get("team"))
        teams = result["teams"]
        if code and code not in teams:
            raise agent.ToolError(f"team must be one of {', '.join(teams)}")
        out = {}
        for c, t in teams.items():
            if code and c != code:
                continue
            out[c] = {"ours": t["ours"], "changes": t["changes"],
                      **{scope: {phase: t[scope][phase]["formation"] for phase in ("in_possession", "out_of_possession")}
                         for scope in ("match", "first_half", "second_half", "last_15")},
                      "average_positions_match": {phase: t["match"][phase]["positions"] for phase in ("in_possession", "out_of_possession")}}
        return {"match": compact(result["match"]), "teams": out, "synthetic_data": result["synthetic_data"]}

    def list_plays(args: dict) -> dict:
        period = args.get("period")
        if period not in (None, "") and str(period) not in ("1", "2", "3", "4"):
            raise agent.ToolError("period must be 1 or 2")
        result = get("/api/v1/agent/plays", {
            "match_id": match_of(args), "type": play_type(args.get("type")), "team": club(args.get("team")),
            "period": period, "key": "true" if args.get("key_only") else None, "limit": 40})
        for p in result.get("plays", []):
            for k in ("details", "camera_id", "time", "match_time_s", "side"):
                p.pop(k, None)
        result["plays_shown"] = len(result.get("plays", []))
        return result

    def get_play(args: dict) -> dict:
        play_id = str(args.get("play_id") or "").strip()
        if play_id:
            return get("/api/v1/agent/play", {"play_id": play_id})
        kind = play_type(args.get("type"))
        if not kind:
            raise agent.ToolError("give play_id, or type (e.g. goal, shot, set_piece) for the latest play of that type")
        found = get("/api/v1/agent/plays", {"match_id": match_of(args), "type": kind, "team": club(args.get("team")), "limit": 100})
        what = f"{kind} play" if kind else "play"
        if not found.get("plays"):
            raise agent.ToolError(f"match {found.get('match_id')} has no {what} yet")
        result = get("/api/v1/agent/play", {"play_id": found["plays"][-1]["play_id"]})
        result["picked"] = f"the latest {what} of {found.get('match_id')}"
        return result

    def get_season_summary(args: dict) -> dict:
        period, note = args.get("period") or "season", None
        try:
            r = get("/api/v1/agent/season", {"period": period})
        except agent.ToolError:
            # A date or a word the service doesn't know: the season, rather than nothing to say
            r, note = get("/api/v1/agent/season", {"period": "season"}), f"no period {period!r}: the whole season"
        record = dict(r["record"])
        record["xg_for_total"], record["xg_against_total"] = record.pop("xg_for", None), record.pop("xg_against", None)
        keep = ("match_id", "opponent_name", "venue", "score", "result", "status", "xg", "xg_against", "ppda", "plan_on_share")
        # Which targets slip most: the most matches off the plan or drifting, first
        slips = sorted(((name, c) for name, c in (r.get("plan_by_metric") or {}).items()),
                       key=lambda x: -(x[1].get("off_plan", 0) * 2 + x[1].get("drifting", 0)))
        return {"period": r["period"]["name"], **({"note": note} if note else {}), "record": record,
                "matches": [{k: m.get(k) for k in keep} for m in r["matches"][-10:]],
                "targets_by_matches": {name: c for name, c in slips},
                "synthetic_data": r["synthetic_data"]}

    def get_set_pieces(args: dict) -> dict:
        if args.get("period") or args.get("opponent"):
            r = get("/api/v1/agent/set_pieces", {"period": args.get("period") or "season", "opponent": known_club(args.get("opponent"))})
            trim = lambda t: {k: v for k, v in t.items() if k not in ("by_delivery",)}  # noqa: E731
            return {"period": r["period"], "opponent": r.get("opponent"), "ours": trim(r["for"]), "theirs": trim(r["against"]),
                    "matches": r["matches"][-10:]}
        r = get("/api/v1/agent/set_pieces", {"match_id": args.get("match_id")})
        return {"match": compact(r["match"]),
                "teams": {t["code"]: {k: t[k] for k in ("ours", "deliveries", "first_contact_pct", "shots", "goals", "xg", "by_routine")}
                          for t in r["teams"].values()},
                "set_pieces": [{"minute": p["minute"], "team": p["team"], "kind": p["kind"], "routine": p["routine"],
                                "first_contact": p["first_contact"], "outcome": p["outcome"], "xg": p["xg"]} for p in r["set_pieces"][-16:]],
                "synthetic_data": r["synthetic_data"]}

    def get_running(args: dict) -> dict:
        r = get("/api/v1/agent/physical", {"match_id": match_of(args)})
        code = club(args.get("team"))
        teams = {t["code"]: t for t in r["teams"].values()}
        if code and code not in teams:
            raise agent.ToolError(f"team must be one of {', '.join(teams)}")
        t = teams[code] if code else next(t for t in teams.values() if t["ours"])
        players = [{"number": p["number"], **({"name": p["name"]} if p.get("name") else {}), "minutes": p["minutes"], "km": p["distance_km"], "m_per_min": p["m_per_min"],
                    "high_speed_m": p["hsr_m"], "sprints": p["sprints"], "top_kmh": p["top_speed_kmh"],
                    **({"keeper": True} if p["keeper"] else {}),
                    **({"drop_last_15_vs_first_30_pct": p["drop_last_15"]["pct"]} if p.get("drop_last_15") else {})}
                   for p in t["players"]]
        # Who is running less than usual: the three biggest drops against their own first 30', as sentences (a small
        # model lists every player's drop when it has them one by one)
        drops = sorted(((p.pop("drop_last_15_vs_first_30_pct", None), label(p)) for p in players), key=lambda d: -(d[0] or -1))
        biggest = [f"{n} is running {pct}% less than in his first 30'" + ("" if pct >= 10 else " (under the 10% the staff watch)")
                   for pct, n in drops[:3] if pct is not None]
        if not biggest and not args.get("every_player"):
            # Nothing to read yet: no players to talk about, or a small model reads minutes as running less
            return {"match": compact(r["match"]), "team": t["code"],
                    "answer": "Too early to say who is running less than usual: it compares each player's last 15' with "
                              "his own first 30', from about 45'.", "synthetic_data": r["synthetic_data"]}
        return {"match": compact(r["match"]), "team": t["code"], "distance_km": t["distance_km"], "high_speed_m": t["hsr_m"],
                "sprints": t["sprints"], "biggest_drops": biggest,
                **({"players": players} if args.get("every_player") or len(players) <= 18 else {}),
                "synthetic_data": r["synthetic_data"]}

    def get_briefing(args: dict) -> dict:
        m = get("/api/v1/agent/match", {"match_id": match_of(args)})
        p = get("/api/v1/agent/plan", {"match_id": m["match"]["match_id"]})
        instructions = (p.get("plan") or {}).get("instructions") or []
        options, going_well = [], []
        for t in p.get("targets", []):
            last, whole = t["last_15"], t["match"]
            if last["status"] in ("off_plan", "drifting") or whole["status"] == "off_plan":
                said = instruction_for(t["metric"], instructions)
                # Already an option, in the staff's terms: what was measured, against the plan, and its own instruction
                side = {">=": "below", "<=": "above"}.get(t.get("op"), "not")  # which side of the plan, in words
                options.append({"key": t["key"], "off": last["status"] == "off_plan",
                                "option": f"{t['name']}: {last['text']} over the last 15', {side} the plan's {t['target']}"
                                          f" ({last['status'].replace('_', ' ')}; {whole['text']} over the match)"
                                          + (f". The plan says: {said}" if said else "")})
            elif t["key"] and last["status"] == "on_plan":
                going_well.append(f"{t['name']}: {last['text']} over the last 15' (plan {t['target']})")
        # Key targets first, then what's off the plan before what's drifting
        options.sort(key=lambda o: (not o["key"], not o["off"]))
        tiring = [f"{label(d)} is running {d['drop_pct']}% less than in his first 30' ({d['m_per_min_now']} m a minute "
                  f"against {d['m_per_min_first_30']})" for d in m.get("running_drops_last_15") or [] if d["drop_pct"] >= 10]
        calls = [f"{d['minute']}' {d['title']}: {CALLS.get(d['status'], d['status'])}" + (f" ({d['note']})" if d.get("note") else "")
                 for d in m.get("deviations", []) if d["status"] != "pending_review"]
        return {"match": compact(m["match"]), "options": [o["option"] for o in options[:5]], "running_drops": tiring[:3],
                "going_well": going_well[:3], "analyst_calls": calls[-4:], "synthetic_data": m["synthetic_data"]}

    def get_device_status(args: dict) -> dict:
        return get("/api/v1/agent/nodes", {})

    return [
        (SUMMARY, get_match_summary),
        (PLAN, get_game_plan),
        (FORMATION, get_formation),
        (PLAYS, list_plays),
        (PLAY, get_play),
        (SET_PIECES, get_set_pieces),
        (RUNNING, get_running),
        (BRIEFING, get_briefing),
        (SEASON, get_season_summary),
        (NODES, get_device_status),
    ]
