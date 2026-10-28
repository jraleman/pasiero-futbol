#!/usr/bin/env python3
"""
The dashboard demo's store (its demo/store.json: p4n4-dashboard's lib/demo/demo_store.dart) from the recording
(record.py): the project and its cameras, Home, the feed, the stacks and the assistant's recorded answers.

    python3 deploy/pages/store.py <site url> <out file>

The feed's matches are laid over the viewer's calendar, one a day, the newest yesterday, each kicking off at
15:00 and played at its real pace. Links to the analysis room go to the site's (<site url>analysis/).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REC = HERE / "recording"
KICKOFF = 15 * 3600
FOOTAGE = 11 * 3600  # the footage's goals (the scouting matches): that morning, before the newest match
SPEED = 3  # the simulator's: its matches ran three times faster than real


def load(name: str):
    return json.loads((REC / name).read_text())


def site_links(text: str, site: str) -> str:
    """The stack's analysis room (any host, port 8090) as the site's."""
    return re.sub(r"https?://[^/\s\"')]+:8090/analysis/", site + "analysis/", text)


def feed_days(site: str) -> tuple[list[dict], dict]:
    feed = load("dashboard/feed.json")
    matches = {m["match_id"]: m for m in load("analysis/analysis_matches.json")["matches"]}
    by_match: dict[str, list] = {}
    footage = sorted((i for i in feed["items"] if matches[i["meta"]["match_id"]].get("scouting")), key=lambda i: i["time"])
    for item in feed["items"]:
        if not matches[item["meta"]["match_id"]].get("scouting"):
            by_match.setdefault(item["meta"]["match_id"], []).append(item)
    labels: dict[str, str] = {}
    days = []
    order = sorted(by_match, key=lambda i: matches[i]["kickoff"])
    for match_id in order:
        kickoff = datetime.fromisoformat(matches[match_id]["kickoff"])
        items, reviews = [], {}
        extra = footage if match_id == order[-1] else []
        start = datetime.fromisoformat(extra[0]["time"]) if extra else None
        for item in extra + sorted(by_match[match_id], key=lambda i: i["time"]):
            t = datetime.fromisoformat(item["time"])
            s = FOOTAGE + (t - start).total_seconds() if item in extra else KICKOFF + (t - kickoff).total_seconds() * SPEED
            for a in item.get("actions") or []:
                labels.setdefault(a["id"], a["label"])
            out = {k: v for k, v in item.items() if k not in ("time", "status", "actions")}
            if out.get("link"):
                out["link"] = {**out["link"], "url": site_links(out["link"]["url"], site)}
            items.append({"s": round(s, 1), **out})
            if item["kind"] == "alert" and item.get("status", "pending_review") != "pending_review":
                reviews[item["id"]] = [round(s + 120, 1), item["status"]]
        days.append({"date": kickoff.date().isoformat(), "items": items, "reviews": reviews})
    # Today: nothing yet (the newest match was yesterday)
    days.append({"date": datetime.now().date().isoformat(), "items": [], "reviews": {}})
    labels.setdefault("reopen", "Reopen")
    return days, {"title": feed["title"], "actions": labels}


def main() -> None:
    site, out = sys.argv[1], Path(sys.argv[2])
    site = site if site.endswith("/") else site + "/"
    project = load("dashboard/project.json")
    for k in ("root", "created_at"):
        project.pop(k, None)
    project["stacks"] = [{"name": s["name"], "relative_dir": s.get("relative_dir")} for s in project.get("stacks", [])]
    dashboard = project["dashboard"]
    dashboard["cameras"] = [{"id": c["id"], "name": c["name"], "url": f"demo/{c['id']}.mp4"} for c in dashboard["cameras"]]
    home = load("dashboard/home.json")["markdown"]
    days, feed = feed_days(site)
    stacks = [{"name": s["name"], "services": [[x["name"], x["image"]] for x in s["services"]]}
              for s in load("dashboard/stacks.json")["stacks"] if s["name"] != "api"]
    store = {
        "lang": "en",
        "currency": "GBP",
        "anchor": days[0]["date"],
        "repeat": False,
        "review": {"pending": ["confirm", "dismiss"], "done": ["reopen"],
                   "status": {"confirm": "confirmed", "dismiss": "dismissed", "reopen": "pending_review"}},
        "project": project,
        "model": load("dashboard/agents.json")["model"],
        "feed_title": feed["title"],
        "actions": feed["actions"],
        "alert_titles": {},
        "home": {"": site_links(home, site)},
        "vision": load("dashboard/vision.json"),
        "stacks": stacks,
        "chats": {},
        "answers": [{"q": a["q"], "steps": a["steps"], "text": site_links(a["text"], site)} for a in load("answers.json")],
        "days": days,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(store, ensure_ascii=False, separators=(",", ":")))
    print(f"{out}: {sum(len(d['items']) for d in days)} feed items over {len(days) - 1} days, "
          f"{len(store['answers'])} answers")


if __name__ == "__main__":
    main()
