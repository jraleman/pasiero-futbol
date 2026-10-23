"""
The rules: from the timeline of analysed frames to the alerts a person
should look at. Each rule says what it needs; a frame without it (no
calibration, no ball) never produces that alert, so nothing is guessed.

  ball out / goal   the ball on the ground plane beyond a line by OUT_MARGIN,
                    having been inside within the last seconds of the shot,
                    and still beyond it in the next analysed frame (a ball in
                    the air projects too far, but only for an instant);
                    between the posts over a goal line it is a goal.
                    Needs: calibration, the ball. The ground plane is the
                    weak point: a ball in the air projects too far.
  offside           a pass between two players of the same team; at the
                    moment the passer last had the ball, the receiver is in
                    the opponents' half and level with or beyond both the
                    ball and the deepest outfield opponent (the goalkeeper
                    is assumed behind). Needs: calibration, the receiver and
                    an opponent with pitch coordinates. At a few frames a
                    second the moment of the pass is approximate: these are
                    candidates for a person, never a measurement to rule on.
  contact           two players of opposite teams (by their own colours at
                    that moment; at least one of them clearly in their kit,
                    which keeps referees and spectators out), touching in a
                    wide view (players under CLOSE_UP of the frame's height:
                    in a TV close-up boxes overlap without any contact), then a player
                    on the ground where they were, within CONTACT_DOWN
                    seconds: the one of the two no longer seen standing.
                    By position, not track: a fall changes a box so much the
                    tracker often starts a new track. Image space only; in
                    the penalty area when calibration says so.
  identity          a card from the referee's report (or the operator: --cards)
                    to a number other than the shirt number read on the
                    offender of the contact alert just before it, same team,
                    within IDENTITY_BEFORE seconds. Needs: the contact alert,
                    and the offender's number read (numbers.py). The video
                    never sees the card: the report says who got it.

Who the alert concerns ("team") follows the contract: the scoring team, the
attacking team, the offender's team, the team that touched the ball last,
the carded player's team.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pitch import Pitch
from .state import Frame, Person

OUT_MARGIN = 0.5          # m beyond a line
MAX_OUT = 4.0             # m: further out, it's no ball that just left play (a spare ball, the crowd)
MAX_GOAL = 3.0            # m: a goal's ball is in the net, about 2 m deep
CROSSING_WINDOW = 3.0     # s: the ball was inside this recently
LAST_TOUCH_M = 3.0
LAST_TOUCH_S = 2.0
POSSESSION_M = 1.8
PASS_MAX_S = 4.0
OFFSIDE_NEAR = 0.5        # m: also alert when the receiver is this close behind the line
CONTACT_DOWN = 2.5        # s
COOLDOWN = 6.0            # s between two alerts of one kind
CLOSE_UP = 0.4            # a player taller than this share of the frame: a close-up
FALL_HEIGHT = 0.65        # on the ground: under this share of their standing height (someone sitting doesn't fall)
IDENTITY_BEFORE = 180.0   # s: a card is for a foul up to this long before it (play stops, then the card)
IDENTITY_AFTER = 5.0      # s: ... or just after (the report's clock is to the minute)


@dataclass
class Card:
    team: str                        # home, away
    number: int
    colour: str                      # yellow, second_yellow, red
    t: float                         # seconds from the clip's start


@dataclass
class Event:
    type: str
    frame: int                       # index into the frames list (the key moment)
    team: str | None
    players: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    confidence: float = 0.5
    line_x: float | None = None
    why: str = ""


def player(p: Person, role: str) -> dict:
    return {"team": p.team, "number": p.number, "role": role, "track_id": p.track}


class Rules:
    def __init__(self, pitch: Pitch, home_attacks: str = "right") -> None:
        self.pitch = pitch
        # The team attacking towards x = length
        self.right_team = "home" if home_attacks == "right" else "away"

    def run(self, frames: list[Frame]) -> list[Event]:
        events = self.ball_out(frames) + self.offside(frames) + self.contacts(frames)
        return sorted(events, key=lambda e: e.frame)

    # ── ball out and goals ────────────────────────────────────────────────────

    def last_touch(self, frames: list[Frame], i: int) -> Person | None:
        f = frames[i]
        best = None
        for g in reversed(frames[:i + 1]):
            if g.shot != f.shot or f.t - g.t > LAST_TOUCH_S:
                break
            if not (g.ball and g.ball.xy):
                continue
            for p in g.persons:
                if p.playing and p.xy:
                    d = ((p.xy[0] - g.ball.xy[0]) ** 2 + (p.xy[1] - g.ball.xy[1]) ** 2) ** 0.5
                    if d <= LAST_TOUCH_M and (best is None or d < best[0]):
                        best = (d, p)
            if best:
                return best[1]
        return None

    def ball_out(self, frames: list[Frame]) -> list[Event]:
        events, last = [], -1e9
        for i, f in enumerate(frames):
            if not (f.calib and f.ball and f.ball.xy and f.ball.conf >= 0.35) or f.t - last < COOLDOWN:
                continue
            beyond = self.pitch.outside(*f.ball.xy)
            if not beyond or beyond[1] < OUT_MARGIN:
                continue
            goal_mouth = beyond[0] == "goal_line" and self.pitch.between_posts(f.ball.xy[1])
            if beyond[1] > (MAX_GOAL if goal_mouth else MAX_OUT):
                continue
            crossed = any(g.shot == f.shot and 0 < f.t - g.t <= CROSSING_WINDOW and g.ball and g.ball.xy
                          and self.pitch.outside(*g.ball.xy) is None for g in frames[:i])
            if not crossed:
                continue
            line, distance = beyond
            after = frames[i + 1] if i + 1 < len(frames) else None
            still = (after is not None and after.shot == f.shot and after.t - f.t <= 1.0 and after.calib and after.ball
                     and after.ball.xy and (self.pitch.outside(*after.ball.xy) or ("", 0))[0] == line)
            if not still:
                continue
            toucher = self.last_touch(frames, i)
            x, y = f.ball.xy
            confidence = round(0.5 * f.calib.confidence + 0.5 * f.ball.conf, 2)
            if line == "goal_line" and self.pitch.between_posts(y):
                scoring = self.right_team if x > self.pitch.length / 2 else ("away" if self.right_team == "home" else "home")
                players = [player(toucher, "scorer")] if toucher and toucher.team == scoring else []
                events.append(Event("goal", i, scoring, players, {"ball_over_line_cm": round(distance * 100)},
                                    confidence, why=f"ball {distance:.2f} m over the goal line between the posts"))
            else:
                details = {"line": line, "margin_cm": round(distance * 100)}
                if toucher:
                    if line == "touchline":
                        details["restart"] = "throw_in"
                    else:
                        # The goal this line belongs to is defended by the team not attacking it
                        defending = ("away" if self.right_team == "home" else "home") if x > self.pitch.length / 2 else self.right_team
                        details["restart"] = "corner_kick" if toucher.team == defending else "goal_kick"
                events.append(Event("ball_out", i, toucher.team if toucher else None,
                                    [player(toucher, "last_touch")] if toucher else [], details, confidence,
                                    why=f"ball {distance:.2f} m beyond the {line}"))
            last = f.t
        return events

    # ── offside ───────────────────────────────────────────────────────────────

    def possessor(self, f: Frame) -> Person | None:
        """Who has the ball: the nearest player on the pitch within POSSESSION_M, or in the image when uncalibrated."""
        if not f.ball:
            return None
        best = None
        for p in f.persons:
            if not p.playing:
                continue
            if f.ball.xy and p.xy:
                d = ((p.xy[0] - f.ball.xy[0]) ** 2 + (p.xy[1] - f.ball.xy[1]) ** 2) ** 0.5 / POSSESSION_M
            else:
                fx, fy = p.foot
                d = max(abs(fx - f.ball.centre[0]) / (0.6 * p.height), abs(fy - f.ball.centre[1]) / (0.35 * p.height))
            if d <= 1 and (best is None or d < best[0]):
                best = (d, p)
        return best[1] if best else None

    def offside(self, frames: list[Frame]) -> list[Event]:
        held = [(i, p) for i, f in enumerate(frames) if (p := self.possessor(f))]
        events, last = [], -1e9
        for (i1, a), (i2, b) in zip(held, held[1:]):
            f = frames[i1]
            if (a.track == b.track or a.team != b.team or frames[i2].shot != f.shot
                    or frames[i2].t - f.t > PASS_MAX_S or f.t - last < COOLDOWN or not f.calib):
                continue
            receiver = f.by_track(b.track)
            defenders = [p for p in f.persons if p.playing and p.team != a.team and p.xy]
            if not (receiver and receiver.xy and defenders):
                continue
            right = a.team == self.right_team
            depth = (lambda x: x) if right else (lambda x: self.pitch.length - x)
            deepest = max(defenders, key=lambda p: depth(p.xy[0]))
            line = depth(deepest.xy[0])
            line_x = deepest.xy[0]
            if f.ball and f.ball.xy and depth(f.ball.xy[0]) > line:
                line, line_x = depth(f.ball.xy[0]), f.ball.xy[0]
            if depth(receiver.xy[0]) <= self.pitch.length / 2:
                continue
            margin = depth(receiver.xy[0]) - line
            if margin <= -OFFSIDE_NEAR:
                continue
            confidence = round(f.calib.confidence * min(1.0, 0.5 + 0.5 * min(receiver.conf, deepest.conf)), 2)
            events.append(Event("offside_candidate", i1, a.team,
                                [player(receiver, "attacker"), player(deepest, "defender"), player(a, "passer")],
                                {"margin_cm": round(margin * 100)}, confidence, line_x,
                                why=f"receiver {margin:+.2f} m against the line at the pass"))
            last = f.t
        return events

    # ── contacts ──────────────────────────────────────────────────────────────

    @staticmethod
    def touching(p: Person, q: Person) -> bool:
        h = (p.height + q.height) / 2
        dx = abs(p.foot[0] - q.foot[0])
        dy = abs(p.foot[1] - q.foot[1])
        return dx < 0.45 * h and dy < 0.3 * h

    @staticmethod
    def down_near(g: Frame, p: Person, q: Person) -> list[Person]:
        """The people lying on the ground in frame g where p and q are (or were)."""
        mx, my = (p.foot[0] + q.foot[0]) / 2, (p.foot[1] + q.foot[1]) / 2
        reach = 1.2 * (p.height + q.height) / 2
        return [d for d in g.persons if d.down and ((d.foot[0] - mx) ** 2 + (d.foot[1] - my) ** 2) ** 0.5 < reach]

    def fallen(self, g: Frame, p: Person, q: Person) -> tuple[Person, Person] | None:
        """(victim, offender) when someone lies on the ground where p and q were in frame g; else None."""
        down = self.down_near(g, p, q)
        if not down:
            return None
        d = down[0]

        def standing(x: Person) -> bool:
            now = g.by_track(x.track)
            return bool(now and not now.down)

        gone = [x for x in (p, q) if not standing(x)] or [p, q]
        victim = min(gone, key=lambda x: (x.foot[0] - d.foot[0]) ** 2 + (x.foot[1] - d.foot[1]) ** 2)
        if d.height > FALL_HEIGHT * victim.height:
            return None
        return victim, (q if victim is p else p)

    def contacts(self, frames: list[Frame]) -> list[Event]:
        events, last, seen = [], -1e9, set()
        for i, f in enumerate(frames):
            if f.t - last < COOLDOWN / 2:
                continue
            players = [p for p in f.persons if p.on_pitch and not p.down and p.own in ("home", "away")
                       and p.height < CLOSE_UP * f.size[1]]

            def side(p: Person) -> str | None:
                """At the moment of contact, a detection's own colours (sure, see above) beat its track's history."""
                return p.own

            found = None
            for x, p in enumerate(players):
                for q in players[x + 1:]:
                    if side(p) == side(q) or not (p.sure or q.sure) or not self.touching(p, q) or (p.track, q.track) in seen:
                        continue
                    # Someone already on the ground there: the aftermath of a fall, not a new one
                    if self.down_near(f, p, q):
                        continue
                    for g in frames[i + 1:]:
                        if g.shot != f.shot or g.t - f.t > CONTACT_DOWN:
                            break
                        pair = self.fallen(g, p, q)
                        if pair:
                            found = (*pair, g)
                            break
                    if found:
                        seen.add((p.track, q.track))
                        break
                if found:
                    break
            if not found:
                continue
            victim, offender, g = found
            # The team each one plays for, as they were seen at the contact
            for x in (victim, offender):
                if x.own in ("home", "away"):
                    x.team = x.own
            details = {}
            if f.calib and victim.xy:
                details["in_penalty_area"] = self.pitch.in_penalty_area(*victim.xy)
            # Lower when one of the two wasn't clearly in their kit
            confidence = round((0.35 + 0.3 * min(victim.conf, offender.conf)) * (1.0 if victim.sure and offender.sure else 0.75), 2)
            events.append(Event("foul_contact", i, offender.team, [player(offender, "offender"), player(victim, "victim")],
                                details, confidence, why=f"contact, then track {victim.track} down {g.t - f.t:.1f} s later"))
            last = f.t
        return events

    # ── identity ──────────────────────────────────────────────────────────────

    @staticmethod
    def identity(frames: list[Frame], events: list[Event], cards: list[Card]) -> list[Event]:
        """Cards given to a number other than the one read on the offender of the contact just before."""
        out = []
        for card in cards:
            contacts = [e for e in events if e.type == "foul_contact" and e.team == card.team
                        and -IDENTITY_AFTER <= card.t - frames[e.frame].t <= IDENTITY_BEFORE]
            if not contacts:
                continue
            contact = max(contacts, key=lambda e: frames[e.frame].t)
            offender = next(p for p in contact.players if p["role"] == "offender")
            if offender["number"] is None or offender["number"] == card.number:
                continue
            likely = {**offender, "role": "likely_offender"}
            out.append(Event("identity", contact.frame, card.team,
                             [{"team": card.team, "number": card.number, "role": "carded", "track_id": None}, likely],
                             {"colour": card.colour}, round(0.9 * contact.confidence, 2),
                             why=f"{card.colour} card to #{card.number} at {card.t:.0f} s; "
                                 f"#{offender['number']} made the contact {card.t - frames[contact.frame].t:.0f} s before"))
        return out
