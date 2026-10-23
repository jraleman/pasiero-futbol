"""
Offline checks for the shirt-number model's code (ai/vision): what the model is
given (matchvision/shirts.py prepare), how its answer is read (decode, with
"never guess"), the labels it's trained on (target), the synthetic players
(shirts/synth.py), and, with PyTorch installed, the ONNX export read back by
the reader. No network, no Azure.

    python3 tests/test_shirts.py      # numpy, opencv-python-headless, pillow; torch, onnx, onnxruntime optional
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "ai/vision"), str(ROOT / "ai/vision/shirts")]

try:
    import cv2
    import numpy as np
except ImportError:
    print("skipped: pip install numpy opencv-python-headless pillow")
    sys.exit(0)

from matchvision import shirts  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else f"  {detail}"))
    failed |= not ok


print("== what the model sees")
crop = np.zeros((448, 260, 3), np.uint8)
crop[int(0.1 * 448):int(0.62 * 448)] = (0, 0, 255)          # the shirt band, red (BGR)
x = shirts.prepare(crop)
check("SIZE × SIZE, channels first, 0–1", x.shape == (3, shirts.SIZE, shirts.SIZE) and x.dtype == np.float32 and 0 <= x.min() <= x.max() <= 1, x.shape)
check("only the band from the shoulders to the shorts, in RGB", x[0].mean() > 0.98 and x[1].max() == 0 and x[2].max() == 0,
      (x[0].mean(), x[1].max(), x[2].max()))
check("a tiny crop still makes an input", shirts.prepare(np.zeros((12, 6, 3), np.uint8)).shape == (3, shirts.SIZE, shirts.SIZE))

print("== labels and answers")
check("labels: none, one digit, two", [shirts.target(n) for n in (None, 7, 23, 90)] == [(0, -1, -1), (1, 7, -1), (2, 2, 3), (2, 9, 0)])


def logits_for(number, sure: float = 12.0) -> np.ndarray:
    out = np.zeros(shirts.OUTPUTS, np.float32)
    n, a, b = shirts.target(number)
    out[n] = sure
    if a >= 0:
        out[shirts.LENGTHS + a] = sure
    if b >= 0:
        out[shirts.LENGTHS + shirts.DIGITS + b] = sure
    return out


check("every number 1–99 reads back", all(shirts.decode(logits_for(n))[0] == n for n in range(1, 100)),
      [n for n in range(1, 100) if shirts.decode(logits_for(n))[0] != n][:5])
check("none legible: None", shirts.decode(logits_for(None))[0] is None)
check("unsure: None, never a guess", shirts.decode(logits_for(23, sure=1.0))[0] is None and shirts.decode(logits_for(23, sure=1.0), 0.0)[0] == 23,
      shirts.decode(logits_for(23, sure=1.0)))
two = logits_for(5)
two[shirts.LENGTHS + 0] = 30                                  # a leading 0 is never a number
two[[1, 2]] = [0, 12]
check("a two-digit number never starts with 0", shirts.decode(two, 0.0)[0] == 50, shirts.decode(two, 0.0))

print("== synthetic players")
try:
    import synth
except ImportError as e:
    synth = None
    print(f"skipped ({e})")
if synth:
    font = synth.FALLBACK
    players = synth.Players([font], seed=3)
    samples = [players.sample() for _ in range(300)]
    whys = Counter(s.why for s in samples)
    check("player crops, CANVAS tall, BGR", all(s.image.shape[0] == synth.CANVAS and s.image.shape[2] == 3 for s in samples))
    check("labels are 1–99 or None", all(s.number is None or 1 <= s.number <= 99 for s in samples))
    check("a legible number is the one printed", all(s.number == s.shown for s in samples if s.number is not None))
    check("None says why: the front, turned, hidden, blank, tiny", set(whys) - {"number"} <= {"front", "turned", "hidden", "blank", "tiny"} and whys["number"] > 120
          and whys["front"] > 20 and whys["hidden"] > 10, whys)
    again = synth.Players([font], seed=3).sample()
    check("a seed always draws the same player", np.array_equal(again.image, samples[0].image) and again.number == samples[0].number)
    check("the clubs' kits are among the kits", len(players.kits) > len(synth.KITS), len(players.kits))
    check("held-out fonts are never trained on", set(synth.HELD_OUT) <= set(synth.FONTS) and len(synth.FONTS) - len(synth.HELD_OUT) >= 12)

print("== the ONNX export, read back")
try:
    import onnxruntime  # noqa: F401
    import torch
    import train
except ImportError as e:
    torch = None
    print(f"skipped ({e})")
if torch:
    with tempfile.TemporaryDirectory() as tmp:
        model = train.Reader(pretrained=False).eval()
        path = Path(tmp) / "shirt_numbers.onnx"
        train.export(model, path, {"size": shirts.SIZE, "band": list(shirts.BAND), "trained": "test"})
        reader = shirts.Onnx(path)
        crops = [samples[i].image for i in range(4)] if synth else [crop] * 4
        with torch.no_grad():
            expected = model(torch.from_numpy(np.stack([shirts.prepare(c) for c in crops]))).numpy()
        got = reader.logits(crops)
        check("ONNX Runtime gives PyTorch's logits, a batch at a time", got.shape == (4, shirts.OUTPUTS) and np.allclose(got, expected, atol=1e-4),
              float(np.abs(got - expected).max()))
        check("the reader answers a number or None", all(reader(c) is None or 1 <= reader(c) <= 99 for c in crops))
        check("what it was trained on, in its metadata", reader.about.get("trained") == "test")
        bad = Path(tmp) / "other.onnx"
        train.export(model, bad, {"size": 64, "band": [0.0, 1.0]})
        try:
            shirts.Onnx(bad)
            check("a model trained on another input is refused", False)
        except ValueError:
            check("a model trained on another input is refused", True)

    print("== a short training run, end to end")
    import shutil
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        fonts = Path(tmp) / "fonts"
        fonts.mkdir()
        for name in synth.FONTS:                    # no download: the repository's font under every name
            shutil.copy(synth.FALLBACK, fonts / name)
        out = Path(tmp) / "out"
        # 3 workers, samples that don't divide among them or into batches: the schedule's steps must still add up
        run = subprocess.run([sys.executable, str(ROOT / "ai/vision/shirts/train.py"), "--out", str(out), "--epochs", "2",
                              "--per-epoch", "200", "--batch", "32", "--workers", "3", "--val", "40", "--no-pretrained",
                              "--fonts", str(fonts)], capture_output=True, text=True, timeout=600)
        check("train.py runs to the end and exports", run.returncode == 0 and (out / "shirt_numbers.onnx").is_file()
              and not (out / "best.pt").exists(), run.stderr[-800:])
        if run.returncode == 0:
            metrics = json.loads((out / "metrics.json").read_text())
            check("…with both test sets measured at every threshold", set(metrics) == {"seen_fonts", "new_fonts"}
                  and set(metrics["new_fonts"]) == {str(t) for t in train.THRESHOLDS}, list(metrics))
            check("…and its results in the model", "synthetic" in shirts.Onnx(out / "shirt_numbers.onnx").about)
    check("batches an epoch, as the workers make them", train.batches_per_epoch(8000, 128, 5) == 65 and train.batches_per_epoch(100, 32, 0) == 4)

print("== comparing readers")
from matchvision import numbers_eval  # noqa: E402

labelled = [("a", None, 7), ("b", None, 9), ("c", None, None), ("d", None, 23)]
answers = iter([7, 6, 4, None])
r = numbers_eval.score(lambda _: next(answers), labelled)
check("read, misread, invented, unread", (r["read"], r["misread"], r["invented"], r["unread"], r["legible"], r["illegible"]) == (1, 1, 1, 1, 3, 1),
      json.dumps({k: v for k, v in r.items() if k != "errors"}))

sys.exit(1 if failed else 0)
