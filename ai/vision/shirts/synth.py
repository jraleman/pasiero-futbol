"""
Synthetic players for the shirt-number model: the crop numbers.crop() makes of
a player, drawn, with the label a person would give it.

A sample is a player seen from behind or the side, or from the front, in a kit
of one or two colours (plain, stripes or hoops, short or long sleeves), on
grass or a blurred crowd. The number is printed in one of a dozen block fonts,
sometimes outlined, with a name above it, then bent by the body's turn, the
shirt's folds and the light. Then what a broadcast does to it: the player far
away (few pixels), motion blur, noise, JPEG.

The label is the number, or None where a person couldn't read it with
certainty, and the model must learn to say so: the front of the shirt (a
small number at most), a number turned too far away, a digit hidden behind an
arm or another player, or too few pixels to read. That's the "never guess"
the vision-language model keeps.

    python3 synth.py --show 64 out.jpg     # a contact sheet of 64 samples, labels in the corner

Kit colours are made up, plus the clubs' colours from league.json when it's
there. Fonts are OFL fonts from Google Fonts' repository, downloaded once to
--fonts (FONTS below); without the network the repository's Barlow Condensed
stands in.
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CANVAS = 224                         # px: the drawn crop's height (numbers.crop's 448, halved)

GOOGLE = "https://raw.githubusercontent.com/google/fonts/main/ofl/"
# Fonts the model trains on, and two it never sees (HELD_OUT: the synthetic test of a new kit's font)
FONTS = {
    "Oswald.ttf": "oswald/Oswald%5Bwght%5D.ttf",
    "Anton.ttf": "anton/Anton-Regular.ttf",
    "BebasNeue.ttf": "bebasneue/BebasNeue-Regular.ttf",
    "Teko.ttf": "teko/Teko%5Bwght%5D.ttf",
    "SairaCondensed.ttf": "sairacondensed/SairaCondensed-Bold.ttf",
    "RobotoCondensed.ttf": "robotocondensed/RobotoCondensed%5Bwght%5D.ttf",
    "ArchivoBlack.ttf": "archivoblack/ArchivoBlack-Regular.ttf",
    "Graduate.ttf": "graduate/Graduate-Regular.ttf",
    "AlfaSlabOne.ttf": "alfaslabone/AlfaSlabOne-Regular.ttf",
    "BigShoulders.ttf": "bigshouldersdisplay/BigShouldersDisplay%5Bwght%5D.ttf",
    "BarlowCondensed.ttf": "barlowcondensed/BarlowCondensed-Bold.ttf",
    "Staatliches.ttf": "staatliches/Staatliches-Regular.ttf",
    "RussoOne.ttf": "russoone/RussoOne-Regular.ttf",
    "FjallaOne.ttf": "fjallaone/FjallaOne-Regular.ttf",
    "ChakraPetch.ttf": "chakrapetch/ChakraPetch-Bold.ttf",
    "Exo2.ttf": "exo2/Exo2%5Bwght%5D.ttf",
    "SquadaOne.ttf": "squadaone/SquadaOne-Regular.ttf",
    "SairaStencilOne.ttf": "sairastencilone/SairaStencilOne-Regular.ttf",
}
HELD_OUT = ("SquadaOne.ttf", "SairaStencilOne.ttf")
FALLBACK = REPO / "theme" / "fonts" / "BarlowCondensed-Bold.ttf"

# Kits as clubs wear them, besides the made-up ones
KITS = [
    ("#d71920", "#ffffff"), ("#ffffff", "#111111"), ("#111111", "#ffffff"), ("#0b3d91", "#ffffff"),
    ("#6cabdd", "#ffffff"), ("#fdb913", "#111111"), ("#7a263a", "#94bee5"), ("#ffffff", "#0b3d91"),
    ("#00843d", "#ffffff"), ("#f47b20", "#111111"), ("#132257", "#ffffff"), ("#e30613", "#ffd700"),
]
SKIN = [(236, 198, 168), (224, 172, 135), (198, 134, 96), (161, 102, 70), (120, 74, 48), (88, 54, 36)]


def fonts(folder: Path, held_out: bool = False) -> list[Path]:
    """The training fonts (or only the held-out ones), downloading any missing."""
    folder.mkdir(parents=True, exist_ok=True)
    names = [n for n in FONTS if (n in HELD_OUT) == held_out]
    got = []
    for name in names:
        path = folder / name
        if not path.is_file():
            try:
                with urllib.request.urlopen(GOOGLE + FONTS[name], timeout=30) as r:
                    path.write_bytes(r.read())
            except OSError as e:
                print(f"font {name} not downloaded ({e})", file=sys.stderr)
                continue
        got.append(path)
    if not got and FALLBACK.is_file():
        got.append(FALLBACK)
    if not got:
        raise SystemExit(f"No fonts in {folder}, and no network to fetch them")
    return got


def club_kits() -> list[tuple[str, str]]:
    league = REPO / "iot" / "ingest" / "analysis" / "league.json"
    try:
        return [tuple(c["kit"][:2]) for c in json.loads(league.read_text())["clubs"].values() if len(c.get("kit", [])) >= 2]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def luminance(c) -> float:
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


@dataclass
class Sample:
    image: np.ndarray        # BGR, the player's crop
    number: int | None       # the label
    shown: int | None        # the number printed, legible or not (for looking at the samples)
    why: str                 # why it's None, or "number"


class Players:
    """Draws samples. One generator a worker: seeded, so a seed and an index always give the same sample."""

    def __init__(self, font_paths: list[Path], seed: int = 0) -> None:
        self.rng = np.random.default_rng(seed)
        self.font_paths = font_paths
        self.cache: dict[tuple[Path, int], ImageFont.FreeTypeFont] = {}
        self.kits = KITS + club_kits()

    # ── helpers ───────────────────────────────────────────────────────────
    def u(self, a: float, b: float) -> float:
        return float(self.rng.uniform(a, b))

    def chance(self, p: float) -> bool:
        return bool(self.rng.random() < p)

    def font(self, size: int) -> ImageFont.FreeTypeFont:
        path = self.font_paths[int(self.rng.integers(len(self.font_paths)))]
        key = (path, size)
        if key not in self.cache:
            f = ImageFont.truetype(str(path), size)
            try:                                      # variable fonts: their bold
                f.set_variation_by_axes([700])
            except (OSError, ValueError, AttributeError):
                pass
            self.cache[key] = f
        return self.cache[key]

    def colour(self) -> tuple[int, int, int]:
        h, s, v = self.u(0, 1), self.u(0.3, 1), self.u(0.25, 1)
        return tuple(int(255 * x) for x in colorsys.hsv_to_rgb(h, s, v))

    def kit(self) -> tuple[tuple, tuple]:
        if self.chance(0.6):
            a, b = self.kits[int(self.rng.integers(len(self.kits)))]
            return rgb(a), rgb(b)
        return self.colour(), self.colour()

    def ink(self, shirt, second) -> tuple[int, int, int]:
        """The number's colour: whatever reads against the shirt, as kits do (sometimes barely)."""
        options = [(255, 255, 255), (20, 20, 20), second, (230, 190, 60)]
        options.sort(key=lambda c: -abs(luminance(c) - luminance(shirt)))
        return options[0] if self.chance(0.7) else options[min(1, len(options) - 1)]

    def number(self) -> int:
        return int(self.rng.integers(1, 31)) if self.chance(0.5) else int(self.rng.integers(1, 100))

    # ── drawing ───────────────────────────────────────────────────────────
    def background(self, w: int, h: int) -> Image.Image:
        if self.chance(0.75):                         # grass, mown in stripes
            g = np.array([self.u(40, 80), self.u(100, 160), self.u(40, 80)])
            img = np.ones((h, w, 3)) * g
            period, angle = self.u(30, 120), self.u(-0.6, 0.6)
            yy, xx = np.mgrid[0:h, 0:w]
            img *= (1 + 0.08 * np.sign(np.sin((yy * math.cos(angle) + xx * math.sin(angle)) / period * math.pi)))[..., None]
            if self.chance(0.3):                      # a pitch line
                y = int(self.u(0, h))
                img[max(0, y - 2):y + 2] = 235
        else:                                         # the crowd, the boards
            small = self.rng.uniform(30, 220, (max(2, h // 24), max(2, w // 24), 3))
            img = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
        img += self.rng.normal(0, 6, img.shape)
        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))

    def other_player(self, draw: ImageDraw.ImageDraw, w: int, h: int) -> None:
        """Someone behind, partly in the crop: an edge of their kit."""
        c = self.colour()
        x = self.u(-0.4, 0.2) * w if self.chance(0.5) else self.u(0.8, 1.2) * w
        draw.rectangle([x, self.u(0.1, 0.3) * h, x + self.u(0.25, 0.5) * w, self.u(0.6, 0.9) * h], fill=c)

    def printed(self, size: tuple[int, int], number: int, ink, shirt, shift: float = 0.0) -> tuple[Image.Image, list[tuple]]:
        """
        The number (and maybe a name) on a transparent layer the size of the shirt's back; the digits' boxes.
        shift moves it sideways, a share of the width: the body turned, the number going round the side.
        """
        w, h = size
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        top = 0.12 * h
        if self.chance(0.5):                          # the name above the number
            letters = "".join(chr(int(c)) for c in self.rng.integers(65, 91, int(self.rng.integers(4, 10))))
            nf = self.font(max(6, int(0.1 * h)))
            tw = draw.textlength(letters, font=nf)
            draw.text(((w - tw) / 2, 0.05 * h), letters, font=nf, fill=ink + (255,))
            top = 0.2 * h
        f = self.font(max(8, int(self.u(0.42, 0.62) * h)))
        text = str(number)
        stroke = int(self.u(1, 0.04 * h)) if self.chance(0.3) else 0
        outline = (20, 20, 20) if luminance(ink) > 128 else (240, 240, 240)
        if self.chance(0.4):
            outline = tuple(int(v) for v in shirt) if stroke else outline
        gap = self.u(-0.02, 0.08) * h
        widths = [draw.textlength(d, font=f) for d in text]
        total = sum(widths) + gap * (len(text) - 1)
        x, boxes = (w - total) / 2 + (self.u(-0.05, 0.05) + shift) * w, []
        for d, dw in zip(text, widths):
            draw.text((x, top), d, font=f, fill=ink + (255,), stroke_width=stroke, stroke_fill=outline + (255,))
            boxes.append(draw.textbbox((x, top), d, font=f, stroke_width=stroke))
            x += dw + gap
        return layer, boxes

    def sample(self) -> Sample:
        rng = self.rng
        aspect = self.u(0.45, 0.85)
        H, W = CANVAS, int(CANVAS * aspect)
        img = self.background(W, H)
        draw = ImageDraw.Draw(img)
        if self.chance(0.35):
            self.other_player(draw, W, H)

        # Where the player stands in the crop (the detector's box is never exact)
        dy, s = self.u(-0.05, 0.05) * H, self.u(0.9, 1.1)
        cx = W / 2 + self.u(-0.08, 0.08) * W
        shirt_c, second = self.kit()
        skin = SKIN[int(rng.integers(len(SKIN)))]
        sw = min(W * 0.92, H * self.u(0.26, 0.36) * s)            # the shoulders
        top, bottom = 0.15 * H * s + dy + 0.02 * H, 0.52 * H * s + dy + 0.02 * H
        view = rng.choice(["back", "side", "front"], p=[0.68, 0.14, 0.18])
        if view == "side":
            sw *= self.u(0.55, 0.75)

        # Head, legs, shorts
        draw.ellipse([cx - 0.07 * H * s, top - 0.15 * H * s, cx + 0.07 * H * s, top + 0.01 * H], fill=skin)
        hair = tuple(int(v) for v in rng.integers(10, 90, 3))
        if view != "front":
            draw.ellipse([cx - 0.07 * H * s, top - 0.15 * H * s, cx + 0.07 * H * s, top - 0.02 * H], fill=hair)
        socks = second if self.chance(0.5) else shirt_c
        for side in (-1, 1):
            lx = cx + side * sw * self.u(0.12, 0.3)
            draw.polygon([(lx - 0.05 * H, bottom + 0.1 * H), (lx + 0.05 * H, bottom + 0.1 * H),
                          (lx + side * self.u(0, 0.1) * H + 0.04 * H, H * 1.0), (lx + side * self.u(0, 0.1) * H - 0.04 * H, H * 1.0)], fill=skin)
            draw.rectangle([lx - 0.045 * H, bottom + 0.3 * H, lx + 0.045 * H, H], fill=socks)
        shorts = second if self.chance(0.6) else (255, 255, 255) if self.chance(0.5) else (20, 20, 20)
        draw.rectangle([cx - sw * 0.48, bottom - 0.02 * H, cx + sw * 0.48, bottom + 0.14 * H], fill=shorts)

        # The shirt, front-on on its own layer, then turned with the body
        lw, lh = int(sw), int(bottom - top)
        lw, lh = max(lw, 8), max(lh, 8)
        shirt = Image.new("RGBA", (lw, lh), shirt_c + (255,))
        sd = ImageDraw.Draw(shirt)
        pattern = rng.choice(["plain", "stripes", "hoops", "sleeves"], p=[0.6, 0.15, 0.1, 0.15])
        if pattern == "stripes":
            n = int(rng.integers(4, 9))
            for i in range(0, n, 2):
                sd.rectangle([i * lw / n, 0, (i + 1) * lw / n, lh], fill=second + (255,))
        elif pattern == "hoops":
            n = int(rng.integers(4, 8))
            for i in range(0, n, 2):
                sd.rectangle([0, i * lh / n, lw, (i + 1) * lh / n], fill=second + (255,))
        number, why = self.number(), "number"
        shown: int | None = number
        if view == "front":
            # The crest, a sponsor, at most a small number: not something to read the player by
            sd.ellipse([0.62 * lw, 0.12 * lh, 0.75 * lw, 0.25 * lh], fill=second + (255,))
            sponsor = "".join(chr(int(c)) for c in rng.integers(65, 91, int(rng.integers(3, 8))))
            sf = self.font(max(6, int(0.12 * lh)))
            sd.text(((lw - sd.textlength(sponsor, font=sf)) / 2, 0.4 * lh), sponsor, font=sf, fill=self.ink(shirt_c, second) + (255,))
            if self.chance(0.3):
                sd.text((0.25 * lw, 0.14 * lh), str(number), font=self.font(max(6, int(0.1 * lh))), fill=self.ink(shirt_c, second) + (255,))
            number, why = None, "front"
        elif self.chance(0.03):
            shown, number, why = None, None, "blank"
        else:
            ink = self.ink(shirt_c, second)
            if pattern in ("stripes", "hoops") and self.chance(0.6):   # a panel behind the number on striped shirts
                sd.rectangle([0.18 * lw, 0.15 * lh, 0.82 * lw, 0.8 * lh], fill=shirt_c + (255,))
            # Either on the back (a little to one side) or well round it: nothing in between, for a clear label
            shift = math.copysign(self.u(0.4, 0.6) if self.chance(0.6) else self.u(0.05, 0.12), self.u(-1, 1)) if view == "side" else 0.0
            layer, boxes = self.printed((lw, lh), number, ink, shirt_c, shift)
            # Round the side: the share of a digit off the shirt
            if max(_covered(b, [-lw, -lh, 0, 2 * lh]) + _covered(b, [lw, -lh, 2 * lw, 2 * lh]) for b in boxes) > 0.15:
                number, why = None, "turned"
            # Hidden: an arm across, or someone in front
            if self.chance(0.22):
                box = boxes[int(rng.integers(len(boxes)))]
                bw, bh = box[2] - box[0], box[3] - box[1]
                ox = box[0] + self.u(-0.6, 0.6) * bw
                oc = skin + (255,) if self.chance(0.6) else self.colour() + (255,)
                cover = [ox, self.u(0, lh * 0.3), ox + bw * self.u(0.4, 1.2), lh]
                ImageDraw.Draw(layer).rectangle(cover, fill=oc)
                hidden = max(_covered(b, cover) for b in boxes)
                if hidden > 0.15 and number is not None:
                    number, why = None, "hidden"      # part of a digit gone: 3 or 8? a person couldn't swear to it
            shirt.alpha_composite(layer)
        # Folds and light, on the shirt and its number
        a = np.asarray(shirt).astype(np.float32)
        xx = np.linspace(0, 1, lw)[None, :, None]
        yy = np.linspace(0, 1, lh)[:, None, None]
        folds = 1 + self.u(0, 0.12) * np.sin(xx * self.u(4, 14) + yy * self.u(-4, 4) + self.u(0, 6))
        light = 1 + self.u(-0.25, 0.25) * (xx - 0.5) + self.u(-0.2, 0.2) * (yy - 0.5)
        a[..., :3] *= folds * light
        shirt = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")
        # Long or short sleeves, and the arms
        sleeve = second if pattern == "sleeves" else shirt_c
        long_sleeves = self.chance(0.3)
        for side in (-1, 1):
            ax = cx + side * sw / 2
            arm = [(ax, top + 0.02 * H), (ax + side * 0.06 * H, top + 0.04 * H),
                   (ax + side * self.u(0.04, 0.12) * H, top + 0.3 * H), (ax + side * self.u(-0.02, 0.04) * H, top + 0.3 * H)]
            draw.polygon(arm, fill=sleeve if long_sleeves else skin)
            if not long_sleeves:
                draw.polygon(arm[:2] + [(ax + side * 0.07 * H, top + 0.12 * H), (ax, top + 0.12 * H)], fill=sleeve)

        # The body's turn: a perspective warp of the shirt into place
        x0 = cx - lw / 2
        turn = self.u(-0.25, 0.25) if view == "back" else self.u(-0.45, 0.45)
        if view == "side":
            turn = math.copysign(self.u(0.35, 0.6), turn)
        squeeze = abs(turn) * lw * 0.35
        lean = self.u(-0.06, 0.06) * lh
        src = np.float32([[0, 0], [lw, 0], [lw, lh], [0, lh]])
        if turn > 0:
            dst = [[x0 + squeeze, top + lean], [x0 + lw, top], [x0 + lw - squeeze * 0.3, top + lh], [x0 + squeeze * 0.8, top + lh + lean]]
        else:
            dst = [[x0, top], [x0 + lw - squeeze, top + lean], [x0 + lw - squeeze * 0.8, top + lh + lean], [x0 + squeeze * 0.3, top + lh]]
        dst = np.float32(dst)
        m = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(np.asarray(shirt), m, (W, H), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))
        base = np.asarray(img).astype(np.float32)
        alpha = warped[..., 3:4].astype(np.float32) / 255
        base = base * (1 - alpha) + warped[..., :3] * alpha

        # The broadcast: the player far away, blurred by motion, noisy, compressed
        out = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
        if self.chance(0.3):
            out = out.filter(ImageFilter.GaussianBlur(self.u(0.3, 1.2)))
        frame = np.asarray(out)[:, :, ::-1].copy()
        tall = int(self.u(45, 260))                   # the player's height in the video, px
        small = cv2.resize(frame, (max(4, int(W * tall / H)), tall), interpolation=cv2.INTER_AREA)
        if self.chance(0.35):
            k = int(self.u(2, 5))
            kernel = np.zeros((k, k), np.float32)
            if self.chance(0.5):
                kernel[k // 2, :] = 1 / k
            else:
                kernel[:, k // 2] = 1 / k
            small = cv2.filter2D(small, -1, kernel)
        small = np.clip(small + self.rng.normal(0, self.u(0, 8), small.shape), 0, 255).astype(np.uint8)
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, int(self.u(35, 92))])
        small = cv2.imdecode(jpg, cv2.IMREAD_COLOR)
        # As numbers.crop() hands it over: enlarged to CROP_HEIGHT (448), here CANVAS
        image = cv2.resize(small, (W, H), interpolation=cv2.INTER_CUBIC)
        # Too few pixels for anyone: a number under about 7 px tall in the video
        if number is not None and 0.5 * lh * tall / H < 7:
            number, why = None, "tiny"
        return Sample(image, number, shown, why)


def _covered(box, cover) -> float:
    """The share of a digit's box under the cover."""
    x1, y1 = max(box[0], cover[0]), max(box[1], cover[1])
    x2, y2 = min(box[2], cover[2]), min(box[3], cover[3])
    area = max(1e-6, (box[2] - box[0]) * (box[3] - box[1]))
    return max(0.0, x2 - x1) * max(0.0, y2 - y1) / area


def sheet(samples: list[Sample], columns: int = 8) -> np.ndarray:
    """A contact sheet, each sample with its label (red: None, and why)."""
    tiles = []
    for s in samples:
        t = cv2.resize(s.image, (120, CANVAS * 120 // max(1, s.image.shape[1])))[:200]
        t = cv2.copyMakeBorder(t, 0, 200 - t.shape[0], 0, 0, cv2.BORDER_CONSTANT)
        label = str(s.number) if s.number is not None else f"- {s.why}"
        cv2.putText(t, label, (3, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0) if s.number else (0, 0, 255), 1, cv2.LINE_AA)
        tiles.append(t)
    while len(tiles) % columns:
        tiles.append(np.zeros_like(tiles[0]))
    return np.vstack([np.hstack(tiles[i:i + columns]) for i in range(0, len(tiles), columns)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", type=int, default=64, help="samples on the sheet")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fonts", type=Path, default=Path.home() / ".cache" / "shirt-fonts")
    parser.add_argument("--held-out", action="store_true", help="the fonts the model never trains on")
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    players = Players(fonts(args.fonts, args.held_out), args.seed)
    samples = [players.sample() for _ in range(args.show)]
    cv2.imwrite(str(args.out), sheet(samples))
    labelled = sum(s.number is not None for s in samples)
    print(f"{args.out}: {len(samples)} samples, {labelled} with a legible number")


if __name__ == "__main__":
    main()
