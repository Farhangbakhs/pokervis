"""Card detector: pretrained 52-class YOLO on the camera frame + our Queen second opinion.

Chosen by bake-off on our own footage (see tools/bakeoff.py): koolguy06/playing-cards (YOLOv8l, MIT),
640 px input beat 1280 px. Detections are returned in IMAGE pixels; zones.py maps them to seats.
"""
from __future__ import annotations

from collections import Counter
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


def same_card(img: np.ndarray, a: tuple[float, float], b: tuple[float, float], n: int = 80, max_gap: float = 0.08) -> bool:
    """Do two box centres lie on one physical card? The straight line between them stays on the card's surface
    (white, pips); between two cards it crosses bare table. Measured on real frames: one card <= 0.01 of the line is
    table, different cards >= 0.14."""
    xs = np.linspace(a[0], b[0], n).astype(int).clip(0, img.shape[1] - 1)
    ys = np.linspace(a[1], b[1], n).astype(int).clip(0, img.shape[0] - 1)
    px = img[ys, xs].astype(float)
    gray, spread = px.mean(1), px.max(1) - px.min(1)
    white = np.percentile(gray, 90)
    table = (gray < white * 0.90) & (gray > white * 0.55) & (spread < 30)   # not card white, not black/red ink
    longest = run = 0
    for is_table in table:
        run = run + 1 if is_table else 0
        longest = max(longest, run)
    return longest / n <= max_gap


def one_read_per_card(img: np.ndarray, dets: list[RawDet]) -> list[RawDet]:
    """A card has two corner indices. If they disagree, the detector reports two different cards for one physical
    card (an upside-down A read as a 4). Drop a code seen only once when its box is on the same card as a stronger read."""
    boxes = Counter(d.code for d in dets)
    return [d for d in dets if not (boxes[d.code] == 1 and any(
        o.code != d.code and o.conf > d.conf and same_card(img, d.center, o.center) for o in dets))]


class CardDetector:
    def __init__(self, weights: str = DEFAULT_WEIGHTS, imgsz: int = 640, conf: float = 0.35,
                 queen_fix: bool = True, device: str = "cpu"):
        from ultralytics import YOLO
        self.model = YOLO(weights)                         # the trained .pt: its OpenVINO export invents extra low-confidence cards
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
