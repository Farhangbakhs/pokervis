"""Tables as rooms. The local game is one permanent room ("main"); a hosted site creates short-lived rooms.

Everything that used to be one global (the table, the camera's latest frame, the drawn areas) belongs to a Room,
so one visitor's game can never touch another's.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field

import base58

from server.table import Table

MAIN = "main"


class RoomsFull(Exception):
    pass


@dataclass
class Room:
    id: str
    table: Table
    hosted: bool = False
    areas: dict | None = None                  # hosted rooms keep their areas here; the main room uses vision/areas.json
    created: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    closed: bool = False
    cam: dict = field(default_factory=lambda: {"jpg": b"", "ts": 0.0})
    cam_seq: float = 0.0
    vframe: dict = field(default_factory=lambda: {"jpg": b"", "ts": 0.0})
    preview_until: float = 0.0                 # the vision worker renders the dealer's picture only while someone looks at it
    ws_count: int = 0
    snap_seq: int = 0                          # bumped each time a new state snapshot is published
    snap_text: str = ""
    snap_version: int = -1
    snap_at: float = 0.0

    def touch(self) -> None:
        self.last_active = time.time()

    def publish(self, version: int, text: str) -> None:
        self.snap_text, self.snap_version, self.snap_at = text, version, time.time()
        self.snap_seq += 1


class RoomManager:
    IDLE_LIMIT = 15 * 60                       # no camera frame or player action for this long: the room closes
    AGE_LIMIT = 2 * 3600

    def __init__(self, max_rooms: int = 3):
        self.max_rooms = max_rooms
        self.rooms: dict[str, Room] = {}
        self.bot_keys = [base58.b58encode(hashlib.sha256(f"pokervis-bot-{n}".encode()).digest()).decode() for n in range(max_rooms * 3)]
        self.bot_leases: dict[str, tuple[str, float]] = {}   # bot address -> (room, when leased); each has one on-chain account

    def add(self, room: Room) -> Room:
        self.rooms[room.id] = room
        return room

    def get(self, room_id: str | None) -> Room | None:
        return self.rooms.get(room_id or MAIN)

    def all(self) -> list[Room]:
        return list(self.rooms.values())

    def hosted_rooms(self) -> list[Room]:
        return [r for r in self.rooms.values() if r.hosted]

    def new_id(self) -> str:
        while True:
            rid = secrets.token_urlsafe(6)
            if rid not in self.rooms and rid != MAIN:
                return rid

    def ensure_capacity(self) -> None:
        if len(self.hosted_rooms()) >= self.max_rooms:
            raise RoomsFull

    def seated_elsewhere(self, pubkey: str, room_id: str) -> bool:
        """One bankroll, one table: the same chips must not be at stake in two games at once."""
        return any(p.pubkey == pubkey for r in self.all() if r.id != room_id for p in r.table.engine.players.values())

    def lease_bot(self, room_id: str, now: float | None = None) -> str | None:
        """A bot address not in use anywhere. A lease lasts while the bot is seated, or briefly while it is being seated."""
        now = now or time.time()

        def in_use(key: str) -> bool:
            lease = self.bot_leases.get(key)
            if lease is None or (room := self.rooms.get(lease[0])) is None:
                return False
            return now - lease[1] < 30 or any(p.pubkey == key for p in room.table.engine.players.values())
        for key in self.bot_keys:
            if not in_use(key):
                self.bot_leases[key] = (room_id, now)
                return key
        return None

    def sweep(self, now: float | None = None) -> list[Room]:
        now = now or time.time()
        gone = [r for r in self.hosted_rooms() if now - r.last_active > self.IDLE_LIMIT or now - r.created > self.AGE_LIMIT]
        for r in gone:
            self.rooms.pop(r.id).closed = True
        return gone
