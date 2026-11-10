"""Reading one table: which areas to look at, when a picture is worth reading, and voting on what was read.

Shared by vision.service (one local table) and vision.worker (many hosted tables). It holds no I/O: the caller
hands it frames and sends the resulting message wherever it needs to go.

Change detection compares small grids of cells, never one average over the area. A card covers only a few percent
of a big area, so an area-wide mean can stay under any threshold while a card is plainly there; a per-cell maximum
sees it at full contrast. The median difference is removed first, so a phone's auto-exposure shifting the whole
picture does not count as change.
"""
from __future__ import annotations

import cv2
import numpy as np

from vision import areas as va
from vision.detect import one_read_per_card
from vision.tracker import CardTracker

COLORS = {"BOARD": (32, 176, 255), "S1": (132, 220, 61), "S2": (255, 168, 74), "S3": (214, 107, 255), "S4": (255, 140, 176)}
# Gray levels, per grid cell. Simulated mount shake of +-2 px reaches ~19; a real card on a real table is 42 or more.
MOTION_THR = 20.0       # between consecutive frames: something is moving here (a false alarm would block reading)
CHANGE_THR = 12.0       # against the picture last read: read this area again (a false alarm only costs one look)
BLACK_LEVEL = 4         # mean gray of a frame from a phone whose camera has been paused


def small_gray(f: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(cv2.resize(f, (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)


def grid_change(a: np.ndarray, b: np.ndarray) -> float:
    """Largest exposure-corrected change in any cell of a 16x9 grid."""
    d = a - b
    d -= np.median(d)
    return float(cv2.resize(np.abs(d), (16, 9), interpolation=cv2.INTER_AREA).max())


def annotate(frame: np.ndarray, areas: dict, drawn: list) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    t = max(2, w // 400)
    for name, rect in areas.items():
        x1, y1, x2, y2 = va.to_pixels(rect, w, h)
        cv2.rectangle(out, (x1, y1), (x2, y2), COLORS[name], t)
        cv2.putText(out, name, (x1 + 6, y1 + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, COLORS[name], 2)
    for (x1, y1, x2, y2), code, conf in drawn:
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), t)
        cv2.putText(out, f"{code} {conf:.2f}", (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
    return out


def preview_jpeg(frame: np.ndarray, areas: dict, msg: dict) -> bytes | None:
    """The dealer's picture: the frame, the areas, and exactly the cards the server was told are on the table."""
    drawn = [(tuple(z["box"]), z["code"], z["conf"]) for z in msg["zones"].values() if z and z["stable"] and z.get("box")]
    h, w = frame.shape[:2]
    ok, buf = cv2.imencode(".jpg", cv2.resize(annotate(frame, areas, drawn), (1080, int(1080 * h / w))), [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


class TableReader:
    def __init__(self, detector, min_conf: float = 0.35, motion_thr: float = MOTION_THR, change_thr: float = CHANGE_THR, settle: float = 0.0):
        self.det = detector
        self.trk = CardTracker(window=4, need=3, min_conf=min_conf)         # a card must be read in 3 of the last 4 looks
        self.motion_thr, self.change_thr, self.settle = motion_thr, change_thr, settle
        self.prev: dict[str, np.ndarray] = {}                               # per area: the previous frame
        self.sig: dict[str, np.ndarray] = {}                                # per area: the picture the last reads were based on
        self.runs: dict[str, int] = {}                                      # per area: reads since it last changed
        self.still_since: dict[str, float] = {}
        self.last_ts = 0.0

    def _drop(self, area: str) -> None:
        self.trk.forget(area)
        for d in (self.prev, self.sig, self.runs, self.still_since):
            d.pop(area, None)

    def process(self, frame: np.ndarray, ts: float, areas: dict, read_seats, now: float) -> tuple[list, list[str]]:
        """Look at one new frame. Returns (areas read this frame, warnings)."""
        self.last_ts = ts
        if float(cv2.resize(frame, (32, 18)).mean()) < BLACK_LEVEL:
            return [], ["CAMERA BLACK: phone camera paused? keep the camera phone's screen on"]
        wanted = {"BOARD"} | {f"S{s}" for s in read_seats}
        for name in [n for n in set(self.prev) | set(self.sig) if n not in wanted or n not in areas]:
            self._drop(name)                                                # no longer looked at (private cards): anything old is forgotten
        todo, moving = [], set()
        for name, rect in sorted(areas.items(), key=lambda kv: kv[0] != "BOARD"):
            if name not in wanted:
                continue
            crop, (x0, y0) = va.crop(frame, rect)
            if not crop.size:
                continue
            g = small_gray(crop)
            p = self.prev.get(name)
            self.prev[name] = g
            if p is not None and grid_change(g, p) > self.motion_thr:       # a hand over THIS area; movement elsewhere does not matter
                self.still_since.pop(name, None)
                moving.add(name)
                continue
            if self.settle > 0 and now - self.still_since.setdefault(name, now) < self.settle:
                moving.add(name)
                continue
            s = self.sig.get(name)
            if s is None or grid_change(g, s) > self.change_thr:
                self.sig[name], self.runs[name] = g, 0                      # the picture changed: start a fresh round of votes
            if self.runs[name] < self.trk.window and not (self.runs[name] >= self.trk.need and self.trk.settled(name)):
                todo.append((name, crop, x0, y0))
        self.trk.set_area_motion(moving)
        if not todo:
            return [], []
        # One model read per frame, for the area with the fewest votes. The total work is the same as reading them all, but the
        # caller gets control back between reads: heartbeats keep flowing and other tables get their turn at the model.
        name, crop, x0, y0 = min(todo, key=lambda t: self.runs[t[0]])
        whole = [d for d in self.det.detect(crop) if not va.touches_edge(d.box, crop.shape[1], crop.shape[0])]
        per_area = {name: [(d.code, d.conf, (d.center[0] + x0, d.center[1] + y0),
                            (int(d.box[0]) + x0, int(d.box[1]) + y0, int(d.box[2]) + x0, int(d.box[3]) + y0))
                           for d in one_read_per_card(crop, whole)]}
        self.runs[name] += 1
        self.trk.update(per_area, {name: 1 if crop.shape[0] > crop.shape[1] else 0})   # order cards along the long side
        return [name], []
