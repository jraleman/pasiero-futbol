"""
The models, and the only module that imports them (Ultralytics, on CPU by
default). Everything else works on what they return.

  people  a general COCO detector (persons and sports balls): it finds
          players from any camera angle, where the football-specific
          detectors on the Hub, trained on high broadcast views, miss most
          of them in a low side-on shot. Spectators come along too;
          teams.py and the pitch mask leave them out.
  ball    a football-specific ball detector, added to the COCO balls
  pitch   the 32 pitch keypoints (calib.py decides whether to believe them)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .state import Person

PERSON, SPORTS_BALL = 0, 32


class Models:
    def __init__(self, models_dir: str | Path, people: str = "yolo11m.pt", ball: str = "ball.pt",
                 pitch: str = "pitch.pt", imgsz: int = 1280, device: str = "cpu") -> None:
        from ultralytics import YOLO

        models_dir = Path(models_dir)
        for name in (people, ball, pitch):
            if not (models_dir / name).is_file():
                raise SystemExit(f"model {models_dir / name} missing: run fetch_models.py first")
        self.people = YOLO(str(models_dir / people))
        self.ball = YOLO(str(models_dir / ball))
        self.pitch = YOLO(str(models_dir / pitch))
        self.imgsz, self.device = imgsz, device

    def detect(self, frame: np.ndarray, person_conf: float = 0.3, ball_conf: float = 0.2):
        """(persons, balls): balls are (centre, conf), from both detectors, best first."""
        r = self.people(frame, imgsz=self.imgsz, conf=min(person_conf, ball_conf), classes=[PERSON, SPORTS_BALL],
                        device=self.device, verbose=False)[0]
        persons, balls = [], []
        for box, cls, conf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist()):
            if int(cls) == PERSON and conf >= person_conf:
                persons.append(Person(tuple(box), round(conf, 3)))
            elif int(cls) == SPORTS_BALL:
                balls.append((((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), round(conf, 3)))
        rb = self.ball(frame, imgsz=self.imgsz, conf=ball_conf, device=self.device, verbose=False)[0]
        for box, conf in zip(rb.boxes.xyxy.tolist(), rb.boxes.conf.tolist()):
            balls.append((((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), round(conf, 3)))
        return persons, sorted(balls, key=lambda b: -b[1])

    def keypoints(self, frame: np.ndarray):
        """(xy (32, 2), conf (32,)), or (None, None) when the model finds no pitch."""
        r = self.pitch(frame, imgsz=640, conf=0.25, device=self.device, verbose=False)[0]
        kp = r.keypoints
        if kp is None or kp.conf is None or not len(kp.conf):
            return None, None
        return kp.xy[0].cpu().numpy(), kp.conf[0].cpu().numpy()
