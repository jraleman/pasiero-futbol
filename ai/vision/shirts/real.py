"""
Real players' crops for the shirt-number model, from SoccerTrack v2 (CC BY 4.0:
A. Scott, I. Uchida, K. Kuroda, Y. Kim, K. Fujii, arXiv:2508.01802): ten
university matches filmed by a fixed panorama, whose game-state files give
every player's shirt number, frame by frame.

    python3 ai/vision/shirts/real.py frames ai/vision/shirts/real/      # on the host (HF_TOKEN): frames and who's in them
    docker run ... brighton-vision:dev ai/vision/shirts/real.py crops /real   # in the vision image: the crops

frames reads small slices of each half's game-state file (gsr/<match>_<half>.json,
2.7 GB each, never downloaded whole), spread over the half; from each slice it
keeps the frame with the most players on the near side (the only ones tall
enough to read), and fetches that frame from the half's video by range
requests. Each frame lands in frames/ with its players: shirt number, team,
and where their feet are (the dataset's boxes are small squares at the feet,
projected from the tracking: not a person's box).

crops runs our detector on each frame and keeps the views numbers.py would
read (tall enough, standing, no one overlapping), cut with numbers.crop(), so
the model trains on what it's given in production. Each is matched to the
nearest player's feet, only when that's unambiguous, and labelled with their
number (1-99: the model reads two digits at most). The frame is deleted
after. Matches 128057 and 132831 (M7, M9: the dataset's own test split) go to
test/, the rest to train/; each folder has labels.json, numbers_eval's format.

The label is the player's number, not whether it shows: a player facing the
camera still has his. The training takes it as it is; the test set is checked
by a person (label.py), so its rates are honest.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
MATCHES = ["117092", "117093", "118575", "118576", "118577", "118578", "128057", "128058", "132831", "132877"]
TEST = {"128057", "132831"}             # M7 and M9: SoccerTrack's own test split
FPS = 25
SLICE = 300_000                         # bytes of a game-state file read at a time: about 7 frames
CANDIDATES = 3                          # slices looked at for each frame kept
NEAR_FRAME = 0.42                       # feet below this share of the height: tall enough to read, about
MIN_NEAR = 2                            # a frame is kept with at least this many players near
NEAR = 0.38                             # a player's feet below this share of the frame's height: the near side
MATCH_PX = 0.9                          # feet matched within this many of the detected player's heights
AMBIGUOUS = 1.6                         # the next nearest at least this much farther


# ── frames (host: the standard library and footage.py's helpers) ──────────────

def remote():
    sys.path.insert(0, str(PROJECT / "extras/scripts"))
    import footage

    return footage


def annotations(text: str) -> list[dict]:
    """The person annotations wholly inside a slice of a game-state file."""
    out = []
    for m in re.finditer(r'\{\s*"id": "\d+",\s*"image_id": "\d+",\s*"track_id"', text):
        depth = 0
        for i in range(m.start(), len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        out.append(json.loads(text[m.start():i + 1]))
                    except json.JSONDecodeError:
                        pass
                    break
    return out


def frame_of(match: str, half: str, offset: int, height: int, f, headers: dict) -> dict | None:
    """From the slice at [offset]: the frame with the most players on the near side, and its players."""
    url, _, needs = f.direct(f.hf_url(f.SOCCERTRACK, f"gsr/{match}/{match}_{half}.json"), headers)
    text = f.fetch_bytes(url, {**needs, "Range": f"bytes={offset}-{offset + SLICE - 1}"}).decode("utf-8", "replace")
    frames: dict[str, list[dict]] = {}
    for a in annotations(text):
        attrs, box = a.get("attributes") or {}, a.get("bbox_image") or {}
        if attrs.get("role") not in ("player", "goalkeeper") or not box:
            continue
        frames.setdefault(a["image_id"], []).append(
            {"number": int(attrs["jersey"]) if str(attrs.get("jersey", "")).isdigit() else None,
             "team": attrs.get("team"), "role": attrs["role"], "player_id": attrs.get("player_id"),
             "feet": [box["x"] + box["w"] / 2, box["y"] + box["h"] / 2]})
    if not frames:
        return None
    near = lambda people: sum(p["feet"][1] > NEAR_FRAME * height for p in people)  # noqa: E731
    image_id, people = max(frames.items(), key=lambda kv: near(kv[1]))
    return {"match": match, "half": half, "frame": int(image_id[-6:]) - 1, "near": near(people), "people": people}


def frames(args: argparse.Namespace) -> None:
    f = remote()
    headers = {"Authorization": f"Bearer {f.hf_token()}"}
    out = args.out / "frames"
    out.mkdir(parents=True, exist_ok=True)
    halves = []
    for match in args.matches:
        count = args.test_per_half if match in TEST else args.per_half
        for half in ("1st", "2nd"):
            for attempt in range(3):
                try:
                    url, size, needs = f.direct(f.hf_url(f.SOCCERTRACK, f"gsr/{match}/{match}_{half}.json"), headers)
                    head = f.fetch_bytes(url, {**needs, "Range": "bytes=0-4095"}).decode("utf-8", "replace")
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(5 * (attempt + 1))
            height = int(re.search(r'"height": (\d+)', head).group(1))
            # The first part of the file lists the frames, the annotations follow: slices over the rest,
            # CANDIDATES times as many as the frames wanted, since most catch play on the far side
            n = count * CANDIDATES
            halves.append((match, half, count, height, [int(size * (0.06 + 0.92 * (k + 0.5) / n)) for k in range(n)]))

    def patiently(what, *args):
        """[what] tried three times over the network; None (said) when it still fails."""
        for attempt in range(3):
            try:
                return what(*args)
            except (OSError, subprocess.SubprocessError, SystemExit) as e:
                if attempt == 2:
                    print(f"skipped after 3 tries: {e}", flush=True)
                    return None
                time.sleep(5 * (attempt + 1))

    def look(job: tuple) -> dict | None:
        match, half, height, offset = job
        return patiently(frame_of, match, half, offset, height, f, headers)

    def fetch(found: dict) -> str:
        return patiently(fetch_once, found) or "skipped"

    def fetch_once(found: dict) -> str:
        name = f"{found['match']}-{found['half']}-{found['frame']:06d}"
        if (out / f"{name}.json").is_file():
            return f"{name}: already there"
        video = f"videos/{found['match']}/{found['match']}_panorama_{found['half']}_half.mp4"
        url, _, needs = f.direct(f.hf_url(f.SOCCERTRACK, video), headers)
        subprocess.run(["ffmpeg", "-v", "error", "-y", *f.ffmpeg_headers(needs), "-ss", f"{found['frame'] / FPS:.3f}",
                        "-i", url, "-frames:v", "1", "-q:v", "2", "-f", "image2", str(out / f"{name}.part")],
                       check=True, timeout=300)
        (out / f"{name}.part").replace(out / f"{name}.jpg")
        (out / f"{name}.json").write_text(json.dumps(found))
        return f"{name}: {found['near']} players near"

    with ThreadPoolExecutor(args.workers) as pool:
        for match, half, count, height, offsets in halves:
            seen = [x for x in pool.map(look, [(match, half, height, o) for o in offsets]) if x]
            # The frames with most players near, as spread over the half as they come
            chosen = sorted(sorted(seen, key=lambda x: -x["near"])[:count], key=lambda x: x["frame"])
            chosen = [x for x in chosen if x["near"] >= MIN_NEAR]
            for line in pool.map(fetch, chosen):
                print(f"{match} {half}: {line}", flush=True)
            print(f"{match} {half}: {len(chosen)} frames of {len(seen)} looked at", flush=True)
    size = sum(p.stat().st_size for p in out.iterdir()) / 1e9
    print(f"{out}: {len(list(out.glob('*.jpg')))} frames, {size:.2f} GB; next: crops (in the vision image)")


# ── crops (the vision image: the detector, numbers.crop) ──────────────────────

def matched(box, people: list[dict]) -> dict | None:
    """The player whose feet are nearest the detected player's, if near enough and clearly the nearest."""
    import math

    x1, y1, x2, y2 = box
    fx, fy, h = (x1 + x2) / 2, y2, y2 - y1
    ranked = sorted((math.hypot(p["feet"][0] - fx, p["feet"][1] - fy), i) for i, p in enumerate(people))
    if not ranked or ranked[0][0] > MATCH_PX * h:
        return None
    if len(ranked) > 1 and ranked[1][0] < AMBIGUOUS * ranked[0][0]:
        return None
    return people[ranked[0][1]]


def crops(args: argparse.Namespace) -> None:
    import cv2

    sys.path.insert(0, str(HERE.parent))
    from matchvision import numbers
    from matchvision.detect import Models
    from matchvision.state import DOWN_ASPECT
    from matchvision.track import iou

    models = Models(args.models, imgsz=args.imgsz, device="cpu")
    labels = {split: {} for split in ("train", "test")}
    about = {split: {} for split in ("train", "test")}
    for split in labels:
        (args.out / split).mkdir(parents=True, exist_ok=True)
        path = args.out / split / "labels.json"
        if path.is_file():
            labels[split] = json.loads(path.read_text())
            about[split] = json.loads((args.out / split / "about.json").read_text())
    shots = sorted((args.out / "frames").glob("*.jpg"))
    for i, shot in enumerate(shots, 1):
        meta = json.loads(shot.with_suffix(".json").read_text())
        split = "test" if meta["match"] in TEST else "train"
        image = cv2.imread(str(shot))
        kept = 0
        if image is not None:
            near = [p for p in meta["people"] if p["feet"][1] > NEAR * image.shape[0]]
            boxes = [p.box for p in models.detect(image)[0]]
            for k, box in enumerate(boxes):
                x1, y1, x2, y2 = box
                others = boxes[:k] + boxes[k + 1:]
                if (y2 - y1 < numbers.MIN_HEIGHT or x2 - x1 >= DOWN_ASPECT * (y2 - y1)
                        or any(iou(box, o) >= numbers.MAX_OVERLAP for o in others)):
                    continue
                who = matched(box, near)
                if who is None or who["number"] is None or not 1 <= who["number"] <= 99:
                    continue
                crop = numbers.crop(image, box)
                if crop is None:
                    continue
                name = f"{shot.stem}-{k:02d}.jpg"
                cv2.imwrite(str(args.out / split / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
                labels[split][name] = who["number"]
                about[split][name] = {"match": meta["match"], "half": meta["half"], "frame": meta["frame"],
                                      "team": who["team"], "player_id": who["player_id"], "height": round(y2 - y1)}
                kept += 1
        print(f"[{i}/{len(shots)}] {shot.name}: {kept} crops ({split})", flush=True)
        if not args.keep_frames:
            shot.unlink()
            shot.with_suffix(".json").unlink()
        if i % 20 == 0 or i == len(shots):
            for s in labels:
                (args.out / s / "labels.json").write_text(json.dumps(labels[s], indent=1, sort_keys=True))
                (args.out / s / "about.json").write_text(json.dumps(about[s], indent=1, sort_keys=True))
    print(", ".join(f"{s}: {len(v)} crops" for s, v in labels.items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("frames", help="frames and who's in them (host, network)")
    f.add_argument("out", type=Path)
    f.add_argument("--matches", nargs="+", default=MATCHES)
    f.add_argument("--per-half", type=int, default=50, help="frames from each half of a training match")
    f.add_argument("--test-per-half", type=int, default=40, help="frames from each half of a test match")
    f.add_argument("--workers", type=int, default=4)
    c = sub.add_parser("crops", help="the players' crops, labelled (vision image)")
    c.add_argument("out", type=Path)
    c.add_argument("--models", default=str(HERE.parent / "models"))
    c.add_argument("--imgsz", type=int, default=1920)
    c.add_argument("--keep-frames", action="store_true")
    args = parser.parse_args()
    {"frames": frames, "crops": crops}[args.command](args)


if __name__ == "__main__":
    main()
