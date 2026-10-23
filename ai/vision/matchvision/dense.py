"""
Dense detection for the Video tab's overlay: the people and the ball, --fps
times a second, on the parts of each source the cameras loop
(extras/config/clips.json), without the pitch calibration (the overlay draws
boxes; it measures nothing).

    python -m matchvision.dense --manifest /config/clips.json --clips /clips --models /models --fps 10 --imgsz 960

Each frame gets the same work as analyze.py's detection stage: a cut starts a
new shot, people and ball from both detectors, who stands on the pitch, and
each person's kit colour (for the teams). A jump from one range to the next is
a cut too. Frames are taken by time, so 10 a second is 10 a second at 29.97 or
23.976 fps.

A source in the manifest may give its own "imgsz" (e.g. a wide panorama,
whose players are small at the default). Writes analysis/<source>/dense.json,
one source at a time (a source already done is skipped unless --force, so a
stopped run picks up where it was).
matchvision.overlay draws from it in place of the analysis's 2 frames a second.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from . import scene
from .analyze import feet_on_pitch, open_clip, pick_ball
from .state import Ball, Frame, Person
from .teams import body_pixels, kit_feature


def merged(ranges: list[list[float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def detect(capture, ranges, fps: float, models) -> list[Frame]:
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    frames: list[Frame] = []
    shot, previous_hist, ball, part = -1, None, None, None
    next_t, index, started = 0.0, 0, time.time()
    while capture.grab():
        t = index / source_fps
        index += 1
        here = next((i for i, (a, b) in enumerate(ranges) if a <= t < b), None)
        if here is None:
            if t >= ranges[-1][1]:
                break
            continue
        if here != part:
            part, next_t, previous_hist = here, ranges[here][0], None
        if t + 1e-6 < next_t:
            continue
        next_t += 1 / fps
        ok, image = capture.retrieve()
        if not ok:
            break
        h, w = image.shape[:2]
        hist = scene.histogram(image)
        if previous_hist is None or scene.is_cut(previous_hist, hist):
            shot += 1
            ball = None
        previous_hist = hist
        colour = scene.pitch_colour(image)
        pitch_px = scene.pitch_mask(image, colour)
        persons, balls = models.detect(image)
        for p in persons:
            p.on_pitch = feet_on_pitch(pitch_px, p.foot, p.height)
            p.pixels = body_pixels(image, p.box, colour)
            p.feature = kit_feature(p.pixels)
        ball = pick_ball(balls, ball, w)
        frames.append(Frame(index - 1, round(t, 3), shot, (w, h), persons, ball))
        if len(frames) % 50 == 0:
            print(f"  {t:6.1f} s, {len(frames)} frames, {time.time() - started:.0f} s", file=sys.stderr, flush=True)
    return frames


def save(path: Path, frames: list[Frame], meta: dict) -> None:
    rows = [{
        "index": f.index, "t": f.t, "shot": f.shot, "size": list(f.size),
        "ball": None if not f.ball else {"centre": [round(v, 1) for v in f.ball.centre], "conf": f.ball.conf},
        "persons": [{"box": [round(v, 1) for v in p.box], "conf": p.conf, "on_pitch": p.on_pitch,
                     "feature": None if p.feature is None else [round(float(v), 2) for v in p.feature],
                     # As cache.py keeps them: L, a, b and the height in the box in hundredths, whole numbers
                     "pixels": None if p.pixels is None else np.round(p.pixels * [1, 1, 1, 100]).astype(int).tolist()}
                    for p in f.persons],
    } for f in frames]
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps({"meta": meta, "frames": rows}, separators=(",", ":")))
    tmp.replace(path)


def load(path: Path) -> tuple[list[Frame], dict]:
    data = json.loads(path.read_text())
    frames = [Frame(
        r["index"], r["t"], r["shot"], tuple(r["size"]),
        [Person(tuple(p["box"]), p["conf"], -1, None if p["feature"] is None else np.float32(p["feature"]),
                None if p["pixels"] is None else np.float32(p["pixels"]) / [1, 1, 1, 100], p["on_pitch"])
         for p in r["persons"]],
        None if not r["ball"] else Ball(tuple(r["ball"]["centre"]), r["ball"]["conf"]),
    ) for r in data["frames"]]
    return frames, data["meta"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matchvision.dense", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, help="extras/config/clips.json")
    parser.add_argument("--clips", required=True, help="extras/clips: the sources, and analysis/<source>/")
    parser.add_argument("--models", default="/models")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--force", action="store_true", help="detect again a source already done")
    args = parser.parse_args(argv)

    from .detect import Models

    manifest = json.loads(Path(args.manifest).read_text())
    models = None
    for name, source in manifest["sources"].items():
        ranges = merged([r for c in manifest["cameras"].values() if c["source"] == name for r in c["ranges"]])
        out = Path(args.clips) / "analysis" / name
        target = out / "dense.json"
        if not ranges:
            continue
        if target.is_file() and not args.force:
            print(f"{name}: {target} already done", file=sys.stderr)
            continue
        models = models or Models(args.models, imgsz=args.imgsz, device=args.device)
        # A source may need a larger input than the rest (a wide panorama's small players)
        models.imgsz = imgsz = source.get("imgsz", args.imgsz)
        out.mkdir(parents=True, exist_ok=True)
        seconds = sum(b - a for a, b in ranges)
        print(f"{name}: {seconds:.0f} s in {len(ranges)} ranges at {args.fps:g} fps, {imgsz} px", file=sys.stderr)
        started = time.time()
        capture = open_clip(Path(args.clips) / source["file"], out)
        frames = detect(capture, ranges, args.fps, models)
        capture.release()
        elapsed = time.time() - started
        save(target, frames, {"fps": args.fps, "imgsz": imgsz, "ranges": ranges, "seconds": round(elapsed, 1)})
        print(f"{name}: {len(frames)} frames in {elapsed / 60:.1f} min → {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
