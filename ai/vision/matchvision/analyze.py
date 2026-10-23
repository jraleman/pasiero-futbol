"""
Analyse a recorded match clip and post what it finds to the ingest service.

    python -m matchvision.analyze CLIP --home TAU --away PLA --home-kit red --away-kit green --post

Stages, all on CPU by default (a few seconds of video a minute):
  1. sample the clip at --fps; a shot cut starts the tracker and the pitch over
  2. per frame: people and the ball (detect.py), who stands on the pitch
     (scene.py), each person's kit colour (teams.py), tracks (track.py), and
     the pitch calibration with its gates (calib.py)
  3. over the clip: the two teams' kits, everyone's team
  4. the rules (events.py) → alerts, each with its keyframe, and its snapshot
     and track when the frame was calibrated; with --numbers, the shirt
     numbers of the players they name, at that moment (numbers.py, the
     stack's Ollama), and with --cards, the cards in the referee's report
     checked against them
  5. out: <out>/alerts.json, summary.json, keyframes/, annotated.mp4 (--video),
     and with --post the fixture, alerts, keyframes and a heartbeat to the
     ingest service (INGEST_URL, SITE_ID, SITE_TOKEN)

A fixed stadium camera is calibrated once (--calibration, made and checked
with python -m matchvision.check); otherwise the pitch-keypoint model tries
every frame, behind calib.py's gates.

The match: --match-id of a fixture the ingest service has, or --home and
--away to create one (status live, then full_time with --full-time, which
sends the match report). --period and --clock say where in the match the
clip starts; --start-time when it was filmed (default: now minus its length,
so the alerts count as fresh and reach the match commissioner).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from . import cache, calib, numbers, scene
from .events import Card, Rules
from .motion import Motion, propagate, verify
from .output import TYPE_NAMES, Ingest, alert_doc, alert_ids, draw, jpeg
from .pitch import Pitch
from .state import Ball, Frame
from .teams import Teams, body_pixels, kit_feature
from .track import track

FEET_ON_PITCH = 0.3


def parse_clock(text: str) -> float:
    """mm:ss (or seconds) since the period's kick-off."""
    if ":" in text:
        m, s = text.split(":", 1)
        return int(m) * 60 + float(s)
    return float(text)


def load_cards(path: str, period: int, clock_s: float) -> list[Card]:
    """
    The cards in the referee's report, as a JSON list of {"team": "away",
    "number": 6, "colour": "yellow", "period": 2, "clock": "16:30"} (the
    clock in its period, like --clock); the clip's period only.
    """
    cards = []
    for c in json.loads(Path(path).read_text()):
        if c.get("team") not in ("home", "away") or c.get("colour") not in ("yellow", "second_yellow", "red"):
            raise SystemExit(f"{path}: each card needs team home|away and colour yellow|second_yellow|red: {c}")
        if int(c.get("period", period)) == period:
            cards.append(Card(c["team"], int(c["number"]), c["colour"], parse_clock(str(c["clock"])) - clock_s))
    return cards


def feet_on_pitch(mask: np.ndarray, foot: tuple[float, float], height: float) -> bool:
    h, w = mask.shape
    x, y = int(foot[0]), int(foot[1])
    r = max(3, int(height * 0.15))
    window = mask[max(0, y - r // 2):min(h, y + r), max(0, x - r):min(w, x + r)]
    return bool(window.size > 0 and window.mean() / 255 >= FEET_ON_PITCH)


def pick_ball(balls, previous: Ball | None, width: int) -> Ball | None:
    """The likeliest ball: near the last one when there was one, else the most confident."""
    if not balls:
        return None
    if previous:
        near = [b for b in balls if np.hypot(b[0][0] - previous.centre[0], b[0][1] - previous.centre[1]) < width * 0.15]
        if near:
            return Ball(near[0][0], near[0][1])
    return Ball(balls[0][0], balls[0][1]) if balls[0][1] >= 0.3 else None


def open_clip(clip: Path, work: Path) -> cv2.VideoCapture:
    """The clip, or an H.264 copy of it when OpenCV can't decode it (AV1, some WebM)."""
    capture = cv2.VideoCapture(str(clip))
    if capture.isOpened() and capture.read()[0]:
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return capture
    capture.release()
    copy = work / f"{clip.stem}.h264.mp4"
    if not copy.is_file():
        print(f"OpenCV can't decode {clip.name}: transcoding it to H.264 with ffmpeg", file=sys.stderr)
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-an", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(copy)], check=True)
    capture = cv2.VideoCapture(str(copy))
    if not capture.isOpened():
        raise SystemExit(f"can't open {clip}")
    return capture


def place(frames: list[Frame]) -> None:
    """Everyone's and the ball's pitch position, in every calibrated frame (their feet, the ball's centre)."""
    for f in frames:
        for p in f.persons:
            p.xy = None
        if f.ball:
            f.ball.xy = None
        if not f.calib:
            continue
        if f.persons:
            for p, (x, y) in zip(f.persons, f.calib.to_pitch(np.array([p.foot for p in f.persons]))):
                p.xy = (float(x), float(y))
        if f.ball:
            bx, by = f.calib.to_pitch(np.array([f.ball.centre]))[0]
            f.ball.xy = (float(bx), float(by))


def detect_frames(args: argparse.Namespace, clip: Path, out: Path, pitch: Pitch):
    """Stage 1 and 2: (frames, their JPEGs, analysed fps, seconds it took, calibration failures by gate)."""
    from .detect import Models

    capture = open_clip(clip, out)
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(source_fps / args.fps))
    fps = source_fps / step
    limit = int(args.max_seconds * source_fps) if args.max_seconds else None
    models = Models(args.models, imgsz=args.imgsz, device=args.device)
    # A fixed camera's calibration, for every frame (the keypoint model isn't run)
    fixed = calib.fixed(args.calibration, pitch) if args.calibration else None

    frames: list[Frame] = []
    images: list[bytes] = []
    shot, previous_hist, ball = -1, None, None
    camera = Motion()
    started = time.time()
    index = 0
    reasons: dict[str, int] = {}
    while True:
        ok = capture.grab()
        if not ok or (limit and index >= limit):
            break
        if index % step:
            index += 1
            continue
        ok, image = capture.retrieve()
        if not ok:
            break
        h, w = image.shape[:2]
        hist = scene.histogram(image)
        if scene.is_cut(previous_hist, hist):
            shot += 1
            ball = None
            camera.reset()
        previous_hist = hist
        colour = scene.pitch_colour(image)
        pitch_px = scene.pitch_mask(image, colour)
        lines_px = scene.line_mask(image, colour, pitch_px)
        persons, balls = models.detect(image)
        for p in persons:
            p.on_pitch = feet_on_pitch(pitch_px, p.foot, p.height)
            p.pixels = body_pixels(image, p.box, colour)
            p.feature = kit_feature(p.pixels)
        ball = pick_ball(balls, ball, w)
        f = Frame(index, index / source_fps, shot, (w, h), persons, ball)
        f.motion = camera.step(image, [p.box for p in persons])
        if fixed:
            f.calib, f.calib_reason = fixed, "ok"
        else:
            xy, conf = models.keypoints(image)
            if xy is not None:
                f.calib, f.calib_reason = calib.calibrate(xy, conf, pitch, pitch_px, lines_px)
            else:
                f.calib_reason = "points: no pitch found"
        reasons[f.calib_reason.split(":")[0]] = reasons.get(f.calib_reason.split(":")[0], 0) + 1
        frames.append(f)
        images.append(jpeg(image, max_width=1920, quality=88))
        if len(frames) % 10 == 0:
            print(f"  {f.t:6.1f} s analysed, {len(frames)} frames, {time.time() - started:.0f} s", file=sys.stderr, flush=True)
        index += 1
    capture.release()
    if not frames:
        raise SystemExit("no frames read")
    return frames, images, fps, time.time() - started, reasons


def analyse(args: argparse.Namespace) -> int:
    clip = Path(args.clip)
    out = Path(args.out) if args.out else clip.parent / "analysis" / clip.stem
    (out / "keyframes").mkdir(parents=True, exist_ok=True)
    pitch = Pitch(args.pitch_length, args.pitch_width)
    if args.reuse:
        frames, images, meta = cache.load(out)
        fps, elapsed, reasons = meta["fps"], meta["analysis_seconds"], meta["calibration_failures"]
        print(f"reusing {len(frames)} analysed frames from {out}", file=sys.stderr)
    else:
        frames, images, fps, elapsed, reasons = detect_frames(args, clip, out, pitch)
        if args.keep_frames:
            cache.save(out, frames, images, {"fps": fps, "analysis_seconds": elapsed, "calibration_failures": reasons})

    dropped = verify(frames)
    carried = propagate(frames)
    place(frames)
    track(frames)
    teams = Teams(args.home_kit, args.away_kit)
    teams.fit(frames)
    teams.assign(frames)
    events = Rules(pitch, args.home_attacks).run(frames)
    reader = None
    if args.numbers:
        lineup = numbers.load_lineup(args.lineup) if args.lineup else None
        dense = None
        if clip.is_file():
            from .detect import Models
            models = Models(args.models, imgsz=args.imgsz, device=args.device)
            def judge(image, boxes) -> list[str]:
                """Each box's team, from its pixels (shirt to socks) against the clip's kits."""
                pitch_lab = scene.pitch_colour(image)
                return [teams.judge(kit_feature(px), px)[0] for px in (body_pixels(image, box, pitch_lab) for box in boxes)]

            dense = numbers.Dense(open_clip(clip, out), lambda image: models.detect(image)[0], judge)
        reader = numbers.Numbers(numbers.Ollama(args.ollama_url, args.numbers_model), args.numbers_model,
                                 out / "numbers.json", lineup, dense)
        reader.label(events, frames, images)
        if dense:
            dense.close()
    if args.cards:
        cards = load_cards(args.cards, args.period, parse_clock(args.clock))
        events = sorted(events + Rules.identity(frames, events, cards), key=lambda e: e.frame)

    start = (datetime.fromisoformat(args.start_time) if args.start_time
             else datetime.now(timezone.utc).astimezone() - timedelta(seconds=frames[-1].t))
    match_id = args.match_id or f"VIDEO-{start:%Y%m%d}-{args.home}-{args.away}"
    clock_s = parse_clock(args.clock)
    docs, ids = [], alert_ids(clip.name, start, len(events))
    for event, alert_id in zip(events, ids):
        doc = alert_doc(event, alert_id, frames, site=args.site_id, match_id=match_id, camera=args.camera, start=start,
                        period=args.period, clock_s=clock_s, pitch=pitch, home_attacks=args.home_attacks, fps=fps,
                        clip_uri=f"file://{clip.name}")
        image = cv2.imdecode(np.frombuffer(images[event.frame], np.uint8), cv2.IMREAD_COLOR)
        keyframe = jpeg(draw(image, frames[event.frame], teams, pitch, event, label=alert_id, keyframe=True))
        (out / "keyframes" / f"{alert_id}.jpg").write_bytes(keyframe)
        docs.append((doc, keyframe, event.why))

    calibrated = sum(1 for f in frames if f.calib)
    summary = {
        "clip": clip.name, "frames": len(frames), "fps": round(fps, 2), "seconds": round(frames[-1].t, 1),
        "shots": frames[-1].shot + 1, "analysis_seconds": round(elapsed, 1),
        "frames_per_second": round(len(frames) / elapsed, 2) if elapsed else None,
        "calibrated_frames": calibrated, "carried_calibrations": carried, "inconsistent_calibrations": dropped,
        "calibration_failures": reasons,
        "teams": teams.note, "kits": {t: list(map(int, teams.kit_bgr(t)[::-1])) for t in teams.kits},
        "people_per_frame": round(sum(len(f.persons) for f in frames) / len(frames), 1),
        "players_per_frame": round(sum(sum(p.playing for p in f.persons) for f in frames) / len(frames), 1),
        "ball_frames": sum(1 for f in frames if f.ball),
        "shirt_numbers": reader.found if reader else None,
        "number_reads": reader.asked if reader else None,
        "alerts": [{"alert_id": d["alert_id"], "type": d["type"], "t": round(frames[e.frame].t, 1), "why": why,
                    "confidence": d["confidence"], "snapshot": "snapshot" in d}
                   for (d, _, why), e in zip(docs, events)],
    }
    (out / "alerts.json").write_text(json.dumps([d for d, _, _ in docs], indent=1, ensure_ascii=False))
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False))

    if args.video:
        write_video(out / "annotated.mp4", frames, images, teams, pitch, fps, {e.frame: e for e in events})

    if args.post:
        post(args, docs, match_id, start, frames, summary)
    return 0


def write_video(path: Path, frames, images, teams, pitch, fps, events) -> None:
    """The analysed frames with everyone's team, the ball, the calibration and each alert's moment, as H.264."""
    raw = path.with_suffix(".raw.mp4")
    first = cv2.imdecode(np.frombuffer(images[0], np.uint8), cv2.IMREAD_COLOR)
    h, w = first.shape[:2]
    writer = cv2.VideoWriter(str(raw), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    held = None
    for i, (f, data) in enumerate(zip(frames, images)):
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        event = events.get(i)
        held = (event, int(fps * 2)) if event else (held if held and held[1] > 0 else None)
        shown = held[0] if held else None
        writer.write(draw(image, f, teams, pitch, shown, label=f"ALERTA: {TYPE_NAMES[shown.type]}" if shown else ""))
        if held:
            held = (held[0], held[1] - 1)
    writer.release()
    try:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)], check=True)
        raw.unlink()
    except (OSError, subprocess.CalledProcessError):
        raw.rename(path)
    print(f"wrote {path}", file=sys.stderr)


def post(args, docs, match_id: str, start: datetime, frames, summary) -> None:
    ingest = Ingest(args.ingest_url, args.site_token)
    if args.home and args.away and not args.match_id:
        fixture = {
            "schema": "p4n4.match.fixture/0.1", "site_id": args.site_id, "match_id": match_id,
            "competition": args.competition, "home": {"code": args.home, "name": args.home_name or args.home},
            "away": {"code": args.away, "name": args.away_name or args.away}, "kickoff": start.isoformat(timespec="seconds"),
            "status": "live", "period": args.period, "score": {"home": 0, "away": 0}, "synthetic": False,
        }
        print("fixture:", ingest.post("/api/v1/fixtures", fixture))
    if docs:
        result = ingest.post("/api/v1/alerts", [d for d, _, _ in docs])
        print("alerts:", {k: result[k] for k in ("accepted", "created", "updated", "unchanged")}, result["rejected"] or "")
        for doc, keyframe, _ in docs:
            ingest.put_jpeg(doc["alert_id"], keyframe)
        print(f"keyframes: {len(docs)} uploaded")
    ingest.post("/api/v1/heartbeat", {
        "schema": "p4n4.match.heartbeat/0.1", "site_id": args.site_id, "timestamp": datetime.now(timezone.utc).isoformat(),
        "device": {"hostname": socket.gethostname()[:64] or "vision", "model": "matchvision on CPU"},
        "metrics": {"analysis_seconds": summary["analysis_seconds"]},
        "pipeline": {"processing_fps": summary["frames_per_second"] or 0, "calibrated_frames": summary["calibrated_frames"],
                     "frames": summary["frames"], "alerts": len(docs)},
        "status": "healthy",
    })
    if args.full_time and args.home and args.away and not args.match_id:
        fixture["status"], fixture["period"] = "full_time", max(2, args.period)
        print("full time:", ingest.post("/api/v1/fixtures", fixture))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matchvision.analyze", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clip")
    parser.add_argument("--fps", type=float, default=2.0, help="frames analysed per second of video (default 2)")
    parser.add_argument("--max-seconds", type=float, help="analyse only the start of the clip")
    parser.add_argument("--models", default=os.environ.get("MODELS_DIR", "models"))
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--device", default=os.environ.get("DEVICE", "cpu"), help="cpu, or 0 for the first GPU")
    parser.add_argument("--pitch-length", type=float, default=float(os.environ.get("PITCH_LENGTH", 105)))
    parser.add_argument("--pitch-width", type=float, default=float(os.environ.get("PITCH_WIDTH", 68)))
    parser.add_argument("--camera", default="principal", help="the camera id alerts name (default principal)")
    parser.add_argument("--calibration", help="a fixed camera's calibration file (see matchvision.check); "
                        "without it, the pitch-keypoint model calibrates each frame")
    parser.add_argument("--home-kit", help="the home shirt's colour: a name (red, green, …) or #rrggbb")
    parser.add_argument("--away-kit")
    parser.add_argument("--numbers", action="store_true",
                        help="read the shirt numbers of the players the alerts name, with the stack's vision model "
                             "(a few seconds a view on CPU, up to 4 views a player)")
    parser.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://ollama:11434"))
    parser.add_argument("--numbers-model", default=os.environ.get("NUMBERS_MODEL", "gemma4:e2b"))
    parser.add_argument("--lineup", help='the match sheet, {"home": [1, 4, ...], "away": [...]}: other numbers are dropped')
    parser.add_argument("--cards", help="the cards in the referee's report (JSON, see load_cards), checked against "
                        "the offender of the contact before each: an identity alert when the numbers differ "
                        "(implies --numbers)")
    parser.add_argument("--home-attacks", choices=("right", "left"), default="right",
                        help="which way the home team attacks in this clip, as the camera sees it (default right)")
    parser.add_argument("--period", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--clock", default="0:00", help="the match clock in the period at the clip's start, mm:ss")
    parser.add_argument("--start-time", help="when the clip's first frame was filmed, ISO 8601 (default: now minus its length)")
    parser.add_argument("--match-id", help="a fixture the ingest service already has")
    parser.add_argument("--home", help="home club code, to create the fixture (e.g. TAU)")
    parser.add_argument("--away", help="away club code (e.g. PLA)")
    parser.add_argument("--home-name")
    parser.add_argument("--away-name")
    parser.add_argument("--competition", default="Video analysis")
    parser.add_argument("--full-time", action="store_true", help="after posting, end the created fixture (sends the match report)")
    parser.add_argument("--out", help="output directory (default: <clip dir>/analysis/<clip name>)")
    parser.add_argument("--video", action="store_true", help="also write annotated.mp4")
    parser.add_argument("--keep-frames", action="store_true",
                        help="save the detection stage (frames.json, frames/) so --reuse can run the rules again without the models")
    parser.add_argument("--reuse", action="store_true", help="run the teams and rules again on the saved detection stage in --out")
    parser.add_argument("--post", action="store_true", help="post the fixture, alerts, keyframes and a heartbeat")
    parser.add_argument("--ingest-url", default=os.environ.get("INGEST_URL", "http://ingest:8080"))
    parser.add_argument("--site-id", default=os.environ.get("SITE_ID", "brighton"))
    parser.add_argument("--site-token", default=os.environ.get("SITE_TOKEN", ""))
    args = parser.parse_args(argv)
    if not args.match_id and not (args.home and args.away):
        parser.error("pass --match-id, or --home and --away to create the fixture")
    if args.cards:
        args.numbers = True
    if args.post:
        parser.error("this project posts what a clip's analysis found as the club's plays: "
                     "python3 extras/scripts/clips.py post (after clips.py analyze)")
    return analyse(args)


if __name__ == "__main__":
    sys.exit(main())
