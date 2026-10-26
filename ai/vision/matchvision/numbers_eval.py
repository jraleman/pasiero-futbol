"""
Two shirt-number readers on the same crops: the vision-language model
(numbers.Ollama) and our own model (shirts.Onnx, trained on Azure ML). The
ONNX model replaces the vision-language model only if, on footage, it reads
more numbers and invents none it doesn't have to.

  cut      players' crops from a clip, as numbers.crop() makes them for the
           readers, every --every seconds, only the readable views (tall
           enough, standing, no one overlapping); and labels.json with every
           crop's number set to null, for a person to fill in (null: no number
           a person could read with certainty).

  compare  each reader on a folder of labelled crops: of the legible numbers,
           how many it reads right, reads wrong (misread) or leaves (unread);
           of the illegible ones, how many it reads anyway (invented).

    python -m matchvision.numbers_eval cut clips/kickabout.mp4 crops/ --every 1
    # fill in crops/labels.json
    python -m matchvision.numbers_eval compare crops/ --onnx models/shirt_numbers.onnx \\
        --ollama-url http://ollama:11434 --ollama-model gemma4:e2b

In the vision image: docker compose run --rm --entrypoint python vision -m matchvision.numbers_eval ...
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

from . import numbers
from .state import DOWN_ASPECT
from .track import iou


def cut(args: argparse.Namespace) -> int:
    from .detect import Models

    models = Models(args.models, imgsz=args.imgsz, device=args.device)
    capture = cv2.VideoCapture(str(args.clip))
    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(fps * args.every))
    args.out.mkdir(parents=True, exist_ok=True)
    labels_path = args.out / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.is_file() else {}
    index = saved = 0
    while True:
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, image = capture.read()
        if not ok:
            break
        boxes = [p.box for p in models.detect(image)[0]]
        for k, box in enumerate(boxes):
            x1, y1, x2, y2 = box
            others = boxes[:k] + boxes[k + 1:]
            if y2 - y1 < numbers.MIN_HEIGHT or x2 - x1 >= DOWN_ASPECT * (y2 - y1) or any(iou(box, o) >= numbers.MAX_OVERLAP for o in others):
                continue
            crop = numbers.crop(image, box)
            if crop is None:
                continue
            name = f"{args.clip.stem}-{index:06d}-{k:02d}.jpg"
            cv2.imwrite(str(args.out / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
            labels.setdefault(name, None)
            saved += 1
        index += step
    capture.release()
    labels_path.write_text(json.dumps(labels, indent=1, sort_keys=True))
    print(f"{saved} crops in {args.out}; label them in {labels_path} (a number, or null where none is legible)")
    return 0


def score(read, crops: list[tuple[str, object, int | None]]) -> dict:
    out = {"read": 0, "misread": 0, "unread": 0, "invented": 0, "legible": 0, "illegible": 0, "seconds": 0.0, "errors": []}
    started = time.time()
    for name, image, label in crops:
        got = read(image)
        if label is None:
            out["illegible"] += 1
            if got is not None:
                out["invented"] += 1
                out["errors"].append(f"{name}: read {got}, none legible")
        else:
            out["legible"] += 1
            if got is None:
                out["unread"] += 1
            elif got == label:
                out["read"] += 1
            else:
                out["misread"] += 1
                out["errors"].append(f"{name}: read {got}, is {label}")
    out["seconds"] = round((time.time() - started) / max(1, len(crops)), 3)
    return out


def compare(args: argparse.Namespace) -> int:
    labels = json.loads((args.crops / args.labels).read_text())
    crops = []
    for name, label in sorted(labels.items()):
        image = cv2.imread(str(args.crops / name))
        if image is None:
            print(f"{name}: not an image, skipped", file=sys.stderr)
            continue
        crops.append((name, image, int(label) if label is not None else None))
    readers = {}
    if args.ollama_model:
        readers[f"{args.ollama_model} (vision-language, Ollama)"] = numbers.Ollama(args.ollama_url, args.ollama_model)
    if args.onnx:
        from .shirts import Onnx

        readers[f"{args.onnx.name} (ours, ONNX)"] = Onnx(args.onnx, args.threshold)
    if not readers:
        raise SystemExit("Nothing to compare: give --onnx and/or --ollama-model")
    results = {}
    for name, read in readers.items():
        r = results[name] = score(read, crops)
        print(f"{name}: read {r['read']} of {r['legible']} legible numbers, misread {r['misread']}, "
              f"left {r['unread']}; invented {r['invented']} of {r['illegible']} illegible; {r['seconds']} s a crop")
        for line in r["errors"][:args.show_errors]:
            print(f"    {line}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=1))
    if len(results) == 2:
        vlm, ours = results.values()
        better = ours["read"] > vlm["read"] and ours["invented"] <= vlm["invented"] and ours["misread"] <= vlm["misread"] + 1
        print("→ " + ("the ONNX model reads more and invents no more: it can replace the vision-language model"
                      if better else "keep the vision-language model: the ONNX model doesn't beat it on these crops"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("cut", help="players' crops from a clip, and a labels.json to fill in")
    c.add_argument("clip", type=Path)
    c.add_argument("out", type=Path)
    c.add_argument("--every", type=float, default=1.0, help="seconds between the frames cut from")
    c.add_argument("--models", default="models")
    c.add_argument("--imgsz", type=int, default=1280)
    c.add_argument("--device", default="cpu")
    m = sub.add_parser("compare", help="the readers on labelled crops")
    m.add_argument("crops", type=Path, help="a folder of crops with labels.json")
    m.add_argument("--labels", default="labels.json", help="the labels' file in it (e.g. checked.json: shirts/label.py)")
    m.add_argument("--onnx", type=Path, help="our model (shirt_numbers.onnx)")
    m.add_argument("--threshold", type=float, help="how sure our model must be (default: shirts.THRESHOLD)")
    m.add_argument("--ollama-url", default="http://localhost:11434")
    m.add_argument("--ollama-model", help="the vision-language model, e.g. gemma4:e2b")
    m.add_argument("--json", type=Path, help="write the results here")
    m.add_argument("--show-errors", type=int, default=10)
    args = parser.parse_args(argv)
    return cut(args) if args.command == "cut" else compare(args)


if __name__ == "__main__":
    sys.exit(main())
