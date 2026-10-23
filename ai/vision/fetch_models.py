#!/usr/bin/env python3
"""
Downloads the vision models into models/, and checks each one before
anything loads it.

    python3 fetch_models.py [models dir]

A .pt file is a pickle: loading it runs whatever it names. So every file's
pickle is read here without loading it, and refused if it imports anything
beyond PyTorch, Ultralytics, NumPy and a few harmless builtins. Ultralytics'
own COCO detector comes from its GitHub releases; the others from the
Hugging Face Hub, pinned to a revision.

Standard library only.
"""

from __future__ import annotations

import hashlib
import pickletools
import sys
import urllib.request
import zipfile
from pathlib import Path

MODELS = {
    # name: (url, what it is, its licence)
    "yolo11m.pt": ("https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11m.pt",
                   "people and balls (COCO)", "AGPL-3.0 (Ultralytics)"),
    "ball.pt": ("https://huggingface.co/bambezius/yolo11n-soccernet-ball/resolve/main/best.pt",
                "football ball detector (SoccerNet)", "Ultralytics AGPL-3.0; SoccerNet data terms"),
    "pitch.pt": ("https://huggingface.co/martinjolif/yolo-football-pitch-detection/resolve/main/yolo-football-pitch-detection.pt",
                 "32 pitch keypoints (roboflow/sports layout)", "AGPL-3.0"),
}
ALLOWED = ("torch", "ultralytics", "collections", "numpy", "_codecs", "__builtin__.set", "builtins.set",
           "__builtin__.slice", "builtins.slice", "__builtin__.getattr", "builtins.getattr")
# getattr appears in some PyTorch pickles to rebuild tensors; anything else from builtins is refused
FORBIDDEN_GETATTR_TARGETS = ("os", "subprocess", "sys", "posix", "shutil", "socket", "importlib", "runpy")


def globals_of(path: Path) -> set[str]:
    found = set()
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".pkl"):
                continue
            strings = []
            for op, arg, _ in pickletools.genops(z.read(name)):
                if op.name in ("SHORT_BINUNICODE", "BINUNICODE", "UNICODE"):
                    strings.append(arg)
                elif op.name == "GLOBAL":
                    found.add(arg.replace(" ", "."))
                elif op.name == "STACK_GLOBAL" and len(strings) >= 2:
                    found.add(f"{strings[-2]}.{strings[-1]}")
    return found


def check(path: Path) -> list[str]:
    """The globals a model's pickle imports that aren't allowed (empty: fine)."""
    bad = []
    for g in sorted(globals_of(path)):
        if g.split(".")[0] in FORBIDDEN_GETATTR_TARGETS or not any(g == a or g.startswith(a + ".") for a in ALLOWED):
            bad.append(g)
    return bad


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("models"))
    target.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, (url, what, licence) in MODELS.items():
        path = target / name
        if not path.is_file():
            print(f"downloading {name}: {what}")
            request = urllib.request.Request(url, headers={"User-Agent": "matchvision/0.1"})
            with urllib.request.urlopen(request, timeout=300) as response:
                data = response.read()
            part = path.with_suffix(".part")
            part.write_bytes(data)
            part.rename(path)
        bad = check(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        if bad:
            failed += 1
            path.rename(path.with_suffix(".refused"))
            print(f"REFUSED {name}: its pickle imports {', '.join(bad)}")
        else:
            print(f"ok {name} ({path.stat().st_size // 1_000_000} MB, sha256 {digest}…, {licence})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
