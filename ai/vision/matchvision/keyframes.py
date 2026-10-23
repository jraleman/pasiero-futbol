"""
An analysis's keyframes drawn again, for when only the drawing changed
(output.draw): from its saved detection stage (analyze.py --keep-frames) and
its alerts.json, without the models or the rules, so the alerts, their ids,
their players and their numbers stay as they are.

    python -m matchvision.keyframes --analysis /clips/analysis/<source>
    python -m matchvision.keyframes --analysis /clips/analysis/dataset/03-foul --boxes /clips/analysis/dataset/03-foul/players.json

Each alert's frame is the one its evidence points at (clip_uri #t=), its
players are found by their track ids, and its shirt numbers are the ones the
alert carries. Writes keyframes/<alert_id>.jpg over the old ones. --boxes
also writes where each alert's players are in its frame: {alert_id: {"t",
"size": [w, h], "players": {role: [x1, y1, x2, y2]}}}, in pixels (e.g. to
find them in another cut of the same footage).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

from .events import Event
from .output import draw, jpeg
from .overlay import analysed_teams
from .pitch import Pitch


def moment(doc: dict) -> float:
    """The alert's frame time: its evidence's #t=, else its match time."""
    uri = (doc.get("evidence") or {}).get("clip_uri") or ""
    return float(uri.rsplit("#t=", 1)[1]) if "#t=" in uri else float(doc["match_time_s"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matchvision.keyframes", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--analysis", required=True, type=Path, help="the analysis folder (alerts.json, frames.json, frames/)")
    parser.add_argument("--boxes", type=Path, help="also write each alert's players' boxes to this JSON file")
    args = parser.parse_args(argv)

    folder: Path = args.analysis
    frames, teams = analysed_teams(folder)
    pitch = Pitch()
    boxes = {}
    for doc in json.loads((folder / "alerts.json").read_text()):
        at = moment(doc)
        i = min(range(len(frames)), key=lambda k: abs(frames[k].t - at))
        f = frames[i]
        numbers = {p["track_id"]: p["number"] for p in doc["players"] if p.get("track_id") is not None}
        for person in f.persons:
            if numbers.get(person.track) is not None:
                person.number = numbers[person.track]
        event = Event(doc["type"], i, doc.get("team"), players=[p for p in doc["players"] if p.get("track_id") is not None])
        roles = {p["track_id"]: p["role"] for p in event.players}
        boxes[doc["alert_id"]] = {"t": f.t, "size": list(f.size), "players": {
            roles[person.track]: [round(v, 1) for v in person.box] for person in f.persons if person.track in roles}}
        image = cv2.imdecode(np.frombuffer((folder / "frames" / f"{f.index:06d}.jpg").read_bytes(), np.uint8),
                             cv2.IMREAD_COLOR)
        out = folder / "keyframes" / f"{doc['alert_id']}.jpg"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(jpeg(draw(image, f, teams, pitch, event, label=doc["alert_id"], keyframe=True)))
        print(f"{doc['alert_id']}: {doc['type']} at {f.t:.1f} s → {out.name}", file=sys.stderr)
    if args.boxes:
        args.boxes.write_text(json.dumps(boxes, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
