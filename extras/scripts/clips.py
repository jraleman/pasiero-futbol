#!/usr/bin/env python3
"""
The demo's footage from anyone: openly licensed match clips the video service
loops as cameras of their own, what the vision models find in them (posted as
scouting matches, as if filmed just now), and the Video tab's boxes over them.

    python3 extras/scripts/clips.py fetch     # sources → one looped clip per camera
    python3 extras/scripts/clips.py analyze   # matchvision on each source: the plays, the teams' tracks (heavy)
    python3 extras/scripts/clips.py detect    # people and ball 10 times a second on the loops (~45 min on CPU)
    python3 extras/scripts/clips.py post      # the sources' matches and plays → the ingest service
    python3 extras/scripts/clips.py overlay   # every demo camera's boxes and moments → the Video tab's overlay
    python3 extras/scripts/clips.py remove    # drop the loops

Run them on the host, from the project directory, then restart the video
service: (cd extras && docker compose restart video).

extras/config/clips.json lists the sources (URL, SHA-1, author, licence, the
credit, one string per line, the match and its two kits) and which
parts of a source each camera loops: news reports cut to interviews, so a
camera's ranges keep only football. A camera's "crop" ([x, y, width,
height], 0-1 of the source's frame) frames part of the picture, e.g. one end
of a panorama, and the overlay crops its boxes to match. fetch reuses a
source already in extras/clips with the right SHA-1, downloads it otherwise
(a source without a URL, like footage.py's clips, must be on disk), and writes
extras/clips/<camera>.mp4 (720p H.264, no sound), nothing burned in: the
footage is credited, as CC BY asks, in the proposal, the deck and the README
(the credit in the manifest), not over the picture. video.sh then loops it
as its own stream. extras/clips is gitignored: the manifest is what's kept,
the footage stays on this disk.

analyze runs python -m matchvision.analyze on each source in the vision image
(brighton-vision:dev, README "The vision pipeline"), 2 frames a second, with
the manifest's kits (and a source's "imgsz", the detection size a wide
panorama needs), keeping the detection stage (frames.json) for the
overlay: extras/clips/analysis/<source>/ (alerts.json, summary.json,
keyframes/). A source already analysed is skipped; --force does it again.

post posts each source's match as a scouting match ("scouting": true:
neither side is ours, no game plan applies), at full time with the
manifest's score, and the plays the models found: a contact and a fall is a
foul (whether it was given, the video doesn't say), each with its keyframe,
on the camera whose loop shows it ("source": "video"). A source's "labels"
(plays a person labelled, e.g. a dataset's goals: {"play_id", "t" in the
source's seconds, "clock" (the seconds since the period's kick-off), "period",
"type", "team", "players"}) are posted too
("source": "label", the frame the camera shows then as evidence, cut once into
the analysis's keyframes/), and the
overlay flags them like the models' plays. Dated as if the clip ended now, so
they top the feed; posting again updates them.

overlay writes extras/clips/vision.json (.p4n4.json dashboard.vision) for
every footage camera: the players the analyses found, by team, the ball, and
each posted play's moment on the camera that shows it, linked to its feed
item (SITE_ID:play_id), so the dashboard lists the play as its moment plays.
The cameras and their moments go to extras/clips/cameras.json first. It runs
python -m matchvision.overlay in the vision image, without models or
network, memory capped. detect runs the detectors again
(python -m matchvision.dense, --fps and --imgsz) on just the seconds the
cameras loop, so overlay draws 10 boxes a second instead of the analyses' 2:
a heavy job, so run nothing else heavy beside it (stop the ai layer's
Ollama). A source already done is skipped; --force does it again.

    INGEST_URL     default http://localhost:8090
    SITE_ID        default: iot/.env's, else brighton
    SITE_TOKEN     the club device's write token; default: iot/.env's
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT / "extras/config/clips.json"
CLIPS = PROJECT / "extras/clips"
USER_AGENT = "p4n4-brighton-match-analysis/0.1 (demo footage fetch)"


def load() -> dict:
    return json.loads(MANIFEST.read_text())


def sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def source_file(name: str, source: dict) -> Path:
    """The source on disk, checked against its SHA-1; downloaded when missing."""
    path = CLIPS / source["file"]
    if path.is_file() and sha1(path) == source["sha1"]:
        return path
    if not source.get("url"):
        raise SystemExit(f"{name}: {path.relative_to(PROJECT)} is missing or changed, and has no URL "
                         "(cut it again: extras/scripts/footage.py)")
    print(f"{name}: downloading {source['page']}")
    part = path.with_suffix(path.suffix + ".part")
    request = urllib.request.Request(source["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as out:
        while block := response.read(1 << 20):
            out.write(block)
    if sha1(part) != source["sha1"]:
        part.unlink()
        raise SystemExit(f"{name}: the download's SHA-1 isn't the manifest's; not using it")
    part.replace(path)
    return path


def cut(camera: str, spec: dict, src: Path) -> None:
    """extras/clips/<camera>.mp4: the camera's ranges of its source, joined and cropped (the tile names the camera)."""
    ranges = spec["ranges"]
    parts = "".join(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[p{i}];" for i, (a, b) in enumerate(ranges))
    joined = "".join(f"[p{i}]" for i in range(len(ranges))) + f"concat=n={len(ranges)}:v=1:a=0[cat];"
    graph = parts + joined + f"[cat]{crop_filter(spec)}scale=-2:720,setsar=1[out]"
    out = CLIPS / f"{camera}.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-filter_complex", graph,
         "-map", "[out]", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         # A keyframe every second: the dashboard seeks each loop to its shared clock, and a seek decodes from the
         # keyframe before it (x264's default, every 10 s, kept the big panoramas black for seconds)
         "-force_key_frames", "expr:gte(t,n_forced*1)", "-movflags", "+faststart", str(out)],
        check=True,
    )
    seconds = sum(b - a for a, b in ranges)
    print(f"{camera}: {out.relative_to(PROJECT)}, {seconds:.0f} s of {spec['source']}")


def fetch(args: argparse.Namespace) -> None:
    manifest = load()
    files = {name: source_file(name, s) for name, s in manifest["sources"].items()}
    for camera, spec in manifest["cameras"].items():
        out = CLIPS / f"{camera}.mp4"
        src = files[spec["source"]]
        newest = max(MANIFEST.stat().st_mtime, src.stat().st_mtime, Path(__file__).stat().st_mtime)
        if out.is_file() and out.stat().st_mtime > newest and not args.force:
            print(f"{camera}: {out.relative_to(PROJECT)} is up to date")
            continue
        cut(camera, spec, src)
    print("now: (cd extras && docker compose restart video)")


def remove(args: argparse.Namespace) -> None:
    for camera in load()["cameras"]:
        out = CLIPS / f"{camera}.mp4"
        if out.is_file():
            out.unlink()
            print(f"{camera}: removed {out.relative_to(PROJECT)}")
    print("now: (cd extras && docker compose restart video)")


# ── post ──────────────────────────────────────────────────────────────────────


def dotenv(path: Path) -> dict:
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip()
    return values


def call(method: str, url: str, token: str, body: bytes, kind: str) -> dict:
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": kind, "X-Site-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {url}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise SystemExit(f"{method} {url}: {e.reason} (is the iot layer up?)") from None


def camera_at(manifest: dict, source: str, t: float) -> str:
    """The camera whose loop shows second t of the source (else the first that loops the source)."""
    mine = [(c, s) for c, s in manifest["cameras"].items() if s["source"] == source]
    for camera, spec in mine:
        if any(a <= t < b for a, b in spec["ranges"]):
            return camera
    return mine[0][0]


# What the vision models' alerts are as the club's plays: a contact and a fall is a foul (the models can't tell if
# it was given); a goal is a goal. The rest (an offside candidate, a ball out) isn't a club's play
PLAY_TYPES = {"foul_contact": "foul", "goal": "goal"}
ROLES = {"offender": "fouler", "victim": "fouled", "scorer": "shooter"}


def play_id(alert_id: str) -> str:
    """The play an alert becomes keeps its number: AL-20261019-311801 → PL-20261019-311801."""
    return "PL-" + alert_id.split("-", 1)[1]


def as_play(doc: dict, site: str, match: dict, camera: str, timestamp: str) -> dict | None:
    """A vision alert as a club play (contract 0.1), or None when it isn't one."""
    kind = PLAY_TYPES.get(doc["type"])
    if kind is None:
        return None
    players = [{"team": p["team"], "number": p.get("number"), "role": ROLES.get(p["role"], "target")}
               for p in doc.get("players") or [] if p.get("role") in ROLES]
    # A foul is the fouled side's play (theirs the free kick); the alert names the offender's side
    team = next((p["team"] for p in players if p["role"] == "fouled"), doc["team"]) if kind == "foul" else doc["team"]
    out = {"schema": "p4n4.team.play/0.1", "play_id": play_id(doc["alert_id"]), "site_id": site,
           "match_id": match["match_id"], "camera_id": camera, "timestamp": timestamp, "period": doc["period"],
           "match_time_s": doc["match_time_s"], "type": kind, "team": team, "players": players[:6],
           "details": {}, "confidence": doc["confidence"], "source": "video", "synthetic": False}
    for key in ("snapshot", "track", "evidence"):
        if doc.get(key):
            out[key] = doc[key]
    return out


def label_play(label: dict, site: str, match: dict, camera: str, timestamp: str) -> dict:
    """A source's labelled play (e.g. a dataset's goal) as a club play (contract 0.1): labelled, so certain."""
    return {"schema": "p4n4.team.play/0.1", "play_id": label["play_id"], "site_id": site,
            "match_id": match["match_id"], "camera_id": camera, "timestamp": timestamp,
            "period": label.get("period", 1), "match_time_s": float(label.get("clock", label["t"])), "type": label["type"],
            "team": label["team"], "players": label.get("players", []), "details": {}, "confidence": 1.0,
            "source": "label", "synthetic": False}


def crop_filter(spec: dict) -> str:
    """The camera's crop as an ffmpeg filter (with its trailing comma), or nothing."""
    if not spec.get("crop"):
        return ""
    x, y, w, h = spec["crop"]
    return f"crop=w=iw*{w}:h=ih*{h}:x=iw*{x}:y=ih*{y},"


def still(src: Path, t: float, spec: dict) -> bytes:
    """Second t of the source as the camera shows it, a 720p JPEG: a labelled play's evidence."""
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(t), "-i", str(src), "-frames:v", "1",
         "-vf", f"{crop_filter(spec)}scale=-2:720", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-"],
        check=True, capture_output=True,
    ).stdout


def post(args: argparse.Namespace) -> None:
    """Each source's match, for scouting (neither side ours), with the plays the models found and their keyframes."""
    manifest, env = load(), dotenv(PROJECT / "iot/.env")
    url = os.environ.get("INGEST_URL", "http://localhost:8090").rstrip("/")
    site = site_id()
    token = os.environ.get("SITE_TOKEN") or env.get("SITE_TOKEN", "")
    if not token:
        raise SystemExit("no SITE_TOKEN (set it, or iot/.env's)")
    now = datetime.now(timezone.utc).astimezone()
    for name, source in manifest["sources"].items():
        analysis, match = CLIPS / "analysis" / name, source.get("match")
        if not match or not (analysis / "alerts.json").is_file():
            print(f"{name}: no analysis in {analysis.relative_to(PROJECT)}, skipped (clips.py analyze)")
            continue
        seconds = json.loads((analysis / "summary.json").read_text())["seconds"]
        start = now - timedelta(seconds=seconds)
        if match.get("score_note"):
            print(f"{name}: note: {match['score_note']}")
        fixture = {
            "schema": "p4n4.team.fixture/0.1", "site_id": site, "match_id": match["match_id"],
            "competition": match["competition"], "home": match["home"], "away": match["away"], "team": "home",
            "kickoff": start.isoformat(timespec="seconds"), "status": "full_time", "period": 2,
            "score": match["score"], "scouting": True, "synthetic": False,
        }
        call("POST", f"{url}/api/v1/fixtures", token, json.dumps(fixture).encode(), "application/json")
        plays = []
        for doc in json.loads((analysis / "alerts.json").read_text()):
            t = float(doc["match_time_s"])
            play = as_play(doc, site, match, camera_at(manifest, name, t),
                           (start + timedelta(seconds=t)).isoformat(timespec="milliseconds"))
            if play:
                plays.append((doc, play))
        for label in source.get("labels", []):
            t = float(label["t"])
            plays.append((None, label_play(label, site, match, camera_at(manifest, name, t),
                                           (start + timedelta(seconds=t)).isoformat(timespec="milliseconds"))))
        if plays:
            result = call("POST", f"{url}/api/v1/plays", token, json.dumps([p for _, p in plays]).encode(), "application/json")
            if result.get("rejected"):
                print(f"{name}: rejected {result['rejected']}")
        for doc, play in plays:
            if doc is None:
                # Cut once, kept with the analysis: posting again (or on another host) needs no footage
                keyframe = analysis / "keyframes" / f"{play['play_id']}.jpg"
                if not keyframe.is_file():
                    keyframe.parent.mkdir(parents=True, exist_ok=True)
                    keyframe.write_bytes(still(CLIPS / source["file"], play["match_time_s"],
                                               manifest["cameras"][play["camera_id"]]))
                call("PUT", f"{url}/api/v1/evidence/{play['play_id']}", token, keyframe.read_bytes(), "image/jpeg")
                continue
            keyframe = analysis / "keyframes" / f"{doc['alert_id']}.jpg"
            if keyframe.is_file():
                call("PUT", f"{url}/api/v1/evidence/{play['play_id']}", token, keyframe.read_bytes(), "image/jpeg")
        where = ", ".join(f"{p['type']} on {p['camera_id']} at {p['match_time_s']:.0f}s" for _, p in plays) or "no plays"
        print(f"{name}: {match['match_id']} (scouting), {where}")


def vision(memory: str, *command: str) -> None:
    """python -m <command> in the vision image: this project's matchvision, the clips, the manifest, no network."""
    subprocess.run(
        # As this user, so what it writes isn't root's
        ["docker", "run", "--rm", "--memory", memory, "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
         "-e", "HOME=/tmp", "-e", "YOLO_CONFIG_DIR=/tmp",
         "-v", f"{PROJECT / 'ai/vision/matchvision'}:/app/matchvision:ro", "-v", f"{PROJECT / 'ai/vision/models'}:/models:ro",
         "-v", f"{CLIPS}:/clips", "-v", f"{MANIFEST.parent}:/config:ro", "--entrypoint", "python", "brighton-vision:dev",
         "-m", *command],
        check=True,
    )


def analyze(args: argparse.Namespace) -> None:
    """matchvision.analyze on each source, 2 frames a second, with the kits the manifest gives its match: the plays
    and the teams' tracks (frames.json, for the overlay). A source already analysed is skipped; --force does it again."""
    manifest = load()
    for name, source in manifest["sources"].items():
        folder, match = CLIPS / "analysis" / name, source["match"]
        if (folder / "alerts.json").is_file() and not args.force:
            print(f"{name}: already analysed")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        print(f"{name}: analysing {source['file']} (about 5 s of video a minute)", flush=True)
        vision("4g", "matchvision.analyze", f"/clips/{source['file']}", "--out", f"/clips/analysis/{name}",
               "--match-id", match["match_id"], "--home-kit", match["kits"]["home"], "--away-kit", match["kits"]["away"],
               "--camera", next(c for c, s in manifest["cameras"].items() if s["source"] == name), "--keep-frames",
               # A wide panorama's own detection size, as matchvision.dense reads it
               *(["--imgsz", str(source["imgsz"])] if source.get("imgsz") else []))
        summary = json.loads((folder / "summary.json").read_text())
        found = ", ".join(f"{a['type']} {a['t']:.1f}s" for a in summary["alerts"]) or "nothing"
        print(f"{name}: {found} ({summary['analysis_seconds']:.0f} s)")


def detect(args: argparse.Namespace) -> None:
    vision("4g", "matchvision.dense", "--manifest", f"/config/{MANIFEST.name}", "--clips", "/clips",
           "--fps", str(args.fps), "--imgsz", str(args.imgsz), *(["--force"] if args.force else []))


def site_id() -> str:
    return os.environ.get("SITE_ID") or dotenv(PROJECT / "iot/.env").get("SITE_ID") or "brighton"


def moments(manifest: dict, site: str) -> dict[str, list[dict]]:
    """Each archive camera's moments, in its source's seconds: every play post posts, on the camera post names,
    from its moment to the end of the range that shows it (6 s at most), linked to its feed item."""
    out: dict[str, list[dict]] = {camera: [] for camera in manifest["cameras"]}
    for name in manifest["sources"]:
        alerts = CLIPS / "analysis" / name / "alerts.json"
        for doc in json.loads(alerts.read_text()) if alerts.is_file() else []:
            if doc["type"] not in PLAY_TYPES:
                continue
            t = float(doc["match_time_s"])
            camera = camera_at(manifest, name, t)
            end = next((b for a, b in manifest["cameras"][camera]["ranges"] if a <= t < b), t + 6)
            # "alert": the overlay frames the alert's own players (the contact pair), not whoever's near the ball
            out[camera].append({"t": t, "until": round(min(t + 6, end - 0.1), 2), "type": PLAY_TYPES[doc["type"]],
                                "confidence": doc["confidence"], "feed": f"{site}:{play_id(doc['alert_id'])}",
                                "alert": doc["alert_id"]})
        # The source's labelled plays: the overlay frames who's near the ball then (no alert names them)
        for label in manifest["sources"][name].get("labels", []):
            t = float(label["t"])
            camera = camera_at(manifest, name, t)
            end = next((b for a, b in manifest["cameras"][camera]["ranges"] if a <= t < b), t + 6)
            out[camera].append({"t": t, "until": round(min(t + 6, end - 0.1), 2), "type": label["type"],
                                "confidence": 1.0, "feed": f"{site}:{label['play_id']}"})
    return out


def overlay(args: argparse.Namespace | None = None) -> None:
    """vision.json for every footage camera."""
    manifest, site = load(), site_id()
    found = moments(manifest, site)
    combined = {
        "sources": {name: {"file": s["file"], "match": s.get("match")} for name, s in manifest["sources"].items()},
        "cameras": {camera: {**spec, "moments": found[camera]} for camera, spec in manifest["cameras"].items()},
    }
    (CLIPS / "cameras.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n")
    vision("2g", "matchvision.overlay", "--manifest", "/clips/cameras.json", "--analysis", "/clips/analysis",
           "--out", "/clips/vision.json")
    print(f"{(CLIPS / 'vision.json').relative_to(PROJECT)}: {len(combined['cameras'])} cameras; reload the dashboard")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = commands.add_parser("fetch", help="download or reuse the sources, cut a loop per camera")
    fetch_cmd.add_argument("--force", action="store_true", help="cut the loops again even if up to date")
    analyze_cmd = commands.add_parser("analyze", help="run matchvision on each source (in the vision image, heavy)")
    analyze_cmd.add_argument("--force", action="store_true", help="analyse again the sources already done")
    commands.add_parser("post", help="post the clips' analyses to the ingest service")
    detect_cmd = commands.add_parser("detect", help="detect people and ball densely on the loops (in the vision image)")
    detect_cmd.add_argument("--fps", type=float, default=10.0)
    detect_cmd.add_argument("--imgsz", type=int, default=960)
    detect_cmd.add_argument("--force", action="store_true", help="detect again the sources already done")
    commands.add_parser("overlay", help="write the Video tab's overlay from the analyses (in the vision image)")
    commands.add_parser("remove", help="delete the loops: the cameras go back to synthetic")
    args = parser.parse_args()
    CLIPS.mkdir(exist_ok=True)
    {"fetch": fetch, "analyze": analyze, "post": post, "detect": detect, "overlay": overlay, "remove": remove}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
