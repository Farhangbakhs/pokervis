"""Threaded camera reader that always returns the latest frame.

Sources:
  * int / "0"                        -> local webcam (phone-as-webcam apps: Camo, Iriun, DroidCam, Continuity)
  * "http://.../video" or rtsp://    -> IP-webcam style stream (Android "IP Webcam" app)
  * path to a video file             -> loops (for offline testing)
  * "web:<server_url>"               -> frames pushed by a phone browser to /cam (no app needed)
"""
from __future__ import annotations

import threading
import time

import cv2
import httpx
import numpy as np


class Camera:
    def __init__(self, source: str, token: str | None = None, width: int = 1920, height: int = 1080):
        self.source, self.token = source, token
        self.frame: np.ndarray | None = None
        self.ts = 0.0
        self.ok = False
        self._stop = False
        self._cap = None
        if source.startswith("web:"):
            self._url = source[4:].rstrip("/") + "/api/cam/latest.jpg"
            self._http = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=5)
            target = self._poll_web
        else:
            src = int(source) if source.isdigit() else source
            self._cap = cv2.VideoCapture(src, cv2.CAP_DSHOW) if isinstance(src, int) else cv2.VideoCapture(src)
            if isinstance(src, int):
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self._file = isinstance(src, str) and not src.startswith(("http", "rtsp"))
            target = self._read_loop
        self._t = threading.Thread(target=target, daemon=True)
        self._t.start()

    def _read_loop(self) -> None:
        while not self._stop:
            ok, f = self._cap.read()
            if not ok:
                if self._file:                       # loop video files
                    self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                self.ok = False
                time.sleep(0.5)                      # camera hiccup: keep last frame, retry
                continue
            self.frame, self.ts, self.ok = f, time.time(), True
            if self._file:
                time.sleep(1 / 30)

    def _poll_web(self) -> None:
        last = 0.0
        while not self._stop:
            try:
                r = self._http.get(self._url)
                if r.status_code == 200:
                    ts = float(r.headers.get("x-frame-ts", "0"))
                    if ts != last:
                        f = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
                        if f is not None:
                            self.frame, self.ts, self.ok, last = f, time.time(), True, ts
                            continue
                else:
                    self.ok = False
            except httpx.HTTPError:
                self.ok = False
            time.sleep(0.15)

    def read(self) -> tuple[np.ndarray | None, float]:
        return self.frame, self.ts

    def age(self) -> float:
        return time.time() - self.ts if self.ts else float("inf")

    def close(self) -> None:
        self._stop = True
        if self._cap is not None:
            self._cap.release()
