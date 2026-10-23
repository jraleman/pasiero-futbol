"""
The game plan, as rules on what the club device measured.

The staff write a plan per match in the plan editor (or the club's
default plan, which a match without its own uses): the formation in and
out of possession, and targets on what the device measures, each a metric,
>= or <=, a value, and whether it is a key instruction. This module says
what each metric is, works it out over any stretch of windows, and rules
whether that stretch was on the plan:

    on_plan    the target is met
    drifting   within the metric's tolerance of it
    off_plan   beyond that
    no_data    not enough of the match to say (e.g. no goal kicks yet)

The device measures, this rules, and the staff decide: a deviation the
ingest service opens (store.py) is for an analyst to flag to the bench or
set aside as expected, never a verdict of its own.
"""

from __future__ import annotations

from collections import Counter

from contract import SHAPE_NAME

# metric: (name, unit, which way is better, default op, tolerance, what it is, what to check when it's off)
METRICS = {
    "possession_pct": ("Possession", "%", "higher", ">=", 4.0,
                       "Our share of the time the ball was in play.",
                       "Check the build-up: the options for the holding midfielder, and how quickly we give it back."),
    "pass_accuracy_pct": ("Pass completion", "%", "higher", ">=", 3.0,
                          "Completed passes over all passes.",
                          "Check the risk in our passing: forward passes into pressure, and long balls."),
    "ppda": ("Press intensity (PPDA)", "", "lower", "<=", 2.0,
             "Their passes in their own 60% of the pitch for each defensive action of ours there. Lower is a harder press.",
             "Check who starts the press and the cover behind them: their centre-backs have time on the ball."),
    "line_height_m": ("Defensive line height", "m", "higher", ">=", 3.0,
                      "Out of possession, the average distance of our four deepest outfield players from our goal line.",
                      "Check the trigger to step up after a back or sideways pass, and the gap to the midfield line."),
    "width_m": ("Width in possession", "m", "higher", ">=", 3.0,
                "In possession, the distance between our widest outfield players.",
                "Check the wide players' starting positions: they are coming inside and narrowing the pitch."),
    "length_m": ("Team length out of possession", "m", "lower", "<=", 3.0,
                 "Out of possession, from our deepest to our highest outfield player. Lower is more compact.",
                 "Check the distance between the lines: the team is stretched out of possession."),
    "field_tilt_pct": ("Field tilt", "%", "higher", ">=", 5.0,
                       "Our share of the completed passes into or inside either final third.",
                       "Check our territory: too much of the game is in our half."),
    "counterpress_pct": ("Counter-press regains", "%", "higher", ">=", 5.0,
                         "Of the times we lost the ball, how often we won it back within 5 seconds.",
                         "Check the reaction in the first five seconds after losing it: the nearest two or three players."),
    "high_regains_p90": ("High regains per 90", "", "higher", ">=", 1.5,
                         "Times we won the ball within 40 m of their goal, per 90 minutes.",
                         "Check the press triggers in their half: we are not winning it up there."),
    "entries_left_pct": ("Final-third entries down the left", "%", "higher", ">=", 6.0,
                         "Of our passes and carries into the final third, the share down our left channel.",
                         "Check where we attack: the plan targets our left channel, and we are going elsewhere."),
    "entries_centre_pct": ("Final-third entries through the middle", "%", "higher", ">=", 6.0,
                           "Of our passes and carries into the final third, the share through the middle.",
                           "Check where we attack: the plan targets the middle, and we are going round."),
    "entries_right_pct": ("Final-third entries down the right", "%", "higher", ">=", 6.0,
                          "Of our passes and carries into the final third, the share down our right channel.",
                          "Check where we attack: the plan targets our right channel, and we are going elsewhere."),
    "build_up_short_pct": ("Goal kicks played short", "%", "higher", ">=", 10.0,
                           "Of our goal kicks, the share played short (under 32 m).",
                           "Check the goal-kick routine: the keeper is going long."),
    "shots_conceded_p90": ("Shots conceded per 90", "", "lower", "<=", 2.0,
                           "Their shots, per 90 minutes.",
                           "Check the protection in front of the back line, and our rest defence when we attack."),
    "xg_for_p90": ("Our xG per 90", "", "higher", ">=", 0.3,
                   "The expected goals of our shots, per 90 minutes.",
                   "Check the final-third entries and how many we get into the box."),
    "xg_against_p90": ("xG conceded per 90", "", "lower", "<=", 0.3,
                       "The expected goals of their shots, per 90 minutes.",
                       "Check the protection in front of the back line, and our rest defence when we attack."),
    "set_piece_first_contact_pct": ("Set pieces: first contact won", "%", "higher", ">=", 10.0,
                                    "Of our deliveries into the box (corners, free kicks within 35 m, long throws), "
                                    "the share where we won the first contact.",
                                    "Check the routines: the runners' timing against their markers, and the "
                                    "delivery's height and pace into the target zone."),
    "set_piece_defence_pct": ("Defending set pieces: first contact", "%", "higher", ">=", 10.0,
                              "Of their deliveries into our box, the share where we won the first contact (or "
                              "nobody did).",
                              "Check the marking at their set pieces: the zonal line, and who is on their best "
                              "header."),
    "set_piece_xg_p90": ("Set-piece xG per 90", "", "higher", ">=", 0.15,
                         "The expected goals of our shots in the 8 seconds after a set piece, per 90 minutes.",
                         "Check the deliveries: are they reaching the target zone, and is anyone attacking the "
                         "second ball?"),
    "set_piece_xg_against_p90": ("Set-piece xG conceded per 90", "", "lower", "<=", 0.15,
                                 "The expected goals of their shots in the 8 seconds after a set piece, per 90 "
                                 "minutes.",
                                 "Check the marking at their set pieces, and the second balls around our box."),
    "hsr_p90": ("High-speed running per 90", "m", "higher", ">=", 400.0,
                "All our players' running over 5.5 m/s (19.8 km/h), per 90 minutes.",
                "Check the intensity: the runs in behind and the recovery runs are dropping off."),
    "sprints_p90": ("Sprints per 90", "", "higher", ">=", 15.0,
                    "All our players' efforts over 7 m/s (25.2 km/h), per 90 minutes.",
                    "Check the intensity: fewer runs in behind and fewer recovery sprints."),
    "load_drop_pct": ("Running drop (any player)", "%", "lower", "<=", 3.0,
                      "The biggest drop in ground covered per minute among our outfield players: each one's last "
                      "15' against his own first 30'. A player on for less than two thirds of the stretch, or "
                      "for less than 20 of the first 30', doesn't count.",
                      "Check the player it names: he is covering less ground than he did, the first sign of "
                      "fatigue. A substitution, or a role with less ground to cover."),
}
# The two shape "targets": the formation the device reads, against the plan's
SHAPES = {
    "formation_in_possession": ("Shape in possession", "in_possession",
                                "Check the shape against the plan in possession: who has moved, and whether it's deliberate."),
    "formation_out_of_possession": ("Shape out of possession", "out_of_possession",
                                    "Check the shape against the plan out of possession: who has moved, and whether it's deliberate."),
}
OPS = (">=", "<=")
# Fewer than this many of a ratio's events, and a stretch says nothing about it
MINIMUM = {"build_up_short_pct": 2, "counterpress_pct": 6, "ppda": 3, "pass_accuracy_pct": 20}
ENTRY_MIN = 4
# Set-piece metrics say nothing on fewer than this many deliveries (theirs, for the defending ones)
SET_PIECE_MIN = 4
# A player's running baseline: the first 30 minutes, of which he must have played 20
BASELINE_S, BASELINE_MIN_S = 1800, 1200


class PlanError(ValueError):
    pass


def check(doc: object) -> dict:
    """A plan as the staff send it, checked and tidied; raises PlanError."""
    if not isinstance(doc, dict):
        raise PlanError("a plan must be a JSON object")
    title = str(doc.get("title") or "").strip()[:120]
    shapes = doc.get("formation") or {}
    if not isinstance(shapes, dict):
        raise PlanError("formation must be {in_possession, out_of_possession}")
    formation = {}
    for phase in ("in_possession", "out_of_possession"):
        value = shapes.get(phase)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not SHAPE_NAME.match(value):
            raise PlanError(f"formation.{phase} must be a formation such as 4-4-2")
        formation[phase] = value
    targets, seen = [], set()
    for t in doc.get("targets") or []:
        if not isinstance(t, dict) or t.get("metric") not in METRICS:
            raise PlanError(f"each target needs a metric, one of {', '.join(METRICS)}")
        if t["metric"] in seen:
            raise PlanError(f"{t['metric']} is in the plan twice")
        seen.add(t["metric"])
        op = t.get("op") or METRICS[t["metric"]][3]
        if op not in OPS:
            raise PlanError("op must be >= or <=")
        value = t.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -1000 <= value <= 1000:
            raise PlanError(f"{t['metric']}: value must be a number")
        tolerance = t.get("tolerance")
        if tolerance is not None and (isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0):
            raise PlanError(f"{t['metric']}: tolerance must be a number, 0 or more")
        targets.append({"metric": t["metric"], "op": op, "value": float(value), "key": bool(t.get("key")),
                        **({"tolerance": float(tolerance)} if tolerance is not None else {})})
    if len(targets) > 16:
        raise PlanError("at most 16 targets")
    instructions = [str(i).strip()[:200] for i in doc.get("instructions") or [] if str(i).strip()][:10]
    notes = str(doc.get("opponent_notes") or "").strip()[:1000]
    return {"title": title, "formation": formation, "targets": targets, "instructions": instructions,
            "opponent_notes": notes}


# ── working the metrics out ───────────────────────────────────────────────────


def _sum(windows: list[dict], side: str, key: str) -> float:
    return sum(w["teams"][side][key] for w in windows)


def _shape_avg(windows: list[dict], side: str, phase: str, key: str) -> float | None:
    weighted = [(w["teams"][side][phase][key], w["teams"][side][phase]["frames"]) for w in windows
                if w["teams"][side][phase].get(key) is not None and w["teams"][side][phase]["frames"]]
    frames = sum(f for _, f in weighted)
    return sum(v * f for v, f in weighted) / frames if frames else None


def minutes(windows: list[dict]) -> float:
    return sum(w["end_s"] - w["start_s"] for w in windows) / 60


def running(windows: list[dict], side: str) -> dict[int, list[float]]:
    """Per shirt number, [seconds, distance m] over some windows (the side's goalkeepers left out)."""
    out: dict[int, list[float]] = {}
    for w in windows:
        t = w["teams"][side]
        keepers = set(t.get("keepers") or ())
        for number, seconds, distance, *_ in t.get("physical") or []:
            if number not in keepers:
                a = out.setdefault(number, [0.0, 0.0])
                a[0] += seconds
                a[1] += distance
    return out


def load_drops(windows: list[dict], ours: str, context: list[dict] | None) -> list[tuple[int, float, float, float]]:
    """
    Each of our outfield players' drop in ground covered per minute over some windows against his own first
    30 minutes of the match (context: all its windows): [(number, drop %, m/min first 30', m/min now)], the
    biggest drop first. Players on for less than two thirds of the windows, or 20 of the first 30', are left out.
    """
    if not windows or not context:
        return []
    base = running([w for w in context if w["period"] == 1 and w["start_s"] < BASELINE_S], ours)
    now = running(windows, ours)
    length = sum(w["end_s"] - w["start_s"] for w in windows)
    out = []
    for number, (seconds, distance) in now.items():
        b = base.get(number)
        if not b or b[0] < BASELINE_MIN_S or seconds < 2 / 3 * length or not b[1]:
            continue
        before, after = 60 * b[1] / b[0], 60 * distance / seconds
        out.append((number, round(100 * (1 - after / before), 1), round(before), round(after)))
    return sorted(out, key=lambda r: -r[1])


def value(metric: str, windows: list[dict], ours: str, context: list[dict] | None = None) -> float | None:
    """
    A metric over some windows, for our side (ours: home or away), or None when there's too little to say.
    context is the match's windows so far, for the metrics that compare a stretch with its start.
    """
    if not windows:
        return None
    them = "away" if ours == "home" else "home"
    s = lambda key, side=ours: _sum(windows, side, key)  # noqa: E731
    per90 = 90 / minutes(windows) if minutes(windows) >= 5 else None
    if metric == "possession_pct":
        total = s("possession_s") + s("possession_s", them)
        return 100 * s("possession_s") / total if total else None
    if metric == "pass_accuracy_pct":
        return 100 * s("passes_completed") / s("passes") if s("passes") >= MINIMUM[metric] else None
    if metric == "ppda":
        return s("opp_passes_own60") / s("def_actions_high") if s("def_actions_high") >= MINIMUM[metric] else (
            None if s("opp_passes_own60") < 10 else float(s("opp_passes_own60")))
    if metric == "line_height_m":
        return _shape_avg(windows, ours, "out_of_possession", "line_height_m")
    if metric == "width_m":
        return _shape_avg(windows, ours, "in_possession", "width_m")
    if metric == "length_m":
        return _shape_avg(windows, ours, "out_of_possession", "length_m")
    if metric == "field_tilt_pct":
        total = s("passes_final_third") + s("passes_final_third", them)
        return 100 * s("passes_final_third") / total if total >= 5 else None
    if metric == "counterpress_pct":
        return 100 * s("counterpress_regains") / s("turnovers") if s("turnovers") >= MINIMUM[metric] else None
    if metric == "high_regains_p90":
        return s("regains_high") * per90 if per90 else None
    if metric.startswith("entries_"):
        lane = metric.split("_")[1]
        counts = Counter()
        for w in windows:
            counts.update(w["teams"][ours]["final_third_entries"])
        total = sum(counts.values())
        return 100 * counts[lane] / total if total >= ENTRY_MIN else None
    if metric == "build_up_short_pct":
        return 100 * s("goal_kicks_short") / s("goal_kicks") if s("goal_kicks") >= MINIMUM[metric] else None
    if metric == "shots_conceded_p90":
        return s("shots", them) * per90 if per90 else None
    if metric == "xg_for_p90":
        return s("xg") * per90 if per90 else None
    if metric == "xg_against_p90":
        return s("xg", them) * per90 if per90 else None
    if metric == "set_piece_first_contact_pct":
        return 100 * s("set_piece_first_contacts") / s("set_pieces") if s("set_pieces") >= SET_PIECE_MIN else None
    if metric == "set_piece_defence_pct":
        theirs = s("set_pieces", them)
        return 100 * (1 - s("set_piece_first_contacts", them) / theirs) if theirs >= SET_PIECE_MIN else None
    if metric == "set_piece_xg_p90":
        return s("set_piece_xg") * per90 if per90 and s("set_pieces") >= SET_PIECE_MIN else None
    if metric == "set_piece_xg_against_p90":
        return s("set_piece_xg", them) * per90 if per90 and s("set_pieces", them) >= SET_PIECE_MIN else None
    if metric == "hsr_p90":
        return s("hsr_m") * per90 if per90 else None
    if metric == "sprints_p90":
        return s("sprints") * per90 if per90 else None
    if metric == "load_drop_pct":
        drops = load_drops(windows, ours, context)
        return max(0.0, drops[0][1]) if drops else None
    raise KeyError(metric)


def shape(windows: list[dict], side: str, phase: str) -> str | None:
    """The formation a side stood in for most of some windows (weighted by the frames of that phase)."""
    seen = Counter()
    for w in windows:
        p = w["teams"][side][phase]
        if p.get("formation"):
            seen[p["formation"]] += p["frames"]
    return seen.most_common(1)[0][0] if seen else None


def shape_share(windows: list[dict], side: str, phase: str, wanted: str) -> float | None:
    frames = [(w["teams"][side][phase].get("formation"), w["teams"][side][phase]["frames"]) for w in windows]
    total = sum(f for name, f in frames if name)
    return 100 * sum(f for name, f in frames if name == wanted) / total if total else None


def status(target: dict, measured: float | None) -> str:
    if measured is None:
        return "no_data"
    goal = target["value"]
    tolerance = target.get("tolerance", METRICS[target["metric"]][4])
    if target["op"] == ">=":
        return "on_plan" if measured >= goal else "drifting" if measured >= goal - tolerance else "off_plan"
    return "on_plan" if measured <= goal else "drifting" if measured <= goal + tolerance else "off_plan"


def shape_status(wanted: str | None, read: str | None) -> str:
    if not wanted or not read:
        return "no_data"
    return "on_plan" if read == wanted else "off_plan"


def checks(plan: dict) -> list[dict]:
    """
    Everything a plan asks, as one list: its targets, then its formations
    as targets of their own ({"metric": "formation_out_of_possession", "shape": "4-4-2"}).
    """
    out = [dict(t) for t in plan.get("targets", [])]
    for metric, (_, phase, _) in SHAPES.items():
        if plan.get("formation", {}).get(phase):
            out.append({"metric": metric, "shape": plan["formation"][phase], "key": False})
    return out


def evaluate(target: dict, windows: list[dict], ours: str, context: list[dict] | None = None) -> tuple[object, str]:
    """(what the windows measured, its status) for one of checks()'s targets; context: the match's windows."""
    if target["metric"] in SHAPES:
        phase = SHAPES[target["metric"]][1]
        read = shape(windows, ours, phase)
        return read, shape_status(target["shape"], read)
    measured = value(target["metric"], windows, ours, context)
    if measured is None:
        return None, "no_data"
    # Ruled at the precision people read it, so 51.9% never shows as "52%, drifting" against 52%
    shown = round(measured, digits(target["metric"]))
    return round(measured, max(1, digits(target["metric"]))), status(target, shown)


def digits(metric: str) -> int:
    return 2 if "xg" in metric else 0 if unit(metric) == "%" or metric == "hsr_p90" else 1


def name(metric: str) -> str:
    return SHAPES[metric][0] if metric in SHAPES else METRICS[metric][0]


def unit(metric: str) -> str:
    return "" if metric in SHAPES else METRICS[metric][1]


def hint(metric: str) -> str:
    return SHAPES[metric][2] if metric in SHAPES else METRICS[metric][6]


def fmt(metric: str, v: object) -> str:
    """A metric's value as people read it: 34.2 m, 55%, 8.4."""
    if v is None:
        return "–"
    if metric in SHAPES or isinstance(v, str):
        return str(v)
    u = unit(metric)
    text = f"{v:.{digits(metric)}f}"
    return f"{text}%" if u == "%" else f"{text} {u}" if u else text


def target_text(target: dict) -> str:
    """The plan's ask in words: ≥ 40 m, ≤ 9, 4-4-2."""
    if target["metric"] in SHAPES:
        return target["shape"]
    return f"{'≥' if target['op'] == '>=' else '≤'} {fmt(target['metric'], target['value'])}"


def catalog() -> dict:
    """What the plan editor offers."""
    return {
        "metrics": [{"metric": k, "name": v[0], "unit": v[1], "better": v[2], "op": v[3], "tolerance": v[4],
                     "description": v[5]} for k, v in METRICS.items()],
        "formations": FORMATION_NAMES,
    }


# The formations the device reads (analytics.FORMATIONS, in the device's code)
FORMATION_NAMES = ["4-4-2", "4-3-3", "4-2-3-1", "4-1-4-1", "3-5-2", "3-4-3", "5-3-2", "5-4-1", "3-2-5", "2-3-5"]
