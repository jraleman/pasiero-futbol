"""
How the camera moved between two analysed frames, and calibrations carried
along with it.

A broadcast or match camera pans and zooms smoothly. Between two frames half a
second apart, the static background (the stands, the boards, the pitch's own
lines) moves by one homography, measured here from ORB features with the
players masked out.

That motion checks the model's calibrations too (verify()): two frames'
calibrations, one carried to the other through the motion, must place the
picture within AGREE metres of each other. A calibration no neighbour agrees
with is dropped: the keypoint model can pass the line gate with a wrong
answer (a worn pitch's bare patches, a caption over the near side), but two
wrong answers rarely agree. Then a frame without a calibration takes a
trusted neighbour's, carried through the motions: up to MAX_STEPS frames
away in the same shot, its confidence lowered at each step.
"""

from __future__ import annotations

from dataclasses import replace

import cv2
import numpy as np

from .state import Frame

MAX_STEPS = 4             # frames, 2 s at 2 frames a second
DECAY = 0.85              # confidence kept per step
MIN_MATCHES = 40
MIN_INLIERS = 30
AGREE = 2.5               # m, the median over the lower two thirds of the picture
VERIFY_STEPS = 2          # frames either side a calibration is checked against


class Motion:
    def __init__(self) -> None:
        self.orb = cv2.ORB_create(2000)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        self.previous = None

    def reset(self) -> None:
        self.previous = None

    def step(self, image: np.ndarray, boxes) -> np.ndarray | None:
        """The homography from this frame's image to the previous frame's, or None (first frame, too little to match)."""
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        mask = np.full(grey.shape, 255, np.uint8)
        for x1, y1, x2, y2 in boxes:
            mask[max(0, int(y1)):int(y2) + 1, max(0, int(x1)):int(x2) + 1] = 0
        points, descriptors = self.orb.detectAndCompute(grey, mask)
        found = None
        if self.previous is not None and descriptors is not None and self.previous[1] is not None:
            matches = self.matcher.match(descriptors, self.previous[1])
            if len(matches) >= MIN_MATCHES:
                here = np.float32([points[m.queryIdx].pt for m in matches])
                there = np.float32([self.previous[0][m.trainIdx].pt for m in matches])
                M, inliers = cv2.findHomography(here, there, cv2.RANSAC, 3.0)
                if M is not None and inliers is not None and int(inliers.sum()) >= MIN_INLIERS:
                    found = M
        self.previous = (points, descriptors)
        return found


def disagreement(frames: list[Frame], i: int, j: int) -> float | None:
    """How far apart (m, median) frames i and j's calibrations place frame i's picture, through the motion; None if unknown."""
    chain = carry(frames, i, j)
    if chain is None:
        return None
    w, h = frames[i].size
    grid = np.float64([[x, y] for x in np.linspace(0.1 * w, 0.9 * w, 5) for y in np.linspace(h / 3, 0.95 * h, 4)])
    own = frames[i].calib.to_pitch(grid)
    carried = cv2.perspectiveTransform(grid.reshape(-1, 1, 2), frames[j].calib.H @ chain).reshape(-1, 2)
    return float(np.median(np.linalg.norm(own - carried, axis=1)))


def verify(frames: list[Frame]) -> int:
    """Drop the model's calibrations that no neighbour agrees with; returns how many were dropped."""
    model = [i for i, f in enumerate(frames) if f.calib and not f.calib.steps and not f.calib.fixed]
    trusted = set()
    for i in model:
        for j in model:
            if j <= i or j - i > VERIFY_STEPS or frames[j].shot != frames[i].shot:
                continue
            d = disagreement(frames, i, j)
            if d is not None and d <= AGREE:
                trusted |= {i, j}
    dropped = 0
    for i in model:
        if i not in trusted:
            frames[i].calib = None
            frames[i].calib_reason = "consistency: no neighbouring calibration agrees with it"
            dropped += 1
    return dropped


def propagate(frames: list[Frame]) -> int:
    """Give uncalibrated frames a calibrated neighbour's calibration, through the camera's motion; returns how many."""
    own = [f.calib if f.calib and f.calib.steps == 0 else None for f in frames]
    filled = 0
    for i, f in enumerate(frames):
        if own[i] is not None:
            continue
        best = None
        for k in range(1, MAX_STEPS + 1):
            for j in (i - k, i + k):
                if not 0 <= j < len(frames) or own[j] is None or frames[j].shot != f.shot:
                    continue
                chain = carry(frames, i, j)
                if chain is not None:
                    best = (k, j, chain)
                    break
            if best:
                break
        if not best:
            if f.calib and f.calib.steps:
                f.calib = None
            continue
        k, j, chain = best
        source = own[j]
        f.calib = replace(source, H=source.H @ chain, confidence=round(source.confidence * DECAY ** k, 3), steps=k)
        f.calib_reason = f"ok: carried {k} frames from {frames[j].t:.1f} s"
        filled += 1
    return filled


def carry(frames: list[Frame], i: int, j: int) -> np.ndarray | None:
    """The homography from frame i's image to frame j's, through the motions between them; None if one is missing."""
    chain = np.eye(3)
    if j < i:
        # Back, frame by frame: k → k-1 is frames[k].motion
        for k in range(i, j, -1):
            if frames[k].motion is None:
                return None
            chain = frames[k].motion @ chain
    else:
        for k in range(i + 1, j + 1):
            if frames[k].motion is None:
                return None
            chain = np.linalg.inv(frames[k].motion) @ chain
    return chain
