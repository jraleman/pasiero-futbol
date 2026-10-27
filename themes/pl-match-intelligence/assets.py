#!/usr/bin/env python3
"""
The Premier League theme's images, from the league's logo (src/premier-league.svg): the lion and the
wordmark, in the league's purple and in white.

    python3 themes/pl-match-intelligence/assets.py

Writes, in this theme's folder (iot/scripts/theme.py use pl-match-intelligence puts them in place):
  site/marks/   premier-league-white.svg, mark.svg, mark-white.svg (the lion: the pages' header, favicons)
  ./            logo-dark.png, logo-light.png (the lion, a rule, MATCH INTELLIGENCE), icon.png
  grafana/      grafana_icon.svg (the lion on purple: menu, login and the dashboard's header),
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
FONT = THEME / "fonts" / "BarlowCondensed-Bold.ttf"
PURPLE, WHITE = "#37003C", "#FFFFFF"
VIEWBOX = "0 0 204.69599 86.213023"
LION_BOX = "0 0 80 86.213023"   # the lion alone, left of the wordmark


def paths() -> tuple[str, str]:
    """The logo's two paths: (wordmark, lion)."""
    found = re.findall(r"<path[^>]*/>", (THEME / "src" / "premier-league.svg").read_text(encoding="utf-8"), re.S)
    if len(found) != 2:
        raise SystemExit("premier-league.svg: expected two paths, the wordmark and the lion")
    return found[0], found[1]


def svg(body: str, box: str, fill: str, title: str, background: str | None = None) -> str:
    body = re.sub(r'style="fill:#[0-9A-Fa-f]{6}"', f'style="fill:{fill}"', body)
    x, y, w, h = box.split()
    back = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{float(w) * .18:.1f}" fill="{background}"/>' if background else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}"><title>{title}</title>'
            f'{back}{body}</svg>\n')


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    wordmark, lion = paths()
    # The lion on a square of purple, padded: icons and Grafana's menu
    padded = "-16 -10 112 112"
    files = {
        MARKS / "premier-league-white.svg": svg(wordmark + lion, VIEWBOX, WHITE, "Premier League"),
        MARKS / "mark.svg": svg(lion, LION_BOX, PURPLE, "Premier League"),
        MARKS / "mark-white.svg": svg(lion, LION_BOX, WHITE, "Premier League"),
        THEME / "grafana" / "grafana_icon.svg": svg(lion, padded, WHITE, "Premier League", PURPLE),
    }
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        icon = Path(tmp) / "icon.svg"
        icon.write_text(svg(lion, padded, WHITE, "Premier League", PURPLE), encoding="utf-8")
        run("rsvg-convert", "-w", "1024", "-h", "1024", str(icon), "-o", str(THEME / "icon.png"))
        run("rsvg-convert", "-w", "180", "-h", "180", str(icon), "-o", str(THEME / "grafana" / "apple-touch-icon.png"))
        run("rsvg-convert", "-w", "32", "-h", "32", str(icon), "-o", str(THEME / "grafana" / "fav32.png"))
        # The dashboard's logos, 605 × 84 like the template's: the lion, a thin rule, MATCH INTELLIGENCE
        for name, ink in (("logo-dark.png", WHITE), ("logo-light.png", PURPLE)):
            mark = Path(tmp) / f"lion-{name}.png"
            (Path(tmp) / "lion.svg").write_text(svg(lion, LION_BOX, ink, "Premier League"), encoding="utf-8")
            run("rsvg-convert", "-h", "76", str(Path(tmp) / "lion.svg"), "-o", str(mark))
            run("magick", "-size", "605x84", "xc:none", str(mark), "-geometry", "+4+4", "-composite",
                "-fill", ink, "-draw", "rectangle 86,14 87,70",
                "-font", str(FONT), "-pointsize", "46", "-kerning", "2", "-gravity", "west",
                "-annotate", "+104+2", "MATCH INTELLIGENCE", str(THEME / name))
    print("wrote", ", ".join(str(p.relative_to(THEME)) for p in files),
          "and icon.png, logo-dark.png, logo-light.png, grafana/fav32.png, apple-touch-icon.png")


if __name__ == "__main__":
    main()
