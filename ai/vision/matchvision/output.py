"""
What the pipeline produces: contract 0.1 documents for the ingest service,
annotated keyframes (the alert's evidence) and frames for the annotated
video, and the HTTP calls that post them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta

import cv2
import numpy as np

from .events import Event
from .pitch import Pitch
from .state import Frame
from .teams import Teams

TRACK_BEFORE, TRACK_AFTER = 4.0, 2.0
# Positions the contract accepts: on the pitch or up to 6 m off it
MARGIN = 6.0


def alert_ids(clip: str, start: datetime, count: int) -> list[str]:
    """AL-<date>-<4 digits from the clip's name><2-digit sequence>: the same on every run of the same clip."""
    base = zlib.crc32(clip.encode()) % 9000 + 1000
    return [f"AL-{start:%Y%m%d}-{base:04d}{n:02d}" for n in range(1, count + 1)]


def positions(f: Frame, pitch: Pitch) -> list[list]:
    out = []
    for p in f.persons:
        if p.playing and p.xy and -MARGIN <= p.xy[0] <= pitch.length + MARGIN and -MARGIN <= p.xy[1] <= pitch.width + MARGIN:
            out.append([p.team, p.number, round(p.xy[0], 2), round(p.xy[1], 2), p.track])
    return out[:40]


def ball_xy(f: Frame, pitch: Pitch) -> list | None:
    if f.ball and f.ball.xy and -MARGIN <= f.ball.xy[0] <= pitch.length + MARGIN and -MARGIN <= f.ball.xy[1] <= pitch.width + MARGIN:
        return [round(f.ball.xy[0], 2), round(f.ball.xy[1], 2)]
    return None


def snapshot_and_track(event: Event, frames: list[Frame], pitch: Pitch, home_attacks: str, fps: float):
    """The key moment on the pitch, and the calibrated frames around it in the same shot; (None, None) without calibration."""
    key = frames[event.frame]
    if not key.calib:
        return None, None
    snapshot = {"home_attacks": home_attacks, "ball": ball_xy(key, pitch), "players": positions(key, pitch)}
    if event.line_x is not None:
        snapshot["line_x"] = round(min(pitch.length, max(0.0, event.line_x)), 2)
    lo = hi = event.frame
    while lo > 0 and frames[lo - 1].shot == key.shot and frames[lo - 1].calib and key.t - frames[lo - 1].t <= TRACK_BEFORE:
        lo -= 1
    while hi + 1 < len(frames) and frames[hi + 1].shot == key.shot and frames[hi + 1].calib and frames[hi + 1].t - key.t <= TRACK_AFTER:
        hi += 1
    track = {"hz": round(min(30.0, max(1.0, fps)), 3), "t0": round(frames[lo].t - key.t, 3),
             "frames": [{"ball": ball_xy(g, pitch), "players": positions(g, pitch)} for g in frames[lo:hi + 1]]}
    return snapshot, track


def alert_doc(event: Event, alert_id: str, frames: list[Frame], *, site: str, match_id: str, camera: str,
              start: datetime, period: int, clock_s: float, pitch: Pitch, home_attacks: str, fps: float, clip_uri: str) -> dict:
    f = frames[event.frame]
    snapshot, track = snapshot_and_track(event, frames, pitch, home_attacks, fps)
    doc = {
        "schema": "p4n4.match.alert/0.1",
        "alert_id": alert_id,
        "site_id": site,
        "match_id": match_id,
        "camera_id": camera,
        "timestamp": (start + timedelta(seconds=f.t)).isoformat(timespec="milliseconds"),
        "period": period,
        "match_time_s": round(min(3600.0, clock_s + f.t), 1),
        "type": event.type,
        "confidence": float(min(1.0, max(0.0, event.confidence))),
        "players": [p for p in event.players if p["team"] in ("home", "away")],
        "details": event.details,
        "evidence": {"clip_uri": f"{clip_uri}#t={f.t:.2f}"},
        "synthetic": False,
    }
    if event.team in ("home", "away"):
        doc["team"] = event.team
    if snapshot:
        doc["snapshot"], doc["track"] = snapshot, track
    return doc


# ── drawing ───────────────────────────────────────────────────────────────────

ROLE_COLOUR = (60, 60, 230)    # BGR: the alert's players (red)
# What the annotated video names (a debugging aid, in Spanish as it was first written); keyframes have no words:
# they're read in any language, and the feed and the review page say who is who
ROLE_NAMES = {"offender": "infractor", "victim": "falta recibida", "attacker": "atacante", "defender": "defensor",
              "passer": "pase", "scorer": "goleador", "last_touch": "ultimo toque", "likely_offender": "infractor probable"}
TYPE_NAMES = {"goal": "Gol", "offside_candidate": "Posible fuera de juego", "foul_contact": "Contacto / falta",
              "ball_out": "Balon fuera", "identity": "Error de identidad?"}
LINE_COLOUR = (38, 89, 217)    # the offside type's orange, #d95926


def draw(image: np.ndarray, f: Frame, teams: Teams, pitch: Pitch, event: Event | None = None, label: str = "",
         keyframe: bool = False) -> np.ndarray:
    """A frame with its players, the ball and the alert's lines. [keyframe]: an alert's picture, words left out
    (only [label], the alert's id, and the time): the players it names are ringed and shown in the inset."""
    out = image.copy()
    zoom = inset_source(image, f, event)
    h, w = out.shape[:2]
    thick = max(2, w // 640)
    roles = {p["track_id"]: p["role"] for p in (event.players if event else [])}
    for p in f.persons:
        if not p.on_pitch and p.track not in roles:
            continue
        x1, y1, x2, y2 = (int(v) for v in p.box)
        colour = teams.kit_bgr(p.team) if p.team in ("home", "away") else (180, 180, 180)
        cv2.rectangle(out, (x1, y1), (x2, y2), colour, thick)
        if p.number:
            cv2.putText(out, f"#{p.number}", (x1, y2 + 14 * thick), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * thick, colour, thick)
        if p.track in roles:
            cv2.rectangle(out, (x1 - 3 * thick, y1 - 3 * thick), (x2 + 3 * thick, y2 + 3 * thick), ROLE_COLOUR, thick * 2)
        if p.track in roles and not keyframe:
            cv2.putText(out, ROLE_NAMES.get(roles[p.track], roles[p.track]), (x1, max(15, y1 - 6 * thick)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5 * thick, ROLE_COLOUR, thick)
    if f.ball:
        cv2.circle(out, (int(f.ball.centre[0]), int(f.ball.centre[1])), 10 * thick, (0, 230, 255), thick)
    if event is not None and event.line_x is not None and f.calib:
        ends = f.calib.to_image(np.array([[event.line_x, -2.0], [event.line_x, pitch.width + 2]]))
        if np.all(np.isfinite(ends)):
            cv2.line(out, tuple(int(v) for v in ends[0]), tuple(int(v) for v in ends[1]), LINE_COLOUR, thick * 2)
    status = f"calibrado {f.calib.confidence:.0%}" if f.calib else "sin calibrar"
    text = f"{label}  t={f.t:.1f}s" if keyframe else f"{label}  t={f.t:5.1f}s  toma {f.shot}  {status}".strip()
    cv2.rectangle(out, (0, 0), (w, 24 * thick), (0, 0, 0), -1)
    cv2.putText(out, text, (8, 17 * thick), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * thick, (255, 255, 255), thick)
    if f.calib:
        out = radar(out, f, teams, pitch)
    if zoom is not None:
        out = inset(out, zoom)
    return out


def inset_source(image: np.ndarray, f: Frame, event: Event | None) -> np.ndarray | None:
    """The players an alert names, cut out of the clean frame with some room around them."""
    if not event:
        return None
    tracks = {p["track_id"] for p in event.players}
    boxes = [p.box for p in f.persons if p.track in tracks]
    if not boxes:
        return None
    h, w = image.shape[:2]
    x1, y1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x2, y2 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    pad = 0.35 * max(x2 - x1, y2 - y1)
    x1, y1, x2, y2 = int(max(0, x1 - pad)), int(max(0, y1 - pad)), int(min(w, x2 + pad)), int(min(h, y2 + pad))
    if x2 - x1 < 8 or y2 - y1 < 8 or (x2 - x1) > w / 2.5:
        return None   # already big enough in the frame
    return image[y1:y2, x1:x2].copy()


def inset(out: np.ndarray, crop: np.ndarray) -> np.ndarray:
    """The crop, enlarged, in the bottom left, framed in the alert's red."""
    h, w = out.shape[:2]
    target_h = int(h / 2.6)
    scale = target_h / crop.shape[0]
    big = cv2.resize(crop, (int(crop.shape[1] * scale), int(target_h)), interpolation=cv2.INTER_CUBIC)
    bh, bw = big.shape[:2]
    if bw > w // 2:
        big = cv2.resize(big, (w // 2, int(bh * (w // 2) / bw)))
        bh, bw = big.shape[:2]
    x0, y0 = 10, h - bh - 10
    out[y0:y0 + bh, x0:x0 + bw] = big
    cv2.rectangle(out, (x0 - 2, y0 - 2), (x0 + bw + 1, y0 + bh + 1), ROLE_COLOUR, max(2, w // 480))
    return out


def radar(image: np.ndarray, f: Frame, teams: Teams, pitch: Pitch) -> np.ndarray:
    """A small top view of the pitch in the bottom right, with everyone the calibration places on it."""
    h, w = image.shape[:2]
    rw = w // 4
    rh = int(rw * pitch.width / pitch.length)
    scale = rw / pitch.length
    x0, y0 = w - rw - 10, h - rh - 10
    cv2.rectangle(image, (x0, y0), (x0 + rw, y0 + rh), (30, 90, 40), -1)
    cv2.rectangle(image, (x0, y0), (x0 + rw, y0 + rh), (230, 230, 230), 1)
    cv2.line(image, (x0 + rw // 2, y0), (x0 + rw // 2, y0 + rh), (230, 230, 230), 1)
    for p in f.persons:
        if p.playing and p.xy and -2 <= p.xy[0] <= pitch.length + 2 and -2 <= p.xy[1] <= pitch.width + 2:
            cv2.circle(image, (x0 + int(p.xy[0] * scale), y0 + int(p.xy[1] * scale)), 4, teams.kit_bgr(p.team), -1)
    if f.ball and f.ball.xy and -2 <= f.ball.xy[0] <= pitch.length + 2 and -2 <= f.ball.xy[1] <= pitch.width + 2:
        cv2.circle(image, (x0 + int(f.ball.xy[0] * scale), y0 + int(f.ball.xy[1] * scale)), 3, (255, 255, 255), -1)
    return image


def jpeg(image: np.ndarray, max_width: int = 1280, quality: int = 85) -> bytes:
    h, w = image.shape[:2]
    if w > max_width:
        image = cv2.resize(image, (max_width, int(h * max_width / w)))
    ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return data.tobytes() if ok else b""


# ── posting ───────────────────────────────────────────────────────────────────


class Ingest:
    """The ingest service, with the stadium's write token."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    def _call(self, method: str, path: str, body: bytes, kind: str) -> dict:
        request = urllib.request.Request(f"{self.url}{path}", data=body, method=method,
                                         headers={"Content-Type": kind, "X-Site-Token": self.token})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from None

    def post(self, path: str, docs: object) -> dict:
        return self._call("POST", path, json.dumps(docs).encode(), "application/json")

    def put_jpeg(self, alert_id: str, data: bytes) -> dict:
        return self._call("PUT", f"/api/v1/evidence/{alert_id}", data, "image/jpeg")
