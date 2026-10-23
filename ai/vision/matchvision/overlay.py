"""
The Video tab's vision overlay for the archive footage: the players the
models found in each analysed clip, by team, and the ball, drawn over the
loops extras/scripts/clips.py cuts for the cameras, in step with them.

    python -m matchvision.overlay --manifest /config/clips.json --analysis /clips/analysis --out /clips/vision.json

No model runs. It reads each source's saved detection stage
(analysis/<source>/frames.json, from analyze.py --keep-frames) and runs the
same steps as analyze.py --reuse up to the teams, with the kits the analysis
found (summary.json), so the tracks are the ones its alerts name. When the
source also has dense detections (analysis/<source>/dense.json, from
matchvision.dense, e.g. 10 a second), those are drawn instead: tracked with a
tracker that keeps a lost player for a second, teams from the same kits,
short gaps filled, tracks seen fewer than 3 times dropped (they only blink)
and each box averaged with its neighbours (the detector's jitter), and each
alert's players found among them by box overlap at the alert's moment. For
each camera in the manifest it keeps the frames inside the camera's ranges,
on the loop's clock, and writes a p4n4-dashboard vision file (the
dashboard's README, "Vision overlay"):

  - boxes: everyone on the pitch with a track; a player's kind is their team
    (one kind per club, named from the manifest's match; a source whose
    "objects" names an earlier source uses that one's kinds), the referee,
    goalkeepers and staff have none (the dashboard's "person" colour). Track
    ids are renumbered from 1 per camera. From the analysis's 2 frames a
    second, a midpoint between two frames of the same shot is interpolated
    for each track seen in both (a track seen only in the first is held), so
    the boxes glide rather than blink.
    Nobody is boxed in a close-up (the bench, the stands): a frame where most
    people are taller than the rules' CLOSE_UP share of it.
  - objects: the ball, a small box around its centre.
  - behaviors: each club play the video rules found (a contact and a fall,
    a foul; a goal), from 1.5 s before its moment to 2.5 s after, on the
    offender's (scorer's) track, with an overlay rule per play type, so the
    camera flags it. A camera whose manifest entry lists "moments" gets
    those instead: the plays posted for it (the club's play types),
    {"t", "until", "type", "confidence", "feed", "ball", "alert"} in the
    source's seconds, each from its moment to "until" on no track, naming the
    feed item it's about ("feed", the dashboard shows the item as it plays),
    and the tracks the dashboard zooms on: the alert's players (the source's
    alert "alert" names, else the feed item's id), else the
    nearest three to the ball at that moment, with the ball. A camera may
    give its "ball" too ([seconds of the source, x, y], 0-1 of the frame),
    where the ball really is (e.g. a dataset's track), drawn in place of
    the detector's.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from pathlib import Path

from . import cache, dense
from .analyze import place
from .events import CLOSE_UP
from .motion import propagate, verify
from .state import Ball, Frame, Person
from .teams import Teams
from .track import Tracker, iou, track

# The alert types the video finds (identity comes from the referee's report), and their labels
BEHAVIORS = {
    "foul_contact": ({"es": "Contacto / falta", "en": "Contact / foul"}, "R12", "Regla 12", "Law 12"),
    "goal": ({"es": "Gol", "en": "Goal"}, "R10", "Regla 10", "Law 10"),
    "offside_candidate": ({"es": "Posible fuera de juego", "en": "Possible offside"}, "R11", "Regla 11", "Law 11"),
    "ball_out": ({"es": "Balón fuera", "en": "Ball out of play"}, "R9", "Regla 9", "Law 9"),
}
# What a camera's moments can be: the club's plays the clips show (the ingest service's types), each with its
# label, the overlay rule's code and its tone (a risk to look at, "review", or an opportunity, "info")
MOMENT_TYPES = {
    "goal": ({"en": "Goal", "es": "Gol"}, "G", "info"),
    "shot": ({"en": "Shot", "es": "Remate"}, "S", "info"),
    "save": ({"en": "Save", "es": "Atajada"}, "SV", "info"),
    "set_piece": ({"en": "Set piece", "es": "Balón parado"}, "SP", "info"),
    "build_up": ({"en": "Build-up", "es": "Salida"}, "B", "info"),
    "high_regain": ({"en": "High regain", "es": "Recuperación alta"}, "R", "info"),
    "counter_attack": ({"en": "Counter-attack", "es": "Contraataque"}, "C", "info"),
    "foul": ({"en": "Foul", "es": "Falta"}, "F", "review"),
}
# The video rules' alerts that are club plays (a contact and a fall is a foul), for a camera without moments
AS_PLAY = {"foul_contact": "foul", "goal": "goal"}
# Two moments starting this close on one camera: the later one moves this much later, so each is its own occurrence
# (the dashboard reads its clips' clocks every 0.25 s)
SAME_MOMENT, STAGGER = 0.3, 0.3
# A moment without named players is about who's near the ball: those this close to it (a share of the frame's
# width), the nearest three, in the frame with the ball nearest the moment, this many seconds away at most
NEAR_BALL, NEAR_COUNT, BALL_WINDOW = 0.12, 3, 0.5
# A ball track's point (the camera's "ball") counts for a frame this many seconds away at most
BALL_TRACK_GAP = 0.1
# Who an alert is about, in order of preference
LEAD_ROLES = ("offender", "attacker", "scorer", "last_touch")
BEFORE, AFTER = 1.5, 2.5
# Interpolate a midpoint only across a gap this short (the analysis samples 2 frames a second)
MAX_GAP = 0.6
BALL_HALF = 9  # pixels, at the source's size
# Dense detections: a lost player is kept this long by the tracker, and filled in over a gap this long
KEEP_S, FILL_S = 1.0, 0.5
# ... a track seen fewer times than this only blinks; a ball gap up to this many frames is filled
MIN_SIGHTINGS, BALL_FILL = 3, 3
# ... an alert's player is the dense box overlapping theirs most, by at least this much
MIN_MATCH = 0.3


def hex_rgb(rgb: list[int]) -> str:
    return "#" + "".join(f"{int(v):02x}" for v in rgb)


def analysed(folder: Path):
    """The source's frames with tracks and teams, as its analysis had them."""
    return analysed_teams(folder)[0]


def analysed_teams(folder: Path):
    """[analysed]'s frames, and the teams their kits were told apart by."""
    frames, _, _ = cache.load(folder)
    summary = json.loads((folder / "summary.json").read_text())
    verify(frames)
    propagate(frames)
    place(frames)
    track(frames)
    kits = summary["kits"]
    teams = Teams(hex_rgb(kits["home"]), hex_rgb(kits["away"]))
    teams.fit(frames)
    teams.assign(frames)
    return frames, teams


def lerp_box(a, b, k: float) -> tuple[float, ...]:
    return tuple(x + (y - x) * k for x, y in zip(a, b))


def smooth(frames: list[Frame], fps: float) -> None:
    """
    Dense tracks, cleaned for drawing (in place): tracks seen fewer than
    MIN_SIGHTINGS times dropped, gaps up to FILL_S filled along the line
    between the sightings, each box averaged with the one before and after,
    and the ball's short gaps filled. Never across a shot.
    """
    fill = max(1, round(FILL_S * fps))
    seen: dict[tuple[int, int], list[int]] = {}
    for i, f in enumerate(frames):
        for p in f.persons:
            if p.track >= 0:
                seen.setdefault((f.shot, p.track), []).append(i)
    blinks = {key for key, at in seen.items() if len(at) < MIN_SIGHTINGS}
    for f in frames:
        f.persons = [p for p in f.persons if p.track < 0 or (f.shot, p.track) not in blinks]
    for key, at in seen.items():
        if key in blinks:
            continue
        for i, j in zip(at, at[1:]):
            if 1 < j - i <= fill + 1:
                a, b = frames[i].by_track(key[1]), frames[j].by_track(key[1])
                for k in range(i + 1, j):
                    frames[k].persons.append(Person(lerp_box(a.box, b.box, (k - i) / (j - i)), min(a.conf, b.conf),
                                                    track=a.track, on_pitch=a.on_pitch, team=a.team))
    averaged = []
    for i, f in enumerate(frames):
        for p in f.persons:
            near = [q.box for g in (frames[i - 1] if i else None, frames[i + 1] if i + 1 < len(frames) else None)
                    if g is not None and g.shot == f.shot for q in [g.by_track(p.track)] if q is not None]
            if near and p.track >= 0:
                averaged.append((p, tuple(sum(v) / (len(near) + 1) for v in zip(p.box, *near))))
    for p, box in averaged:
        p.box = box
    sightings = [i for i, f in enumerate(frames) if f.ball]
    for i, j in zip(sightings, sightings[1:]):
        if 1 < j - i <= BALL_FILL + 1 and frames[i].shot == frames[j].shot:
            a, b = frames[i].ball, frames[j].ball
            for k in range(i + 1, j):
                frames[k].ball = Ball(lerp_box(a.centre, b.centre, (k - i) / (j - i)), min(a.conf, b.conf))


def dense_frames(folder: Path) -> list[Frame]:
    """The source's dense detections, tracked, by team (the analysis's kits) and cleaned for drawing."""
    frames, meta = dense.load(folder / "dense.json")
    fps = float(meta["fps"])
    tracker, shot = Tracker(max_step=0.4, max_age=max(2, round(KEEP_S * fps))), None
    for f in frames:
        if f.shot != shot:
            tracker.reset()
            shot = f.shot
        tracker.update(f.persons)
    kits = json.loads((folder / "summary.json").read_text())["kits"]
    teams = Teams(hex_rgb(kits["home"]), hex_rgb(kits["away"]))
    teams.fit(frames)
    teams.assign(frames)
    smooth(frames, fps)
    return frames


def remap(alerts: list[dict], sparse: list[Frame], frames: list[Frame]) -> list[dict]:
    """The alerts with their players' track ids in [frames]: the box overlapping theirs most at the alert's moment."""
    def nearest(fs: list[Frame], t: float) -> Frame | None:
        return min(fs, key=lambda f: abs(f.t - t), default=None)

    out = []
    for alert in alerts:
        t = float(alert["match_time_s"])
        then, now = nearest(sparse, t), nearest(frames, t)
        players = []
        for p in alert["players"]:
            q = then.by_track(p["track_id"]) if then and p.get("track_id") is not None else None
            best = max(now.persons, key=lambda r: iou(q.box, r.box), default=None) if q and now and abs(now.t - t) < 0.3 else None
            match = best.track if best is not None and iou(q.box, best.box) >= MIN_MATCH else None
            players.append({**p, "track_id": match})
        out.append({**alert, "players": players})
    return out


def norm(box, size) -> list[float]:
    w, h = size
    x1, y1, x2, y2 = box
    return [round(max(0.0, x1 / w), 4), round(max(0.0, y1 / h), 4), round(min(1.0, x2 / w), 4), round(min(1.0, y2 / h), 4)]


def lerp(a: list[float], b: list[float], k: float) -> list[float]:
    return [round(x + (y - x) * k, 4) for x, y in zip(a, b)]


def camera_recording(frames, ranges, kinds: dict[str, int], ball_kind: int, alerts: list[dict],
                     midpoints: bool = True, moments: list[dict] | None = None,
                     ball_track: list[list[float]] | None = None) -> tuple[dict, dict]:
    """One camera's recording, and how its tracks were renumbered. [midpoints]: interpolate between frames.
    [moments]: labelled plays, in place of the alerts' behaviours; each names the tracks it's about, for the
    dashboard's zoom: its alert's players (by its feed item's alert id), else who's near the ball, and the ball.
    [ball_track]: where the ball really is ([seconds of the source, x, y], 0-1 of the frame), drawn in place of
    the detector's."""
    ids: dict[int, int] = {}
    by_id = {a["alert_id"]: a for a in alerts if "alert_id" in a}

    def people(f) -> dict[int, list]:
        out = {}
        # A close-up (the bench, the stands, an interview): most people taller than the rules' CLOSE_UP. Nobody boxed
        heights = sorted(p.height for p in f.persons)
        if heights and heights[len(heights) // 2] >= CLOSE_UP * f.size[1]:
            return out
        for p in f.persons:
            if not p.on_pitch or p.track < 0:
                continue
            tid = ids.setdefault(p.track, len(ids) + 1)
            box = [tid, *norm(p.box, f.size), round(p.conf, 2)]
            if p.team in kinds:
                box.append(kinds[p.team])
            out[tid] = box
        return out

    track_t = [p[0] for p in ball_track or []]

    def tracked(t: float) -> list[float] | None:
        """The ball track's point nearest second [t] of the source, if one is close enough."""
        i = bisect.bisect_left(track_t, t)
        near = [p for p in ball_track[max(0, i - 1):i + 1] if abs(p[0] - t) <= BALL_TRACK_GAP]
        return min(near, key=lambda p: abs(p[0] - t), default=None)

    def ball(f) -> list:
        if ball_track is not None:
            point = tracked(f.t)
            if point is None:
                return []
            (cx, cy), r = (point[1] * f.size[0], point[2] * f.size[1]), BALL_HALF
            return [[ball_kind, *norm((cx - r, cy - r, cx + r, cy + r), f.size), 1.0]]
        if not f.ball:
            return []
        (cx, cy), r = f.ball.centre, BALL_HALF
        return [[ball_kind, *norm((cx - r, cy - r, cx + r, cy + r), f.size), round(f.ball.conf, 2)]]

    boxes, objects, behaviors = [], [], []
    loop = 0.0
    for a, b in ranges:
        part = [f for f in frames if a <= f.t < b]
        for i, f in enumerate(part):
            t = round(loop + f.t - a, 3)
            now, seen = people(f), ball(f)
            boxes.append([t, list(now.values())])
            objects.append([t, seen])
            nxt = part[i + 1] if i + 1 < len(part) else None
            if not midpoints or nxt is None or nxt.shot != f.shot or nxt.t - f.t > MAX_GAP:
                continue
            # The midpoint: each track walks towards its next box; one the next frame lost is held
            ahead = people(nxt)
            mid = []
            for tid, box in now.items():
                to = ahead.get(tid)
                mid.append(box if to is None else [tid, *lerp(box[1:5], to[1:5], 0.5), *box[5:]])
            tm = round(t + (nxt.t - f.t) / 2, 3)
            boxes.append([tm, mid])
            later = ball(nxt)
            objects.append([tm, [[ball_kind, *lerp(seen[0][1:5], later[0][1:5], 0.5), seen[0][5]]] if seen and later else []])
        def focus(at: float, feed: str | None, hint: list[float] | None = None,
                  near: list[float] | None = None, alert_id: str | None = None) -> tuple[int, list[int], list[int]]:
            """(the lead's track, or -1; every track the moment is about; the object kinds it's about, the ball's
            when it's who's near the ball), at second [at] of the source. [hint]: where the ball is then (0-1 of
            the frame), when the moment knows, else the ball track's, else where the detector saw it. [near]: where
            its players are (0-1 of the frame), when the moment knows but they aren't named here: who's there.
            [alert_id]: the analysis's alert the moment is (its feed item may have an id of its own): its players."""
            alert = by_id.get(alert_id or (feed or "").rpartition(":")[2])
            if alert:
                players = [p for p in alert["players"] if p.get("track_id") in ids]
                lead = next((p for role in LEAD_ROLES for p in players if p.get("role") == role), None)
                tracks = list(dict.fromkeys(ids[p["track_id"]] for p in ([lead] if lead else []) + players))
                if tracks:
                    return (ids[lead["track_id"]] if lead else -1), tracks, []
            if near:
                f = min(part, key=lambda f: abs(f.t - at), default=None)
                if f is not None and abs(f.t - at) <= BALL_WINDOW:
                    nx, ny = near[0] * f.size[0], near[1] * f.size[1]

                    def off(p) -> float:
                        x1, y1, x2, y2 = p.box
                        return math.hypot((x1 + x2) / 2 - nx, (y1 + y2) / 2 - ny)

                    close = sorted((off(p), ids[p.track]) for p in f.persons
                                   if p.on_pitch and p.track in ids and off(p) <= NEAR_BALL * f.size[0])
                    if close:
                        return -1, [tid for _, tid in close[:2]], []
            if hint is None and ball_track is not None and (point := tracked(at)):
                hint = point[1:]
            # Where the moment says the ball is, then where the detector saw it: the first with anyone near
            for told in ([hint] if hint else []) + [None]:
                near = sorted((f for f in part if (told or f.ball) and abs(f.t - at) <= BALL_WINDOW),
                              key=lambda f: abs(f.t - at))
                if not near:
                    continue
                f = near[0]
                bx, by = (told[0] * f.size[0], told[1] * f.size[1]) if told else f.ball.centre

                def gap(p) -> float:
                    x1, y1, x2, y2 = p.box
                    return math.hypot(max(x1 - bx, 0, bx - x2), max(y1 - by, 0, by - y2))

                close = sorted((gap(p), ids[p.track]) for p in f.persons
                               if p.on_pitch and p.track in ids and gap(p) <= NEAR_BALL * f.size[0])
                if close:
                    return -1, [tid for _, tid in close[:NEAR_COUNT]], [ball_kind]
            # Nobody boxed near it: the ball alone, where the moment knows it is (the picture shows who's there)
            return -1, [], [ball_kind] if hint else []

        last = None
        for m in sorted(moments or [], key=lambda m: float(m["t"])):
            at = float(m["t"])
            if m["type"] not in MOMENT_TYPES or not a <= at < b:
                continue
            if last is not None and at - last < SAME_MOMENT:
                at = min(last + STAGGER, b - 0.1)
            last = at
            until = min(max(float(m.get("until", at + AFTER)), at + 0.5), b)
            lead, tracks, things = focus(float(m["t"]), m.get("feed"), m.get("ball"), m.get("near"), m.get("alert"))
            extra = [m.get("feed"), tracks, *([things] if things else [])] if tracks or things else (
                [m["feed"]] if m.get("feed") else [])
            behaviors.append([round(loop + at - a, 2), round(loop + until - a, 2), lead, m["type"],
                              round(float(m.get("confidence", 1.0)), 2), *extra])
        for alert in alerts if moments is None else []:
            at = float(alert["match_time_s"])
            kind = AS_PLAY.get(alert["type"])
            if kind is None or not a <= at < b:
                continue
            lead = next((p for role in LEAD_ROLES for p in alert["players"]
                         if p.get("role") == role and p.get("track_id") in ids), None)
            t = loop + at - a
            behaviors.append([round(max(loop, t - BEFORE), 2), round(min(loop + b - a, t + AFTER), 2),
                              ids[lead["track_id"]] if lead else -1, kind, round(alert["confidence"], 2)])
        loop += b - a
    behaviors.sort(key=lambda b: (b[0], b[1]))
    first = frames[0].size if frames else (1280, 720)
    scale = 720 / first[1]
    return {
        "duration": round(loop, 3), "width": round(first[0] * scale), "height": 720,
        "boxes": boxes, "objects": objects, "behaviors": behaviors,
    }, ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="matchvision.overlay", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, help="extras/config/clips.json")
    parser.add_argument("--analysis", required=True, help="the analyses' folder (extras/clips/analysis)")
    parser.add_argument("--out", required=True, help="the vision file to write")
    args = parser.parse_args(argv)

    manifest = json.loads(Path(args.manifest).read_text())
    objects, kinds = [], {}
    for name, source in manifest["sources"].items():
        match = source.get("match") or {}
        for side in ("home", "away"):
            # Two sources of one match (say a panorama and the loops cut from it) can share their teams' kinds
            if (source.get("objects"), side) in kinds:
                kinds[(name, side)] = kinds[(source["objects"], side)]
                continue
            team = match.get(side) or {"name": f"{name} {side}"}
            kinds[(name, side)] = len(objects)
            objects.append({"id": f"{name}-{side}", "label": {"es": team["name"], "en": team["name"]}})
    ball_kind = len(objects)
    objects.append({"id": "ball", "label": {"es": "Balón", "en": "Ball"}})

    cameras, used = {}, set()
    cache_by_source = {}
    for camera, spec in manifest["cameras"].items():
        name = spec["source"]
        folder = Path(args.analysis) / name
        if not (folder / "frames.json").is_file():
            print(f"{camera}: no detection stage in {folder}, skipped")
            continue
        if name not in cache_by_source:
            alerts = json.loads((folder / "alerts.json").read_text()) if (folder / "alerts.json").is_file() else []
            sparse = analysed(folder)
            if (folder / "dense.json").is_file():
                frames = dense_frames(folder)
                cache_by_source[name] = (frames, remap(alerts, sparse, frames), False)
                print(f"{name}: dense detections, {len(frames)} frames")
            else:
                cache_by_source[name] = (sparse, alerts, True)
        frames, alerts, midpoints = cache_by_source[name]
        recording, ids = camera_recording(frames, spec["ranges"], {side: kinds[(name, side)] for side in ("home", "away")},
                                          ball_kind, alerts, midpoints, spec.get("moments"), spec.get("ball"))
        cameras[camera] = {"recording": recording}
        used |= {b[3] for b in recording["behaviors"]}
        missing = [a["alert_id"] for a in alerts if "moments" not in spec and a["type"] in BEHAVIORS
                   and any(x <= a["match_time_s"] < y for x, y in spec["ranges"])
                   and not any(p.get("track_id") in ids for p in a["players"])]
        people = sum(len(s[1]) for s in recording["boxes"]) / max(1, len(recording["boxes"]))
        print(f"{camera}: {recording['duration']:.1f} s, {len(recording['boxes'])} samples, {people:.1f} people a sample, "
              f"{len(ids)} tracks, {len(recording['behaviors'])} alert moments"
              + (f"; alerts whose players weren't found: {', '.join(missing)}" if missing else ""))

    bundle = {
        "version": 1,
        "behaviors": {k: MOMENT_TYPES[k][0] for k in sorted(used)},
        "alerts": [
            {"code": MOMENT_TYPES[k][1], "tone": MOMENT_TYPES[k][2], "behaviors": [k],
             "title": MOMENT_TYPES[k][0],
             "suggestion": {"en": "Open it in the analysis room: the 2D replay and what the device measured.",
                            "es": "Ábrela en la sala de análisis: la repetición en 2D y lo que midió el dispositivo."}}
            for k in sorted(used)
        ],
        "objects": objects,
        "cameras": cameras,
    }
    Path(args.out).write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {args.out}: {Path(args.out).stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
