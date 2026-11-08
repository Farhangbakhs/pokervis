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
import threading
import time

import cv2
import httpx
import numpy as np

from server import config
from vision import areas as va
from vision.camera import Camera
from vision.detect import DEFAULT_WEIGHTS, CardDetector, one_read_per_card
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
    for (x1, y1, x2, y2), code, conf in drawn:
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), t)
        cv2.putText(out, f"{code} {conf:.2f}", (x1, max(18, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
    return out


def confirmed_boxes(msg: dict) -> list:
    """The boxes the viewer draws: exactly the cards in the message sent to the server that count as on the table."""
    return [(tuple(z["box"]), z["code"], z["conf"]) for z in msg["zones"].values() if z and z["stable"] and z.get("box")]


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
    ap.add_argument("--settle", type=float, default=0.3, help="seconds the picture must be still before reading")
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
    trk = CardTracker(window=4, need=3, min_conf=a.min_conf)    # a card must be read in 3 of the last 4 looks
    areas = va.load()
    last_areas_fetch = 0.0
    print("ready. Draw the areas at /areas on the dealer phone. Ctrl+C to stop.", flush=True)

    prev, still_since, last_ts = None, None, 0.0
    sig: dict[str, np.ndarray] = {}                          # per area: picture the last read was based on
    runs: dict[str, int] = {}                                # per area: reads since it last changed
    board_five = False
    warnings: list[str] = []
    last_post = 0.0
    shared = {"drawn": [], "areas": areas}

    def preview_loop() -> None:
        """Dealer's live picture: newest camera frame + the latest boxes, ~10 fps, independent of how long YOLO takes."""
        c2, sent_ts = httpx.Client(base_url=a.server, headers=http.headers, timeout=5), 0.0
        while True:
            fr, fts = cam.read()
            if a.no_post or fr is None or fts == sent_ts:
                time.sleep(0.03)
                continue
            sent_ts = fts
            try:
                hh, ww = fr.shape[:2]
                ok, buf = cv2.imencode(".jpg", cv2.resize(annotate(fr, shared["areas"], shared["drawn"]), (1080, int(1080 * hh / ww))),
                                       [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok:
                    c2.post("/api/vision/frame", content=buf.tobytes(), headers={"Content-Type": "image/jpeg"})
            except (httpx.HTTPError, cv2.error):
                time.sleep(0.5)
            time.sleep(0.05)

    threading.Thread(target=preview_loop, daemon=True).start()
    while True:
        f, ts = cam.read()
        if f is None:
            time.sleep(0.02)
            continue
        new_frame = ts != last_ts                            # never re-read a frame we already looked at
        last_ts = ts
        now = time.time()
        h, w = f.shape[:2]

        if now - last_areas_fetch > 3:                       # pick up areas drawn on the phone, no restart
            last_areas_fetch = now
            try:
                areas = va.validate(http.get("/api/vision/areas").json()["areas"]) if not a.no_post else va.load()
            except (httpx.HTTPError, ValueError, KeyError):
                pass

        shared["areas"] = areas
        warnings = ["CAMERA LOST"] if cam.age() > 3 else []
        if new_frame:
            g = small_gray(f)
            if float(g.mean()) < 4:                              # all-black frames: the phone paused its camera
                warnings = ["CAMERA BLACK: phone camera paused? keep the camera phone's screen on"]
            moving = prev is not None and float(np.abs(g - prev).mean()) > a.motion_thr
            prev = g
            if moving:
                still_since = None
            elif still_since is None:
                still_since = now
            trk.set_motion(moving or still_since is None or now - still_since < a.settle)

            # Only areas whose picture changed are read again (an unchanged table costs nothing); a few reads per
            # change give the tracker its votes, each on a NEW frame.
            if not trk.moving and not warnings:
                per_area, axis, todo = {}, {}, []
                for name, rect in sorted(areas.items(), key=lambda kv: kv[0] != "BOARD"):
                    if name != "BOARD" and not board_five:
                        if name in sig:                      # not looked at (private cards), anything old is forgotten
                            trk.forget(name)
                            sig.pop(name, None)
                            runs.pop(name, None)
                        continue
                    crop, (x0, y0) = va.crop(f, rect)
                    if not crop.size:
                        continue
                    cs = small_gray(crop)
                    if name not in sig or sig[name].shape != cs.shape or float(np.abs(cs - sig[name]).mean()) > a.motion_thr * 1.5:
                        runs[name], sig[name] = 0, cs        # picture changed: start a fresh voting round
                    if runs[name] <= trk.window:
                        todo.append((name, crop, x0, y0))
                if todo:
                    t0 = time.time()
                    for name, crop, x0, y0 in todo:
                        axis[name] = 1 if crop.shape[0] > crop.shape[1] else 0     # order cards along the long side
                        whole = [d for d in det.detect(crop) if not va.touches_edge(d.box, crop.shape[1], crop.shape[0])]
                        per_area[name] = [(d.code, d.conf, (d.center[0] + x0, d.center[1] + y0),
                                           (int(d.box[0]) + x0, int(d.box[1]) + y0, int(d.box[2]) + x0, int(d.box[3]) + y0))
                                          for d in one_read_per_card(crop, whole)]
                        runs[name] += 1
                    trk.update(per_area, axis)
                    board_five = sum(1 for z, v in trk.message()["zones"].items() if z.startswith("B") and v and v["stable"]) >= 5
                    summary = " | ".join(f"{n}: {' '.join(sorted({r[0] for r in d}))}" for n, d in per_area.items() if d)
                    print(f"[{time.strftime('%H:%M:%S')}] {summary or 'no cards'}  ({time.time() - t0:.2f}s, {len(todo)} area)", flush=True)

        if now - last_post > 0.15:
            last_post = now
            msg = trk.message(warnings)
            shared["drawn"] = confirmed_boxes(msg)               # viewer and UI are fed from this one message
            if not a.no_post:
                try:
                    http.post("/api/vision/update", json=msg)
                except httpx.HTTPError as e:
                    print(f"server unreachable: {e}", flush=True)
        if a.debug:
            cv2.imshow("vision", cv2.resize(annotate(f, areas, shared["drawn"]), (960, int(960 * h / w))))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
        time.sleep(0.005)


if __name__ == "__main__":
    main()
