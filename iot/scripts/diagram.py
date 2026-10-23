"""
The demo's keyframes: an alert's key moment drawn from above, as PNG.

A real stadium's device uploads the camera frame it flagged; the simulator
has no camera, so it draws what its snapshot says: the pitch to scale, each
player a disc in their kit with their shirt number, the referee, the ball,
the offside line when there is one, and the players the alert names ringed
in red, with the match minute in the corner. The dashboard's Feed shows it
as the alert's picture (it says the item is demo data).

Standard library only, like the simulator: pixels in a bytearray, PNG by zlib.
"""

from __future__ import annotations

import math
import struct
import zlib

LENGTH, WIDTH = 105.0, 68.0
W, MARGIN = 960, 36
SCALE = (W - 2 * MARGIN) / LENGTH
H = round(WIDTH * SCALE) + 2 * MARGIN

GRASS = ((46, 110, 60), (52, 121, 66))
LINE = (232, 238, 232)
KITS = {"home": ((236, 238, 240), (20, 40, 80)), "away": ((200, 16, 46), (255, 255, 255))}   # disc, number
REFEREE = ((24, 24, 24), (250, 200, 30))
RING = (255, 64, 48)
OFFSIDE = (217, 89, 38)
BALL = ((255, 255, 255), (20, 20, 20))
LABEL = (255, 255, 255)

# 3 × 5 digits, and the minute's apostrophe
GLYPHS = {
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
    "'": ("1", "1", "0", "0", "0"), "+": ("000", "010", "111", "010", "000"),
}


class Canvas:
    def __init__(self, pixels: bytearray) -> None:
        self.px = pixels

    def put(self, x: int, y: int, c) -> None:
        if 0 <= x < W and 0 <= y < H:
            i = (y * W + x) * 3
            self.px[i:i + 3] = bytes(c)

    def rect(self, x0: float, y0: float, x1: float, y1: float, c) -> None:
        x0, x1 = sorted((max(0, round(x0)), min(W, round(x1))))
        y0, y1 = sorted((max(0, round(y0)), min(H, round(y1))))
        row = bytes(c) * (x1 - x0)
        for y in range(y0, y1):
            i = (y * W + x0) * 3
            self.px[i:i + len(row)] = row

    def disc(self, cx: float, cy: float, r: float, c, inner: float = -1.0) -> None:
        """A filled disc, or a ring when inner ≥ 0."""
        for y in range(math.floor(cy - r), math.ceil(cy + r) + 1):
            for x in range(math.floor(cx - r), math.ceil(cx + r) + 1):
                d = math.hypot(x - cx, y - cy)
                if d <= r and d >= inner:
                    self.put(x, y, c)

    def arc(self, cx: float, cy: float, r: float, c, start: float = 0.0, end: float = 2 * math.pi) -> None:
        steps = max(16, int(r * (end - start) * 2))
        for k in range(steps + 1):
            a = start + (end - start) * k / steps
            x, y = cx + r * math.cos(a), cy + r * math.sin(a)
            self.rect(x - 1, y - 1, x + 1, y + 1, c)

    def text(self, x: float, y: float, s: str, scale: int, c) -> None:
        for ch in s:
            glyph = GLYPHS.get(ch)
            if glyph is None:
                x += 2 * scale
                continue
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        self.rect(x + gx * scale, y + gy * scale, x + (gx + 1) * scale, y + (gy + 1) * scale, c)
            x += (len(glyph[0]) + 1) * scale


def text_width(s: str, scale: int) -> int:
    return sum((len(GLYPHS[ch][0]) + 1) * scale if ch in GLYPHS else 2 * scale for ch in s) - scale


def px(x: float, y: float) -> tuple[float, float]:
    """Pitch metres → pixels: x along the length from the left goal line, y from the far touchline."""
    return MARGIN + x * SCALE, MARGIN + y * SCALE


def _pitch() -> bytes:
    canvas = Canvas(bytearray(bytes(GRASS[0]) * (W * H)))
    stripes = 12
    for k in range(stripes):
        if k % 2:
            canvas.rect(*px(k * LENGTH / stripes, -MARGIN / SCALE), *px((k + 1) * LENGTH / stripes, WIDTH + MARGIN / SCALE),
                        GRASS[1])

    def box(x0, y0, x1, y1):
        (a, b), (c, d) = px(x0, y0), px(x1, y1)
        for (p, q, r, s) in ((a, b, c, b), (a, d, c, d), (a, b, a, d), (c, b, c, d)):
            canvas.rect(min(p, r) - 1, min(q, s) - 1, max(p, r) + 1, max(q, s) + 1, LINE)

    box(0, 0, LENGTH, WIDTH)
    canvas.rect(*px(LENGTH / 2, 0), *px(LENGTH / 2, WIDTH), LINE)
    canvas.rect(px(LENGTH / 2, 0)[0] - 1, px(0, 0)[1], px(LENGTH / 2, 0)[0] + 1, px(0, WIDTH)[1], LINE)
    canvas.arc(*px(LENGTH / 2, WIDTH / 2), 9.15 * SCALE, LINE)
    canvas.disc(*px(LENGTH / 2, WIDTH / 2), 3, LINE)
    for end, sign in ((0.0, 1), (LENGTH, -1)):
        box(end, WIDTH / 2 - 20.16, end + sign * 16.5, WIDTH / 2 + 20.16)
        box(end, WIDTH / 2 - 9.16, end + sign * 5.5, WIDTH / 2 + 9.16)
        box(end, WIDTH / 2 - 3.66, end - sign * 2.0, WIDTH / 2 + 3.66)
        spot = px(end + sign * 11, WIDTH / 2)
        canvas.disc(*spot, 3, LINE)
        # The arc outside the penalty area: 9.15 m from the spot, beyond 16.5 m
        reach = math.acos(5.5 / 9.15)
        centre = math.pi if sign == -1 else 0.0
        canvas.arc(*spot, 9.15 * SCALE, LINE, centre - reach, centre + reach)
    return bytes(canvas.px)


_BACKGROUND: bytes | None = None


def png(pixels: bytes) -> bytes:
    rows = b"".join(b"\x00" + pixels[y * W * 3:(y + 1) * W * 3] for y in range(H))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def render(doc: dict, kits: dict | None = None) -> bytes | None:
    """
    The alert's key moment as PNG, or None when it has no snapshot. kits:
    {"home"|"away": (shirt, number)} RGB colours, the clubs' (default KITS).
    """
    kits = kits or KITS
    global _BACKGROUND
    snapshot = doc.get("snapshot")
    if not snapshot:
        return None
    if _BACKGROUND is None:
        _BACKGROUND = _pitch()
    canvas = Canvas(bytearray(_BACKGROUND))
    if snapshot.get("line_x") is not None:
        x = px(snapshot["line_x"], 0)[0]
        canvas.rect(x - 2, MARGIN - 6, x + 2, H - MARGIN + 6, OFFSIDE)
    # The alert's players, the main one first (the attacker, the scorer, the offender)
    named = [(p["team"], p.get("number")) for p in doc.get("players", [])]
    r = 0.95 * SCALE + 3
    # ... drawn last, the main one on top of anyone standing where they are
    people = sorted(snapshot.get("players", []),
                    key=lambda p: len(named) - named.index((p[0], p[1])) if (p[0], p[1]) in named else 0)
    for team, number, x, y, *_ in people:
        cx, cy = px(x, y)
        if (team, number) in named:
            canvas.disc(cx, cy, r + 8, RING, inner=r + 4)
        disc, ink = kits.get(team, REFEREE)
        canvas.disc(cx, cy, r + 1, (20, 20, 20))
        canvas.disc(cx, cy, r, disc)
        if team in kits and number is not None:
            label = str(number)
            canvas.text(cx - text_width(label, 2) / 2, cy - 5, label, 2, ink)
    if snapshot.get("ball"):
        bx, by = px(*snapshot["ball"])
        canvas.disc(bx, by, 7, BALL[1])
        canvas.disc(bx, by, 5, BALL[0])
    # The minute as the referee writes it (contract.minute): 45+2 is the second of stoppage time
    played = int(doc["match_time_s"] // 60) + 1
    base, length = {1: (0, 45), 2: (45, 45), 3: (90, 15), 4: (105, 15)}[doc["period"]]
    label = (f"{base + length}+{played - length}" if played > length else str(base + played)) + "'"
    canvas.rect(8, 6, 16 + text_width(label, 4), 34, (12, 18, 30))
    canvas.text(12, 10, label, 4, LABEL)
    return png(bytes(canvas.px))
