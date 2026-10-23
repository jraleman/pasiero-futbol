"""
Shirt numbers of the players an alert names, read by the stack's
vision-language model (Ollama, OLLAMA_MODEL: gemma4:e2b by default).

On 160 player crops from two broadcast clips it read 24 of the 30 numbers
a person could read, misread 2 (14 as 74, 12 as 27) and never invented one
where none was legible; a jersey-number classifier (2/30) and PP-OCR
(12/30) did far worse on the same crops. It takes a few seconds a crop on
CPU, so only the alerts' players are read, at the alert's moment.

The analysed frames are too few for this (2 a second: a player in a tangle
shows in two or three, half of them overlapped), and a track is not always
one player there (the tracker hands it from one to another). So the clip is
read again around the alert (Dense): people detected every EVERY-th video
frame (about 6 a second) within WINDOW seconds of the key frame, and the
player followed from their key-frame box by overlap, frame to frame, among
the boxes the clip's kits (teams.py) put in his team: in a contact the other
player overlaps him but wears the other kit. It stops where two of his team
could be him (a teammate in the way), or he is missing two frames running. Without the clip (--reuse with no clip, the tests),
the analysed frames' own views of the track serve instead, while it keeps
the player's team.

Of those views, at most MAX_READS are read, nearest the key frame first and
SPACING apart, each standing, tall enough, and no one overlapping it much (a
neighbour's number would do). The number is the one AGREE reads agree on,
with more reads than any other number; otherwise it stays unread (null in
the alert).

A match sheet (--lineup) drops numbers a team doesn't field. Every read is
kept in <out>/numbers.json, so --reuse asks the model nothing it already
answered.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .state import DOWN_ASPECT, Frame, Person
from .track import iou

MIN_HEIGHT = 90            # px: a shorter player's number is a few pixels
MAX_READS = 6              # views read per player, at most
AGREE = 2                  # reads that must agree on a number
WINDOW = 2.0               # s either side of the alert's key frame
SPACING = 0.15             # s between two views read: each of Dense's frames
EVERY = 1 / 6              # s between the frames Dense detects people in
FOLLOW_IOU = 0.25          # the player's box in the next dense frame overlaps the last this much
AMBIGUOUS = 0.6            # ... and a teammate's box overlapping it this share as much makes it his or the teammate's
MAX_OVERLAP = 0.2          # IoU with anyone else's box, at most (the crop is centred on him)
CROP_HEIGHT = 448          # px: the crop the model sees
PROMPT = ("A football player cut out of a match video. Read the shirt number printed on their shirt "
          "(back or front). If no number is clearly legible, answer null; never guess.")
SCHEMA = {"type": "object", "properties": {"number": {"type": ["integer", "null"]}}, "required": ["number"]}

# A reader: a player's crop (BGR) → their number, or None
Read = Callable[[np.ndarray], "int | None"]


class Ollama:
    """The vision model behind Ollama's chat API; None when it can't be reached (said once)."""

    def __init__(self, url: str, model: str, timeout: float = 300) -> None:
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout
        self.down = False

    def __call__(self, crop: np.ndarray) -> int | None:
        if self.down:
            return None
        ok, data = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        body = {"model": self.model, "stream": False, "think": False, "format": SCHEMA,
                "options": {"temperature": 0, "num_predict": 20},
                "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(data.tobytes()).decode()]}]}
        request = urllib.request.Request(f"{self.url}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                number = json.loads(json.load(response)["message"]["content"]).get("number")
        except (OSError, urllib.error.URLError, ValueError, KeyError) as e:
            print(f"shirt numbers off: {self.model} at {self.url} didn't answer ({e})", file=sys.stderr)
            self.down = True
            return None
        return number if isinstance(number, int) and 1 <= number <= 99 else None


def crop(image: np.ndarray, box, scale: float = 1.0) -> np.ndarray | None:
    """The player with a little room around them, enlarged to CROP_HEIGHT."""
    x1, y1, x2, y2 = (v * scale for v in box)
    w, h = x2 - x1, y2 - y1
    H, W = image.shape[:2]
    out = image[int(max(0, y1 - 0.05 * h)):int(min(H, y2 + 0.02 * h)), int(max(0, x1 - 0.15 * w)):int(min(W, x2 + 0.15 * w))]
    if out.size == 0:
        return None
    return cv2.resize(out, None, fx=CROP_HEIGHT / out.shape[0], fy=CROP_HEIGHT / out.shape[0], interpolation=cv2.INTER_CUBIC)


@dataclass
class View:
    """One sight of the player: where, how far from the key frame, and the picture's way to get it."""
    index: int                       # video frame
    dt: float                        # s from the key frame
    box: tuple                       # x1, y1, x2, y2 in the video's pixels
    others: list                     # everyone else's boxes in that frame
    picture: Callable[[], np.ndarray]
    scale: float = 1.0               # picture pixels per video pixel

    @property
    def readable(self) -> bool:
        x1, y1, x2, y2 = self.box
        return (y2 - y1 >= MIN_HEIGHT and x2 - x1 < DOWN_ASPECT * (y2 - y1)
                and all(iou(self.box, o) < MAX_OVERLAP for o in self.others))


def analysed_views(frames: list[Frame], images: list[bytes], key: int, track: int, team: str) -> list[View]:
    """
    The track's views in the analysed frames around the key frame, each side
    stopping where the track wears the other team's kit (it has moved to
    someone else).
    """
    out = []
    for step in (-1, 1):
        i = key if step == 1 else key - 1
        while 0 <= i < len(frames) and frames[i].shot == frames[key].shot and abs(frames[i].t - frames[key].t) <= WINDOW:
            f, p = frames[i], frames[i].by_track(track)
            if p is not None:
                if (p.own or p.team) not in (team, "other", None):
                    break
                if p.on_pitch:
                    def picture(data=images[i]):
                        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    out.append(View(f.index, f.t - frames[key].t, p.box, [q.box for q in f.persons if q is not p], picture,
                                    _jpeg_width(images[i]) / f.size[0]))
            i += step
    return out


def _jpeg_width(data: bytes) -> int:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8).shape[1] * 8


class Dense:
    """
    The clip read again around a moment: people every EVERY seconds, each
    given a team by the clip's kits (teams.py), and one of them followed from
    a box by overlap, both ways from the key frame, among their own team.
    """

    def __init__(self, capture: cv2.VideoCapture, detect: Callable[[np.ndarray], list[Person]],
                 judge: Callable[[np.ndarray, list], list[str]]) -> None:
        self.capture = capture
        self.fps = self.capture.get(cv2.CAP_PROP_FPS) or 25.0
        self.step = max(1, round(self.fps * EVERY))
        self.detect, self.judge = detect, judge
        self.seen: dict[int, tuple[np.ndarray, list, list]] = {}   # video frame → (picture, boxes, their teams)

    def frame(self, index: int) -> tuple[np.ndarray, list, list] | None:
        if index not in self.seen:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, image = self.capture.read()
            if not ok:
                return None
            boxes = [p.box for p in self.detect(image)]
            self.seen[index] = (image, boxes, self.judge(image, boxes))
        return self.seen[index]

    def views(self, index: int, box, team: str) -> list[View]:
        out = []
        reach = int(WINDOW * self.fps)
        # Pictures of other moments aren't needed again (alerts come in order)
        self.seen = {k: v for k, v in self.seen.items() if abs(k - index) <= reach}
        for step in (-self.step, self.step):
            last, i, missed = box, index + (step if step < 0 else 0), 0
            while abs(i - index) <= reach and i >= 0:
                got = self.frame(i)
                if got is None:
                    break
                image, boxes, teams = got
                # Where the player could be now: overlapping where he was, in his team's kit
                near = sorted(((iou(last, b), b) for b, t in zip(boxes, teams) if t == team and iou(last, b) >= FOLLOW_IOU),
                              key=lambda v: -v[0])
                if not near:
                    # Not found (hidden, unsure of his kit, or the detector missed him): one frame is let pass
                    missed += 1
                    if missed > 1:
                        break
                    i += step
                    continue
                # Two of his team about as much where he was: a teammate in the way, so stop rather than
                # follow the wrong one
                if len(near) > 1 and near[1][0] >= AMBIGUOUS * near[0][0]:
                    break
                missed, last = 0, near[0][1]
                out.append(View(i, (i - index) / self.fps, last, [b for b in boxes if b is not last], lambda image=image: image))
                i += step
        return out

    def close(self) -> None:
        self.capture.release()


class Numbers:
    """The alerts' players' shirt numbers, with every read cached by view."""

    def __init__(self, read: Read, model: str = "", cache: Path | None = None,
                 lineup: dict[str, set[int]] | None = None, dense: Dense | None = None) -> None:
        self.read, self.model, self.cache, self.lineup, self.dense = read, model, cache, lineup or {}, dense
        self.reads: dict[str, int | None] = {}
        if cache and cache.is_file():
            saved = json.loads(cache.read_text())
            if saved.get("model") == model:
                self.reads = saved.get("reads", {})
        self.asked = 0
        self.found: dict[str, int] = {}

    def save(self) -> None:
        if self.cache:
            self.cache.write_text(json.dumps({"model": self.model, "reads": self.reads}, indent=0))

    def _read(self, view: View) -> int | None:
        key = f"{view.index}:" + ",".join(str(round(v)) for v in view.box)
        if key not in self.reads:
            picture = crop(view.picture(), view.box, view.scale)
            self.reads[key] = self.read(picture) if picture is not None else None
            self.asked += 1
        return self.reads[key]

    def player(self, frames: list[Frame], images: list[bytes], key: int, track: int, team: str) -> int | None:
        """The number of the player on this track at the key frame, or None."""
        at_key = frames[key].by_track(track)
        if at_key is None:
            return None
        found = (self.dense.views(frames[key].index, at_key.box, team) if self.dense
                 else analysed_views(frames, images, key, track, team))
        chosen: list[View] = []
        for v in sorted((v for v in found if v.readable), key=lambda v: abs(v.dt)):
            if all(abs(v.dt - c.dt) >= SPACING for c in chosen):
                chosen.append(v)
        allowed = self.lineup.get(team)
        votes: Counter = Counter()
        for v in chosen[:MAX_READS]:
            number = self._read(v)
            if number is None or (allowed and number not in allowed):
                continue
            votes[number] += 1
            if votes[number] >= AGREE and len(votes) == 1:
                break
        best = votes.most_common(2)
        if not best or best[0][1] < AGREE or (len(best) == 2 and best[1][1] >= best[0][1]):
            return None
        # The key frame's detection is the player the alert names (the snapshot shows the number)
        at_key.number = best[0][0]
        return best[0][0]

    def label(self, events, frames: list[Frame], images: list[bytes]) -> None:
        """Every alert player's number (event.players, in place)."""
        for e in events:
            for p in e.players:
                if p.get("track_id") is not None and p["team"] in ("home", "away") and p["number"] is None:
                    p["number"] = self.player(frames, images, e.frame, p["track_id"], p["team"])
                    if p["number"]:
                        self.found[f"{e.frame}:{p['role']}"] = p["number"]
        self.save()


def load_lineup(path: str) -> dict[str, set[int]]:
    """A match sheet: {"home": [1, 4, 9, ...], "away": [...]} (starters and substitutes)."""
    data = json.loads(Path(path).read_text())
    return {team: {int(n) for n in data.get(team, [])} for team in ("home", "away") if data.get(team)}
