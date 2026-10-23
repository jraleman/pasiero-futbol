"""
The pitch, in the match contract's coordinates: metres, x along the length
from the left goal line as the main camera sees it, y across from the far
touchline. Law 1 sizes; the length and width are the ground's own
(PITCH_LENGTH, PITCH_WIDTH: grounds vary).

KEYPOINTS are the 32 landmarks the pitch-keypoint model finds, in its order
(roboflow/sports SoccerPitchConfiguration): the same points by meaning, at
this pitch's real size rather than the 120 × 70 m it was drawn at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PENALTY_AREA = (16.5, 40.32)
GOAL_AREA = (5.5, 18.32)
PENALTY_SPOT = 11.0
CIRCLE = 9.15
GOAL_WIDTH = 7.32


@dataclass(frozen=True)
class Pitch:
    length: float = 105.0
    width: float = 68.0
    keypoints: list[tuple[float, float]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        L, W = self.length, self.width
        pa_d, pa_w = PENALTY_AREA
        ga_d, ga_w = GOAL_AREA
        points = [
            (0, 0), (0, (W - pa_w) / 2), (0, (W - ga_w) / 2), (0, (W + ga_w) / 2), (0, (W + pa_w) / 2), (0, W),
            (ga_d, (W - ga_w) / 2), (ga_d, (W + ga_w) / 2),
            (PENALTY_SPOT, W / 2),
            (pa_d, (W - pa_w) / 2), (pa_d, (W - ga_w) / 2), (pa_d, (W + ga_w) / 2), (pa_d, (W + pa_w) / 2),
            (L / 2, 0), (L / 2, W / 2 - CIRCLE), (L / 2, W / 2 + CIRCLE), (L / 2, W),
            (L - pa_d, (W - pa_w) / 2), (L - pa_d, (W - ga_w) / 2), (L - pa_d, (W + ga_w) / 2), (L - pa_d, (W + pa_w) / 2),
            (L - PENALTY_SPOT, W / 2),
            (L - ga_d, (W - ga_w) / 2), (L - ga_d, (W + ga_w) / 2),
            (L, 0), (L, (W - pa_w) / 2), (L, (W - ga_w) / 2), (L, (W + ga_w) / 2), (L, (W + pa_w) / 2), (L, W),
            (L / 2 - CIRCLE, W / 2), (L / 2 + CIRCLE, W / 2),
        ]
        object.__setattr__(self, "keypoints", [(float(x), float(y)) for x, y in points])

    # ── what is where ─────────────────────────────────────────────────────────

    def segments(self) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """The straight lines painted on the pitch, to check a calibration against the image."""
        L, W = self.length, self.width
        pa_d, pa_w = PENALTY_AREA
        ga_d, ga_w = GOAL_AREA
        lines = [((0, 0), (L, 0)), ((0, W), (L, W)), ((0, 0), (0, W)), ((L, 0), (L, W)), ((L / 2, 0), (L / 2, W))]
        for x0, sign in ((0.0, 1), (L, -1)):
            for d, w in ((pa_d, pa_w), (ga_d, ga_w)):
                y0, y1 = (W - w) / 2, (W + w) / 2
                lines += [((x0, y0), (x0 + sign * d, y0)), ((x0, y1), (x0 + sign * d, y1)),
                          ((x0 + sign * d, y0), (x0 + sign * d, y1))]
        return lines

    def in_penalty_area(self, x: float, y: float) -> bool:
        depth, width = PENALTY_AREA
        return abs(y - self.width / 2) <= width / 2 and (x <= depth or x >= self.length - depth)

    def between_posts(self, y: float) -> bool:
        return abs(y - self.width / 2) <= GOAL_WIDTH / 2

    def outside(self, x: float, y: float) -> tuple[str, float] | None:
        """
        Which line the point is beyond, and by how many metres: ("touchline"
        or "goal_line", distance), or None on the pitch. The farther one wins.
        """
        beyond = []
        if y < 0:
            beyond.append(("touchline", -y))
        if y > self.width:
            beyond.append(("touchline", y - self.width))
        if x < 0:
            beyond.append(("goal_line", -x))
        if x > self.length:
            beyond.append(("goal_line", x - self.length))
        return max(beyond, key=lambda b: b[1]) if beyond else None
