"""
Shirt numbers read by our own model: a small network trained on Azure Machine
Learning on synthetic shirts (ai/vision/shirts/), exported to ONNX and run
here with ONNX Runtime on CPU, in milliseconds a crop. It's an alternative to
the vision-language model in numbers.py (--numbers-onnx), and it takes the
same crops: numbers.crop()'s player, CROP_HEIGHT tall.

The model sees the shirt only: the band of the crop from the shoulders to the
shorts (BAND), squared to SIZE. It answers three things at once: how many
digits the number has (none, one or two), the first digit and the second.
A number is read only when the model is at least THRESHOLD sure of all three;
otherwise it stays unread, as the vision-language model's "never guess".

prepare() and decode() are shared with the training (shirts/train.py), so the
model is trained on exactly what it's given here.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

SIZE = 96                       # px: the square the model sees
BAND = (0.10, 0.62)             # rows of the player's crop: the shoulders to the shorts
THRESHOLD = 0.8                 # read a number only this sure of it
LENGTHS, DIGITS = 3, 10         # outputs: digits in the number (0 = none legible), then each digit
OUTPUTS = LENGTHS + 2 * DIGITS


def prepare(crop: np.ndarray) -> np.ndarray:
    """A player's crop (BGR, uint8) → the model's input: RGB, 0–1, channels first, SIZE × SIZE."""
    h = crop.shape[0]
    band = crop[int(BAND[0] * h):max(int(BAND[1] * h), int(BAND[0] * h) + 1)]
    band = cv2.resize(band, (SIZE, SIZE), interpolation=cv2.INTER_AREA if band.shape[0] > SIZE else cv2.INTER_CUBIC)
    return np.ascontiguousarray(band[:, :, ::-1].transpose(2, 0, 1), dtype=np.float32) / 255.0


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - x.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


def decode(logits: np.ndarray, threshold: float = THRESHOLD) -> tuple[int | None, float]:
    """One crop's OUTPUTS logits → (the number or None, how sure)."""
    length = softmax(logits[:LENGTHS])
    first = softmax(logits[LENGTHS:LENGTHS + DIGITS])
    second = softmax(logits[LENGTHS + DIGITS:])
    n = int(length.argmax())
    if n == 0:
        return None, float(length[0])
    if n == 1:
        d = int(first[1:].argmax()) + 1
        number, sure = d, float(length[1] * first[d])
    else:
        tens, units = int(first[1:].argmax()) + 1, int(second.argmax())
        number, sure = 10 * tens + units, float(length[2] * first[tens] * second[units])
    return (number if sure >= threshold else None), sure


def target(number: int | None) -> tuple[int, int, int]:
    """A label → (length, first digit, second digit); -1 where a digit isn't there (the training masks it)."""
    if number is None:
        return 0, -1, -1
    if number < 10:
        return 1, number, -1
    return 2, number // 10, number % 10


class Onnx:
    """A reader (numbers.Read) on the exported model; None when it isn't sure."""

    def __init__(self, path: str | Path, threshold: float | None = None) -> None:
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        meta = self.session.get_modelmeta().custom_metadata_map
        self.about = json.loads(meta.get("shirts", "{}"))
        if self.about.get("size", SIZE) != SIZE or tuple(self.about.get("band", BAND)) != BAND:
            raise ValueError(f"{path} was trained on another input ({self.about}); this reader prepares {SIZE} px of {BAND}")
        self.threshold = THRESHOLD if threshold is None else threshold
        self.input = self.session.get_inputs()[0].name

    def logits(self, crops: list[np.ndarray]) -> np.ndarray:
        return self.session.run(None, {self.input: np.stack([prepare(c) for c in crops])})[0]

    def __call__(self, crop: np.ndarray) -> int | None:
        return decode(self.logits([crop])[0], self.threshold)[0]
