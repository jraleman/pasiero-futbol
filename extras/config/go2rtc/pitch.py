#!/usr/bin/env python3
"""
A synthetic match camera for the demo, as MJPEG on stdout: go2rtc runs it
(an exec: source) for every camera that has neither a URL nor a clip.

    pitch.py <camera>      tactical or tv
    pitch.py --prepare     render every camera's background into /tmp first

It draws the pitch in perspective from where that camera stands, and on it
the live match the demo simulator is posting: the same engine, the same
seed, so the players and the ball are where the analysis room says they
are, minute by minute (iot/scripts/engine.py, mounted read-only). Between
matches it shows the teams lined up. The overlay says SYNTHETIC: this is
not footage, and nothing measures it.

Standard library only (the go2rtc image has python3 and ffmpeg): frames are
raw RGB, piped to ffmpeg, which encodes MJPEG and draws the text.

    DEMO_SPEED   the simulator's SIMULATOR_SPEED, so the clock matches the live match (default 3)
    SITE_ID, SITE_TZ, LEAGUE_FILE   as the simulator's
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from pathlib import Path

FPS = 10
W, H = 640, 360
CACHE = Path("/tmp")
FONT = "/usr/share/fonts/droid/DroidSans.ttf"
os.environ.setdefault("SIMULATOR_SPEED", os.environ.get("DEMO_SPEED") or "3")
SIM = Path(os.environ.get("SIM_DIR", "/p4n4/sim"))
sys.path.insert(0, str(SIM))

# World: x along the pitch (0-105, left goal as the tactical camera sees it), w
# away from the near touchline (0-68), z up. Metres.
CAMERAS = {
    #            position            looks at          focal (px at 640 wide)
    "tactical": ((52.5, -26.0, 24.0), (52.5, 30.0, 0.0), 315),
    "tv": ((52.5, -26.0, 24.0), (52.5, 30.0, 0.0), 660),          # the tactical view, zoomed ×2 on the ball
}
LABELS = {"tactical": "tactical", "tv": "broadcast"}

GRASS = ((40, 98, 52), (46, 110, 58))
LINE = (226, 232, 226)
STANDS = (18, 26, 20)
SKY = (10, 16, 12)
BOARD = (74, 139, 44)
SKIN = (150, 110, 80)


# ── projection ────────────────────────────────────────────────────────────────


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a):
    n = math.sqrt(dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


class Camera:
    def __init__(self, name: str, scale: int = 1) -> None:
        pos, target, focal = CAMERAS[name]
        self.pos = pos
        self.w, self.h = W * scale, H * scale
        self.f = focal * scale / (2 if name == "tv" else 1)
        self.fwd = norm(sub(target, pos))
        self.right = norm(cross(self.fwd, (0, 0, 1)))
        self.up = cross(self.right, self.fwd)

    def project(self, x: float, w: float, z: float) -> tuple[float, float, float] | None:
        d = sub((x, w, z), self.pos)
        depth = dot(d, self.fwd)
        if depth < 1:
            return None
        return (self.w / 2 + self.f * dot(d, self.right) / depth, self.h / 2 - self.f * dot(d, self.up) / depth, depth)

    def background(self) -> bytes:
        """The pitch, its lines and the stands, once: every pixel's ray meets the ground somewhere."""
        out = bytearray(self.w * self.h * 3)
        cx, cy, f = self.w / 2, self.h / 2, self.f
        px_, pw, pz = self.pos
        r, u, fw = self.right, self.up, self.fwd
        for py in range(self.h):
            vy = -(py - cy) / f
            row = py * self.w * 3
            for px in range(self.w):
                vx = (px - cx) / f
                dx = fw[0] + vx * r[0] + vy * u[0]
                dw = fw[1] + vx * r[1] + vy * u[1]
                dz = fw[2] + vx * r[2] + vy * u[2]
                if dz >= -1e-4:
                    colour = SKY
                else:
                    t = -pz / dz
                    x, w = px_ + t * dx, pw + t * dw
                    colour = ground(x, w, max(0.07, 1.4 * t / f))
                i = row + px * 3
                out[i:i + 3] = bytes(colour)
        return bytes(out)


def ground(x: float, w: float, half: float) -> tuple[int, int, int]:
    """The colour of the ground at (x, w); half: a line's half width there, so far lines stay visible."""
    if x < -6 or x > 111 or w < -5 or w > 73:
        # The advertising boards, then the stands
        if (-7.5 < x < 112.5 and -6.5 < w < 74.5):
            return BOARD
        return STANDS
    if is_line(x, w, half):
        return LINE
    return GRASS[int((x + 6) // (105 / 14)) % 2]


def is_line(x: float, w: float, h: float) -> bool:
    inside_w = -h <= w <= 68 + h
    inside_x = -h <= x <= 105 + h
    # Touchlines, goal lines, halfway line
    if inside_x and (abs(w) < h or abs(w - 68) < h):
        return True
    if inside_w and (abs(x) < h or abs(x - 105) < h or abs(x - 52.5) < h):
        return True
    r = math.hypot(x - 52.5, w - 34)
    if abs(r - 9.15) < h or r < 0.25:
        return True
    for goal_x, sign in ((0.0, 1), (105.0, -1)):
        depth = (x - goal_x) * sign
        for d, half_width in ((16.5, 20.16), (5.5, 9.16)):
            if abs(depth - d) < h and abs(w - 34) <= half_width + h:
                return True
            if -h <= depth <= d + h and abs(abs(w - 34) - half_width) < h:
                return True
        if math.hypot(depth - 11, w - 34) < 0.25:
            return True
        # The arc outside the area
        if depth > 16.5 and abs(math.hypot(depth - 11, w - 34) - 9.15) < h:
            return True
    for cxx, cww in ((0, 0), (105, 0), (0, 68), (105, 68)):
        if abs(math.hypot(x - cxx, w - cww) - 1) < h and 0 <= x <= 105 and 0 <= w <= 68:
            return True
    return False


def cached_background(name: str, scale: int) -> bytes:
    path = CACHE / f"bg-{name}-{scale}.rgb"
    if path.is_file():
        return path.read_bytes()
    data = Camera(name, scale).background()
    path.write_bytes(data)
    return data


# ── the play: the simulator's live match ─────────────────────────────────────


def match_minute(period: int, t: float) -> str:
    """45+2 is the second minute of stoppage time (as the analysis room writes it)."""
    played, base = int(t // 60) + 1, 0 if period == 1 else 45
    return f"{base + 45}+{played - 45}" if played > 45 else str(base + played)


class Live:
    """The live match on the wall clock: the engine's frames, replayed at the demo's speed."""

    def __init__(self) -> None:
        import simulate  # noqa: E402  (the simulator's own code, from SIM_DIR)

        self.sim = simulate
        from zoneinfo import ZoneInfo
        name = os.environ.get("SITE_TZ") or "UTC"
        self.tz = ZoneInfo(name) if name not in ("UTC", "Z") else __import__("datetime").timezone.utc
        self.match = None

    def at(self, now: float):
        """(people, ball, clock text, kits) at wall time now."""
        length = self.sim.CYCLE * 60 / self.sim.SPEED
        if self.match is None or now >= self.match.kickoff + length:
            self.match = self.sim.live(self.tz, now, measure=False)
            colours = self.sim.kits(self.match.home, self.match.away)
            self.kits = {side: (shirt, tuple(max(0, c - 70) for c in shirt)) for side, (shirt, _) in colours.items()}
            self.kits["home_gk"], self.kits["away_gk"] = ((250, 200, 30), (30, 30, 30)), ((230, 90, 160), (30, 30, 30))
            self.by_period = {1: [f for f in self.match.frames if f[0] == 1], 2: [f for f in self.match.frames if f[0] == 2]}
        m = self.match
        status, period, t = m.where(now)
        frames = self.by_period.get(period or 1) or m.frames
        if status in ("scheduled", "half_time", "full_time"):
            frame = frames[0] if status == "scheduled" else frames[-1]
        else:
            frame = frames[min(len(frames) - 1, int(t * 5))]
        _, _, ball, players = frame
        people = [(f"{side}_gk" if n == 1 else side, n, x, 68 - y) for side, n, x, y in players]
        score = m.fixture(now)["score"]
        minute = "" if status != "live" else f" {match_minute(period, t)}'"
        text = {"scheduled": "kick-off soon", "half_time": "half time", "full_time": "full time"}.get(status, "live")
        clock = f"{m.home} {score['home']}-{score['away']} {m.away}  ·  {text}{minute}"
        return people, (ball[0], 68 - ball[1]) if ball else (52.5, 34.0), clock, self.kits


# ── drawing ───────────────────────────────────────────────────────────────────


def fill(frame: bytearray, fw: int, fh: int, x0: float, y0: float, x1: float, y1: float, colour: bytes) -> None:
    x0, x1 = max(0, int(x0)), min(fw, int(math.ceil(x1)))
    y0, y1 = max(0, int(y0)), min(fh, int(math.ceil(y1)))
    if x1 <= x0 or y1 <= y0:
        return
    run = colour * (x1 - x0)
    for y in range(y0, y1):
        i = (y * fw + x0) * 3
        frame[i:i + len(run)] = run


def goals(frame: bytearray, cam: Camera, ox: float, oy: float) -> None:
    """Both goals' posts and crossbars: 7.32 m wide, 2.44 m high."""
    for gx in (0.0, 105.0):
        for w0, w1, z0, z1 in ((30.34, 30.34, 0, 2.44), (37.66, 37.66, 0, 2.44), (30.34, 37.66, 2.44, 2.44)):
            for k in range(81):
                a = k / 80
                p = cam.project(gx, w0 + (w1 - w0) * a, z0 + (z1 - z0) * a)
                if p:
                    size = max(1.0, 0.12 * cam.f / p[2])
                    fill(frame, W, H, p[0] - ox - size / 2, p[1] - oy - size / 2, p[0] - ox + size / 2, p[1] - oy + size / 2,
                         b"\xf0\xf0\xf0")


KITS: dict = {}


def draw(frame: bytearray, cam: Camera, people, ball, ox: float = 0, oy: float = 0) -> None:
    goals(frame, cam, ox, oy)
    sprites = []
    for kind, _, x, w in people:
        foot, head = cam.project(x, w, 0), cam.project(x, w, 1.8)
        if foot and head:
            sprites.append((foot[1], kind, foot, head))
    bfoot = cam.project(ball[0], ball[1], 0)
    btop = cam.project(ball[0], ball[1], 0.45)
    if bfoot and btop:
        sprites.append((bfoot[1], "ball", bfoot, btop))
    for _, kind, foot, head in sorted(sprites):
        fx, fy = foot[0] - ox, foot[1] - oy
        h = fy - (head[1] - oy)
        if kind == "ball":
            size = max(2.0, h * 0.5)
            fill(frame, W, H, fx - size, fy - size * 2, fx + size, fy, b"\xf4\xf4\xf4")
            continue
        half = max(1.0, h * 0.17)
        shirt, shorts = KITS.get(kind, ((25, 25, 25), (25, 25, 25)))
        fill(frame, W, H, fx - half, fy - h * 0.12, fx + half, fy, bytes(SKIN))            # legs
        fill(frame, W, H, fx - half, fy - h * 0.45, fx + half, fy - h * 0.12, bytes(shorts))
        fill(frame, W, H, fx - half * 1.1, fy - h * 0.85, fx + half * 1.1, fy - h * 0.45, bytes(shirt))
        fill(frame, W, H, fx - half * 0.6, fy - h, fx + half * 0.6, fy - h * 0.85, bytes(SKIN))  # head


def run(name: str) -> None:
    hi = name == "tv"
    cam = Camera(name, 2 if hi else 1)
    background = cached_background(name, 2 if hi else 1)
    text = CACHE / f"label-{name}-{os.getpid()}.txt"
    text.write_text("")
    style = f"fontfile={FONT}:fontcolor=white:fontsize=15:box=1:boxcolor=black@0.45:boxborderw=6"
    filters = (f"drawtext=textfile={text}:reload=1:x=10:y=10:{style},"
               f"drawtext=text=SYNTHETIC:x=w-tw-10:y=10:{style}")
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-", "-vf", filters, "-c:v", "mjpeg", "-q:v", "6", "-f", "mjpeg", "-"],
        stdin=subprocess.PIPE,
    )
    live = Live()
    start = time.time()
    frame_no = 0
    try:
        while True:
            now = time.time()
            people, ball, clock, kits = live.at(now)
            KITS.clear()
            KITS.update(kits)
            if frame_no % FPS == 0:
                text.write_text(f"{LABELS[name]}  ·  {clock}")
            if hi:
                # Follow the ball: a 640×360 window of the ×2 background
                p = cam.project(ball[0], ball[1], 0)
                ox = int(min(cam.w - W, max(0, (p[0] if p else cam.w / 2) - W / 2)))
                oy = int(min(cam.h - H, max(0, (p[1] if p else cam.h / 2) - H / 2)))
                rowlen = cam.w * 3
                frame = bytearray(b"".join(background[(oy + y) * rowlen + ox * 3:(oy + y) * rowlen + (ox + W) * 3]
                                           for y in range(H)))
                draw(frame, cam, people, ball, ox, oy)
            else:
                frame = bytearray(background)
                draw(frame, cam, people, ball)
            ffmpeg.stdin.write(frame)
            frame_no += 1
            time.sleep(max(0.0, start + frame_no / FPS - time.time()))
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        text.unlink(missing_ok=True)
        ffmpeg.kill()


def main() -> None:
    if len(sys.argv) != 2 or (sys.argv[1] not in CAMERAS and sys.argv[1] != "--prepare"):
        raise SystemExit(f"usage: pitch.py {{{'|'.join(CAMERAS)}}} | --prepare")
    if sys.argv[1] == "--prepare":
        for name in CAMERAS:
            cached_background(name, 2 if name == "tv" else 1)
        return
    run(sys.argv[1])


if __name__ == "__main__":
    main()
