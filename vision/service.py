"""Live vision service: camera -> per-area YOLO -> tracker -> server.

    python -m vision.service --source web:http://127.0.0.1:8000   # phone browser at /cam (no app)
    python -m vision.service --source 0                           # webcam / phone-as-webcam app
    python -m vision.service --source http://PHONE_IP:8080/video  # IP-webcam style stream

The play areas (BOARD, S1..S4) are drawn on the dealer's phone at /areas. Each area is cropped out of the
frame and the detector runs on the crop (cards fill the crop, which reads far better than a whole-table view).
Inference only runs while the scene is still, a few passes per change, so a CPU-only laptop keeps up.
"""
from __future__ import annotations

import argparse
import time

import cv2
import httpx
import numpy as np

from server import config
from vision import areas as va
from vision.camera import Camera
from vision.detect import DEFAULT_WEIGHTS, CardDetector
from vision.tracker import CardTracker

COLORS = {"BOARD": (32, 176, 255), "S1": (132, 220, 61), "S2": (255, 168, 74), "S3": (214, 107, 255), "S4": (255, 140, 176)}


def small_gray(f: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(cv2.resize(f, (160, 90)), cv2.COLOR_BGR2GRAY).astype(np.int16)


def annotate(frame: np.ndarray, areas: dict, drawn: list) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    t = max(2, w // 400)
    for name, rect in areas.items():
        x1, y1, x2, y2 = va.to_pixels(rect, w, h)
        cv2.rectangle(out, (x1, y1), (x2, y2), COLORS[name], t)
        cv2.putText(out, name, (x1 + 6, y1 + 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, COLORS[name], 2)
    for (x1, y1, x2, y2), code, conf, fixed in drawn:
        col = (0, 255, 255) if fixed else (0, 255, 0)
        cv2.rectangle(out, (x1, y1), (x2, y2), col, t)
        cv2.putText(out, f"{code} {conf:.2f}", (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, col, 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.25, help="detector confidence floor (tracker voting does the filtering)")
    ap.add_argument("--min-conf", type=float, default=0.35, help="mean confidence for a card to count as stable")
    ap.add_argument("--no-post", action="store_true", help="don't talk to the server (offline test)")
    ap.add_argument("--debug", action="store_true", help="show an OpenCV window")
    ap.add_argument("--no-queen-fix", action="store_true")
    ap.add_argument("--motion-thr", type=float, default=2.0, help="mean gray change = moving (raise for shaky cameras)")
    a = ap.parse_args()

    http, token = httpx.Client(base_url=a.server, timeout=5), None
    if not a.no_post:
        token = http.post("/api/dealer/login", json={"pin": config.get("DEALER_PIN")}).json()["token"]
        http.headers["Authorization"] = f"Bearer {token}"

    cam = Camera(a.source, token)
    while cam.frame is None:
        time.sleep(0.1)
    print("loading YOLO...", flush=True)
    det = CardDetector(a.weights, a.imgsz, a.conf, queen_fix=not a.no_queen_fix)
    trk = CardTracker(min_conf=a.min_conf)
    areas = va.load()
    last_areas_fetch = 0.0
    print("ready. Draw the areas at /areas on the dealer phone. Ctrl+C to stop.", flush=True)

    prev, still_since, last_inf_gray, runs, last_post, last_frame_post = None, None, None, 99, 0.0, 0.0
    drawn: list = []
    warnings: list[str] = []
    while True:
        f, ts = cam.read()
        if f is None:
            time.sleep(0.05)
            continue
        now = time.time()
        h, w = f.shape[:2]

        if now - last_areas_fetch > 3:                       # pick up areas drawn on the phone, no restart
            last_areas_fetch = now
            try:
                areas = va.validate(http.get("/api/vision/areas").json()["areas"]) if not a.no_post else va.load()
            except (httpx.HTTPError, ValueError, KeyError):
                pass

        warnings = ["CAMERA LOST"] if cam.age() > 3 else []
        g = small_gray(f)
        if float(g.mean()) < 4:                                  # all-black frames: the phone paused its camera
            warnings = ["CAMERA BLACK: phone camera paused? keep the camera phone's screen on"]
        moving = prev is not None and float(np.abs(g - prev).mean()) > a.motion_thr
        prev = g
        if moving:
            still_since = None
        elif still_since is None:
            still_since = now
        trk.set_motion(moving or still_since is None or now - still_since < 0.3)

        if last_inf_gray is None or float(np.abs(g - last_inf_gray).mean()) > a.motion_thr * 1.5:
            runs = 0                                          # scene changed: allow a fresh voting round
        if not trk.moving and runs < trk.window + 1 and not warnings:
            t0 = time.time()
            per_area, axis, drawn = {}, {}, []
            # The board area is always read. The players' areas are looked at ONLY when 5 cards are out on the
            # board (so nobody's private look at their own cards is ever scanned).
            five_out = False
            for name, rect in sorted(areas.items(), key=lambda kv: kv[0] != "BOARD"):
                if name != "BOARD" and not five_out:
                    per_area[name] = []                          # not looked at, and anything old is forgotten
                    continue
                crop, (x0, y0) = va.crop(f, rect)
                axis[name] = 1 if crop.shape[0] > crop.shape[1] else 0     # order cards along the long side
                dets = det.detect(crop) if crop.size else []
                per_area[name] = [(d.code, d.conf, (d.center[0] + x0, d.center[1] + y0)) for d in dets]
                drawn += [((int(d.box[0]) + x0, int(d.box[1]) + y0, int(d.box[2]) + x0, int(d.box[3]) + y0), d.code, d.conf, d.fixed)
                          for d in dets]
                if name == "BOARD":
                    five_out = len({d.code for d in dets}) >= 5
            trk.update(per_area, axis)
            last_inf_gray, runs = g, runs + 1
            summary = " | ".join(f"{n}: {' '.join(c for c, _, _ in sorted(set((c, 0, 0) for c, _, _ in d)))}" for n, d in per_area.items() if d)
            print(f"[{time.strftime('%H:%M:%S')}] {summary or 'no cards'}  ({time.time() - t0:.1f}s)", flush=True)

        if not a.no_post and now - last_post > 0.5:
            last_post = now
            try:
                http.post("/api/vision/update", json=trk.message(warnings))
                if now - last_frame_post > 0.7:
                    last_frame_post = now
                    ok, buf = cv2.imencode(".jpg", cv2.resize(annotate(f, areas, drawn), (960, int(960 * h / w))),
                                           [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok:
                        http.post("/api/vision/frame", content=buf.tobytes(), headers={"Content-Type": "image/jpeg"})
            except httpx.HTTPError as e:
                print(f"server unreachable: {e}", flush=True)
        if a.debug:
            cv2.imshow("vision", cv2.resize(annotate(f, areas, drawn), (960, int(960 * h / w))))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
        time.sleep(0.02)


if __name__ == "__main__":
    main()
