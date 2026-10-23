"""
The detection stage, saved: every analysed frame's people, ball and
calibration (frames.json) and its picture (frames/), so the teams and the
rules can run again (--reuse) without the models: to tune a rule on a match
already analysed, or to try other kit colours.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .calib import Calibration
from .state import Ball, Frame, Person


def save(out: Path, frames: list[Frame], images: list[bytes], meta: dict) -> None:
    (out / "frames").mkdir(parents=True, exist_ok=True)
    rows = []
    for f, data in zip(frames, images):
        (out / "frames" / f"{f.index:06d}.jpg").write_bytes(data)
        rows.append({
            "index": f.index, "t": f.t, "shot": f.shot, "size": list(f.size), "calib_reason": f.calib_reason,
            # A frame's own calibration only: carried ones are worked out again on --reuse
            "calib": None if not f.calib or f.calib.steps else {"H": f.calib.H.tolist(), "confidence": f.calib.confidence,
                                                                "points": f.calib.points, "error_m": f.calib.error_m,
                                                                "line_score": f.calib.line_score, "fixed": f.calib.fixed},
            "motion": None if f.motion is None else f.motion.tolist(),
            "ball": None if not f.ball else {"centre": list(f.ball.centre), "conf": f.ball.conf, "xy": f.ball.xy and list(f.ball.xy)},
            "persons": [{"box": list(p.box), "conf": p.conf, "track": p.track, "on_pitch": p.on_pitch,
                         "feature": None if p.feature is None else [float(v) for v in p.feature],
                         # L, a, b and the height in the box in hundredths, as whole numbers
                         "pixels": None if p.pixels is None else np.round(p.pixels * [1, 1, 1, 100]).astype(int).tolist(),
                         "xy": p.xy and list(p.xy)} for p in f.persons],
        })
    (out / "frames.json").write_text(json.dumps({"meta": meta, "frames": rows}, default=_plain))


def _plain(value):
    """NumPy scalars as Python ones."""
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def load(out: Path) -> tuple[list[Frame], list[bytes], dict]:
    data = json.loads((out / "frames.json").read_text())
    frames, images = [], []
    for r in data["frames"]:
        c = r["calib"]
        b = r["ball"]
        frames.append(Frame(
            r["index"], r["t"], r["shot"], tuple(r["size"]),
            [Person(tuple(p["box"]), p["conf"], p["track"], None if p["feature"] is None else np.float32(p["feature"]),
                    None if p.get("pixels") is None else np.float32(p["pixels"]) / [1, 1, 1, 100],
                    p["on_pitch"], xy=p["xy"] and tuple(p["xy"])) for p in r["persons"]],
            None if not b else Ball(tuple(b["centre"]), b["conf"], b["xy"] and tuple(b["xy"])),
            None if not c else Calibration(np.array(c["H"]), c["confidence"], c["points"], c["error_m"], c["line_score"],
                                           fixed=c.get("fixed", False)),
            r["calib_reason"],
            None if r.get("motion") is None else np.array(r["motion"]),
        ))
        images.append((out / "frames" / f"{r['index']:06d}.jpg").read_bytes())
    return frames, images, data["meta"]
