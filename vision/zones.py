"""Table-plane zones: which card is in which seat / board slot.

All coordinates are in the rectified table plane, millimetres (origin top-left of the mat).
Zones: B1..B5 (board / dealer cards), "<seat>a" and "<seat>b" (each seat's two cards).
MUCK / DECK are ignored by vision.

Seat identity comes from WHERE a card lies (its zone), never from who put it there:
seat N's cards are the ones in zones Na/Nb, and seat N is bound to a wallet at join time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

Point = tuple[float, float]
Polygon = list[Point]

MAT_W, MAT_H = 1000.0, 600.0          # mm, table plane
CARD_W, CARD_H = 63.0, 88.0           # mm
SLOT_W, SLOT_H = CARD_W * 1.3, CARD_H * 1.3
IGNORED = {"MUCK", "DECK"}


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


def default_zones(n_seats: int = 4) -> dict[str, Polygon]:
    """Default layout matching the printed mat (CLAUDE.md section 1).

    Seat 1 bottom, 2 right, 3 top, 4 left. Board row in the centre.
    """
    z: dict[str, Polygon] = {}
    for i in range(5):                                           # B1..B5, >= 2 cm gaps
        z[f"B{i + 1}"] = _rect(MAT_W / 2 + (i - 2) * (SLOT_W + 25), MAT_H / 2, SLOT_W, SLOT_H)
    gap = 25.0
    seat_centres = {1: (MAT_W / 2, MAT_H - 90), 2: (MAT_W - 150, MAT_H / 2),
                    3: (MAT_W / 2, 90), 4: (150, MAT_H / 2)}
    for seat in range(1, n_seats + 1):
        cx, cy = seat_centres[seat]
        dx = (SLOT_W + gap) / 2
        if seat in (1, 3):
            z[f"{seat}a"], z[f"{seat}b"] = _rect(cx - dx, cy, SLOT_W, SLOT_H), _rect(cx + dx, cy, SLOT_W, SLOT_H)
        else:                                                    # side seats: stacked vertically
            dy = (SLOT_H + gap) / 2
            z[f"{seat}a"], z[f"{seat}b"] = _rect(cx, cy - dy, SLOT_W, SLOT_H), _rect(cx, cy + dy, SLOT_W, SLOT_H)
    return z


def load_zones(path: str | Path | None = None) -> dict[str, Polygon]:
    p = Path(path) if path else Path(__file__).with_name("zones.json")
    if p.exists():
        raw = json.loads(p.read_text())
        return {k: [(float(x), float(y)) for x, y in v] for k, v in raw.items()}
    return default_zones()


def save_zones(zones: dict[str, Polygon], path: str | Path | None = None) -> None:
    p = Path(path) if path else Path(__file__).with_name("zones.json")
    p.write_text(json.dumps({k: [list(pt) for pt in v] for k, v in zones.items()}, indent=1))


def point_in_polygon(pt: Point, poly: Polygon) -> bool:
    """Ray casting."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def zone_of(pt: Point, zones: dict[str, Polygon]) -> str | None:
    for name, poly in zones.items():
        if point_in_polygon(pt, poly):
            return name
    return None


def is_board(zone: str) -> bool:
    return zone.startswith("B")


def seat_of_zone(zone: str) -> int | None:
    """'2a' -> 2; board/muck/deck -> None."""
    if len(zone) == 2 and zone[0].isdigit() and zone[1] in "ab":
        return int(zone[0])
    return None


@dataclass
class Detection:
    center: Point                      # card centroid in table-plane mm
    code: str                          # e.g. 'As'
    conf: float
    stable: bool = True


@dataclass
class ZoneReading:
    code: str
    conf: float
    stable: bool


def assign(dets: list[Detection], zones: dict[str, Polygon]) -> tuple[dict[str, ZoneReading], list[str]]:
    """Detections -> zone readings. Two cards in one zone -> warning and the zone is dropped (don't guess)."""
    out: dict[str, ZoneReading] = {}
    warnings: list[str] = []
    seen: dict[str, int] = {}
    for d in dets:
        z = zone_of(d.center, zones)
        if z is None or z in IGNORED:
            continue
        seen[z] = seen.get(z, 0) + 1
        out[z] = ZoneReading(d.code, d.conf, d.stable)
    for z, n in seen.items():
        if n > 1:
            warnings.append(f"MULTIPLE cards in {z}")
            del out[z]
    return out, warnings


def find_conflicts(readings: dict[str, ZoneReading], already_used: set[str] = frozenset()) -> list[str]:
    """One deck: a card code may appear only once (and not in `already_used`)."""
    where: dict[str, list[str]] = {}
    for z, r in readings.items():
        where.setdefault(r.code, []).append(z)
    msgs = [f"{c} read in {', '.join(zs)}" for c, zs in where.items() if len(zs) > 1]
    msgs += [f"{r.code} in {z} was already used" for z, r in readings.items() if r.code in already_used]
    return msgs


def board_cards(readings: dict[str, ZoneReading], need: int, have: int,
                min_conf: float = 0.0) -> list[str] | None:
    """Cards for board slots B(have+1)..B(have+need), only if all stable (and >= min_conf)."""
    out = []
    for i in range(have + 1, have + need + 1):
        r = readings.get(f"B{i}")
        if r is None or not r.stable or r.conf < min_conf:
            return None
        out.append(r.code)
    return out


def seat_hole_cards(readings: dict[str, ZoneReading], seats: list[int],
                    min_conf: float = 0.0) -> dict[int, list[str]] | None:
    """seat -> [card_a, card_b] for every live seat, only if every card is stable. None if incomplete."""
    out: dict[int, list[str]] = {}
    for s in seats:
        a, b = readings.get(f"{s}a"), readings.get(f"{s}b")
        if not a or not b or not (a.stable and b.stable) or min(a.conf, b.conf) < min_conf:
            return None
        if a.code in ("BACK", "NONE") or b.code in ("BACK", "NONE"):
            return None
        out[s] = [a.code, b.code]
    return out
