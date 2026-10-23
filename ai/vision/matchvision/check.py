"""
Check a fixed camera's calibration: draws the pitch's lines, as the
calibration places them, over one of the camera's frames.

    python -m matchvision.check CLIP_OR_IMAGE CALIBRATION.json [--at SECONDS] [--out check.jpg]

The calibration file is image points with their pitch position (metres, x
from the left goal line, y from the far touchline), picked by hand on a
frame of the camera, at least 4, not 3 on one line:

    {"image_size": [1920, 1080], "points": [[px, py, x, y], ...]}

The lines must sit on the painted ones. If they don't, a point is wrong.
"""

from __future__ import annotations

import argparse
import sys

import cv2
import numpy as np

from .calib import fixed
from .pitch import Pitch


def frame_at(path: str, seconds: float) -> np.ndarray:
    image = cv2.imread(path)
    if image is not None:
        return image
    capture = cv2.VideoCapture(path)
    capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
    ok, image = capture.read()
    if not ok:
        raise SystemExit(f"can't read a frame of {path} at {seconds} s")
    return image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matchvision.check", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source")
    parser.add_argument("calibration")
    parser.add_argument("--at", type=float, default=0.0)
    parser.add_argument("--out", default="check.jpg")
    parser.add_argument("--pitch-length", type=float, default=105.0)
    parser.add_argument("--pitch-width", type=float, default=68.0)
    args = parser.parse_args(argv)
    pitch = Pitch(args.pitch_length, args.pitch_width)
    c = fixed(args.calibration, pitch)
    image = frame_at(args.source, args.at)
    thick = max(2, image.shape[1] // 640)
    inv = np.linalg.inv(c.H)
    for (x0, y0), (x1, y1) in pitch.segments():
        world = np.array([[(x0 + (x1 - x0) * k / 40, y0 + (y1 - y0) * k / 40)] for k in range(41)], dtype=np.float64)
        pts = cv2.perspectiveTransform(world, inv).reshape(-1, 2)
        ok = np.all(np.isfinite(pts), axis=1) & (np.abs(pts) < 1e5).all(axis=1)
        for a, b, keep in zip(pts, pts[1:], ok[:-1] & ok[1:]):
            if keep:
                cv2.line(image, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), (255, 0, 255), thick)
    centre = np.array([[(pitch.length / 2 + 9.15 * np.cos(a), pitch.width / 2 + 9.15 * np.sin(a))]
                       for a in np.linspace(0, 2 * np.pi, 72)])
    circle = cv2.perspectiveTransform(centre, inv).reshape(-1, 2)
    cv2.polylines(image, [circle.astype(np.int32)], True, (255, 0, 255), thick)
    cv2.imwrite(args.out, image)
    print(f"{args.out}: {c.points} points, {c.error_m} m mean error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
