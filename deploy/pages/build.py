#!/usr/bin/env python3
"""
The static site for GitHub Pages, from the recording (record.py): no stack behind it.

    python3 deploy/pages/build.py                 # -> deploy/pages/site/
    python3 deploy/pages/build.py --no-flutter    # reuse the last dashboard build (only the data changed)
    python3 deploy/pages/build.py --publish       # and push it to the gh-pages branch of origin

    /             p4n4-dashboard's in-browser demo (P4N4_DEMO) in the theme in use: demo/store.json (store.py),
                  the cameras' clips, the feed's pictures; Grafana's tab shows grafana/
    analysis/     the analysis room, plan editor, season page and reports, answered from the recording (static.js)
    grafana/      Grafana's Match analysis board, a snapshot per match
    reports/      the emails n8n sent the staff

    PAGES_URL       where the site is served (default https://jraleman.github.io/pasiero-futbol/)
    PAGES_PASSWORD  the dashboard's sign-in password, for admin, power and normie (default pasiero): it's
                    checked in the browser and keeps casual visitors out, nothing more
    P4N4_SRC        p4n4's sources (default ../p4n4 beside this repo), for its dashboard/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
REC = HERE / "recording"
OUT = HERE / "site"
WORK = HERE / ".work"
SITE_URL = os.environ.get("PAGES_URL", "https://jraleman.github.io/pasiero-futbol/").rstrip("/") + "/"
PASSWORD = os.environ.get("PAGES_PASSWORD", "pasiero")
DASHBOARD = Path(os.environ.get("P4N4_SRC", ROOT.parent / "p4n4")) / "dashboard"
SCRIPTS = '<script src="brand.js"></script>'


def run(*cmd, cwd=None):
    print("$", " ".join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), cwd=cwd, check=True)


def flutter() -> None:
    """The dashboard's demo build, in the theme in use, from a copy (p4n4's own dashboard/ keeps its brand)."""
    src = WORK / "dashboard"
    src.mkdir(parents=True, exist_ok=True)
    run("rsync", "-a", "--delete", "--exclude", "build/", "--exclude", ".git/", f"{DASHBOARD}/", f"{src}/")
    run("dart", "run", "tool/brand.dart", "install", ROOT / "theme", "--apply", "--web-only", cwd=src)
    base = urllib.parse.urlsplit(SITE_URL).path
    run("flutter", "build", "web", "--release", "--no-web-resources-cdn", "--base-href", base,
        "--dart-define=P4N4_DEMO=true", f"--dart-define=P4N4_DEMO_PASSWORD={PASSWORD}",
        "--dart-define=P4N4_DEMO_GRAFANA=grafana/", cwd=src)


def dashboard() -> None:
    shutil.copytree(WORK / "dashboard" / "build" / "web", OUT)
    # The API is the app itself (DemoApi answers /api/v1/)
    (OUT / "config.json").write_text(json.dumps({"defaults": {"apiBase": "/"}}) + "\n")
    run(sys.executable, HERE / "store.py", SITE_URL, OUT / "demo" / "store.json")
    store = json.loads((OUT / "demo" / "store.json").read_text())
    for cam in store["project"]["dashboard"]["cameras"]:
        shutil.copyfile(ROOT / "extras" / "clips" / f"{cam['id']}.mp4", OUT / cam["url"])
    if (REC / "dashboard" / "feed").is_dir():
        shutil.copytree(REC / "dashboard" / "feed", OUT / "demo" / "feed")


def analysis() -> None:
    out = OUT / "analysis"
    shutil.copytree(ROOT / "iot" / "ingest" / "analysis", out)
    shutil.copyfile(HERE / "static.js", out / "static.js")
    for page in out.glob("*.html"):
        text = page.read_text()
        if SCRIPTS in text:
            page.write_text(text.replace(SCRIPTS, SCRIPTS + '\n<script src="static.js"></script>', 1))
    data = out / "data"
    shutil.copytree(REC / "analysis", data)
    shutil.copytree(REC / "stories", data / "stories")
    shutil.copyfile(REC / "stories.json", data / "stories.json")


def grafana() -> None:
    out = OUT / "grafana"
    shutil.copytree(HERE / "pages" / "grafana", out)
    shutil.copytree(REC / "grafana", out / "img")
    matches = {m["match_id"]: m for m in json.loads((REC / "analysis" / "analysis_matches.json").read_text())["matches"]}
    shown = sorted({p.name.split(".")[0] for p in (out / "img").glob("*.jpg")}, key=lambda i: matches[i]["kickoff"], reverse=True)
    label = lambda m: f"{m['kickoff'][:10]} · {m['home']['code']} {m['score']['home']}–{m['score']['away']} {m['away']['code']}"  # noqa: E731
    (out / "matches.json").write_text(json.dumps({"recorded": recorded_on(), "matches": [
        {"id": i, "label": label(matches[i])} for i in shown]}, ensure_ascii=False))


def reports() -> None:
    out = OUT / "reports"
    shutil.copytree(HERE / "pages" / "reports", out)
    (out / "mail").mkdir()
    messages = json.loads((REC / "mail" / "messages.json").read_text())
    for m in messages:
        html = (REC / "mail" / f"{m['id']}.html").read_text()
        # The emails' links to the stack's analysis room, to the site's; they open outside the reader
        html = re.sub(r"https?://[^/\s\"'<>]+:8090/analysis/", "../../analysis/", html)
        html = re.sub(r"(<head[^>]*>)", r'\1<base target="_blank">', html, count=1, flags=re.I) if re.search(r"<head", html, re.I) \
            else '<base target="_blank">' + html
        (out / "mail" / f"{m['id']}.html").write_text(html)
    (out / "messages.json").write_text(json.dumps(sorted(messages, key=lambda m: m["at"], reverse=True), ensure_ascii=False))


def recorded_on() -> str:
    at = json.loads((REC / "recorded.json").read_text())["at"]
    return f"recorded {at[:10]}"


def publish() -> None:
    """The site as the only commit of origin's gh-pages branch (Settings → Pages: deploy from gh-pages, /)."""
    remote = subprocess.run(["git", "-C", ROOT, "remote", "get-url", "origin"], capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(OUT, tmp, dirs_exist_ok=True)
        run("git", "init", "-q", "-b", "gh-pages", cwd=tmp)
        run("git", "add", "-A", cwd=tmp)
        run("git", "commit", "-q", "-m", f"site: {recorded_on()}", cwd=tmp)
        run("git", "push", "-f", remote, "gh-pages", cwd=tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-flutter", action="store_true", help="reuse the last dashboard build")
    parser.add_argument("--publish", action="store_true", help="push the site to origin's gh-pages branch")
    args = parser.parse_args()
    if not (REC / "recorded.json").is_file():
        raise SystemExit("no recording: python3 deploy/pages/record.py first")
    if not args.no_flutter:
        flutter()
    shutil.rmtree(OUT, ignore_errors=True)
    dashboard()
    analysis()
    grafana()
    reports()
    (OUT / ".nojekyll").write_text("")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"{OUT}: {size / 1e6:.0f} MB, for {SITE_URL}")
    if args.publish:
        publish()


if __name__ == "__main__":
    main()
