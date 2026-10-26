#!/usr/bin/env python3
"""
The candidate footage (extras/config/clips.json "_candidates") cut into short
clips around the moments that matter, without downloading whole matches:
a fallback for the Video tab and the vision models if the kickabout isn't
filmed, pending the organisers' answer on real footage.

    python3 extras/scripts/footage.py plan soccertrack      # the clips it would cut, their size, what fits
    python3 extras/scripts/footage.py soccertrack           # cut them (HF_TOKEN)
    python3 extras/scripts/footage.py soccernet --accept-terms   # off by default: the SoccerNet NDA (HF_TOKEN)
    python3 extras/scripts/footage.py dfl --accept-terms         # off by default: Kaggle's rules (KAGGLE_*)
    python3 extras/scripts/footage.py status                # what's on disk against the budget

soccertrack reads SoccerTrack v2's ball-action labels (CC BY 4.0), puts a
window around every goal and shot (12 s before, 8 s after, overlapping
windows joined), goals first and the matches in turn, and has ffmpeg read
only those seconds of the panoramas (25 fps; 4096×1080, 117092 3840×1906, 132831 3840×1504) by range
requests: no re-encode, about 75 MB a minute (twice that for 117092 and 132831). soccernet lists one of SoccerNet's zips remotely, takes the
first --games games, extracts one video at a time, cuts the same windows from
its Labels-ball.json and deletes the full video (--keep-full keeps it). dfl
lists the Bundesliga Data Shootout's files, reads train.csv, and cuts windows
around its challenges and throw-ins from --videos videos, by range requests.
SoccerNet and DFL refuse to run without --accept-terms: read their terms
first (the NDA, the competition's rules); neither is likely to allow the
public video.

Everything goes to extras/clips/footage/<source>/ (gitignored, beside the
other footage), with extras/clips/footage/index.json: each clip's file,
SHA-1, size, source, match, half, window and the labelled events in it, and
the credit. Clips already there with the right SHA-1 are kept. The budget
covers extras/clips/footage only, 5 GB by default (--gb, FOOTAGE_GB): a clip
that would pass it isn't cut, and a full video to extract must fit too. To use
a clip as a camera, add it to clips.json's sources (file: footage/<source>/<clip>,
its SHA-1, the credit, the match and kits) and a camera with its ranges.

    HF_TOKEN                        a Hugging Face read token, after accepting the dataset's terms on its
                                    page (default: ~/.cache/huggingface/token)
    KAGGLE_API_TOKEN                a Kaggle API token (default: ~/.kaggle/access_token), or a legacy key:
    KAGGLE_USERNAME, KAGGLE_KEY     (default: ~/.kaggle/kaggle.json); either after
                                    accepting the competition's rules on Kaggle
    FOOTAGE_GB                      the budget, in GB (default 5)
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

PROJECT = Path(__file__).resolve().parents[2]
FOOTAGE = PROJECT / "extras/clips/footage"
INDEX = FOOTAGE / "index.json"
USER_AGENT = "p4n4-brighton-match-analysis/0.1 (candidate footage)"
GB = 1_000_000_000

SOCCERTRACK = "atomscott/soccertrack-v2"
SOCCERTRACK_CREDIT = ("SoccerTrack v2, A. Scott, I. Uchida, K. Kuroda, Y. Kim, K. Fujii (arXiv:2508.01802), "
                      "CC BY 4.0, https://huggingface.co/datasets/atomscott/soccertrack-v2; cut, no other change")
DFL = "dfl-bundesliga-data-shootout"


# --- HTTP ---------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url: str, headers: dict | None = None, method: str = "GET"):
    return urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})


def get(url: str, headers: dict | None = None) -> bytes:
    try:
        with urllib.request.urlopen(request(url, headers), timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GET {url}: HTTP {e.code}{hint(e.code)}") from None


def hint(code: int) -> str:
    return {401: " (no or wrong credentials)", 403: " (accept the terms on the dataset's page first)"}.get(code, "")


def direct(url: str, headers: dict | None = None) -> tuple[str, int, dict]:
    """Where the file is really served from, its size, and the headers that URL needs.

    Gated files redirect to a signed URL that needs no credentials: ffmpeg and the range reads go there, so
    the token isn't sent to the storage host or shown in ps. One byte is asked for (GET: a signed URL may
    allow no other method), the size read from Content-Range."""
    opener = urllib.request.build_opener(_NoRedirect)
    target, needs = url, dict(headers or {})
    while True:
        try:
            with opener.open(request(target, {**needs, "Range": "bytes=0-0"}), timeout=60) as response:
                total = response.headers.get("Content-Range", "").rpartition("/")[2]
                return target, int(total if total.isdigit() else response.headers["Content-Length"]), needs
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise SystemExit(f"{target.split('?')[0]}: HTTP {e.code}{hint(e.code)}") from None
            location = urllib.parse.urljoin(target, e.headers["Location"])
            if urllib.parse.urlsplit(location).netloc != urllib.parse.urlsplit(target).netloc:
                needs = {}  # the credentials stay with the host they're for
            target = location


def fetch_bytes(url: str, headers: dict | None = None) -> bytes:
    target, _, needs = direct(url, headers)
    return get(target, needs)


def ffmpeg_headers(headers: dict) -> list[str]:
    return ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())] if headers else []


class RemoteFile(io.RawIOBase):
    """A file on a server that takes range requests, read as if local (zipfile reads only what it needs)."""

    def __init__(self, url: str, size: int, headers: dict | None = None):
        self.url, self.size, self.pos, self.headers = url, size, 0, headers or {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def readinto(self, buffer) -> int:
        if self.pos >= self.size:
            return 0
        end = min(self.pos + len(buffer), self.size) - 1
        with urllib.request.urlopen(request(self.url, {**self.headers, "Range": f"bytes={self.pos}-{end}"}),
                                    timeout=120) as r:
            data = r.read()
        buffer[:len(data)] = data
        self.pos += len(data)
        return len(data)


def hf_token() -> str:
    token = os.environ.get("HF_TOKEN", "").strip()
    path = Path.home() / ".cache/huggingface/token"
    if not token and path.is_file():
        token = path.read_text().strip()
    if not token:
        raise SystemExit("no HF_TOKEN: accept the dataset's terms on its Hugging Face page, make a read token "
                         "(huggingface.co/settings/tokens), then export HF_TOKEN=hf_...")
    return token


def hf_tree(repo: str) -> dict[str, int]:
    """Every file in a dataset repo and its size (public, even when the files are gated)."""
    tree = json.loads(get(f"https://huggingface.co/api/datasets/{repo}/tree/main?recursive=1"))
    return {f["path"]: f.get("size", 0) for f in tree if f["type"] == "file"}


def hf_url(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{urllib.parse.quote(path)}"


# --- The budget, the index, the cut -------------------------------------------------------------------------------

def budget(args: argparse.Namespace) -> int:
    return int(float(args.gb if args.gb is not None else os.environ.get("FOOTAGE_GB", 5)) * GB)


def used() -> int:
    return sum(p.stat().st_size for p in FOOTAGE.rglob("*") if p.is_file()) if FOOTAGE.is_dir() else 0


def load_index() -> dict:
    return json.loads(INDEX.read_text()) if INDEX.is_file() else {"clips": {}}


def save_index(index: dict) -> None:
    FOOTAGE.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n")


def sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def have(index: dict, name: str) -> bool:
    entry, path = index["clips"].get(name), FOOTAGE / name
    return bool(entry) and path.is_file() and sha1(path) == entry["sha1"]


def windows(events: list[dict], labels: set[str], before: float, after: float) -> list[dict]:
    """A window around each event with one of the labels, per half; overlapping windows joined."""
    out: list[dict] = []
    picked = sorted((e for e in events if e["label"].lower() in labels), key=lambda e: (e["half"], e["t"]))
    for e in picked:
        start, end = max(0.0, e["t"] - before), e["t"] + after
        if out and out[-1]["half"] == e["half"] and start <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], end)
            out[-1]["events"].append(e)
        else:
            out.append({"half": e["half"], "start": start, "end": end, "events": [e]})
    return out


def ranked(by_match: dict[str, list[dict]], first: str) -> list[tuple[str, dict]]:
    """Windows with the first label (a goal) before the others, the matches in turn within each."""
    def order(group: list[tuple[str, list[dict]]]) -> list[tuple[str, dict]]:
        out, i = [], 0
        while any(i < len(ws) for _, ws in group):
            out += [(m, ws[i]) for m, ws in group if i < len(ws)]
            i += 1
        return out
    top = [(m, [w for w in ws if any(e["label"].lower() == first for e in w["events"])]) for m, ws in by_match.items()]
    rest = [(m, [w for w in ws if all(e["label"].lower() != first for e in w["events"])]) for m, ws in by_match.items()]
    return order(top) + order(rest)


def probe_duration(url: str, headers: dict | None = None) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-user_agent", USER_AGENT, *ffmpeg_headers(headers or {}),
                          "-show_entries", "format=duration", "-of", "csv=p=0", url], capture_output=True, text=True)
    if out.returncode or not out.stdout.strip():
        raise SystemExit(f"ffprobe couldn't read {url.split('?')[0]}: {out.stderr.strip()[:200]}")
    return float(out.stdout.strip())


def cut(src: str, headers: dict, start: float, end: float, out: Path) -> None:
    """[start, end] of src (a URL, with the headers it needs, or a file) to out, the video stream only, copied:
    it starts on the keyframe at or before start."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".part" + out.suffix)
    run = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-user_agent", USER_AGENT, *ffmpeg_headers(headers),
         "-ss", f"{start:.2f}", "-i", src, "-t", f"{end - start:.2f}", "-map", "0:v:0", "-c", "copy",
         "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(part)])
    if run.returncode:
        part.unlink(missing_ok=True)
        raise SystemExit(f"ffmpeg failed on {out.name}")
    part.replace(out)


def take(plan: list[dict], index: dict, limit: int, cap: int, dry: bool) -> None:
    """Cut each planned clip that isn't there yet, while the budget holds: plan entries carry name, src() (the
    source to cut from and its headers, resolved when needed), start, end, estimate and the index fields."""
    total, cut_n, skipped = used(), 0, 0
    print(f"budget {cap / GB:.1f} GB, {total / GB:.2f} GB used")
    for clip in plan[:limit]:
        name, seconds = clip["name"], clip["end"] - clip["start"]
        if have(index, name):
            print(f"  {name}: there")
            continue
        if total + clip["estimate"] > cap:
            skipped += 1
            continue
        line = f"  {name}: {seconds:.0f} s, ~{clip['estimate'] / 1e6:.0f} MB, " + ", ".join(clip["labels"])
        if dry:
            print(line)
            total += clip["estimate"]
            cut_n += 1
            continue
        print(line)
        out = FOOTAGE / name
        cut(*clip["src"](), clip["start"], clip["end"], out)
        size = out.stat().st_size
        total += size
        cut_n += 1
        index["clips"][name] = {**clip["entry"], "sha1": sha1(out), "bytes": size, "seconds": round(seconds, 2)}
        save_index(index)
    verb = "would cut" if dry else "cut"
    print(f"{verb} {cut_n} clips: {total / GB:.2f} GB of {cap / GB:.1f}"
          + (f"; {skipped} more don't fit (--gb, or fewer --clips)" if skipped else "")
          + (f"; {max(0, len(plan) - limit)} left out by --clips {limit}" if len(plan) > limit else ""))


# --- SoccerTrack v2 -----------------------------------------------------------------------------------------------

def actions(labels: dict) -> list[dict]:
    """The labelled ball actions: "actions" in SoccerTrack v2's released files (position: ms from the first
    frame of the half's video), "annotations" in SoccerNet's and in SoccerTrack's format doc."""
    return labels.get("actions") or labels.get("annotations") or []


def soccertrack(args: argparse.Namespace, dry: bool = False) -> None:
    token = hf_token()
    auth = {"Authorization": f"Bearer {token}"}
    files = hf_tree(SOCCERTRACK)
    matches = args.matches or sorted({PurePosixPath(p).parts[1] for p in files if p.startswith("videos/")})
    labels = {label.lower() for label in args.labels}
    by_match: dict[str, list[dict]] = {}
    for match in matches:
        bas = json.loads(fetch_bytes(hf_url(SOCCERTRACK, f"bas/{match}/{match}_12_class_events.json"), auth))
        events = [{"half": int(a["gameTime"].split(" - ")[0]), "t": int(a["position"]) / 1000, "label": a["label"],
                   "team": a.get("team"), "player_id": a.get("player_id")} for a in actions(bas)]
        by_match[match] = windows(events, labels, args.before, args.after)
    halves: dict[tuple[str, int], tuple[str, dict]] = {}

    def half_video(match: str, half: int) -> tuple[str, dict]:
        if (match, half) not in halves:
            path = f"videos/{match}/{match}_panorama_{'1st' if half == 1 else '2nd'}_half.mp4"
            url, _, needs = direct(hf_url(SOCCERTRACK, path), auth)
            halves[match, half] = (url, needs)
        return halves[match, half]

    plan = []
    for match, w in ranked(by_match, args.labels[0].lower()):
        half = w["half"]
        size = files[f"videos/{match}/{match}_panorama_{'1st' if half == 1 else '2nd'}_half.mp4"]
        rate = size / (47 * 60)  # bytes a second, until the half is probed: halves run about 47 minutes
        plan.append({
            "name": f"soccertrack/{match}-{half}-{int(w['start']) // 60:02d}m{int(w['start']) % 60:02d}.mp4",
            "start": w["start"], "end": w["end"], "estimate": rate * (w["end"] - w["start"]),
            "labels": sorted({e["label"] for e in w["events"]}),
            "src": lambda match=match, half=half: half_video(match, half),
            "entry": {"source": "soccertrack-v2", "match": match, "half": half,
                      "window": [round(w["start"], 2), round(w["end"], 2)],
                      "events": [{k: e[k] for k in ("t", "label", "team", "player_id")} for e in w["events"]],
                      "credit": SOCCERTRACK_CREDIT, "licence": "CC BY 4.0"},
        })
    print(f"SoccerTrack v2: {len(plan)} windows around {', '.join(args.labels)} in {len(matches)} matches")
    take(plan, load_index(), args.clips, budget(args), dry)


# --- SoccerNet (NDA) ----------------------------------------------------------------------------------------------

def soccernet(args: argparse.Namespace, dry: bool = False) -> None:
    terms(args, "SoccerNet's NDA (sent when you request access on Hugging Face)")
    auth = {"Authorization": f"Bearer {hf_token()}"}
    repo = f"SoccerNet/{args.repo}"
    url, size, needs = direct(hf_url(repo, f"{args.split}.zip"), auth)
    archive = zipfile.ZipFile(io.BufferedReader(RemoteFile(url, size, needs), 1 << 20))
    names = archive.namelist()
    games = sorted({str(PurePosixPath(n).parent) for n in names if PurePosixPath(n).name.startswith("Labels")})
    games = [g for g in games if not args.game or args.game.lower() in g.lower()][:args.games]
    if not games:
        raise SystemExit(f"{repo} {args.split}.zip: no game folder with labels" + (f" matching {args.game!r}" if args.game else ""))
    labels, index, cap = {label.lower() for label in args.labels}, load_index(), budget(args)
    for game in games:
        inside = [n for n in names if str(PurePosixPath(n).parent) == game]
        label_file = next(n for n in inside if PurePosixPath(n).name.startswith("Labels"))
        videos = sorted((n for n in inside if n.endswith((".mp4", ".mkv"))),
                        key=lambda n: ("720" not in n, -archive.getinfo(n).file_size))
        events = [{"half": int(a["gameTime"].split(" - ")[0]), "t": int(a["position"]) / 1000, "label": a["label"],
                   "team": a.get("team"), "player_id": None}
                  for a in actions(json.loads(archive.read(label_file)))]
        slug = PurePosixPath(game).name.replace(" ", "_")[:60]
        print(f"{game}: {len(events)} labels, videos {', '.join(PurePosixPath(v).name for v in videos)}")

        def video_for(half: int) -> str:
            # per-half files (1_720p.mkv, 2_720p.mkv) when there are, else the one video for the game
            return next((v for v in videos if PurePosixPath(v).name.startswith(f"{half}_")), videos[0])

        by_video: dict[str, list[dict]] = {}
        for w in windows(events, labels, args.before, args.after):
            by_video.setdefault(video_for(w["half"]), []).append(w)
        for video, ws in by_video.items():
            full = FOOTAGE / "soccernet" / "full" / f"{slug}-{PurePosixPath(video).name}"
            vsize = archive.getinfo(video).file_size
            rate = vsize / probe_or(full, 45 * 60)
            plan = [{
                "name": f"soccernet/{slug}-{w['half']}-{int(w['start']) // 60:02d}m{int(w['start']) % 60:02d}.mp4",
                "start": w["start"], "end": w["end"], "estimate": rate * (w["end"] - w["start"]),
                "labels": sorted({e["label"] for e in w["events"]}), "src": lambda full=full: (str(full), {}),
                "entry": {"source": "soccernet", "repo": repo, "game": game, "half": w["half"],
                          "window": [round(w["start"], 2), round(w["end"], 2)],
                          "events": [{k: e[k] for k in ("t", "label", "team")} for e in w["events"]],
                          "credit": f"SoccerNet ({repo}), under the SoccerNet NDA", "licence": "SoccerNet NDA"},
            } for w in ws]
            todo = [c for c in plan if not have(index, c["name"])]
            if dry or not todo:
                take(plan, index, args.clips, cap, dry)
                continue
            if not full.is_file():
                if used() + vsize > cap:
                    raise SystemExit(f"{video}: {vsize / GB:.2f} GB to extract won't fit the budget ({used() / GB:.2f} "
                                     f"of {cap / GB:.1f} GB used)")
                print(f"  extracting {PurePosixPath(video).name}, {vsize / GB:.2f} GB")
                full.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(video) as src, full.open("wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
            take(plan, index, args.clips, cap + (0 if args.keep_full else vsize), False)
            if not args.keep_full:
                full.unlink()


def probe_or(path: Path, default: float) -> float:
    return probe_duration(str(path)) if path.is_file() else default


# --- DFL Bundesliga Data Shootout (Kaggle) ------------------------------------------------------------------------

def kaggle_auth() -> dict:
    """An API token (KGAT_..., KAGGLE_API_TOKEN or ~/.kaggle/access_token) or a legacy key (kaggle.json)."""
    token, path = os.environ.get("KAGGLE_API_TOKEN", "").strip(), Path.home() / ".kaggle/access_token"
    if not token and path.is_file():
        token = path.read_text().strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    path = Path.home() / ".kaggle/kaggle.json"
    if not (user and key) and path.is_file():
        creds = json.loads(path.read_text())
        user, key = creds.get("username"), creds.get("key")
    if not (user and key):
        raise SystemExit("no Kaggle credentials: an API token in ~/.kaggle/access_token (or KAGGLE_API_TOKEN), "
                         "or a legacy key in ~/.kaggle/kaggle.json (or KAGGLE_USERNAME and KAGGLE_KEY); "
                         "kaggle.com/settings, API")
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{key}".encode()).decode()}


def kaggle_files(auth: dict) -> dict[str, int]:
    files, page = {}, ""
    while True:
        body = json.loads(get(f"https://www.kaggle.com/api/v1/competitions/data/list/{DFL}"
                              + (f"?pageToken={urllib.parse.quote(page)}" if page else ""), auth))
        listing = body.get("files", []) if isinstance(body, dict) else body
        files.update({f.get("name") or f.get("ref"): int(f.get("totalBytes") or f.get("size") or 0) for f in listing})
        page = body.get("nextPageToken", "") if isinstance(body, dict) else ""
        if not page:
            return files


def kaggle_url(path: str) -> str:
    return f"https://www.kaggle.com/api/v1/competitions/data/download/{DFL}/{urllib.parse.quote(path)}"


def dfl(args: argparse.Namespace, dry: bool = False) -> None:
    terms(args, "the competition's rules (kaggle.com/competitions/dfl-bundesliga-data-shootout/rules) and the DFL's")
    auth = kaggle_auth()
    files = kaggle_files(auth)
    labels_path = next((n for n in files if PurePosixPath(n).name == "train.csv"), None)
    if not labels_path:
        raise SystemExit("no train.csv in the competition's files: accept the rules on Kaggle first")
    rows = list(csv.DictReader(io.StringIO(fetch_bytes(kaggle_url(labels_path), auth).decode())))
    by_video: dict[str, list[dict]] = {}
    for row in rows:
        by_video.setdefault(row["video_id"], []).append(
            {"half": 1, "t": float(row["time"]), "label": row["event"], "team": None, "player_id": None})
    videos = [v for v in sorted(by_video) if any(PurePosixPath(n).stem == v for n in files)][:args.videos]
    labels, by_match, sources = {label.lower() for label in args.labels}, {}, {}
    for video in videos:
        path = next(n for n in files if PurePosixPath(n).stem == video and n.endswith(".mp4"))
        sources[video] = path
        # a window every --spacing seconds at most, or the challenges alone fill the budget
        ws, last = [], -1e9
        for w in windows(by_video[video], labels, args.before, args.after):
            if w["start"] - last >= args.spacing:
                ws.append(w)
                last = w["start"]
        by_match[video] = ws
    resolved: dict[str, tuple[str, dict]] = {}

    def src(video: str) -> tuple[str, dict]:
        if video not in resolved:
            url, _, needs = direct(kaggle_url(sources[video]), auth)
            resolved[video] = (url, needs)
        return resolved[video]

    plan = []
    for video, w in ranked(by_match, args.labels[0].lower()):
        rate = files[sources[video]] / (45 * 60)
        plan.append({
            "name": f"dfl/{video}-{int(w['start']) // 60:02d}m{int(w['start']) % 60:02d}.mp4",
            "start": w["start"], "end": w["end"], "estimate": rate * (w["end"] - w["start"]),
            "labels": sorted({e["label"] for e in w["events"]}), "src": lambda video=video: src(video),
            "entry": {"source": "dfl-bundesliga-data-shootout", "video": video,
                      "window": [round(w["start"], 2), round(w["end"], 2)],
                      "events": [{k: e[k] for k in ("t", "label")} for e in w["events"]],
                      "credit": "DFL Bundesliga Data Shootout (Kaggle), under the competition's rules",
                      "licence": "Kaggle competition rules"},
        })
    print(f"DFL: {len(plan)} windows around {', '.join(args.labels)} in {len(videos)} videos")
    take(plan, load_index(), args.clips, budget(args), dry)


def terms(args: argparse.Namespace, what: str) -> None:
    if not args.accept_terms:
        raise SystemExit(f"off by default: read {what}, then run again with --accept-terms. "
                         "It's unlikely to allow the public video; ask the organisers first.")


# --- plan, status -------------------------------------------------------------------------------------------------

def plan(args: argparse.Namespace) -> None:
    {"soccertrack": soccertrack, "soccernet": soccernet, "dfl": dfl}[args.source](args, dry=True)


def status(args: argparse.Namespace) -> None:
    index, cap = load_index(), budget(args)
    by_source: dict[str, list] = {}
    for name, entry in index["clips"].items():
        by_source.setdefault(entry["source"], []).append((name, entry))
    for source, clips in sorted(by_source.items()):
        seconds = sum(e["seconds"] for _, e in clips)
        size = sum(e["bytes"] for _, e in clips)
        missing = [n for n, _ in clips if not (FOOTAGE / n).is_file()]
        print(f"{source}: {len(clips)} clips, {seconds / 60:.1f} min, {size / GB:.2f} GB"
              + (f", {len(missing)} missing on disk" if missing else ""))
    total = used()
    print(f"extras/clips/footage: {total / GB:.2f} GB of {cap / GB:.1f} GB ({(cap - total) / GB:.2f} GB left); "
          f"disk free: {shutil.disk_usage(PROJECT).free / GB:.1f} GB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gb", type=float, help="the budget for extras/clips/footage, in GB (default 5, FOOTAGE_GB)")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, labels: list[str], before: float = 12, after: float = 8,
               clips: int = 40) -> None:
        p.add_argument("--labels", nargs="+", default=labels,
                       help=f"the events to cut around, the first ranked first (default {' '.join(labels)})")
        p.add_argument("--before", type=float, default=before, help=f"seconds before each event (default {before:g})")
        p.add_argument("--after", type=float, default=after, help=f"seconds after it (default {after:g})")
        p.add_argument("--clips", type=int, default=clips, help=f"at most this many clips (default {clips})")
        p.add_argument("--gb", type=float, default=argparse.SUPPRESS, help="the budget, in GB (default 5)")

    def soccertrack_args(p: argparse.ArgumentParser) -> None:
        common(p, ["Goal", "Shot"])
        p.add_argument("--matches", nargs="+", help="match ids (default: all ten, e.g. 117093 118575)")

    def soccernet_args(p: argparse.ArgumentParser) -> None:
        common(p, ["GOAL", "SHOT"])
        p.add_argument("--accept-terms", action="store_true", help="you've read and accepted SoccerNet's NDA")
        p.add_argument("--repo", default="SN-BAS-2025", help="the SoccerNet dataset (default SN-BAS-2025)")
        p.add_argument("--split", default="valid", help="the zip: train, valid, test, challenge (default valid)")
        p.add_argument("--games", type=int, default=1, help="how many games (default 1)")
        p.add_argument("--game", help="only games whose folder contains this, e.g. a club's name")
        p.add_argument("--keep-full", action="store_true", help="keep each extracted video (counts in the budget)")

    def dfl_args(p: argparse.ArgumentParser) -> None:
        common(p, ["challenge", "throwin"], before=6, after=6)
        p.add_argument("--accept-terms", action="store_true", help="you've read and accepted the competition's rules")
        p.add_argument("--videos", type=int, default=1, help="how many of the labelled videos (default 1)")
        p.add_argument("--spacing", type=float, default=45, help="seconds between windows at least (default 45)")

    plan_cmd = commands.add_parser("plan", help="list the clips a source would give, their size, what fits; cut nothing")
    sources = plan_cmd.add_subparsers(dest="source", required=True)
    for name, add in (("soccertrack", soccertrack_args), ("soccernet", soccernet_args), ("dfl", dfl_args)):
        add(sources.add_parser(name))
    soccertrack_args(commands.add_parser("soccertrack", help="cut SoccerTrack v2's goals and shots (CC BY 4.0)"))
    soccernet_args(commands.add_parser("soccernet", help="cut a SoccerNet game's goals and shots (NDA, off by default)"))
    dfl_args(commands.add_parser("dfl", help="cut the DFL Shootout's challenges (Kaggle rules, off by default)"))
    status_cmd = commands.add_parser("status", help="the clips on disk, by source, against the budget")
    status_cmd.add_argument("--gb", type=float, default=argparse.SUPPRESS)
    args = parser.parse_args()
    args.gb = getattr(args, "gb", None)
    if args.command != "status" and not shutil.which("ffmpeg"):
        raise SystemExit("needs ffmpeg and ffprobe")
    {"plan": plan, "soccertrack": soccertrack, "soccernet": soccernet, "dfl": dfl, "status": status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
