"""
What the pitch looks like in a frame, without any model: its colour (green
or the dry brown of a small ground), where it is, where its white lines are,
and when the shot changes.

The pitch's colour is the commonest colour in the lower two thirds of the
picture that spans most of its height; everything else is measured against it.
"""

from __future__ import annotations

import cv2
import numpy as np

# Lab distance (OpenCV's 0-255 scale) still counted as the pitch's colour
PITCH_DISTANCE = 22.0
# How much brighter than the pitch right around it (Lab lightness), and how grey, a line pixel is
LINE_BRIGHTER = 12
LINE_MAX_SATURATION = 80


def content(frame: np.ndarray) -> np.ndarray:
    """The picture without black letterbox or pillarbox bars."""
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    cols = np.where(grey.mean(axis=0) > 12)[0]
    rows = np.where(grey.mean(axis=1) > 12)[0]
    if len(cols) < 16 or len(rows) < 16:
        return frame
    return frame[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def pitch_colour(frame: np.ndarray) -> np.ndarray:
    """
    The pitch's Lab colour. Among the most common colours in the lower two
    thirds of the picture (the top is often sky or stands), the one present
    across the most rows: the pitch covers a tall stretch of the picture,
    where a caption banner or an advertising board is only a band. Refined as
    the median of the pixels near it.
    """
    picture = content(frame)
    h = picture.shape[0]
    small = cv2.resize(picture[h // 3:], (160, 60), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
    flat = lab.reshape(-1, 3)
    # The commonest colours on a coarse grid (8 Lab units)
    bins = np.floor(flat / 8).astype(np.int32)
    keys, counts = np.unique(bins[:, 0] * 10000 + bins[:, 1] * 100 + bins[:, 2], return_counts=True)
    best, best_score = None, -1.0
    for key in keys[np.argsort(counts)[::-1][:5]]:
        mode = np.float32([key // 10000, (key // 100) % 100, key % 100]) * 8 + 4
        near = np.linalg.norm(lab - mode, axis=2) < PITCH_DISTANCE
        rows = np.mean(near.mean(axis=1) >= 0.2)
        score = rows * near.mean()
        if score > best_score:
            best, best_score = mode, score
    near = flat[np.linalg.norm(flat - best, axis=1) < PITCH_DISTANCE]
    return np.median(near, axis=0) if len(near) else best


def pitch_mask(frame: np.ndarray, colour: np.ndarray | None = None) -> np.ndarray:
    """uint8 mask (255 = pitch): pixels of the pitch's colour, with its lines and players' gaps closed."""
    colour = pitch_colour(frame) if colour is None else colour
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB).astype(np.float32)
    distance = np.linalg.norm(lab - colour, axis=2)
    mask = (distance < PITCH_DISTANCE).astype(np.uint8) * 255
    k = max(3, frame.shape[1] // 120) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))


def line_mask(frame: np.ndarray, colour: np.ndarray | None = None, pitch: np.ndarray | None = None) -> np.ndarray:
    """
    uint8 mask of the white lines: thin strokes brighter than the pitch
    around them (a top-hat on lightness, so faint lines in fog or at night
    count), grey rather than coloured, on the pitch (never its edges: walls,
    boards, goals).
    """
    h, w = frame.shape[:2]
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    k = max(9, w // 80) | 1
    tophat = cv2.morphologyEx(lab[:, :, 0], cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    mask = ((tophat > LINE_BRIGHTER) & (hsv[:, :, 1] < LINE_MAX_SATURATION)).astype(np.uint8) * 255
    if pitch is not None:
        mask &= cv2.erode(pitch, np.ones((9, 9), np.uint8))
    return mask


def histogram(frame: np.ndarray) -> np.ndarray:
    small = cv2.resize(content(frame), (160, 90))
    hist = cv2.calcHist([cv2.cvtColor(small, cv2.COLOR_BGR2HSV)], [0, 1, 2], None, [16, 8, 8], [0, 180, 0, 256, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def is_cut(previous: np.ndarray | None, current: np.ndarray, threshold: float = 0.5) -> bool:
    """
    Whether the shot changed between two frames' colour histograms (a cut, or a
    pan too fast to follow), as Bhattacharyya distance: within a shot it stays
    under 0.4 at two frames a second; across a cut it's over 0.5.
    """
    if previous is None:
        return True
    return cv2.compareHist(previous, current, cv2.HISTCMP_BHATTACHARYYA) > threshold
