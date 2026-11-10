"""Card reader for hosted tables: one model, one TableReader per room, round-robin over rooms with a new frame.

    python -m vision.worker --server http://127.0.0.1:8000

Runs beside the game server (needs the same SESSION_SECRET). A model read only happens when a table's picture
changes, so one process serves several tables; the server caps how many can exist at once.
"""
from __future__ import annotations

import argparse
import time

import cv2
import httpx
import numpy as np

from server import config
from server.auth import Auth
from vision.detect import DEFAULT_WEIGHTS, CardDetector
from vision.reader import TableReader, preview_jpeg

HEARTBEAT = 0.5


def serve_room(http: httpx.Client, reader: TableReader, r: dict, last_post: dict) -> bool:
    """Read this room's newest frame (if any) and report. Returns True if a frame was processed."""
    room, now, jpg_frame = r["room"], time.time(), None
    warnings = ["CAMERA LOST"] if r["age"] > 3 else []
    read = []
    if r["frame_ts"] != reader.last_ts:
        got = http.get("/api/cam/latest.jpg", params={"room": room})
        if got.status_code != 200:
            return False
        jpg_frame = cv2.imdecode(np.frombuffer(got.content, np.uint8), cv2.IMREAD_COLOR)
        if jpg_frame is None:
            reader.last_ts = r["frame_ts"]
            return False
        read, frame_warnings = reader.process(jpg_frame, r["frame_ts"], r["areas"], r["read_seats"], now)
        warnings += frame_warnings
    if read or now - last_post.get(room, 0) > HEARTBEAT:
        last_post[room] = now
        msg = reader.trk.message(warnings)
        http.post("/api/vision/update", params={"room": room}, json=msg)
        if jpg_frame is not None and r.get("preview") and (jpg := preview_jpeg(jpg_frame, r["areas"], msg)):
            http.post("/api/vision/frame", params={"room": room}, content=jpg, headers={"Content-Type": "image/jpeg"})
    return jpg_frame is not None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS)
    a = ap.parse_args()

    token = Auth(config.get("SESSION_SECRET")).issue({"role": "worker"}, ttl=365 * 24 * 3600)
    http = httpx.Client(base_url=a.server, timeout=10, headers={"Authorization": f"Bearer {token}"})
    print("loading YOLO...", flush=True)
    det = CardDetector(a.weights, 640, 0.25)
    det.warm_up()
    print("worker ready", flush=True)
    readers: dict[str, TableReader] = {}
    last_post: dict[str, float] = {}
    while True:
        worked = False
        try:
            rooms = http.get("/api/worker/rooms").json()["rooms"]
            for r in rooms:
                reader = readers.setdefault(r["room"], TableReader(det))
                try:
                    worked = serve_room(http, reader, r, last_post) or worked
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    print(f"room {r['room']}: {e}", flush=True)
            live = {r["room"] for r in rooms}
            for gone in [x for x in readers if x not in live]:
                readers.pop(gone)
                last_post.pop(gone, None)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            print(f"server unreachable: {e}", flush=True)
            time.sleep(1)
        if not worked:
            time.sleep(0.02)


if __name__ == "__main__":
    main()
