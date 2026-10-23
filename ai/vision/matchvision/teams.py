"""
Which team a person plays for, from their shirt's colour.

Every detected person gives a sample of Lab pixels from shirt to socks
(pitch-coloured ones left out), and their shirt's median colour; lightness is
weighted down (LIGHTNESS) everywhere: sun and shade change how light a shirt
is far more than its hue.

The shirts' medians find the two kits. Each kit then gets a palette from its
clear views (shirt, shorts, socks), without the colours both teams share
(skin, white socks). A person goes to the team whose palette covers the
biggest share of their pixels: in a tangle, an opponent across the shirt
lowers that share, but the shorts and socks still count. Clustering all of them over the clip finds the kits: the two
biggest clusters of people standing on the pitch are the teams; the rest
(referee, goalkeepers, staff, spectators) are "other". A track takes the
team most of its frames voted for.

Which team is home comes from the kits named on the command line
(--home-kit red --away-kit green); without them, the bigger cluster is home,
and the result says so.
"""

from __future__ import annotations

from collections import Counter

import cv2
import numpy as np

from .scene import PITCH_DISTANCE
from .state import Frame

COLOURS = {
    "white": (255, 255, 255), "black": (20, 20, 20), "grey": (128, 128, 128), "red": (200, 20, 30),
    "blue": (20, 60, 200), "navy": (20, 30, 90), "sky": (110, 170, 230), "green": (20, 140, 60),
    "yellow": (240, 210, 30), "orange": (240, 120, 20), "purple": (110, 40, 140), "brown": (110, 70, 40),
    "pink": (240, 130, 180), "maroon": (110, 20, 40),
}
K = 4
# Kit colours are compared as Lab with L scaled by this
LIGHTNESS = 0.4
WEIGHT = np.array([LIGHTNESS, 1.0, 1.0], dtype=np.float32)
# A person whose nearest kit is a team's but further than this (Lab) is still "other"
MAX_TEAM_DISTANCE = 60.0
# Clusters closer than this (Lab) are one kit in different light
SAME_KIT = 25.0
# A pixel this close (weighted Lab) to a palette colour is that colour
PIXEL_NEAR = 15.0
# The share of a person's pixels a kit's palette must cover for them to wear it
MIN_SHARE = 0.15
# ... and to be sure of it (a contact alert needs both players sure)
SURE_SHARE = 0.3
SURE_LEAD = 0.1
PALETTE = 4
SAMPLE = 128


def lab_of(name: str) -> np.ndarray:
    """A colour name (COLOURS) or #rrggbb, as Lab on OpenCV's scale."""
    name = name.strip().lower()
    if name.startswith("#") and len(name) == 7:
        rgb = tuple(int(name[i:i + 2], 16) for i in (1, 3, 5))
    elif name in COLOURS:
        rgb = COLOURS[name]
    else:
        raise ValueError(f"unknown kit colour {name!r}: #rrggbb or one of {', '.join(COLOURS)}")
    pixel = np.uint8([[list(reversed(rgb))]])
    return cv2.cvtColor(pixel, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32) * WEIGHT


def body_pixels(frame: np.ndarray, box, pitch_lab: np.ndarray) -> np.ndarray | None:
    """
    Up to SAMPLE pixels from shirt to socks, without the pitch showing
    through, as rows of (L, a, b, height in the box 0-1); None if too few.
    """
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    h, w = y2 - y1, x2 - x1
    if h < 12 or w < 4:
        return None
    top, bottom = max(0, y1 + int(h * 0.18)), max(0, y1 + int(h * 0.95))
    crop = frame[top:bottom, max(0, x1 + int(w * 0.2)):max(0, x2 - int(w * 0.2))]
    if crop.size == 0:
        return None
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB).astype(np.float32)
    rows = (np.arange(crop.shape[0], dtype=np.float32)[:, None].repeat(crop.shape[1], 1) + top - y1) / h
    pixels = np.column_stack([lab.reshape(-1, 3), rows.reshape(-1)])
    keep = pixels[np.linalg.norm(pixels[:, :3] - pitch_lab, axis=1) > PITCH_DISTANCE]
    if len(keep) < 12:
        return None
    return keep[np.linspace(0, len(keep) - 1, min(SAMPLE, len(keep))).astype(int)]


def kit_feature(pixels: np.ndarray | None) -> np.ndarray | None:
    """The shirt's median colour (raw Lab): the pixels in the upper half of the box."""
    if pixels is None:
        return None
    shirt = pixels[pixels[:, 3] < 0.5]
    return np.median((shirt if len(shirt) >= 12 else pixels)[:, :3], axis=0)


def merge(centres: np.ndarray, labels: np.ndarray, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Join clusters closer than SAME_KIT: one kit split by sun and shade."""
    centres, labels = list(centres), labels.copy()
    while len(centres) > 2:
        pairs = [(np.linalg.norm(centres[i] - centres[j]), i, j) for i in range(len(centres)) for j in range(i + 1, len(centres))]
        distance, i, j = min(pairs)
        if distance >= SAME_KIT:
            break
        labels[labels == j] = i
        labels[labels > j] -= 1
        del centres[j]
        centres[i] = points[labels == i].mean(axis=0)
    return np.array(centres), labels


def kmeans(points: np.ndarray, k: int, iterations: int = 30) -> tuple[np.ndarray, np.ndarray]:
    """(centres, labels): Lloyd's algorithm from farthest-point seeds, so it's deterministic."""
    centres = [points[np.argmin(np.linalg.norm(points - np.median(points, axis=0), axis=1))]]
    while len(centres) < k:
        distance = np.min([np.linalg.norm(points - c, axis=1) for c in centres], axis=0)
        centres.append(points[np.argmax(distance)])
    centres = np.array(centres, dtype=np.float32)
    for _ in range(iterations):
        labels = np.argmin(np.linalg.norm(points[:, None] - centres[None], axis=2), axis=1)
        moved = np.array([points[labels == i].mean(axis=0) if np.any(labels == i) else centres[i] for i in range(k)])
        if np.allclose(moved, centres):
            break
        centres = moved
    return centres, labels


class Teams:
    def __init__(self, home_kit: str | None = None, away_kit: str | None = None) -> None:
        self.hints = (lab_of(home_kit), lab_of(away_kit)) if home_kit and away_kit else None
        self.kits: dict[str, np.ndarray] = {}
        self.others: list[np.ndarray] = []
        self.palettes: dict[str, np.ndarray] = {}
        self.other_palettes: list[np.ndarray] = []
        self.note = ""

    def fit(self, frames: list[Frame]) -> None:
        """Find the two kits among the people on the pitch over the whole clip."""
        # Big enough to have a clean shirt colour: far-off people mix in the background
        heights = [p.height for f in frames for p in f.persons if p.on_pitch]
        tall = 0.5 * float(np.median(heights)) if heights else 0
        features = np.array([p.feature for f in frames for p in f.persons
                             if p.on_pitch and p.feature is not None and p.height >= tall]) * WEIGHT
        if len(features) < 8:
            self.note = "too few players to tell the teams apart"
            return
        centres, labels = merge(*kmeans(features, min(K + 2, len(features))), features)
        sizes = Counter(labels.tolist())
        if self.hints:
            # The clusters nearest the kits given, among those big enough to be a team (spectators
            # on the grass can outnumber one of the teams)
            big = [i for i, n in sizes.items() if n >= max(3, 0.05 * len(features))]
            home, away = self.hints
            ia, ib = min(((i, j) for i in big for j in big if i != j),
                         key=lambda ij: np.linalg.norm(centres[ij[0]] - home) + np.linalg.norm(centres[ij[1]] - away))
            self.note = "home and away from the kits given"
        else:
            (ia, _), (ib, _) = sizes.most_common(2)
            self.note = "home is the bigger cluster: pass --home-kit and --away-kit to be sure"
        self.kits = {"home": centres[ia], "away": centres[ib]}
        # The other clusters: the referee, goalkeepers, staff, spectators standing near the pitch
        self.others = [c for i, c in enumerate(centres) if i not in (ia, ib)]
        self.palettes_from(frames, centres, ia, ib)

    def palettes_from(self, frames: list[Frame], centres: np.ndarray, ia: int, ib: int) -> None:
        """Each cluster's colours from shirt to socks, from the clear views of the people nearest it."""
        from .track import iou

        groups: dict[int, list[np.ndarray]] = {}
        for f in frames:
            for p in f.persons:
                if not p.on_pitch or p.pixels is None or p.feature is None or p.down:
                    continue
                if any(iou(p.box, q.box) >= 0.1 for q in f.persons if q is not p):
                    continue
                nearest = int(np.argmin(np.linalg.norm(centres - p.feature * WEIGHT, axis=1)))
                groups.setdefault(nearest, []).append(p.pixels[:, :3] * WEIGHT)
        palettes = {}
        for i, chunks in groups.items():
            pixels = np.concatenate(chunks)
            if len(pixels) > 6000:
                pixels = pixels[np.linspace(0, len(pixels) - 1, 6000).astype(int)]
            if len(pixels) >= PALETTE * 10:
                palettes[i] = kmeans(pixels, PALETTE)[0]

        def distinctive(own: np.ndarray, *others: np.ndarray) -> np.ndarray:
            keep = [c for c in own if all(np.min(np.linalg.norm(o - c, axis=1)) >= PIXEL_NEAR for o in others if len(o))]
            return np.array(keep) if keep else own[:0]

        home, away = palettes.get(ia, np.zeros((0, 3))), palettes.get(ib, np.zeros((0, 3)))
        self.palettes = {"home": distinctive(home, away), "away": distinctive(away, home)}
        self.other_palettes = [distinctive(p, home, away) for i, p in palettes.items() if i not in (ia, ib)]

    def label(self, feature: np.ndarray | None, pixels: np.ndarray | None = None) -> str:
        return self.judge(feature, pixels)[0]

    def judge(self, feature: np.ndarray | None, pixels: np.ndarray | None = None) -> tuple[str, bool]:
        """
        (team, sure): sure when the kit covers at least SURE_SHARE of the
        pixels and SURE_LEAD more than any other kit (the referee's, a
        spectator's). Without pixels, never sure.
        """
        if (feature is None and pixels is None) or not self.kits:
            return "other", False
        if pixels is not None and any(len(p) for p in self.palettes.values()):
            weighted = pixels[:, :3] * WEIGHT

            def share(palette: np.ndarray) -> float:
                if not len(palette):
                    return 0.0
                return float(np.mean(np.min(np.linalg.norm(weighted[:, None] - palette[None], axis=2), axis=1) < PIXEL_NEAR))

            shares = {team: share(palette) for team, palette in self.palettes.items()}
            other = max((share(p) for p in self.other_palettes), default=0.0)
            team = max(shares, key=shares.get)
            if not (shares[team] >= MIN_SHARE and shares[team] > other):
                return "other", False
            runner_up = max(other, *(v for t, v in shares.items() if t != team))
            return team, shares[team] >= SURE_SHARE and shares[team] - runner_up >= SURE_LEAD
        feature = feature * WEIGHT
        distances = {team: float(np.linalg.norm(feature - kit)) for team, kit in self.kits.items()}
        team = min(distances, key=distances.get)
        nearest_other = min((float(np.linalg.norm(feature - c)) for c in self.others), default=np.inf)
        return (team if distances[team] <= min(MAX_TEAM_DISTANCE, nearest_other) else "other"), False

    def assign(self, frames: list[Frame]) -> None:
        """
        Every person's team: their track's majority vote, or their own frame's
        if untracked. Only clear views on the pitch vote: a shirt half behind
        someone else's, or a player on the ground, shows mixed or wrong
        colours. A track with clear views on the pitch keeps its team in the
        frames the pitch mask misses its feet.
        """
        from .track import iou

        votes: dict[int, Counter] = {}
        for f in frames:
            for p in f.persons:
                p.own, p.sure = self.judge(p.feature, p.pixels) if p.on_pitch else ("other", False)
                p.team = p.own
                # Off the pitch says nothing about a player's kit (the mask misses some feet)
                clear = p.on_pitch and not p.down and all(iou(p.box, q.box) < 0.1 for q in f.persons if q is not p)
                if p.track >= 0 and clear:
                    votes.setdefault(p.track, Counter())[p.team] += 1
        for f in frames:
            for p in f.persons:
                if p.track in votes and (p.on_pitch or sum(votes[p.track].values()) >= 2):
                    (p.team, n), = votes[p.track].most_common(1)
                    # A track that saw the player clearly, consistently, vouches for this detection
                    if not p.sure and p.team == p.own and p.team in ("home", "away") \
                            and n >= 2 and n >= 0.7 * sum(votes[p.track].values()):
                        p.sure = True

    def kit_bgr(self, team: str) -> tuple[int, int, int]:
        """A team's kit colour, for drawing."""
        if team not in self.kits:
            return (200, 200, 200)
        lab = np.uint8([[np.clip(self.kits[team] / WEIGHT, 0, 255)]])
        return tuple(int(v) for v in cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)[0, 0])
