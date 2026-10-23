"""
A small tracker: matches each frame's people to the last frame's by box
overlap, or failing that by how far their feet moved relative to their
height. Frames are a fraction of a second apart and shots are short, so this
is enough to follow a player through a contact; a cut starts over. A box
whose kit colour changed a lot isn't the same player, however close: in a
tangle, an opponent steps into the spot a fallen player left.
"""

from __future__ import annotations

import numpy as np

from .state import Frame, Person

# Most a kit colour may change (Lab, lightness weighted down as in teams.py) between frames
MAX_COLOUR_CHANGE = 30.0
WEIGHT = np.array([0.4, 1.0, 1.0], dtype=np.float32)


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class Tracker:
    def __init__(self, min_iou: float = 0.2, max_step: float = 0.8, max_age: int = 2) -> None:
        self.min_iou, self.max_step, self.max_age = min_iou, max_step, max_age
        self.next_id = 0
        self.tracks: dict[int, tuple[Person, int]] = {}   # id → (last seen, frames since)

    def reset(self) -> None:
        self.tracks.clear()

    def update(self, persons: list[Person]) -> None:
        """Give each person a track id (in place)."""
        pairs = []
        for i, p in enumerate(persons):
            for tid, (q, _) in self.tracks.items():
                overlap = iou(p.box, q.box)
                step = ((p.foot[0] - q.foot[0]) ** 2 + (p.foot[1] - q.foot[1]) ** 2) ** 0.5 / max(1.0, max(p.height, q.height))
                if p.feature is not None and q.feature is not None and \
                        float(np.linalg.norm((p.feature - q.feature) * WEIGHT)) > MAX_COLOUR_CHANGE:
                    continue
                if overlap >= self.min_iou or step <= self.max_step:
                    pairs.append((overlap - step, i, tid))
        taken_people, taken_tracks = set(), set()
        for _, i, tid in sorted(pairs, reverse=True):
            if i in taken_people or tid in taken_tracks:
                continue
            persons[i].track = tid
            taken_people.add(i)
            taken_tracks.add(tid)
        for i, p in enumerate(persons):
            if i not in taken_people:
                p.track = self.next_id
                self.next_id += 1
        aged = {tid: (q, age + 1) for tid, (q, age) in self.tracks.items() if tid not in taken_tracks and age + 1 <= self.max_age}
        self.tracks = {**aged, **{p.track: (p, 0) for p in persons}}


def track(frames: list[Frame]) -> None:
    """Every person's track id, over the clip: a new shot starts over."""
    tracker, shot = Tracker(), None
    for f in frames:
        if f.shot != shot:
            tracker.reset()
            shot = f.shot
        tracker.update(f.persons)
