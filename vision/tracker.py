"""Temporal voting per zone -> the section-3 vision message.

CLAUDE.md asks for 6 of the last 8 readings; the YOLOv8l CPU loop runs ~1-2 Hz, so the default is
4 of the last 5 (configurable). A zone is `stable` only if one code holds the vote AND its mean
confidence is high AND the scene has been motion-free. Unstable zones are still reported (the dealer
UI shows them yellow) but the server never auto-confirms them.
"""
from __future__ import annotations

import collections
import time
from collections import deque


class ZoneTracker:
    def __init__(self, window: int = 5, need: int = 4, min_conf: float = 0.90):
        self.window, self.need, self.min_conf = window, need, min_conf
        self.buf: dict[str, deque] = {}
        self.moving = False

    def set_motion(self, moving: bool) -> None:
        self.moving = moving

    def clear(self) -> None:
        self.buf.clear()

    def update(self, readings: dict[str, tuple[str, float]], zone_names: list[str] | None = None) -> None:
        """readings: zone -> (code, conf) for zones that currently contain a card."""
        for z in set(self.buf) | set(readings) | set(zone_names or []):
            q = self.buf.setdefault(z, deque(maxlen=self.window))
            q.append(readings.get(z))                       # None = empty this frame

    def message(self, warnings: list[str] | None = None, ts: float | None = None) -> dict:
        zones: dict[str, dict | None] = {}
        for z, q in self.buf.items():
            if not q:
                continue
            empties = sum(1 for x in q if x is None)
            if empties > len(q) / 2:
                zones[z] = None
                continue
            votes = collections.Counter(x[0] for x in q if x is not None)
            code, n = votes.most_common(1)[0]
            confs = [x[1] for x in q if x is not None and x[0] == code]
            mean = sum(confs) / len(confs)
            tot = sum(votes.values())
            stable = (not self.moving and len(q) >= self.need and n >= self.need and mean >= self.min_conf)
            zones[z] = {"code": code, "conf": round(mean, 4), "stable": stable,
                        "top3": [[c, round(v / tot, 3)] for c, v in votes.most_common(3)]}
        return {"ts": ts or time.time(), "zones": zones, "warnings": warnings or []}


class CardTracker:
    """Per-AREA tracking of distinct cards (the area-based replacement for ZoneTracker).

    The detector may report one card several times (each corner index is its own box) and may flicker a
    phantom. We therefore track every (area, code) pair across the last `window` passes and call a card real
    only if it is present in a majority of them; it is `stable` when present in >= `need`, with enough mean
    confidence, and the scene is not moving. Cards are ordered by position along the area's long axis and
    given the labels the server understands (B1.., 1a/1b, ...).
    """

    def __init__(self, window: int = 4, need: int = 3, min_conf: float = 0.35):
        self.window, self.need, self.min_conf = window, need, min_conf
        self.buf: dict[tuple[str, str], deque] = {}
        self.moving = False
        self.axis: dict[str, int] = {}                     # area -> 0 sort by x, 1 sort by y
        self._stable_keys: set[tuple[str, str]] = set()    # cards that have already earned "stable"

    def set_motion(self, moving: bool) -> None:
        self.moving = moving

    def clear(self) -> None:
        self.buf.clear()

    def update(self, per_area: dict[str, list[tuple[str, float, tuple[float, float]]]],
               axis: dict[str, int] | None = None) -> None:
        """per_area: area -> [(code, conf, (x, y)), ...] for this pass (areas with no cards = [])."""
        if axis:
            self.axis.update(axis)
        for area, dets in per_area.items():
            best: dict[str, tuple[float, tuple[float, float]]] = {}
            for code, conf, pos in dets:                    # duplicates of one card collapse to the best box
                if code not in best or conf > best[code][0]:
                    best[code] = (conf, pos)
            for key in [k for k in self.buf if k[0] == area and k[1] not in best]:
                self.buf[key].append(None)
            for code, (conf, pos) in best.items():
                self.buf.setdefault((area, code), deque(maxlen=self.window)).append((conf, pos))
        for key in [k for k, q in self.buf.items() if len(q) == self.window and all(x is None for x in q)]:
            del self.buf[key]
            self._stable_keys.discard(key)

    def message(self, warnings: list[str] | None = None, ts: float | None = None) -> dict:
        from vision.areas import label_for
        by_area: dict[str, list[dict]] = {}
        for (area, code), q in self.buf.items():
            seen = [x for x in q if x is not None]
            if len(seen) <= len(q) / 2:                     # absent most of the time: not a card
                continue
            conf = sum(c for c, _ in seen) / len(seen)
            pos = [sum(p[i] for _, p in seen) / len(seen) for i in (0, 1)]
            earned = len(q) >= self.need and len(seen) >= self.need and conf >= self.min_conf
            # motion (a hand reaching in) only stops NEW cards from becoming stable; cards already stable stay stable
            stable = earned and (not self.moving or (area, code) in self._stable_keys)
            if stable:
                self._stable_keys.add((area, code))
            by_area.setdefault(area, []).append({"code": code, "conf": round(conf, 4), "stable": stable, "pos": pos})
        zones: dict[str, dict | None] = {}
        warns = list(warnings or [])
        for area, cards in by_area.items():
            cards.sort(key=lambda c: c["pos"][self.axis.get(area, 0)])
            for i, c in enumerate(cards):
                zones[label_for(area, i)] = {"code": c["code"], "conf": c["conf"], "stable": c["stable"],
                                             "top3": [[c["code"], c["conf"]]]}
            limit = 5 if area == "BOARD" else 2
            if len(cards) > limit:
                warns.append(f"{'Board' if area == 'BOARD' else 'Seat ' + area[1:]} area shows {len(cards)} cards")
        return {"ts": ts or time.time(), "zones": zones, "warnings": warns}
