"""Big, freely-drawable play areas (replaces the small fixed slot boxes).

An area is a rectangle in the camera image stored as FRACTIONS [x1, y1, x2, y2] of the frame (0..1), so it
survives resolution changes. There is one BOARD area (any number of board cards, laid out anywhere inside it)
and one area per seat S1..S4 (that seat's two hole cards, anywhere inside). Cards are attributed to an area by
being detected INSIDE it: crop the area, run the detector on the crop, convert back to frame coordinates.

Areas are edited from the dealer's phone at /areas and saved to vision/areas.json (the service reloads them live).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

AREAS_FILE = Path(__file__).with_name("areas.json")
NAMES = ["BOARD", "S1", "S2", "S3", "S4"]

# sensible starting points for a portrait overhead view; drag them to fit your table
DEFAULT_AREAS: dict[str, list[float]] = {
    "BOARD": [0.30, 0.34, 0.60, 0.74],
    "S1": [0.58, 0.44, 0.86, 0.64],
    "S2": [0.22, 0.72, 0.72, 0.88],
    "S3": [0.06, 0.44, 0.30, 0.64],
    "S4": [0.26, 0.24, 0.72, 0.33],
}


def validate(areas: dict) -> dict[str, list[float]]:
    """Raise ValueError unless every entry is a known name with 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1."""
    out: dict[str, list[float]] = {}
    for name, r in areas.items():
        if name not in NAMES:
            raise ValueError(f"unknown area {name}")
        if not (isinstance(r, (list, tuple)) and len(r) == 4):
            raise ValueError(f"{name}: need [x1,y1,x2,y2]")
        x1, y1, x2, y2 = (float(v) for v in r)
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError(f"{name}: coordinates must satisfy 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1")
        if (x2 - x1) < 0.02 or (y2 - y1) < 0.02:
            raise ValueError(f"{name}: area is too small")
        out[name] = [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]
    return out


def load(path: Path = AREAS_FILE) -> dict[str, list[float]]:
    if path.exists():
        try:
            return validate(json.loads(path.read_text()))
        except (ValueError, json.JSONDecodeError):
            pass                                              # corrupt file: fall back to defaults
    return {k: list(v) for k, v in DEFAULT_AREAS.items()}


def save(areas: dict, path: Path = AREAS_FILE) -> dict[str, list[float]]:
    clean = validate(areas)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=1))
    tmp.replace(path)
    return clean


def to_pixels(rect: list[float], w: int, h: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = rect
    return int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)


def crop(frame: np.ndarray, rect: list[float]) -> tuple[np.ndarray, tuple[int, int]]:
    """Crop an area out of the frame. Returns (crop, (x0, y0)) so detections can be mapped back."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = to_pixels(rect, w, h)
    return frame[y1:y2, x1:x2], (x1, y1)


def label_for(area: str, index: int) -> str:
    """Zone label the server understands: BOARD -> B1..B5, S2 -> 2a, 2b (then 2c.. = too many cards)."""
    if area == "BOARD":
        return f"B{index + 1}"
    return f"{area[1:]}{'abcdefgh'[index]}"
