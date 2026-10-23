"""
From the pitch-keypoint model's points to a homography: image pixels →
pitch metres on the ground plane.

A keypoint model trained on high broadcast views happily finds "keypoints" in
the sky or the stands of a low, side-on shot. A wrong homography is worse
than none: it would draw an offside line through the crowd. So a calibration
has to pass every gate, and the frame gets no pitch coordinates otherwise
(the reason says which gate failed):

  points    at least 4 confident keypoints, each on the pitch's colour
  spread    the points cover a real area of the pitch, not a line
  fit       RANSAC keeps most of them, with a small error in metres
  lines     the pitch's painted lines, projected back into the image, land
            on white pixels inside the pitch, on at least two different
            lines, one of them across the pitch (a goal line, the halfway
            line, a penalty area's front): touchlines alone match almost
            any homography that gets the camera's tilt right

Keypoints alone place a pitch a metre or two off. Before the line gate, the
homography is refined against the white lines themselves (refine(): each
projected line point snaps to the nearest white pixel, the homography is
refit, again with a smaller reach), and kept if it fits the lines better and
still agrees with the keypoints.

A fixed stadium camera doesn't need the model: it is calibrated once, from
points an operator picks (fixed()), and keeps that homography all match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .pitch import Pitch

MIN_CONF = 0.5
MIN_POINTS = 4
MIN_AREA_M2 = 120.0
RANSAC_M = 1.5
MAX_ERROR_M = 1.2
MIN_INLIER_SHARE = 0.6
MIN_LINE_SAMPLES = 40
MIN_LINE_SCORE = 0.25
MIN_LINES_HIT = 2
# A painted line counts as found when this share of its visible samples lands on white
LINE_HIT_SHARE = 0.3


@dataclass
class Calibration:
    H: np.ndarray            # image (px) → pitch (m)
    confidence: float
    points: int
    error_m: float
    line_score: float
    steps: int = 0           # 0: this frame's own; n: carried n frames through the camera's motion (motion.py)
    fixed: bool = False      # an operator's calibration of a fixed camera, not the model's

    def to_pitch(self, points: np.ndarray) -> np.ndarray:
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(pts, self.H).reshape(-1, 2)

    def to_image(self, points: np.ndarray) -> np.ndarray:
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
        return cv2.perspectiveTransform(pts, np.linalg.inv(self.H)).reshape(-1, 2)


def _on_pitch(mask: np.ndarray, x: float, y: float, radius: int) -> bool:
    h, w = mask.shape
    if not (0 <= x < w and 0 <= y < h):
        return False
    x0, x1 = max(0, int(x) - radius), min(w, int(x) + radius + 1)
    y0, y1 = max(0, int(y) - radius), min(h, int(y) + radius + 1)
    return mask[y0:y1, x0:x1].mean() / 255 >= 0.25


def segment_samples(pitch: Pitch, step_m: float = 0.5) -> list[tuple[np.ndarray, bool]]:
    """Points along each painted line, every step_m metres, and whether the line runs across the pitch."""
    out = []
    for (x0, y0), (x1, y1) in pitch.segments():
        n = max(2, int(np.hypot(x1 - x0, y1 - y0) / step_m))
        world = np.array([(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n) for k in range(n + 1)], dtype=np.float64)
        out.append((world, x0 == x1))
    return out


def project(H: np.ndarray, world: np.ndarray, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """(image points, which of them are inside the image), for pitch points under image→pitch homography H."""
    h, w = shape
    image = cv2.perspectiveTransform(world.reshape(-1, 1, 2), np.linalg.inv(H)).reshape(-1, 2)
    inside = np.all(np.isfinite(image), axis=1) & (image[:, 0] >= 2) & (image[:, 0] < w - 2) & (image[:, 1] >= 2) & (image[:, 1] < h - 2)
    return image, inside


def line_score(H: np.ndarray, pitch: Pitch, lines: np.ndarray) -> tuple[float, int, int, int]:
    """
    (share of the visible painted-line samples that land on a white pixel,
    how many were visible, how many different painted lines were found, how
    many of those run across the pitch).
    """
    tolerance = max(7, lines.shape[1] // 160) | 1
    near = cv2.dilate(lines, np.ones((tolerance, tolerance), np.uint8))
    hits = visible = found = across = 0
    for world, transversal in segment_samples(pitch):
        image, inside = project(H, world, lines.shape)
        seen = image[inside].astype(int)
        on = int(np.count_nonzero(near[seen[:, 1], seen[:, 0]])) if len(seen) else 0
        hits, visible = hits + on, visible + len(seen)
        if len(seen) >= 8 and on / len(seen) >= LINE_HIT_SHARE:
            found += 1
            across += transversal
    return (hits / visible if visible else 0.0), visible, found, across


def refine(H: np.ndarray, pitch: Pitch, lines: np.ndarray) -> np.ndarray:
    """
    The homography fitted to the white lines: every projected line point
    snaps to the nearest white pixel within a reach that shrinks each round,
    and the homography is refit to those pairs (iterative closest point).
    """
    ys, xs = np.nonzero(lines)
    if len(xs) < 50:
        return H
    # Distance to, and index of, the nearest white pixel, for every pixel
    distance, label = cv2.distanceTransformWithLabels(255 - lines, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    world = np.concatenate([w for w, _ in segment_samples(pitch, 0.5)])
    scale = lines.shape[1] / 1280
    for reach in (40, 28, 18, 12, 8, 5):
        image, inside = project(H, world, lines.shape)
        pts = image[inside].astype(int)
        d = distance[pts[:, 1], pts[:, 0]]
        close = d < reach * scale
        if close.sum() < 30:
            break
        nearest = label[pts[close, 1], pts[close, 0]] - 1
        target = np.column_stack([xs[nearest], ys[nearest]]).astype(np.float64)
        refit, _ = cv2.findHomography(target, world[inside][close], cv2.RANSAC, 0.75)
        if refit is None:
            break
        H = refit
    return H


def calibrate(xy: np.ndarray, conf: np.ndarray, pitch: Pitch, pitch_px: np.ndarray, lines_px: np.ndarray,
              min_conf: float = MIN_CONF) -> tuple[Calibration | None, str]:
    """xy (32, 2) and conf (32,) from the keypoint model; masks from scene.py. Returns (calibration or None, reason)."""
    h, w = pitch_px.shape
    radius = max(6, w // 100)
    chosen = [i for i in range(min(len(conf), len(pitch.keypoints)))
              if conf[i] >= min_conf and _on_pitch(pitch_px, xy[i][0], xy[i][1], radius)]
    if len(chosen) < MIN_POINTS:
        return None, f"points: {len(chosen)} confident keypoints on the pitch, under {MIN_POINTS}"
    image = np.array([xy[i] for i in chosen], dtype=np.float64)
    world = np.array([pitch.keypoints[i] for i in chosen], dtype=np.float64)
    hull = cv2.convexHull(world.astype(np.float32))
    area = cv2.contourArea(hull)
    if area < MIN_AREA_M2:
        return None, f"spread: the keypoints cover {area:.0f} m², under {MIN_AREA_M2:.0f}"
    H, inliers = cv2.findHomography(image, world, cv2.RANSAC, RANSAC_M)
    if H is None or inliers is None:
        return None, "fit: no homography"
    keep = inliers.ravel().astype(bool)
    share = keep.mean()
    if keep.sum() < MIN_POINTS or share < MIN_INLIER_SHARE:
        return None, f"fit: {keep.sum()} of {len(keep)} keypoints agree"
    projected = cv2.perspectiveTransform(image[keep].reshape(-1, 1, 2), H).reshape(-1, 2)
    error = float(np.mean(np.linalg.norm(projected - world[keep], axis=1)))
    if error > MAX_ERROR_M:
        return None, f"fit: {error:.2f} m mean error, over {MAX_ERROR_M}"
    # The bottom middle of the frame (the nearest ground the camera sees) must land near the pitch
    near = cv2.perspectiveTransform(np.array([[[w / 2, h - 1]]], dtype=np.float64), H).ravel()
    if not np.all(np.isfinite(near)) or not (-30 < near[0] < pitch.length + 30 and -30 < near[1] < pitch.width + 30):
        return None, "fit: the frame's foreground lands far off the pitch"
    score, visible, found, across = line_score(H, pitch, lines_px)
    refined = refine(H, pitch, lines_px)
    if refined is not H:
        r_score, r_visible, r_found, r_across = line_score(refined, pitch, lines_px)
        # Better on the lines, and still where the keypoints say (a refit can slide along a line)
        agree = cv2.perspectiveTransform(image[keep].reshape(-1, 1, 2), refined).reshape(-1, 2)
        r_error = float(np.mean(np.linalg.norm(agree - world[keep], axis=1)))
        if r_score > score and r_error <= 2.5 * MAX_ERROR_M:
            H, score, visible, found, across, error = refined, r_score, r_visible, r_found, r_across, r_error
    if visible < MIN_LINE_SAMPLES or score < MIN_LINE_SCORE or found < MIN_LINES_HIT or across < 1:
        return None, (f"lines: {score:.0%} of {visible} projected line samples on white, "
                      f"{found} lines found, {across} across the pitch")
    confidence = float(np.clip(0.4 * share + 0.6 * min(1.0, score / 0.5), 0, 1))
    return Calibration(H, round(confidence, 3), int(keep.sum()), round(error, 3), round(score, 3)), "ok"


def fixed(path: str | Path, pitch: Pitch) -> Calibration:
    """
    A fixed camera's calibration, from a file of points an operator picked on
    one of its frames:

        {"image_size": [1920, 1080],
         "points": [[px, py, x, y], ...]}    image pixels → pitch metres, at least 4

    Pitch metres as the match contract has them (x from the left goal line,
    y from the far touchline): corners, the halfway line's ends, the penalty
    areas' corners are easy to pick. The fit's error is checked like the
    model's.
    """
    data = json.loads(Path(path).read_text())
    points = np.array(data["points"], dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 4 or len(points) < MIN_POINTS:
        raise ValueError(f"{path}: points must be at least {MIN_POINTS} [px, py, x, y]")
    H, _ = cv2.findHomography(points[:, :2], points[:, 2:], 0)
    if H is None:
        raise ValueError(f"{path}: the points don't define a homography (are 3 of them on one line?)")
    projected = cv2.perspectiveTransform(points[:, :2].reshape(-1, 1, 2), H).reshape(-1, 2)
    error = float(np.mean(np.linalg.norm(projected - points[:, 2:], axis=1)))
    if error > MAX_ERROR_M:
        raise ValueError(f"{path}: the points disagree by {error:.2f} m on average; check them")
    return Calibration(H, 0.9, len(points), round(error, 3), 1.0, fixed=True)
