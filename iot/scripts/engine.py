"""
The demo's match engine: a whole match played out from a seed, as tracking
frames (every player and the ball, 5 times a second) and on-ball events,
the way a club device's tracker would see it. analytics.py then measures it
like any other match, so the synthetic season goes through the same code as
the dataset's real one.

It isn't football, but it is shaped like it. Each team has a style per
5-minute window (style()): its formation in and out of possession, how high
its back line stands, how long and wide the team is, how hard it presses,
how well it passes, how direct it is and which channel it attacks. Players
stand in their formation's places around a block that follows the ball,
the nearest defender closes the ball down, and possession moves by passes,
carries, tackles, interceptions, shots and restarts, with chances depending
on the place and the pressure. A story (story()) can change a team's style
from one window to the next, and script plays (a regain and a goal, a
counter-attack, a corner routine), so a match can drift off its game plan
and back on, as the live match does.

Deterministic: the same seed plays the same match, so the simulator, the
synthetic cameras and a restart all agree. Standard library only.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace

from analytics import FORMATIONS, LENGTH, WIDTH, attacks_right, norm, xg

HZ = 5
DT = 1 / HZ
HALF = 2700

# Which shirt number stands in each place of a formation (FORMATIONS order):
# 1 keeper, 2 RB, 3 LB, 4 LCB, 5 RCB, 6 holding mid, 8 CM, 10 AM, 7 right, 11 left, 9 striker
SLOTS = {
    "4-4-2": [3, 4, 5, 2, 11, 6, 8, 7, 10, 9],
    "4-3-3": [3, 4, 5, 2, 8, 6, 10, 11, 9, 7],
    "4-2-3-1": [3, 4, 5, 2, 6, 8, 11, 10, 7, 9],
    "4-1-4-1": [3, 4, 5, 2, 6, 11, 8, 10, 7, 9],
    "3-5-2": [4, 6, 5, 3, 8, 11, 10, 2, 7, 9],
    "3-4-3": [4, 6, 5, 3, 8, 10, 2, 11, 9, 7],
    "5-3-2": [3, 4, 6, 5, 2, 8, 11, 10, 7, 9],
    "5-4-1": [3, 4, 6, 5, 2, 11, 8, 10, 7, 9],
    "3-2-5": [4, 5, 2, 3, 6, 11, 8, 9, 10, 7],
    "2-3-5": [4, 5, 3, 6, 2, 11, 8, 9, 10, 7],
}
OUTFIELD = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
TOP_SPEED = 8.6   # m/s, a winger's sprint
# How players move (m/s): a small gap closed at a jog, a bigger one run, a long one sprinted
CRUISE, RUN_RATE = 1.1, 0.15
ACCEL = 3.0       # m/s², speeding up and slowing down
# Seconds between one player's runs, about, by shirt number (forwards most, centre-backs least)
RUN_EVERY = {2: 70, 3: 70, 4: 240, 5: 240, 6: 120, 8: 80, 10: 60, 7: 70, 11: 70, 9: 45}
SHAPE_TAU = 2.0
RECOVERY_M = 20.0 # out of possession, further than this from his place and a player sprints back   # s, how long a team takes to get most of the way from one shape to the other


# Set-piece routines: who the delivery is for, where it goes (the team's frame, from a set piece on its left:
# mirrored from the right), its swing, and how much it adds to winning the first contact
ROUTINES = {
    "near-post flick": {"target": 5, "spot": (99.5, 30.0), "swing": "inswing", "edge": 0.10},
    "back post": {"target": 4, "spot": (98.0, 41.0), "swing": "outswing", "edge": 0.02},
    "six-yard screen": {"target": 9, "spot": (101.5, 33.5), "swing": "inswing", "edge": 0.05},
    "penalty spot": {"target": 10, "spot": (94.0, 35.0), "swing": "driven", "edge": -0.04},
    "short corner": {"target": 8, "spot": (99.0, 6.0), "swing": "short", "edge": 0.0},
    "long throw": {"target": 5, "spot": (98.0, 30.5), "swing": "long_throw", "edge": 0.08},
    "wide free kick": {"target": 4, "spot": (95.5, 37.0), "swing": "inswing", "edge": 0.0},
}
# Those that end in a header at goal more often than not
HEADED = {"near-post flick": 0.6, "back post": 0.55, "six-yard screen": 0.5, "penalty spot": 0.45, "long throw": 0.5,
          "wide free kick": 0.5}


@dataclass(frozen=True)
class Style:
    """How a team plays for a while. Lengths in metres; the rest 0-1."""
    shape_in: str = "4-3-3"
    shape_out: str = "4-4-2"
    line_in: float = 38.0       # back line from own goal, in possession (ball at halfway)
    line_out: float = 34.0      # out of possession
    length_in: float = 42.0     # deepest to highest outfield player
    length_out: float = 32.0
    width_in: float = 54.0
    width_out: float = 40.0
    press: float = 0.5          # closing down and winning the ball high
    passing: float = 0.5        # pass completion
    directness: float = 0.4     # forward passes over safe ones
    left: float = 1.0           # weights of the channels it attacks through
    centre: float = 1.0
    right: float = 1.0
    short_gk: float = 0.6       # goal kicks played short
    aerial: float = 0.5         # winning the first contact at set pieces, attacking and defending
    long_throws: bool = False   # throw-ins in the final third go long into the box
    # Set-piece routines it uses (ROUTINES), each with its weight
    corners: tuple = (("near-post flick", 1.0), ("back post", 1.0), ("six-yard screen", 1.0))


@dataclass
class Script:
    """A play the story wants: kind, the team that makes it, and when (period, seconds)."""
    kind: str
    team: str
    period: int
    t: float
    extra: dict = field(default_factory=dict)


class Engine:
    """
    One match. styles(team, period, t) → Style; scripts: plays forced at
    their moments; score_random: whether shots outside scripts can go in.
    """

    def __init__(self, seed: str, styles, scripts: list[Script] | None = None, home_attacks: str = "right",
                 halves: tuple[float, float] = (2880, 2940), score_random: bool = True, fatigue=None,
                 subs: list[tuple[str, int, float, int, int]] | None = None) -> None:
        self.rng = random.Random(seed)
        self.styles, self.scripts = styles, sorted(scripts or [], key=lambda s: (s.period, s.t))
        self.home_attacks, self.halves, self.score_random = home_attacks, halves, score_random
        self.phase = {(side, n): self.rng.uniform(0, 6.3) for side in ("home", "away") for n in range(1, 12)}
        self.frames: list = []
        self.events: list[dict] = []
        self.smooth: dict[str, tuple[float, float]] = {}
        self.last: dict[tuple[str, int], tuple[float, float]] = {}
        self.vel: dict[tuple[str, int], tuple[float, float]] = {}
        self.inpos: dict[str, float] = {}
        self.runs: dict[str, dict[int, tuple[float, float, float]]] = {}
        self.bursting: dict[str, set[int]] = {"home": set(), "away": set()}
        self.legs_rng = random.Random(f"{seed}/runs")
        # Substitutions: (team, period, seconds, off, on); from then on the place is the new player's
        self.subs = sorted(subs or [], key=lambda x: (x[1], x[2]))
        self.fatigue = fatigue or (lambda side, n, period, t: 1.0 - (0.06 if period >= 2 else 0.0) * min(1.0, t / 2700))

    # ── positions ─────────────────────────────────────────────────────────────

    def right(self, period: int, team: str) -> bool:
        return attacks_right(self.home_attacks, period, team)

    def place(self, team: str, period: int, t: float, style: Style, holder: str, ball: tuple[float, float],
              override: dict | None = None) -> dict[int, tuple[float, float]]:
        """Where each of a team's players stands (pitch coordinates)."""
        right = self.right(period, team)
        # The block follows the ball as a team does, a second or two behind it, not on every pass
        smooth = self.smooth.setdefault(team, ball)
        k = min(1.0, DT / 1.6)
        smooth = self.smooth[team] = (smooth[0] + (ball[0] - smooth[0]) * k, smooth[1] + (ball[1] - smooth[1]) * k)
        bx, by = norm(*smooth, right)
        has = holder == team
        # A team moves from one shape to the other over a few seconds after the ball changes hands, so a short
        # spell on the ball doesn't send the wide players the length of the pitch and back
        k = self.inpos[team] = self.inpos.get(team, 1.0 if has else 0.0) + ((1.0 if has else 0.0) - self.inpos.get(team, 0.0)) * min(1.0, DT / SHAPE_TAU)
        spots = [self.spots(team, t, style, phase, bx, by) for phase in (True, False)]
        out = {n: (k * spots[0][n][0] + (1 - k) * spots[1][n][0], k * spots[0][n][1] + (1 - k) * spots[1][n][1])
               for n in spots[0]}
        back = k * spots[0][1][0] + (1 - k) * spots[1][1][0]
        # Runs: in possession a forward goes in behind or a full-back overlaps; out of it, a recovery run
        runs = self.runs.setdefault(team, {})
        bursting = self.bursting[team] = set()
        for n in OUTFIELD:
            run = runs.get(n)
            if run and t < run[0]:
                out[n] = (out[n][0] + run[1], out[n][1] + run[2])
                bursting.add(n)
            elif has and self.legs_rng.random() < DT / RUN_EVERY[n] * self.fatigue(team, n, period, t) ** 4:
                runs[n] = (t + self.legs_rng.uniform(2.0, 4.0), self.legs_rng.uniform(12, 22), self.legs_rng.uniform(-6, 6))
        out[1] = (max(2.5, min(back, 30)), 34 + 0.2 * (by - 34))
        if override:
            out.update(override)
        # The pressing player: the nearest outfield player closes the ball down
        if not has:
            bx, by = norm(*ball, right)
            near = min(OUTFIELD, key=lambda n: math.dist(out[n], (bx, by)))
            px, py = out[near]
            k = 0.35 + 0.55 * style.press
            out[near] = (px + (bx - px) * k, py + (by - py) * k)
        out = {n: (min(max(x, -1), LENGTH + 1), min(max(y, -1), WIDTH + 1)) if right else
               (min(max(LENGTH - x, -1), LENGTH + 1), min(max(WIDTH - y, -1), WIDTH + 1)) for n, (x, y) in out.items()}
        # Out of possession, a player far from his place sprints back to it (a recovery run)
        if not has:
            bursting.update(n for n in OUTFIELD if math.dist(self.last.get((team, n), out[n]), out[n]) > RECOVERY_M)
        return out

    def spots(self, team: str, t: float, style: Style, has: bool, bx: float, by: float) -> dict[int, tuple[float, float]]:
        """Each player's place in one of the team's shapes (the team's frame), the keeper's x under 1."""
        shape = style.shape_in if has else style.shape_out
        if has:
            back = min(62, max(14, style.line_in + 0.38 * (bx - 52.5)))
            length, width, cy = style.length_in, style.width_in, 34 + 0.25 * (by - 34)
        else:
            back = min(66, max(7, style.line_out + 0.42 * (bx - 52.5)))
            length, width, cy = style.length_out, style.width_out, 34 + 0.42 * (by - 34)
        length = min(length, 99 - back)
        out = {}
        for (d, w), number in zip(FORMATIONS[shape], SLOTS[shape]):
            p = self.phase[(team, number)]
            out[number] = (back + d * length + 1.1 * math.sin(t * 0.37 + p),
                           cy + (w - 0.5) * width + 1.0 * math.cos(t * 0.29 + p * 1.7))
        out[1] = (back * (0.42 if has else 0.3), 0.0)
        return out

    # ── the match ─────────────────────────────────────────────────────────────

    def run(self) -> "Engine":
        for period, length in ((1, self.halves[0]), (2, self.halves[1])):
            self.half(period, length)
        return self

    def label(self, team: str, number: int | None, period: int, t: float) -> int | None:
        """The shirt number in a place (its starter's number) at a moment: a substitute's once he's on."""
        for side, p, at, off, on in self.subs:
            if side == team and number == off and (period, t) >= (p, at):
                number = on
        return number

    def event(self, period, t, team, number, kind, at, **extra) -> None:
        other = "away" if team == "home" else "home"
        for key, side in (("to", team), ("on", other)):
            if extra.get(key) is not None:
                extra[key] = self.label(side, extra[key], period, t)
        self.events.append({"period": period, "t": round(t, 2), "team": team, "number": self.label(team, number, period, t),
                            "kind": kind, "x": round(at[0], 2), "y": round(at[1], 2), **extra})

    def half(self, period: int, length: float) -> None:
        rng = self.rng
        kick_off = "home" if period == 1 else "away"
        st = {"holder": kick_off, "carrier": 9, "ball": (52.5, 34.0), "pass": None, "set": None, "until": 0.0,
              "override": {}, "script": None}
        self.event(period, 0.0, kick_off, 9, "kick_off", st["ball"])
        scripts = [s for s in self.scripts if s.period == period]
        # Each half's clock starts at 0: no run carries over the break
        self.runs = {}
        t = 0.0
        while t < length:
            styles = {side: self.styles(side, period, t) for side in ("home", "away")}
            if scripts and t >= scripts[0].t and st["script"] is None and st["pass"] is None:
                st["script"] = self.start_script(scripts.pop(0), st, t)
            if st["script"] is not None:
                self.run_script(st, period, t)
            else:
                self.step(st, styles, period, t, rng)
            holder = st["holder"]
            ball = st["ball"]
            players, seen = [], []
            for side in ("home", "away"):
                override = st["override"].get(side) if t < st["until"] else None
                where = self.place(side, period, t, styles[side], holder, ball, override)
                carrier = st["carrier"] if side == holder and st["pass"] is None else None
                for n, (x, y) in sorted(where.items()):
                    if n == carrier:
                        x, y = ball
                    else:
                        x, y = self.stride(side, n, period, t, x, y, n in self.bursting[side])
                    self.last[(side, n)] = (x, y)
                    players.append((side, n, round(x, 2), round(y, 2)))
                    seen.append((side, self.label(side, n, period, t), round(x, 2), round(y, 2)))
            self.frames.append((period, round(t, 2), (round(ball[0], 2), round(ball[1], 2)), seen))
            st["positions"] = {(p[0], p[1]): (p[2], p[3]) for p in players}
            t += DT

    def stride(self, side: str, n: int, period: int, t: float, x: float, y: float, burst: bool = False) -> tuple[float, float]:
        """
        One step of a player towards where he should be: nobody jumps from one
        place to another. He walks or jogs to close a small gap, runs a bigger
        one, sprints only a long one, speeds up and slows down at most ACCEL,
        and his legs are what fatigue() says they are at that minute.
        """
        lx, ly = self.last.get((side, n), (x, y))
        vx, vy = self.vel.get((side, n), (0.0, 0.0))
        gx, gy = x - lx, y - ly
        gap = math.hypot(gx, gy)
        legs = self.fatigue(side, n, period, t)
        want = (min(gap / 0.8, TOP_SPEED) if burst else min(gap / 2.0, CRUISE + RUN_RATE * gap, TOP_SPEED)) * legs
        wx, wy = (gx / gap * want, gy / gap * want) if gap > 1e-6 else (0.0, 0.0)
        dvx, dvy = wx - vx, wy - vy
        dv = math.hypot(dvx, dvy)
        if dv > ACCEL * DT:
            dvx, dvy = dvx * ACCEL * DT / dv, dvy * ACCEL * DT / dv
        vx, vy = vx + dvx, vy + dvy
        self.vel[(side, n)] = (vx, vy)
        return lx + vx * DT, ly + vy * DT

    def pos(self, st: dict, team: str, number: int) -> tuple[float, float]:
        return st.get("positions", {}).get((team, number), st["ball"])

    def step(self, st: dict, styles: dict, period: int, t: float, rng: random.Random) -> None:
        holder = st["holder"]
        other = "away" if holder == "home" else "home"
        me, them = styles[holder], styles[other]
        right = self.right(period, holder)
        # A pass in flight
        if st["pass"]:
            p = st["pass"]
            p["k"] += DT / p["time"]
            a, b = p["from"], self.pos(st, holder, p["to"]) if p["ok"] else p["end"]
            st["ball"] = (a[0] + (b[0] - a[0]) * min(1, p["k"]), a[1] + (b[1] - a[1]) * min(1, p["k"]))
            if p["k"] >= 1:
                st["pass"] = None
                if p["ok"]:
                    st["carrier"] = p["to"]
                    if p.get("shot_after"):
                        self.shoot(st, period, t, holder, p["to"], header=p.get("header", False), force=p.get("force"))
                elif not 0 <= p["end"][1] <= WIDTH:
                    # Out of play: a throw-in to them where it went out
                    st["ball"] = (min(max(p["end"][0], 1.0), LENGTH - 1), 0.0 if p["end"][1] < 0 else WIDTH)
                    self.out_for_throw(st, period, t, other, them)
                elif p.get("set_piece") and self.rng.random() < 0.8:
                    # A set piece's delivery not won: a defender heads or kicks it away, sometimes behind for a corner
                    who = self.nearest(st, other, st["ball"])
                    self.event(period, t, other, who, "clearance", st["ball"], success=True)
                    if self.rng.random() < 0.15:
                        self.concede_corner(st, period, t, holder)
                    else:
                        self.turnover(st, period, t, other, None, who)
                else:
                    # Most misplaced passes are just picked up; a pressing side intercepts more of them
                    caught = self.rng.random() < 0.25 + 0.35 * them.press
                    self.turnover(st, period, t, other, "interception" if caught else "recovery")
            return
        # A restart waiting to be taken
        if st["set"] and t < st["set"]["at"]:
            return
        if st["set"]:
            self.restart(st, period, t, rng, styles)
            return
        bx, by = norm(*st["ball"], right)
        carrier = st["carrier"] or 9
        pressure = them.press
        # Tackled
        # Their press: more duels, higher up the pitch
        if rng.random() < DT * (0.006 + 0.03 * pressure) * (1.5 if bx < 52.5 else 0.7):
            if rng.random() < 0.7:
                taker = self.nearest(st, other, st["ball"])
                self.event(period, t, other, taker, "tackle", st["ball"], success=True)
                if (by < 8 or by > WIDTH - 8) and rng.random() < 0.6:
                    # Near the touchline, the tackle puts it out: their throw-in
                    self.out_for_throw(st, period, t, holder, me)
                else:
                    self.turnover(st, period, t, other, None, taker)
            else:
                self.foul(st, period, t, holder, other, rng)
            return
        # An attack into the final third ends one of three ways: a shot, the ball lost, or the ball played back
        if bx > 75 and not st.get("entered"):
            st["entered"] = True
            roll = rng.random()
            st["plan"] = (("shot" if roll < 0.07 + 0.08 * me.passing else "lose" if roll < 0.55 else "recycle"),
                          t + rng.uniform(0.8, 3.5))
        elif bx < 60:
            st["entered"] = False
        plan = st.get("plan")
        if plan and t >= plan[1]:
            st["plan"] = None
            if plan[0] == "shot":
                self.shoot(st, period, t, holder, carrier)
            elif plan[0] == "lose":
                taker = self.nearest(st, other, st["ball"])
                self.event(period, t, other, taker, "tackle", st["ball"], success=True)
                if rng.random() < 0.07:
                    # Put behind: a corner
                    self.concede_corner(st, period, t, holder)
                else:
                    self.turnover(st, period, t, other, None, taker)
            else:
                self.pass_(st, period, t, holder, carrier, me, them, rng, target=rng.choice((4, 5, 6, 8)), ok=True)
            return
        # A pass, about every 2.5 s on the ball
        if rng.random() < DT / (2.8 - 0.8 * me.passing) * (2.5 if bx > 95 else 1):
            self.pass_(st, period, t, holder, carrier, me, them, rng)
            return
        # Else carry it on, slower as the defence gets tighter near goal
        step = (1.2 + 2.5 * me.directness) * DT * max(0.15, 1 - max(0.0, bx - 72) / 28)
        drift = rng.uniform(-1.5, 1.5) * DT
        nx, ny = min(bx + step, 103), min(max(by + drift, 2), 66)
        st["ball"] = norm(nx, ny, right)

    def nearest(self, st: dict, team: str, at: tuple[float, float], skip: int | None = None) -> int:
        return min(OUTFIELD, key=lambda n: math.dist(self.pos(st, team, n), at) + (999 if n == skip else 0))

    def pass_(self, st, period, t, holder, carrier, me: Style, them: Style, rng, target: int | None = None,
              ok: bool | None = None, kind: str = "pass", extra: dict | None = None, end: tuple | None = None,
              **flags) -> None:
        right = self.right(period, holder)
        bx, by = norm(*st["ball"], right)
        if target is None:
            weights = []
            for n in (1, *OUTFIELD):
                if n == carrier:
                    continue
                tx, ty = norm(*self.pos(st, holder, n), right)
                dist = math.hypot(tx - bx, ty - by)
                if dist > 48 or (n == 1 and bx > 40):
                    continue
                forward = (tx - bx) / 20
                lane = {"left": me.left, "centre": me.centre, "right": me.right}[
                    "left" if ty < WIDTH / 3 else "right" if ty > 2 * WIDTH / 3 else "centre"]
                w = math.exp(me.directness * 2.2 * forward - 0.04 * dist) * (lane if tx > 60 else 1.0)
                weights.append((w, n))
            if not weights:
                return
            total = sum(w for w, _ in weights)
            pick = rng.uniform(0, total)
            for w, n in weights:
                pick -= w
                if pick <= 0:
                    target = n
                    break
            target = target or weights[-1][1]
        end = end or self.pos(st, holder, target)
        tx, ty = norm(*end, right)
        dist = math.dist(st["ball"], end)
        if ok is None:
            # Harder forward, far, high up the pitch, and against a press
            p = 0.97 - 0.2 * them.press * (1.2 if tx > 60 else 0.85) - 0.0042 * max(0, dist - 12) \
                - (0.05 if tx > 80 else 0) + 0.12 * (me.passing - 0.5)
            ok = rng.random() < p
        miss = (end[0] + rng.uniform(-4, 4), end[1] + rng.uniform(-4, 4))
        # A misplaced ball out wide often runs out of play: a throw-in
        if not ok and kind == "pass" and (ty < 10 or ty > WIDTH - 10) and rng.random() < 0.6:
            miss = (miss[0], -1.5 if miss[1] < WIDTH / 2 else WIDTH + 1.5)
        # The ball's speed as the ball tracker reads it: a longer ball struck harder, about 50 km/h over 10 m and
        # 80 km/h over 40 m. The engine still flies every pass at one pace (its story is timed to it), and the
        # spread comes from the moment, not the random stream
        spread = (math.sin(t * 12.9898 + dist * 78.233) * 43758.5453) % 1
        speed = min(26.0, 12.0 + 0.25 * dist) * (0.9 + 0.2 * spread)
        self.event(period, t, holder, carrier, kind, st["ball"], success=ok, to=target if ok else None,
                   end=(round(end[0], 2), round(end[1], 2)) if ok else (round(miss[0], 2), round(miss[1], 2)),
                   speed_ms=round(speed, 1), **(extra or {}))
        st["pass"] = {"from": st["ball"], "to": target, "ok": ok, "end": miss, "k": 0.0,
                      "time": max(0.4, dist / 17), **flags}
        st["carrier"] = None

    def turnover(self, st, period, t, winner, kind, number=None) -> None:
        number = number or self.nearest(st, winner, st["ball"])
        st["entered"], st["plan"] = False, None
        if kind:
            self.event(period, t, winner, number, kind, st["ball"], success=True)
        st["holder"], st["carrier"] = winner, number

    def foul(self, st, period, t, fouled, by, rng) -> None:
        number = self.nearest(st, by, st["ball"])
        self.event(period, t, by, number, "foul", st["ball"], success=True, on=st["carrier"])
        st["set"] = {"kind": "free_kick", "team": fouled, "at": t + 6, "spot": st["ball"]}
        fx, fy = norm(*st["ball"], self.right(period, fouled))
        if fx >= 70 and not (fx >= 77 and 20 < fy < 48):
            # Wide and within 35 m: a delivery, so the box fills while it's set up
            st["set"]["at"] = t + 12
            self.set_up(st, period, t, fouled, (("wide free kick", 1.0),), fy > 34, until=t + 15)

    def shoot(self, st, period, t, team, number, header=False, force=None) -> None:
        rng = self.rng
        right = self.right(period, team)
        x, y = norm(*st["ball"], right)
        value = xg(x, y, header)
        if force:
            outcome = force
        else:
            roll = rng.random()
            outcome = ("goal" if roll < value else "saved" if roll < value + 0.3
                       else "blocked" if roll < value + 0.5 else "off_target")
            if outcome == "goal" and not self.score_random:
                outcome = "saved"
        # How hard it's struck, as the ball tracker reads it (the ball isn't flown to goal here): harder from
        # further out, a header softer; the spread comes from the moment, not the random stream, so the story holds
        spread = (math.sin(t * 12.9898 + x * 78.233) * 43758.5453) % 1
        far = math.hypot(LENGTH - x, WIDTH / 2 - y)
        speed = 10 + 0.25 * far + 5 * spread if header else min(36.0, 18 + 0.4 * far + 8 * spread)
        self.event(period, t, team, number, "shot", st["ball"], outcome=outcome, header=header, success=outcome == "goal",
                   speed_ms=round(speed, 1))
        other = "away" if team == "home" else "home"
        goal_x = LENGTH if right else 0.0
        st["ball"] = (goal_x - (0.5 if right else -0.5), 34 + rng.uniform(-3, 3)) if outcome == "goal" else st["ball"]
        st["carrier"], st["entered"], st["plan"] = None, False, None
        if outcome == "goal":
            st["set"] = {"kind": "kick_off", "team": other, "at": t + 40, "spot": (52.5, 34.0)}
        elif outcome == "saved" and rng.random() < 0.3 or outcome == "blocked" and rng.random() < 0.45:
            st["set"] = {"kind": "corner", "team": team, "at": t + 12,
                         "spot": norm(LENGTH - 0.5, 0.5 if y < 34 else WIDTH - 0.5, right)}
            # Everyone goes up for it while the corner is set up, the routine's target to his spot
            st["set"]["routine"] = self.set_up(st, period, t, team, self.styles(team, period, t).corners, y >= 34, until=t + 19)
        elif outcome == "saved":
            st["holder"], st["carrier"] = other, 1
            st["ball"] = self.pos(st, other, 1)
        elif outcome == "blocked":
            winner = team if rng.random() < 0.4 else other
            st["holder"], st["carrier"] = winner, self.nearest(st, winner, st["ball"])
        else:
            st["set"] = {"kind": "goal_kick", "team": other, "at": t + 14,
                         "spot": norm(5.5, 34.0 + rng.uniform(-8, 8), not right)}

    def restart(self, st, period, t, rng, styles) -> None:
        s = st["set"]
        st["set"], st["entered"], st["plan"] = None, False, None
        team = s["team"]
        st["holder"] = team
        st["ball"] = s["spot"]
        me = styles[team]
        them = styles["away" if team == "home" else "home"]
        if s["kind"] == "kick_off":
            st["carrier"] = 9
            self.event(period, t, team, 9, "kick_off", st["ball"])
            self.pass_(st, period, t, team, 9, me, them, rng, target=8, ok=True)
        elif s["kind"] == "goal_kick":
            st["carrier"] = 1
            short = rng.random() < me.short_gk
            target = rng.choice((4, 5)) if short else rng.choice((9, 10, 7, 11))
            self.pass_(st, period, t, team, 1, me, them, rng, target=target,
                       ok=True if short else rng.random() < 0.45, kind="goal_kick")
        elif s["kind"] == "corner":
            right = self.right(period, team)
            from_right = norm(*s["spot"], right)[1] > 34
            taker = 7 if from_right else 11
            st["carrier"] = taker
            routine = s.get("routine") or self.pick(me.corners, rng)
            self.deliver(st, period, t, team, taker, routine, from_right, me, them, rng, "corner",
                         ok=s.get("ok"), force=s.get("force"))
        elif s["kind"] == "throw_in":
            right = self.right(period, team)
            fx, fy = norm(*st["ball"], right)
            if me.long_throws and fx >= 70:
                # The long throw: the right back takes it, the big men attacking the near post
                st["carrier"] = 2
                self.deliver(st, period, t, team, 2, "long throw", fy > 34, me, them, rng, "throw_in")
            else:
                st["carrier"] = self.nearest(st, team, st["ball"])
                self.pass_(st, period, t, team, st["carrier"], me, them, rng, kind="throw_in")
        else:  # free kick
            st["carrier"] = self.nearest(st, team, st["ball"])
            right = self.right(period, team)
            fx, fy = norm(*st["ball"], right)
            if fx >= 77 and 20 < fy < 48 and rng.random() < 0.5:
                # Central and close: a shot at goal
                self.event(period, t, team, st["carrier"], "free_kick", st["ball"], success=True,
                           routine="direct free kick", swing="direct")
                self.shoot(st, period, t, team, st["carrier"])
            elif fx >= 70:
                self.deliver(st, period, t, team, st["carrier"], "wide free kick", fy > 34, me, them, rng, "free_kick")
            else:
                self.pass_(st, period, t, team, st["carrier"], me, them, rng, kind="free_kick")

    def out_for_throw(self, st, period, t, team, style: Style) -> None:
        """The ball out over the touchline where it is: a throw-in to team (a long one into the box takes setting up)."""
        st["ball"] = (min(max(st["ball"][0], 1.0), LENGTH - 1), 0.0 if st["ball"][1] < WIDTH / 2 else WIDTH)
        st["carrier"], st["entered"], st["plan"] = None, False, None
        fx, fy = norm(*st["ball"], self.right(period, team))
        long = style.long_throws and fx >= 70
        st["set"] = {"kind": "throw_in", "team": team, "at": t + (11 if long else 7), "spot": st["ball"]}
        if long:
            self.set_up(st, period, t, team, (("long throw", 1.0),), fy > 34, until=t + 14)

    def concede_corner(self, st, period, t, team) -> None:
        """Put behind by the defence: a corner to team, on the side the ball went out."""
        right = self.right(period, team)
        _, y = norm(*st["ball"], right)
        st["carrier"], st["entered"], st["plan"], st["holder"] = None, False, None, team
        st["set"] = {"kind": "corner", "team": team, "at": t + 12,
                     "spot": norm(LENGTH - 0.5, 0.5 if y < 34 else WIDTH - 0.5, right)}
        st["set"]["routine"] = self.set_up(st, period, t, team, self.styles(team, period, t).corners, y >= 34, until=t + 19)

    def set_up(self, st, period, t, team, routines, from_right: bool, until: float) -> str:
        """A set piece being set up: the box fills, and the routine's target goes to his spot. Returns the routine."""
        routine = self.pick(routines, self.rng)
        self.corner_shape(st, period, t, team, until=until)
        r = ROUTINES[routine]
        x, y = r["spot"]
        if r["target"] in st["override"][team] or r["swing"] == "short":
            st["override"][team][r["target"]] = (x, WIDTH - y if from_right else y)
        return routine

    @staticmethod
    def pick(weighted, rng: random.Random) -> str:
        total = sum(w for _, w in weighted)
        roll = rng.uniform(0, total)
        for name, w in weighted:
            roll -= w
            if roll <= 0:
                return name
        return weighted[-1][0]

    def deliver(self, st, period, t, team, taker, routine, from_right, me: Style, them: Style, rng, kind,
                ok: bool | None = None, force: str | None = None) -> None:
        """A set piece's delivery into the box: a routine's target, where it's aimed, whether he wins it."""
        r = ROUTINES[routine]
        x, y = r["spot"]
        spot = norm(x, WIDTH - y if from_right else y, self.right(period, team))
        target = r["target"] if r["target"] != taker else 10
        if ok is None:
            ok = rng.random() < 0.34 + r["edge"] + 0.45 * (me.aerial - them.aerial)
        header = r["swing"] != "short"
        shot = force is not None or (ok and rng.random() < HEADED.get(routine, 0.3))
        self.pass_(st, period, t, team, taker, me, them, rng, target=target, ok=ok, kind=kind, end=spot,
                   extra={"routine": routine, "swing": r["swing"]}, shot_after=shot, header=header, force=force,
                   set_piece=True)
    def corner_shape(self, st, period, t, team, until: float | None = None) -> None:
        """For a set piece near goal: five attackers in the box and the defenders marking them (for 7 s, or until)."""
        other = "away" if team == "home" else "home"
        spots = [(99, 30), (97, 37), (94, 33), (100, 40), (92, 27)]
        st["override"] = {
            team: dict(zip((4, 5, 9, 10, 8), spots)),
            # The other side marks them, in its own frame (it defends x = 0)
            other: {**{n: (LENGTH - x - 0.8, WIDTH - y - 0.6) for n, (x, y) in zip((4, 5, 6, 2, 3), spots)}, 1: (1.0, 34.0)},
        }
        st["until"] = until or t + 7
        # They sprint to their spots, and their markers with them
        for side, numbers in ((team, (4, 5, 9, 10, 8)), (other, (4, 5, 6, 2, 3))):
            for n in numbers:
                self.runs.setdefault(side, {})[n] = (t + 6.0, 0.0, 0.0)

    # ── scripted plays ────────────────────────────────────────────────────────

    def start_script(self, s: Script, st: dict, t: float) -> dict:
        st["set"], st["pass"] = None, None
        return {"s": s, "step": 0, "next": t}

    def run_script(self, st: dict, period: int, t: float) -> None:
        sc = st["script"]
        if st["pass"]:
            # Let the pass fly (step() moves it), then go on
            styles = {side: self.styles(side, period, t) for side in ("home", "away")}
            self.step(st, styles, period, t, self.rng)
            return
        if t < sc["next"]:
            if st["carrier"] is not None and sc.get("carry"):
                dx, dy = sc["carry"]
                st["ball"] = (st["ball"][0] + dx * DT, st["ball"][1] + dy * DT)
            return
        s, team = sc["s"], sc["s"].team
        other = "away" if team == "home" else "home"
        right = self.right(period, team)
        pitch = lambda x, y: norm(x, y, right)  # noqa: E731  (the scripting team's frame → pitch)
        styles = {side: self.styles(side, period, t) for side in ("home", "away")}
        me, them = styles[team], styles[other]
        step = sc["step"]
        sc["step"] += 1
        sc["carry"] = None
        if s.kind == "high_regain_goal":
            # They play out from the back; our #10 steps in at the edge of their third, #9 scores
            if step == 0:
                st["holder"], st["carrier"], st["ball"] = other, 4, pitch(84, 40)
                sc["next"] = t + 1.6
            elif step == 1:
                self.pass_(st, period, t, other, 4, them, me, self.rng, target=6, ok=False)
                st["pass"]["end"] = pitch(80, 36)
                sc["next"] = t
            elif step == 2:
                st["ball"] = pitch(80, 36)
                self.turnover(st, period, t, team, "interception", s.extra.get("regain", 10))
                sc["carry"] = (2.5 if right else -2.5, 0)
                sc["next"] = t + 1.4
            elif step == 3:
                self.pass_(st, period, t, team, s.extra.get("regain", 10), me, them, self.rng, target=s.extra.get("scorer", 9), ok=True)
                sc["next"] = t
            elif step == 4:
                st["ball"] = pitch(93, 31)
                self.shoot(st, period, t, team, s.extra.get("scorer", 9), force="goal")
                st["script"] = None
        elif s.kind == "counter_goal":
            # We lose it in their third; they break through our press in three passes and score
            if step == 0:
                st["holder"], st["carrier"], st["ball"] = other, 8, pitch(27, 38)
                # Our #8 on the ball in their third, so the tackle is a change of possession
                self.event(period, t, other, 8, "carry", st["ball"], end=pitch(28, 37), success=True)
                sc["next"] = t + 1.2
            elif step == 1:
                st["ball"] = pitch(28, 37)
                taker = 6
                self.event(period, t, team, taker, "tackle", st["ball"], success=True)
                self.turnover(st, period, t, team, None, taker)
                sc["next"] = t + 0.4
            elif step == 2:
                self.pass_(st, period, t, team, 6, me, them, self.rng, target=8, ok=True)
                sc["next"] = t
            elif step == 3:
                sc["carry"] = (5.5 if right else -5.5, 0.5)
                sc["next"] = t + 2.2
            elif step == 4:
                self.pass_(st, period, t, team, 8, me, them, self.rng, target=11, ok=True)
                sc["next"] = t
            elif step == 5:
                sc["carry"] = (6.0 if right else -6.0, 1.0)
                sc["next"] = t + 2.0
            elif step == 6:
                self.pass_(st, period, t, team, 11, me, them, self.rng, target=9, ok=True)
                sc["next"] = t
            elif step == 7:
                st["ball"] = pitch(95, 37)
                self.shoot(st, period, t, team, 9, force="goal")
                st["script"] = None
        elif s.kind == "corner_goal":
            # The near-post routine: #7 delivers, #5 attacks the near post and heads it in
            if step == 0:
                side = s.extra.get("side", 0.5)
                st["holder"], st["carrier"], st["ball"] = team, 7, pitch(104.5, side)
                self.corner_shape(st, period, t, team, until=t + 30)
                spot = (99.5, 30.5) if side < 34 else (99.5, 37.5)
                st["override"][team][5] = spot
                # #5 sprints all the way to the near post; the corner waits for him (up to 25 s)
                self.runs.setdefault(team, {})[5] = (t + 25, 0.0, 0.0)
                sc["spot"], sc["t0"] = pitch(*spot), t
                sc["next"] = t + 12
            elif step == 1 and math.dist(self.pos(st, team, 5), sc["spot"]) > 2.5 and t < sc["t0"] + 25:
                sc["step"], sc["next"] = 1, t + DT
            elif step == 1:
                self.pass_(st, period, t, team, 7, me, them, self.rng, target=5, ok=True, kind="corner",
                           extra={"routine": "near-post flick", "swing": "inswing"}, shot_after=True, header=True,
                           force="goal")
                st["script"] = None
        else:
            st["script"] = None


def play(seed: str, styles, scripts=None, home_attacks="right", halves=(2880, 2940), score_random=True,
         fatigue=None, subs=None) -> Engine:
    return Engine(seed, styles, scripts, home_attacks, halves, score_random, fatigue, subs).run()


def drift(style: Style, rng: random.Random, amount: float = 1.0) -> Style:
    """A style with a little noise, as a team plays a bit differently window to window."""
    return replace(
        style,
        line_in=style.line_in + rng.gauss(0, 1.5 * amount), line_out=style.line_out + rng.gauss(0, 1.5 * amount),
        length_in=style.length_in + rng.gauss(0, 1.5 * amount), length_out=style.length_out + rng.gauss(0, 1.2 * amount),
        width_in=style.width_in + rng.gauss(0, 1.5 * amount), width_out=style.width_out + rng.gauss(0, 1.2 * amount),
        press=min(1, max(0, style.press + rng.gauss(0, 0.05 * amount))),
        passing=min(1, max(0, style.passing + rng.gauss(0, 0.05 * amount))),
    )
