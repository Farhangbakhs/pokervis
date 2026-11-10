"""Card reader for one local table: camera -> per-area YOLO -> votes -> server.

    python -m vision.service --source web:http://127.0.0.1:8000   # phone browser at /cam (no app)
    python -m vision.service --source 0                           # webcam / phone-as-webcam app
    python -m vision.service --source http://PHONE_IP:8080/video  # IP-webcam style stream

The play areas (BOARD, S1..S4) are drawn on the dealer's phone at /areas. Hosted tables use vision.worker instead.
"""
from __future__ import annotations

import argparse
import time

import cv2
import httpx

from server import config
from vision import areas as va
from vision.camera import Camera
from vision.detect import DEFAULT_WEIGHTS, CardDetector
from vision.reader import CHANGE_THR, MOTION_THR, TableReader, annotate, preview_jpeg

HEARTBEAT = 0.5                 # seconds between messages when nothing was read: keeps the camera marked as live


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0")
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.25, help="detector confidence floor (voting does the filtering)")
    ap.add_argument("--min-conf", type=float, default=0.35, help="mean confidence for a card to count as stable")
    ap.add_argument("--no-post", action="store_true", help="don't talk to the server (offline test)")
    ap.add_argument("--debug", action="store_true", help="show an OpenCV window")
    ap.add_argument("--no-queen-fix", action="store_true")
    ap.add_argument("--settle", type=float, default=0.0, help="extra seconds an area must be still before reading")
    ap.add_argument("--motion-thr", type=float, default=MOTION_THR, help="per-cell gray change = moving (raise for shaky cameras)")
    ap.add_argument("--change-thr", type=float, default=CHANGE_THR, help="per-cell gray change = read this area again")
    a = ap.parse_args()

    http, token = httpx.Client(base_url=a.server, timeout=5), None
    if not a.no_post:
        token = http.post("/api/dealer/login", json={"pin": config.get("DEALER_PIN")}).json()["token"]
        http.headers["Authorization"] = f"Bearer {token}"

    cam = Camera(a.source, token)
    while cam.frame is None:
        time.sleep(0.05)
    print("loading YOLO...", flush=True)
    det = CardDetector(a.weights, a.imgsz, a.conf, queen_fix=not a.no_queen_fix)
    det.warm_up()
    reader = TableReader(det, a.min_conf, a.motion_thr, a.change_thr, a.settle)
    areas = va.load()
    print("ready. Draw the areas at /areas on the dealer phone. Ctrl+C to stop.", flush=True)

    last_areas, last_post, read_seats, want_preview = 0.0, 0.0, [], False
    frame_warnings: list[str] = []
    while True:
        f, ts = cam.read()
        now = time.time()
        if now - last_areas > 3:                             # pick up areas drawn on the phone, no restart
            last_areas = now
            try:
                areas = va.validate(http.get("/api/vision/areas").json()["areas"]) if not a.no_post else va.load()
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        read = []
        fresh = f is not None and ts != reader.last_ts       # never read a frame we already looked at
        if fresh:
            t0 = time.time()
            read, frame_warnings = reader.process(f, ts, areas, read_seats, now)
            if read:
                print(f"[{time.strftime('%H:%M:%S')}] {' '.join(read)} ({time.time() - t0:.2f}s)", flush=True)
        if read or now - last_post > HEARTBEAT:
            last_post = now
            warnings = ["CAMERA LOST"] if cam.age() > 3 else list(frame_warnings)
            msg = reader.trk.message(warnings)
            if not a.no_post:
                try:
                    reply = http.post("/api/vision/update", json=msg).json()
                    read_seats, want_preview = reply.get("read_seats", []), reply.get("preview", False)
                    if fresh and want_preview and (jpg := preview_jpeg(f, areas, msg)):
                        http.post("/api/vision/frame", content=jpg, headers={"Content-Type": "image/jpeg"})
                except (httpx.HTTPError, ValueError) as e:
                    print(f"server unreachable: {e}", flush=True)
        if a.debug and f is not None:
            h, w = f.shape[:2]
            drawn = [(tuple(z["box"]), z["code"], z["conf"]) for z in reader.trk.message()["zones"].values() if z and z["stable"] and z.get("box")]
            cv2.imshow("vision", cv2.resize(annotate(f, areas, drawn), (960, int(960 * h / w))))
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
        if not fresh:
            time.sleep(0.01)


if __name__ == "__main__":
    main()
