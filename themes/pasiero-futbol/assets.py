#!/usr/bin/env python3
"""
The Pasiero Futbol theme's images, from its mark (src/mark.svg: the smiling face in the hat, drawn after
pasiero-futbol-banner.png): in ink for light surfaces, in cream for dark ones, and on a cream square.

    python3 themes/pasiero-futbol/assets.py

Writes, in this theme's folder (iot/scripts/theme.py use pasiero-futbol puts them in place):
  site/marks/   mark.svg, mark-white.svg (the pages' header, favicons)
  ./            logo-dark.png, logo-light.png (the mark, Pasiero, FUTBOL), icon.png
  grafana/      grafana_icon.svg (the mark on cream: menu, login and the dashboard's header),
                fav32.png, apple-touch-icon.png

Needs rsvg-convert (librsvg) and ImageMagick's magick, at design time only: the outputs are committed.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

THEME = Path(__file__).resolve().parent
MARKS = THEME / "site" / "marks"
WORD = THEME / "fonts" / "Fredoka-Bold.ttf"
SUB = THEME / "fonts" / "Fredoka-SemiBold.ttf"
INK, ORANGE, CREAM = "#1e1214", "#d9553a", "#fbf3f1"
ORANGE_ON_DARK = "#f2774f"


def body() -> tuple[str, str]:
    """The mark's drawing and its viewBox."""
    text = (THEME / "src" / "mark.svg").read_text(encoding="utf-8")
    box = re.search(r'viewBox="([^"]+)"', text).group(1)
    return re.sub(r"^.*?<svg[^>]*>|</svg>\s*$", "", text, flags=re.S).strip(), box


def svg(ink: str, title: str, background: str | None = None) -> str:
    drawing, box = body()
    drawing = drawing.replace(INK, ink)
    if background:
        # A square around the mark, padded, with rounded corners
        x, y, w, h = map(float, box.split())
        side = max(w, h) * 1.3
        x, y = x - (side - w) / 2, y - (side - h) / 2
        box = f"{x:g} {y:g} {side:g} {side:g}"
        drawing = f'<rect x="{x:g}" y="{y:g}" width="{side:g}" height="{side:g}" rx="{side * .18:.1f}" fill="{background}"/>' + drawing
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}"><title>{title}</title>{drawing}</svg>\n'


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    files = {
        MARKS / "mark.svg": svg(INK, "Pasiero Futbol"),
        MARKS / "mark-white.svg": svg(CREAM, "Pasiero Futbol"),
        THEME / "grafana" / "grafana_icon.svg": svg(INK, "Pasiero Futbol", CREAM),
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        icon = THEME / "grafana" / "grafana_icon.svg"
        run("rsvg-convert", "-w", "1024", "-h", "1024", str(icon), "-o", str(THEME / "icon.png"))
        run("rsvg-convert", "-w", "180", "-h", "180", str(icon), "-o", str(THEME / "grafana" / "apple-touch-icon.png"))
        run("rsvg-convert", "-w", "32", "-h", "32", str(icon), "-o", str(THEME / "grafana" / "fav32.png"))
        # The dashboard's logos, 605 × 84 like the template's: the mark, Pasiero, FUTBOL in orange, spaced
        for name, ink, orange in (("logo-dark.png", CREAM, ORANGE_ON_DARK), ("logo-light.png", INK, ORANGE)):
            mark = Path(tmp) / f"mark-{name}"
            (Path(tmp) / "mark.svg").write_text(svg(ink, "Pasiero Futbol"), encoding="utf-8")
            run("rsvg-convert", "-h", "78", str(Path(tmp) / "mark.svg"), "-o", str(mark))
            run("magick", "-size", "605x84", "xc:none", str(mark), "-geometry", "+2+3", "-composite",
                "-fill", ink, "-font", str(WORD), "-pointsize", "58", "-gravity", "west",
                "-annotate", "+100+0", "Pasiero",
                "-fill", orange, "-font", str(SUB), "-pointsize", "27", "-kerning", "7",
                "-annotate", "+314+6", "FUTBOL", str(THEME / name))
    print("wrote", ", ".join(str(p.relative_to(THEME)) for p in files),
          "and icon.png, logo-dark.png, logo-light.png, grafana/fav32.png, apple-touch-icon.png")


if __name__ == "__main__":
    main()
