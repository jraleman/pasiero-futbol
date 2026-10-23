"""What the pipeline knows about one analysed frame."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .calib import Calibration

# A player is "down" when their box is wider than this share of its height
DOWN_ASPECT = 1.05


@dataclass
class Person:
    box: tuple[float, float, float, float]        # x1, y1, x2, y2 in pixels
    conf: float
    track: int = -1
    feature: np.ndarray | None = None              # kit colour (Lab median), see teams.py
    pixels: np.ndarray | None = None               # a sample of pixels, shirt to socks: (L, a, b, height in box)
    on_pitch: bool = True
    team: str | None = None                        # home, away, other (referee, staff, spectators): the track's
    own: str | None = None                         # ... and this detection's own, from its pixels alone
    sure: bool = False                             # whether that own label is clear (teams.judge), or its track vouches for it
    xy: tuple[float, float] | None = None          # pitch metres, when the frame is calibrated
    number: int | None = None                      # shirt number, when its track's was read (numbers.py)

    @property
    def foot(self) -> tuple[float, float]:
        x1, _, x2, y2 = self.box
        return ((x1 + x2) / 2, y2)

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def down(self) -> bool:
        return self.width > DOWN_ASPECT * self.height

    @property
    def playing(self) -> bool:
        return self.on_pitch and self.team in ("home", "away")


@dataclass
class Ball:
    centre: tuple[float, float]
    conf: float
    xy: tuple[float, float] | None = None


@dataclass
class Frame:
    index: int                     # in the video
    t: float                       # seconds from the clip's start
    shot: int
    size: tuple[int, int]          # width, height
    persons: list[Person] = field(default_factory=list)
    ball: Ball | None = None
    calib: Calibration | None = None
    calib_reason: str = ""
    motion: np.ndarray | None = None                # homography from this frame's image to the previous one's

    def by_track(self, track: int) -> Person | None:
        return next((p for p in self.persons if p.track == track), None)
