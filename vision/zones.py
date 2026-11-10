"""Zone labels shared by the reader and the table: B1..B5 (board) and "<seat>a" / "<seat>b" (hole cards)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ZoneReading:
    code: str
    conf: float
    stable: bool


def is_board(zone: str) -> bool:
    return zone.startswith("B")


def seat_of_zone(zone: str) -> int | None:
    """'2a' -> 2; board/muck/deck -> None."""
    if len(zone) == 2 and zone[0].isdigit() and zone[1] in "ab":
        return int(zone[0])
    return None


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
