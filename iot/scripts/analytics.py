"""
The club device's match analytics: tracking frames and on-ball events in,
contract 0.1 team windows and technical plays out.

A match device (a tactical camera high on the main stand, or a tracking
provider's feed) gives two things: where every player and the ball are,
several times a second, and what happened on the ball (passes, shots,
tackles, restarts). This module turns them into what the staff read:

  - Windows: every 5 minutes of each half, per team, the team data
    (possession, passes, PPDA's two halves, final-third entries by channel,
    shots and xG, regains, set pieces and what came of them) and the shape
    (defensive line height, width and length, average positions and the
    formation they make, in and out of possession), the pass network, and
    every player's running (distance, high-speed running, sprints, top
    speed).
  - Plays: the technical plays worth a look, each with its key moment and
    the seconds around it for the replay: goals and shots, set pieces (the
    delivery, its swing and routine, who won the first contact, the xG that
    came of it), high regains, counter-attacks, build-ups from goal kicks,
    presses played through, fouls won near goal, and formation changes.

It measures; it never judges. Whether a number is on the game plan is the
ingest service's rule (iot/ingest/plan.py), not the device's.

The demo simulator (simulate.py) goes through this module, as a club's
device would with its own tracking and event feeds. Standard library only.

Coordinates are metres on a 105 × 68 m pitch, x along the length from the
left goal line as the main camera sees it, y across from the far touchline.
Internally each team is measured attacking right ("normalised"): x is then
the distance from its own goal line, and a small y is its left.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict

LENGTH, WIDTH = 105.0, 68.0
WINDOW_S = 300
# Periods: 1 and 2 (45 min), 3 and 4 (extra time, 15 min)
PERIOD_LENGTH = {1: 2700, 2: 2700, 3: 900, 4: 900}
FINAL_THIRD = 70.0
OWN_60 = 63.0                 # a team's own 60% of the pitch, for PPDA
HIGH_REGAIN = 65.0            # within 40 m of the opponent's goal
COUNTERPRESS_S = 5.0
SETTLE_S = 5.0                # shape is read from this long after the ball changes hands
BOX = (88.5, 13.84, 54.16)    # the opponent's penalty area, normalised: x ≥ 88.5, 13.84 ≤ y ≤ 54.16
TRACK_HZ = 5
PLAY_BEFORE, PLAY_AFTER = 6.0, 4.0
# Running: speeds from one position a second; over 5.5 m/s is high-speed running, over 7 m/s a sprint, and over
# 10 m/s the tracker jumped (a player swapped for another, a lost track), not a run
HSR_MS, SPRINT_MS, JUMP_MS = 5.5, 7.0, 10.0
# Set pieces: a delivery into the box from a corner, a free kick within 35 m of goal, or a long throw in the final
# third; what follows it for SET_PIECE_S (a shot, the ball cleared), and the first contact within FIRST_CONTACT_S
SET_PIECE_X, SET_PIECE_S, FIRST_CONTACT_S, LONG_THROW_M = 70.0, 8.0, 4.0, 15.0
# Pass quality: a pass's difficulty, 0 to 1, from its length, how far forward it goes, how close the nearest opponent
# is to where it's received (pressure), and whether it reaches the final third; HARD_PASS and over is a hard one (about one pass in five)
HARD_PASS = 0.6
# Ball speed: the ball tracker's reading on the event when the device sends one (speed_ms), else measured from the
# ball's track in the moments after it's struck; a step faster than BALL_JUMP_MS is the tracker losing the ball
BALL_JUMP_MS, BALL_FLIGHT_S = 45.0, 0.8

# On-ball events a device sends. Possession goes to the team of the last one
ON_BALL = {"pass", "carry", "shot", "tackle", "interception", "recovery", "clearance",
           "corner", "free_kick", "throw_in", "goal_kick", "kick_off"}
# What counts as touching the ball first after a set piece's delivery
CONTACT = ON_BALL | {"tackle", "interception", "block", "header", "save"}
# Ways of winning the ball (defensive actions, for PPDA)
DEFENSIVE = {"tackle", "interception", "foul", "block"}
RESTARTS = {"corner", "free_kick", "throw_in", "goal_kick", "kick_off"}

# Formations, as the lines a team's ten outfield players stand in: depth 0
# is the deepest line, 1 the highest; width 0 is the team's left, 1 its right
FORMATIONS = {
    "4-4-2": [(0, .1), (0, .37), (0, .63), (0, .9), (.5, .1), (.5, .37), (.5, .63), (.5, .9), (1, .37), (1, .63)],
    "4-3-3": [(0, .1), (0, .37), (0, .63), (0, .9), (.5, .25), (.45, .5), (.5, .75), (1, .08), (1, .5), (1, .92)],
    "4-2-3-1": [(0, .1), (0, .37), (0, .63), (0, .9), (.35, .37), (.35, .63), (.72, .1), (.72, .5), (.72, .9), (1, .5)],
    "4-1-4-1": [(0, .1), (0, .37), (0, .63), (0, .9), (.28, .5), (.62, .08), (.62, .37), (.62, .63), (.62, .92), (1, .5)],
    "3-5-2": [(0, .22), (0, .5), (0, .78), (.5, .02), (.45, .3), (.4, .5), (.45, .7), (.5, .98), (1, .37), (1, .63)],
    "3-4-3": [(0, .22), (0, .5), (0, .78), (.5, .05), (.45, .37), (.45, .63), (.5, .95), (1, .12), (1, .5), (1, .88)],
    "5-3-2": [(0, .02), (0, .26), (0, .5), (0, .74), (0, .98), (.5, .25), (.5, .5), (.5, .75), (1, .37), (1, .63)],
    "5-4-1": [(0, .02), (0, .26), (0, .5), (0, .74), (0, .98), (.55, .1), (.55, .37), (.55, .63), (.55, .9), (1, .5)],
    "3-2-5": [(0, .2), (0, .5), (0, .8), (.38, .37), (.38, .63), (1, .02), (.92, .28), (1, .5), (.92, .72), (1, .98)],
    "2-3-5": [(0, .3), (0, .7), (.4, .12), (.35, .5), (.4, .88), (1, .02), (.92, .28), (1, .5), (.92, .72), (1, .98)],
}
# Depth counts more than width: lines are what a formation is
DEPTH_WEIGHT = 1.6


# ── geometry ──────────────────────────────────────────────────────────────────


def attacks_right(home_attacks: str, period: int, team: str) -> bool:
    """Whether `team` attacks towards x = 105 in `period` (ends change every period)."""
    home_right = (home_attacks == "right") == (period % 2 == 1)
    return home_right if team == "home" else not home_right


def norm(x: float, y: float, right: bool) -> tuple[float, float]:
    """Pitch → the team's frame (attacking right)."""
    return (x, y) if right else (LENGTH - x, WIDTH - y)


def xg(x: float, y: float, header: bool = False) -> float:
    """
    A simple expected-goals model on the shot's place (normalised): the
    angle the goal mouth makes, and the distance to its centre. About 0.33
    from the penalty spot, 0.05 from 25 m, 0.7 from 6 m; a header 60% of it.
    """
    dx, dy = LENGTH - x, abs(y - WIDTH / 2)
    dist = math.hypot(dx, dy)
    a = math.atan2(7.32 * dx, dx * dx + dy * dy - 3.66 ** 2) if dx > 0 else 0.0
    a = a if a >= 0 else a + math.pi
    value = 1 / (1 + math.exp(-(-0.9 + 2.2 * a - 0.11 * dist)))
    return round(value * (0.6 if header else 1.0), 3)


def channel(y: float) -> str:
    """The third of the width a normalised y is in, from the attacking team's view."""
    return "left" if y < WIDTH / 3 else "right" if y > 2 * WIDTH / 3 else "centre"


def in_box(x: float, y: float) -> bool:
    return x >= BOX[0] and BOX[1] <= y <= BOX[2]


# ── formations ────────────────────────────────────────────────────────────────


def hungarian(cost: list[list[float]]) -> list[int]:
    """The assignment of rows to columns with the least total cost (square matrix): column for each row."""
    n = len(cost)
    u, v, p, way = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [math.inf] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    result = [0] * n
    for j in range(1, n + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


def formation(positions: list[tuple[float, float]]) -> tuple[str | None, float]:
    """
    The formation ten outfield players' average positions (normalised) make,
    and how far they are from it (0 is the template exactly). Each position
    is scaled to the team's own depth and width, then matched one to one to
    each template's places; the template with the least distance wins.
    """
    if len(positions) != 10:
        return None, 1.0
    xs, ys = [p[0] for p in positions], [p[1] for p in positions]
    span_x, span_y = max(xs) - min(xs) or 1.0, max(ys) - min(ys) or 1.0
    shape = [((x - min(xs)) / span_x, (y - min(ys)) / span_y) for x, y in positions]
    best, best_cost = None, math.inf
    for name, template in FORMATIONS.items():
        cost = [[DEPTH_WEIGHT * (d - td) ** 2 + (w - tw) ** 2 for td, tw in template] for d, w in shape]
        assign = hungarian(cost)
        total = sum(cost[i][assign[i]] for i in range(10)) / 10
        if total < best_cost:
            best, best_cost = name, total
    return best, round(best_cost, 4)


# ── the match ─────────────────────────────────────────────────────────────────


class Match:
    """
    One match's tracking and events, ready to measure.

    frames:  [(period, t, ball (x, y) or None, [(team, number, x, y), ...]), ...]
             t in seconds since the period's kick-off, at a steady rate (hz)
    events:  [{"period", "t", "team", "number", "kind", "x", "y", "end": (x, y) | None,
               "success": bool, "to": number | None, "outcome"?, "header"?, "speed_ms"?}, ...] by time
             kind is one of ON_BALL, DEFENSIVE, or "goal" (the shot's outcome, a goal)
    keepers: {"home": {numbers}, "away": {numbers}} (goalkeepers are left out of team shape)
    home_attacks: "right" or "left", in the first period
    """

    def __init__(self, frames, events, keepers, home_attacks: str = "right", hz: float = TRACK_HZ,
                 periods: dict[int, float] | None = None) -> None:
        self.frames, self.events, self.keepers = frames, sorted(events, key=lambda e: (e["period"], e["t"])), keepers
        self.home_attacks, self.hz = home_attacks, hz
        # How long each period lasted (stoppage time included): the last frame
        self.periods = periods or {}
        for period, t, *_ in frames:
            self.periods[period] = max(self.periods.get(period, 0.0), t)
        self.by_period: dict[int, list] = defaultdict(list)
        for f in frames:
            self.by_period[f[0]].append(f)
        self._times: dict[int, list[float]] = {}
        self.possession = self._possession()
        self._starts = {p: [s[0] for s in spells] for p, spells in self.possession.items()}
        self._changes = self._find_changes()

    def right(self, period: int, team: str) -> bool:
        return attacks_right(self.home_attacks, period, team)

    def n(self, e: dict, team: str | None = None) -> tuple[float, float]:
        return norm(e["x"], e["y"], self.right(e["period"], team or e["team"]))

    # ── possession ────────────────────────────────────────────────────────────

    def _possession(self) -> dict[int, list[tuple[float, str]]]:
        """Per period, [(from t, team in possession)]: the team of the last on-ball event."""
        spells: dict[int, list[tuple[float, str]]] = defaultdict(list)
        for e in self.events:
            team = e["team"]
            if e["kind"] in ("tackle", "interception") and not e.get("success", True):
                continue
            if e["kind"] in ON_BALL or e["kind"] in ("tackle", "interception"):
                spell = spells[e["period"]]
                if not spell or spell[-1][1] != team:
                    spell.append((e["t"], team))
        return spells

    def holder(self, period: int, t: float) -> str | None:
        i = bisect.bisect_right(self._starts.get(period, []), t) - 1
        return self.possession[period][i][1] if i >= 0 else None

    def since(self, period: int, t: float) -> float:
        """When the spell of possession going on at t began."""
        i = bisect.bisect_right(self._starts.get(period, []), t) - 1
        return self._starts[period][i] if i >= 0 else 0.0

    def changes(self) -> list[dict]:
        return self._changes

    def _find_changes(self) -> list[dict]:
        """Every change of possession: {"period", "t", "team" (who won it), "x", "y" (pitch)}."""
        out, last = [], {}
        for e in self.events:
            if e["kind"] not in ON_BALL and e["kind"] not in ("tackle", "interception"):
                continue
            if e["kind"] in ("tackle", "interception") and not e.get("success", True):
                continue
            prev = last.get(e["period"])
            if prev and prev != e["team"] and e["kind"] not in RESTARTS:
                out.append({"period": e["period"], "t": e["t"], "team": e["team"], "x": e["x"], "y": e["y"],
                            "kind": e["kind"], "number": e.get("number")})
            last[e["period"]] = e["team"]
        return out

    # ── windows ───────────────────────────────────────────────────────────────

    def windows(self, upto: tuple[int, float] | None = None) -> list[dict]:
        """
        Every 5-minute window so far: [{"period", "start_s", "end_s", "final", "teams": {home, away}}].
        The last window of a period runs to its end (stoppage time). upto: (period, t) of a match
        still being played; the window it falls in is "final": false.
        """
        out = []
        for period in sorted(self.periods):
            length = PERIOD_LENGTH.get(period, 2700)
            end_of_period = self.periods[period]
            count = max(1, int(length // WINDOW_S))
            for i in range(count):
                start = i * WINDOW_S
                end = (i + 1) * WINDOW_S if i < count - 1 else max(end_of_period, length)
                final = True
                if upto is not None:
                    if (period, start) >= (upto[0], upto[1]):
                        break
                    if period == upto[0] and upto[1] < end:
                        end, final = upto[1], False
                elif start > end_of_period:
                    break
                out.append({"period": period, "start_s": start, "end_s": round(end, 1), "final": final,
                            "teams": {side: self.measure(side, period, start, end) for side in ("home", "away")}})
                if not final:
                    return out
        return out

    def measure(self, team: str, period: int, lo: float, hi: float) -> dict:
        other = "away" if team == "home" else "home"
        right = self.right(period, team)
        evs = [e for e in self.events if e["period"] == period and lo <= e["t"] < hi]
        mine = [e for e in evs if e["team"] == team]
        theirs = [e for e in evs if e["team"] == other]
        nx = lambda e: norm(e["x"], e["y"], right)  # noqa: E731
        passes = [e for e in mine if e["kind"] == "pass"]
        done = [e for e in passes if e.get("success")]
        their_passes = [e for e in theirs if e["kind"] == "pass"]
        entries = Counter()
        box_entries = final_third = 0
        for e in done:
            if not e.get("end"):
                continue
            sx, _ = nx(e)
            ex, ey = norm(*e["end"], right)
            if ex >= FINAL_THIRD:
                final_third += 1
                if sx < FINAL_THIRD:
                    entries[channel(ey)] += 1
            if in_box(ex, ey) and not in_box(sx, nx(e)[1]):
                box_entries += 1
        for e in mine:
            if e["kind"] == "carry" and e.get("end"):
                sx, _ = nx(e)
                ex, ey = norm(*e["end"], right)
                if sx < FINAL_THIRD <= ex:
                    entries[channel(ey)] += 1
        shots = [e for e in mine if e["kind"] == "shot"]
        defensive = [e for e in mine if e["kind"] in DEFENSIVE and (e["kind"] == "foul" or e.get("success", True))]
        # Their passes in their own 60%, and our defensive actions in the same zone (our x > 42)
        their_right = self.right(period, other)
        own60 = sum(1 for e in their_passes if norm(e["x"], e["y"], their_right)[0] < OWN_60)
        high_def = sum(1 for e in defensive if nx(e)[0] > LENGTH - OWN_60)
        changes = [c for c in self.changes() if c["period"] == period]
        regains = [c for c in changes if c["team"] == team and lo <= c["t"] < hi]
        losses_all = [c for c in changes if c["team"] == other]
        turnovers = [c for c in losses_all if lo <= c["t"] < hi]
        counterpress = 0
        for r in regains:
            lost = [c for c in losses_all if c["t"] <= r["t"]]
            if lost and r["t"] - lost[-1]["t"] <= COUNTERPRESS_S:
                counterpress += 1
        rated = [(e, self.pass_difficulty(e)) for e in passes if e.get("end")]
        pass_speeds = [v for v in (self.ball_speed(e) for e in passes) if v is not None]
        shot_speeds = [v for v in (self.ball_speed(e) for e in shots) if v is not None]
        goal_kicks = [e for e in mine if e["kind"] == "goal_kick"]
        short = sum(1 for e in goal_kicks if e.get("end") and math.dist((e["x"], e["y"]), e["end"]) < 32)
        deliveries = [d for d in self.deliveries() if d["team"] == team and d["period"] == period and lo <= d["t"] < hi]
        running = self.running(team, period, lo, hi)
        pairs = Counter((e["number"], e["to"]) for e in done if e.get("to") is not None and e.get("number") is not None)
        return {
            "possession_s": round(self._held(team, period, lo, hi), 1),
            "passes": len(passes), "passes_completed": len(done),
            "passes_own60": sum(1 for e in passes if nx(e)[0] < OWN_60),
            "passes_final_third": final_third,
            "def_actions": len(defensive), "def_actions_high": high_def,
            "opp_passes_own60": own60,
            "final_third_entries": {k: entries[k] for k in ("left", "centre", "right")},
            "box_entries": box_entries,
            "shots": len(shots),
            "shots_on_target": sum(1 for e in shots if e.get("outcome") in ("goal", "saved")),
            "xg": round(sum(xg(*nx(e), e.get("header", False)) for e in shots), 3),
            "goals": sum(1 for e in shots if e.get("outcome") == "goal"),
            "turnovers": len(turnovers), "regains": len(regains),
            "regains_high": sum(1 for r in regains if norm(r["x"], r["y"], right)[0] >= HIGH_REGAIN),
            "counterpress_regains": counterpress,
            "corners": sum(1 for e in mine if e["kind"] == "corner"),
            "free_kicks": sum(1 for e in mine if e["kind"] == "free_kick" and nx(e)[0] > LENGTH / 2),
            "goal_kicks": len(goal_kicks), "goal_kicks_short": short,
            "set_pieces": len(deliveries), "set_piece_shots": sum(d["shots"] for d in deliveries),
            "set_piece_xg": round(sum(d["xg"] for d in deliveries), 3),
            "set_piece_first_contacts": sum(1 for d in deliveries if d["first_contact"] == "attack"),
            "passes_rated": len(rated), "pass_distance_m": round(sum(math.dist((e["x"], e["y"]), e["end"]) for e, _ in rated)),
            "pass_difficulty": round(sum(d for _, d in rated), 2),
            "passes_hard": sum(1 for _, d in rated if d >= HARD_PASS),
            "passes_hard_completed": sum(1 for e, d in rated if d >= HARD_PASS and e.get("success")),
            "passes_timed": len(pass_speeds), "pass_speed_ms": round(sum(pass_speeds), 1),
            "ball_speed_max_ms": max(pass_speeds + shot_speeds, default=0.0), "shot_speed_max_ms": max(shot_speeds, default=0.0),
            **self.shape(team, period, lo, hi),
            "distance_m": round(sum(p[2] for p in running)),
            "hsr_m": round(sum(p[3] for p in running)), "sprints": sum(p[4] for p in running),
            "physical": running, "keepers": sorted(self.keepers.get(team, ())),
            "pass_pairs": [[a, b, k] for (a, b), k in sorted(pairs.items())],
        }

    def pass_difficulty(self, e: dict) -> float:
        """
        How hard a pass is, 0 to 1: its length (up to 40 m), how far forward it goes (up to 30 m), the pressure where
        it's received (the nearest opponent there when it arrives, within 10 m) and whether it reaches the final third.
        """
        right = self.right(e["period"], e["team"])
        sx, _ = norm(e["x"], e["y"], right)
        ex, _ = norm(*e["end"], right)
        dist = math.dist((e["x"], e["y"]), e["end"])
        arrival = self.frame_at(e["period"], e["t"] + dist / 17)
        nearest = min((math.dist((x, y), e["end"]) for side, _, x, y in arrival[3] if side != e["team"]), default=99.0) \
            if arrival else 99.0
        value = (0.35 * min(dist / 40, 1) + 0.25 * min(max(ex - sx, 0) / 30, 1) + 0.25 * max(0.0, 1 - nearest / 10)
                 + 0.15 * (ex >= FINAL_THIRD))
        return round(value, 2)

    def ball_speed(self, e: dict) -> float | None:
        """
        The ball's speed when a pass or shot is struck, m/s: the device's reading on the event, else the fastest
        step of the ball's track in the BALL_FLIGHT_S after it (none if the track has no ball, or only jumps).
        """
        if e.get("speed_ms") is not None:
            return float(e["speed_ms"])
        frames = [f for f in self.by_period.get(e["period"], []) if e["t"] <= f[1] <= e["t"] + BALL_FLIGHT_S and f[2]]
        steps = [math.dist(a[2], b[2]) / (b[1] - a[1]) for a, b in zip(frames, frames[1:]) if b[1] > a[1]]
        steps = [v for v in steps if v <= BALL_JUMP_MS]
        return round(max(steps), 1) if steps and max(steps) > 0 else None

    def _held(self, team: str, period: int, lo: float, hi: float) -> float:
        spells = self.possession.get(period, [])
        total = 0.0
        for i, (start, who) in enumerate(spells):
            end = spells[i + 1][0] if i + 1 < len(spells) else self.periods.get(period, hi)
            if who == team:
                total += max(0.0, min(end, hi) - max(start, lo))
        return total

    def shape(self, team: str, period: int, lo: float, hi: float) -> dict:
        """Shape in and out of possession: line height, width, length, average positions, formation."""
        right = self.right(period, team)
        keepers = self.keepers.get(team, set())
        phases = {"in_possession": defaultdict(list), "out_of_possession": defaultdict(list)}
        lines = {"in_possession": [], "out_of_possession": []}
        for _, t, _, players in (f for f in self.by_period.get(period, []) if lo <= f[1] < hi):
            holder = self.holder(period, t)
            if holder is None:
                continue
            # The shape is measured in settled phases: a team takes a few seconds to get into it after the ball
            # changes hands (distance counts throughout)
            settled = t - self.since(period, t) >= SETTLE_S
            phase = "in_possession" if holder == team else "out_of_possession"
            outfield = []
            for side, number, x, y in players:
                if side != team:
                    continue
                if number in keepers or not settled:
                    continue
                p = norm(x, y, right)
                phases[phase][number].append(p)
                outfield.append(p)
            if len(outfield) >= 8:
                xs = sorted(p[0] for p in outfield)
                ys = [p[1] for p in outfield]
                lines[phase].append((sum(xs[:4]) / 4, max(ys) - min(ys), xs[-1] - xs[0]))
        out = {}
        for phase, seen in phases.items():
            frames = len(lines[phase])
            # Who was there for most of the phase: the ten outfield players of the moment
            regulars = sorted(seen, key=lambda n: -len(seen[n]))[:10]
            positions = [[n, round(sum(p[0] for p in seen[n]) / len(seen[n]), 1),
                          round(sum(p[1] for p in seen[n]) / len(seen[n]), 1)] for n in sorted(regulars)]
            shape, cost = formation([(x, y) for _, x, y in positions]) if frames >= 10 else (None, 1.0)
            out[phase] = {
                "frames": frames,
                "line_height_m": round(sum(v[0] for v in lines[phase]) / frames, 1) if frames else None,
                "width_m": round(sum(v[1] for v in lines[phase]) / frames, 1) if frames else None,
                "length_m": round(sum(v[2] for v in lines[phase]) / frames, 1) if frames else None,
                "formation": shape, "formation_fit": cost,
                "positions": positions,
            }
        return out

    def running(self, team: str, period: int, lo: float, hi: float) -> list[list]:
        """
        Each of a team's players' running between lo and hi: [number, seconds on the pitch, distance m,
        high-speed running m, sprints, top speed m/s], from one position a second (so the tracker's jitter
        doesn't add up). A step faster than JUMP_MS is the tracker losing a player, and counts for nothing.
        """
        step = max(1, round(self.hz))
        frames = self.by_period.get(period, [])
        if period not in self._times:
            self._times[period] = [f[1] for f in frames]
        start = bisect.bisect_left(self._times[period], lo)
        start -= start % step
        acc: dict[int, list] = {}
        last: dict[int, tuple[float, float, float, bool]] = {}
        for _, t, _, players in frames[start::step]:
            if t >= hi:
                break
            for side, number, x, y in players:
                if side != team or number is None:
                    continue
                a = acc.setdefault(number, [number, 0.0, 0.0, 0.0, 0, 0.0])
                prev = last.get(number)
                last[number] = (t, x, y, False)
                if prev is None or t < lo or not 0 < t - prev[0] <= 1.5:
                    continue
                dt = t - prev[0]
                d = math.dist(prev[1:3], (x, y))
                speed = d / dt
                if speed > JUMP_MS:
                    continue
                a[1] += dt
                a[2] += d
                if speed > HSR_MS:
                    a[3] += d
                sprinting = speed > SPRINT_MS
                if sprinting and not prev[3]:
                    a[4] += 1
                last[number] = (t, x, y, sprinting)
                a[5] = max(a[5], speed)
        return [[n, round(a[1]), round(a[2]), round(a[3]), a[4], round(a[5], 1)] for n, a in sorted(acc.items()) if a[1] > 0]

    # ── set pieces ────────────────────────────────────────────────────────────

    def deliveries(self) -> list[dict]:
        """
        Every set piece delivered into the box: corners, free kicks within 35 m of goal, long throws in the
        final third (sorted by time). Each with where it went, its swing and routine (when the device was told
        or matched them), who won the first contact, and the shots and xG it led to before the ball was lost.
        """
        if hasattr(self, "_deliveries"):
            return self._deliveries
        out = []
        evs = self.events
        for i, e in enumerate(evs):
            if e["kind"] not in ("corner", "free_kick", "throw_in"):
                continue
            x, y = self.n(e)
            end = norm(*e["end"], self.right(e["period"], e["team"])) if e.get("end") else None
            if e["kind"] != "corner" and x < SET_PIECE_X:
                continue
            if e["kind"] == "throw_in" and not (end and in_box(*end) and math.dist((x, y), end) >= LONG_THROW_M):
                continue
            after = [f for f in evs[i + 1:i + 40] if f["period"] == e["period"] and e["t"] < f["t"] <= e["t"] + SET_PIECE_S]
            restart = next((f["t"] for f in after if f["kind"] in RESTARTS), None)
            after = [f for f in after if restart is None or f["t"] < restart]
            first = next((f for f in after if f["kind"] in CONTACT and f["t"] <= e["t"] + FIRST_CONTACT_S), None)
            lost = next((f["t"] for f in after if f["team"] != e["team"] and f["kind"] in ON_BALL | DEFENSIVE
                         and f.get("success", True)), None)
            shots = [f for f in after if f["kind"] == "shot" and f["team"] == e["team"] and (lost is None or f["t"] < lost)]
            value = sum(xg(*self.n(f), f.get("header", False)) for f in shots)
            side = "left" if y < WIDTH / 3 else "right" if y > 2 * WIDTH / 3 else "centre"
            delivery = None
            if end:
                near = (end[1] < WIDTH / 2) == (y < WIDTH / 2)
                delivery = ("short" if math.dist((x, y), end) < 15 else
                            "six_yard" if end[0] >= 99.5 and 24.84 <= end[1] <= 43.16 else
                            "penalty_spot" if 91 <= end[0] < 99.5 and 27 <= end[1] <= 41 else
                            "edge" if end[0] < BOX[0] + 2 else "near_post" if near else "far_post")
            out.append({
                "period": e["period"], "t": e["t"], "team": e["team"], "kind": e["kind"], "number": e.get("number"),
                "x": x, "y": y, "end": end, "side": side, "delivery": delivery,
                "swing": e.get("swing") or ("long_throw" if e["kind"] == "throw_in" else "short" if delivery == "short" else None),
                "routine": e.get("routine"),
                "first_contact": "none" if first is None else "attack" if first["team"] == e["team"] else "defence",
                "first_number": first.get("number") if first else None,
                "shots": len(shots), "xg": round(value, 3),
                "outcome": "goal" if any(f.get("outcome") == "goal" for f in shots) else "shot" if shots
                else "cleared" if lost is not None else "retained",
            })
        self._deliveries = out
        return out

    # ── plays ─────────────────────────────────────────────────────────────────

    def frame_at(self, period: int, t: float) -> tuple | None:
        frames = self.by_period.get(period, [])
        if not frames:
            return None
        lo, hi = 0, len(frames) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if frames[mid][1] < t:
                lo = mid + 1
            else:
                hi = mid
        return frames[lo]

    def scene(self, period: int, t: float) -> tuple[dict | None, dict | None]:
        """(snapshot, track) around a moment, in contract form."""
        key = self.frame_at(period, t)
        if key is None:
            return None, None

        def players(f):
            return [[side, number, round(min(max(x, -5), 110), 1), round(min(max(y, -5), 73), 1)]
                    for side, number, x, y in f[3]]

        def ball(f):
            return [round(min(max(f[2][0], -5), 110), 1), round(min(max(f[2][1], -5), 73), 1)] if f[2] else None

        snapshot = {"home_attacks": "right" if self.right(period, "home") else "left", "ball": ball(key), "players": players(key)}
        frames = [f for f in self.by_period.get(period, []) if t - PLAY_BEFORE <= f[1] <= t + PLAY_AFTER]
        # At most TRACK_HZ frames a second
        step = max(1, round(self.hz / TRACK_HZ))
        frames = frames[::step][:300]
        if not frames:
            return snapshot, None
        track = {"hz": round(self.hz / step, 2), "t0": round(frames[0][1] - t, 2),
                 "frames": [{"ball": ball(f), "players": players(f)} for f in frames]}
        return snapshot, track

    def plays(self, windows: list[dict] | None = None) -> list[dict]:
        """
        The technical plays: [{"period", "match_time_s", "type", "team", "players", "details",
        "snapshot", "track", "confidence"}], in match order.
        """
        out = []
        evs = self.events
        changes = self.changes()

        def later(e, seconds, test):
            return [x for x in evs if x["period"] == e["period"] and e["t"] < x["t"] <= e["t"] + seconds and test(x)]

        def add(e_period, t, kind, team, players, details, confidence=0.9):
            snapshot, track = self.scene(e_period, t)
            out.append({"period": e_period, "match_time_s": round(t, 1), "type": kind, "team": team,
                        "players": players, "details": details, "snapshot": snapshot, "track": track,
                        "confidence": confidence})

        for i, e in enumerate(evs):
            # Goals and shots
            if e["kind"] == "shot":
                x, y = self.n(e)
                value = xg(x, y, e.get("header", False))
                before = [p for p in evs[max(0, i - 6):i] if p["period"] == e["period"] and e["t"] - p["t"] <= 12]
                assist = next((p for p in reversed(before) if p["kind"] in ("pass", "corner", "free_kick")
                               and p["team"] == e["team"] and p.get("success") and p.get("to") == e.get("number")), None)
                players = [{"team": e["team"], "number": e.get("number"), "role": "shooter"}]
                struck = self.ball_speed(e)
                speed = {"speed_kmh": round(struck * 3.6, 1)} if struck is not None else {}
                if assist and assist.get("number") is not None:
                    players.append({"team": e["team"], "number": assist["number"], "role": "assist"})
                if e.get("outcome") == "goal":
                    pattern = "open_play"
                    won = next((c for c in reversed(changes) if c["team"] == e["team"] and c["period"] == e["period"]
                                and 0 <= e["t"] - c["t"] <= 15), None)
                    won_x = norm(won["x"], won["y"], self.right(won["period"], won["team"]))[0] if won else None
                    if any(d["team"] == e["team"] and d["period"] == e["period"] and 0 <= e["t"] - d["t"] <= SET_PIECE_S
                           for d in self.deliveries()) or any(p["kind"] in ("corner", "free_kick") and p["team"] == e["team"]
                                                              for p in before):
                        pattern = "set_piece"
                    elif won_x is not None and won_x >= HIGH_REGAIN:
                        pattern = "high_press"
                    elif won_x is not None and won_x < LENGTH / 2:
                        pattern = "counter"
                    add(e["period"], e["t"], "goal", e["team"], players,
                        {"xg": value, "pattern": pattern, "body": "head" if e.get("header") else "foot", **speed}, 0.98)
                elif value >= 0.08:
                    add(e["period"], e["t"], "shot", e["team"], players,
                        {"xg": value, "outcome": e.get("outcome") or "off_target", "body": "head" if e.get("header") else "foot",
                         **speed}, 0.9)
            # Fouls won within 35 m of goal: a free kick to deliver, or a penalty
            elif e["kind"] == "foul" and e.get("on") is not None:
                won = "away" if e["team"] == "home" else "home"
                x, _ = norm(e["x"], e["y"], self.right(e["period"], won))
                if x >= SET_PIECE_X:
                    add(e["period"], e["t"], "foul", won,
                        [{"team": won, "number": e["on"], "role": "fouled"},
                         *([{"team": e["team"], "number": e["number"], "role": "fouler"}] if e.get("number") is not None else [])],
                        {"area": "box" if in_box(x, norm(e["x"], e["y"], self.right(e["period"], won))[1]) else "final_third",
                         "whistled": True, "free_kick_x_m": round(LENGTH - x, 1)}, 0.85)
            # Build-up from a goal kick
            elif e["kind"] == "goal_kick":
                follow = later(e, 15, lambda x_: True)
                lost = next((f for f in follow if f["team"] != e["team"] and f["kind"] in ON_BALL | {"tackle", "interception"}
                             and f.get("success", True)), None)
                kept = [f for f in follow if f["team"] == e["team"] and (not lost or f["t"] < lost["t"])]
                progressed = any(self.n(f)[0] >= LENGTH / 2 or (f.get("end") and norm(*f["end"], self.right(f["period"], f["team"]))[0] >= LENGTH / 2)
                                 for f in kept if f["kind"] in ("pass", "carry") and f.get("success", True))
                short = bool(e.get("end")) and math.dist((e["x"], e["y"]), e["end"]) < 32
                add(e["period"], e["t"] + 1.0, "build_up", e["team"],
                    [{"team": e["team"], "number": e.get("number"), "role": "goalkeeper"}],
                    {"short": short, "progressed": progressed,
                     "passes": sum(1 for f in kept if f["kind"] == "pass" and f.get("success"))}, 0.95)
        # Set pieces delivered into the box
        for d in self.deliveries():
            players = [{"team": d["team"], "number": d["number"], "role": "taker"}] if d["number"] is not None else []
            if d["first_number"] is not None and d["first_contact"] != "none":
                won = d["team"] if d["first_contact"] == "attack" else ("away" if d["team"] == "home" else "home")
                players.append({"team": won, "number": d["first_number"], "role": "first_contact"})
            details = {"kind": d["kind"], "outcome": d["outcome"], "side": d["side"], "first_contact": d["first_contact"],
                       "xg": d["xg"], **({"delivery": d["delivery"]} if d["delivery"] else {}),
                       **({"swing": d["swing"]} if d["swing"] else {}), **({"routine": d["routine"]} if d["routine"] else {})}
            if d["end"]:
                ex, ey = norm(*d["end"], self.right(d["period"], d["team"]))
                details["end_x_m"], details["end_y_m"] = round(ex, 1), round(ey, 1)
            add(d["period"], d["t"] + 1.0, "set_piece", d["team"], players, details, 0.95)
        for c in changes:
            team = c["team"]
            x, y = norm(c["x"], c["y"], self.right(c["period"], team))
            follow = [f for f in evs if f["period"] == c["period"] and c["t"] < f["t"] <= c["t"] + 15]
            ended = next((f for f in follow if f["team"] != team and f["kind"] in ON_BALL | {"tackle", "interception"}
                          and f.get("success", True) and f["kind"] not in RESTARTS), None)
            spell = [f for f in follow if f["team"] == team and (not ended or f["t"] < ended["t"])]
            shot = next((f for f in spell if f["kind"] == "shot"), None)
            who = [{"team": team, "number": c.get("number"), "role": "regain"}] if c.get("number") is not None else []
            # The plays: regains in their final third (the metric counts every one within 40 m of goal)
            if x >= FINAL_THIRD:
                lost = [d for d in changes if d["period"] == c["period"] and d["team"] != team and d["t"] < c["t"]]
                since = round(c["t"] - lost[-1]["t"], 1) if lost else None
                add(c["period"], c["t"], "high_regain", team, who,
                    {"x_m": round(x, 1), "seconds_after_loss": since, "counterpress": since is not None and since <= COUNTERPRESS_S,
                     "led_to_shot": shot is not None}, 0.9)
            elif x < 35:
                # A counter-attack: from deep in our half to their box, or a shot, within 8 s
                reached = next((f for f in spell if f["t"] - c["t"] <= 8 and (f["kind"] == "shot" or (
                    f["kind"] in ("pass", "carry") and f.get("success", True) and f.get("end")
                    and in_box(*norm(*f["end"], self.right(f["period"], team)))))), None)
                if reached:
                    outcome = "goal" if shot and shot.get("outcome") == "goal" else "shot" if shot else "none"
                    add(c["period"], c["t"] + 1.0, "counter_attack", team, who,
                        {"duration_s": round(reached["t"] - c["t"], 1), "outcome": outcome, "from_x_m": round(x, 1)}, 0.85)
            # The other side's press played through: won deep in our own third, and in their final third within 7 s
            if x < 22:
                deep = next((f for f in spell if f["kind"] in ("pass", "carry") and f.get("success", True) and f.get("end")
                             and norm(*f["end"], self.right(f["period"], team))[0] >= FINAL_THIRD), None)
                if deep and deep["t"] - c["t"] <= 7:
                    other = "away" if team == "home" else "home"
                    add(c["period"], deep["t"], "press_broken", other,
                        [{"team": team, "number": deep.get("number"), "role": "line_breaker"}] if deep.get("number") is not None else [],
                        {"seconds": round(deep["t"] - c["t"], 1), "from_x_m": round(x, 1), "led_to_shot": shot is not None}, 0.8)
        # Formation changes: a new shape that holds for two windows running
        if windows:
            for team in ("home", "away"):
                for phase in ("out_of_possession", "in_possession"):
                    seen = [(w, w["teams"][team][phase]["formation"]) for w in windows if w["final"]
                            and w["teams"][team][phase]["formation"]]
                    current = seen[0][1] if seen else None
                    for (w, shape), (_, after) in zip(seen[1:], seen[2:]):
                        if shape != current and after == shape:
                            add(w["period"], w["start_s"] + 1.0, "formation_change", team, [],
                                {"phase": phase, "from": current, "to": shape}, 0.8)
                            current = shape
        out.sort(key=lambda p: (p["period"], p["match_time_s"]))
        return out
