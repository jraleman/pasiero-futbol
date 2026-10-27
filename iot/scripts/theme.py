#!/usr/bin/env python3
"""
The project's themes (themes/<id>/) and the one in use.

    python3 iot/scripts/theme.py              # the themes, the one in use marked
    python3 iot/scripts/theme.py use <id>     # put one in place
    python3 iot/scripts/theme.py check        # fail when the files in place aren't the theme's

A theme is a folder named by its brand id:
  brand.json, logo-*.png, icon.png, fonts/, grafana/   p4n4-dashboard's theme → theme/ (Grafana's icons are mounted from there)
  site/theme.css, site/marks/                         the analysis room's colours, fonts and mark → iot/ingest/analysis/
  theme.json                                          its names, its bar and its emails' colours → iot/ingest/analysis/brand.js,
                                                      Grafana's header (build_dashboard.py) and n8n's emails (ai/config/n8n/build.py)
  assets.py, src/                                     how its images are drawn (design time; run assets.py, then use)

`use` copies them and rebuilds the Grafana dashboard and the n8n workflows. Then restart the iot and ai
layers (N8N_REIMPORT_WORKFLOWS=true for one start) and rebuild the dashboard's image (`make image THEME=…`
in p4n4's dashboard/).
"""

from __future__ import annotations

import filecmp
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THEMES = ROOT / "themes"
ACTIVE = ROOT / "theme"
SITE = ROOT / "iot" / "ingest" / "analysis"
NOT_DASHBOARD = ("site", "src", "assets.py", "theme.json", "__pycache__")
BRAND_KEYS = ("name", "title", "mark", "wordmark", "letterhead")


def active() -> str:
    return json.loads((ACTIVE / "brand.json").read_text(encoding="utf-8"))["id"]


def names() -> list[str]:
    return sorted(p.parent.name for p in THEMES.glob("*/theme.json"))


def brand_js(theme: Path) -> str:
    spec = json.loads((theme / "theme.json").read_text(encoding="utf-8"))
    names = {k: spec[k] for k in BRAND_KEYS}
    return (f"// The theme's names, from themes/{theme.name}/theme.json (iot/scripts/theme.py writes it: edit the theme)\n"
            f"const BRAND = {json.dumps(names, ensure_ascii=False, indent=1)};\n")


def use(name: str) -> None:
    theme = THEMES / name
    if not (theme / "theme.json").is_file():
        raise SystemExit(f"no theme {name!r} (themes: {', '.join(names())})")
    shutil.rmtree(ACTIVE, ignore_errors=True)
    shutil.copytree(theme, ACTIVE, ignore=shutil.ignore_patterns(*NOT_DASHBOARD))
    shutil.copyfile(theme / "site" / "theme.css", SITE / "theme.css")
    shutil.rmtree(SITE / "marks", ignore_errors=True)
    shutil.copytree(theme / "site" / "marks", SITE / "marks")
    (SITE / "brand.js").write_text(brand_js(theme), encoding="utf-8")
    for script in (ROOT / "iot" / "scripts" / "build_dashboard.py", ROOT / "ai" / "config" / "n8n" / "build.py"):
        subprocess.run([sys.executable, str(script)], check=True, stdout=subprocess.DEVNULL)
    print(f"theme {name}: theme/, iot/ingest/analysis/theme.css, brand.js and marks/, the Grafana dashboard and the "
          "n8n workflows. Restart the iot and ai layers (N8N_REIMPORT_WORKFLOWS=true once) and rebuild the dashboard's image.")


def check() -> list[str]:
    """What differs between the files in place and the theme in use's."""
    theme = THEMES / active()
    problems = []

    def compare(a: Path, b: Path) -> None:
        cmp = filecmp.dircmp(a, b, ignore=list(NOT_DASHBOARD) if a == theme else None)
        problems.extend(f"{b.relative_to(ROOT)}/{n}: missing" for n in cmp.left_only)
        problems.extend(f"{b.relative_to(ROOT)}/{n}: not the theme's" for n in cmp.right_only + cmp.diff_files)
        for sub in cmp.common_dirs:
            compare(a / sub, b / sub)

    compare(theme, ACTIVE)
    compare(theme / "site" / "marks", SITE / "marks")
    if not filecmp.cmp(theme / "site" / "theme.css", SITE / "theme.css", shallow=False):
        problems.append("iot/ingest/analysis/theme.css: not the theme's")
    if (SITE / "brand.js").read_text(encoding="utf-8") != brand_js(theme):
        problems.append("iot/ingest/analysis/brand.js: not the theme's")
    return problems


def main() -> None:
    match sys.argv[1:]:
        case []:
            for name in names():
                print(("* " if name == active() else "  ") + name)
        case ["use", name]:
            use(name)
        case ["check"]:
            problems = check()
            for p in problems:
                print(p, file=sys.stderr)
            if problems:
                raise SystemExit(f"the files in place aren't theme {active()}'s: python3 iot/scripts/theme.py use {active()}")
            print(f"theme {active()}: in place")
        case _:
            raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
