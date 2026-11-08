"""Card detector: pretrained 52-class YOLO on the camera frame + our Queen second opinion.

Chosen by bake-off on our own footage (see tools/bakeoff.py): koolguy06/playing-cards (YOLOv8l, MIT),
640 px input beat 1280 px. Detections are returned in IMAGE pixels; zones.py maps them to seats.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vision.cards import norm_label
from vision.queen_fix import QueenFix, crop_card, load_or_none

DEFAULT_WEIGHTS = "models/koolguy06/playing-cards.pt"


@dataclass
class RawDet:
    center: tuple[float, float]            # image px
    box: tuple[float, float, float, float]
    code: str                              # 'As'
    conf: float
    yolo_code: str                         # before the Queen fix
    fixed: bool = False


class CardDetector:
    def __init__(self, weights: str = DEFAULT_WEIGHTS, imgsz: int = 640, conf: float = 0.35,
                 queen_fix: bool = True, device: str = "cpu"):
        from ultralytics import YOLO
        self.model = YOLO(weights)
        self.imgsz, self.conf, self.device = imgsz, conf, device
        self.qfix: QueenFix | None = load_or_none() if queen_fix else None

    def detect(self, frame: np.ndarray) -> list[RawDet]:
        r = self.model.predict(frame, imgsz=self.imgsz, conf=self.conf, verbose=False,
                               device=self.device, agnostic_nms=True)[0]
        out: list[RawDet] = []
        for b in r.boxes:
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0].tolist())
            yolo = norm_label(self.model.names[int(b.cls)])
            code = yolo
            if self.qfix is not None:
                code = self.qfix.correct(yolo, crop_card(frame, (x1, y1, x2, y2)))
            out.append(RawDet(((x1 + x2) / 2, (y1 + y2) / 2), (x1, y1, x2, y2), code, float(b.conf), yolo, code != yolo))
        return out
