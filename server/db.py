"""Player database (Postgres on Tiger Cloud). Stores players and a per-player ledger of every chip movement.

Design for a live game: the table NEVER waits on the database. Writes go onto a queue that one background thread
drains (and retries); if DATABASE_URL is unset or the DB is down, the game carries on and `history()` just returns
what it can. The on-chain balances stay the source of truth for chips; this is the history/analytics layer.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    pubkey      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hands (
    hand_id        TEXT PRIMARY KEY,
    ended_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    pot            INTEGER NOT NULL,
    board          TEXT NOT NULL,
    showdown_hash  TEXT,
    signature      TEXT,
    explorer_url   TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    pubkey         TEXT NOT NULL REFERENCES players(pubkey),
    kind           TEXT NOT NULL,
    hand_id        TEXT REFERENCES hands(hand_id),
    seat           INTEGER,
    delta          INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    hand_name      TEXT,
    won            BOOLEAN NOT NULL DEFAULT false,
    signature      TEXT,
    explorer_url   TEXT
);
ALTER TABLE ledger DROP CONSTRAINT IF EXISTS ledger_kind_check;
ALTER TABLE ledger ADD CONSTRAINT ledger_kind_check CHECK (kind IN ('faucet','deposit','cashout','hand'));
CREATE INDEX IF NOT EXISTS ledger_pubkey_id ON ledger (pubkey, id);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_row_per_hand ON ledger (pubkey, hand_id) WHERE kind = 'hand';
"""


class NullDb:
    enabled = False

    def upsert_player(self, pubkey: str, name: str) -> None: ...
    def record_faucet(self, pubkey: str, name: str, amount: int, balance_after: int, signature: str = "", url: str = "") -> None: ...
    def record_entry(self, pubkey: str, name: str, kind: str, amount: int, balance_after: int, signature: str = "", url: str = "") -> None: ...
    def record_hand(self, hand_id: str, pot: int, board: list[str], showdown_hash: str, signature: str, url: str,
                    rows: list[dict]) -> None: ...

    def history(self, pubkey: str) -> dict:
        return {"enabled": False, "stats": _empty_stats(), "series": [], "recent": []}

    def close(self) -> None: ...


def _empty_stats() -> dict:
    return {"hands": 0, "wins": 0, "net": 0, "best": 0, "balance": None}


class Db:
    enabled = True

    def __init__(self, url: str):
        from psycopg_pool import ConnectionPool
        self.pool = ConnectionPool(url, min_size=1, max_size=4, open=False, kwargs={"autocommit": False}, timeout=10)
        self.q: queue.Queue[Callable[[Any], None] | None] = queue.Queue()
        self.ready = threading.Event()
        self.last_error = ""
        threading.Thread(target=self._run, daemon=True, name="db-writer").start()

    # ----- writer thread -----
    def _run(self) -> None:
        while True:
            try:
                self.pool.open()
                with self.pool.connection() as c:
                    c.execute(SCHEMA)
                self.ready.set()
                break
            except Exception as e:  # noqa: BLE001 - DB down at boot: keep trying, the game does not care
                self.last_error = str(e)
                print("db: waiting for database:", e)
                time.sleep(5)
        while True:
            job = self.q.get()
            if job is None:
                return
            for attempt in range(4):
                try:
                    with self.pool.connection() as c:
                        job(c)
                    break
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
                    print(f"db: write failed (attempt {attempt + 1}):", e)
                    time.sleep(1 + attempt * 2)

    def _enqueue(self, job: Callable[[Any], None]) -> None:
        self.q.put(job)

    # ----- writes (non-blocking) -----
    def upsert_player(self, pubkey: str, name: str) -> None:
        def job(c):
            c.execute("""INSERT INTO players (pubkey, name) VALUES (%s, %s)
                         ON CONFLICT (pubkey) DO UPDATE SET name = EXCLUDED.name, last_seen = now()""", (pubkey, name))
        self._enqueue(job)

    def record_faucet(self, pubkey: str, name: str, amount: int, balance_after: int, signature: str = "", url: str = "") -> None:
        self.record_entry(pubkey, name, "faucet", amount, balance_after, signature, url)

    def record_entry(self, pubkey: str, name: str, kind: str, amount: int, balance_after: int, signature: str = "", url: str = "") -> None:
        """A non-hand chip movement: 'faucet' (free guest chips), 'deposit' (SOL in), 'cashout' (SOL out, amount < 0)."""
        def job(c):
            c.execute("INSERT INTO players (pubkey, name) VALUES (%s, %s) ON CONFLICT (pubkey) DO NOTHING", (pubkey, name))
            c.execute("""INSERT INTO ledger (pubkey, kind, delta, balance_after, signature, explorer_url)
                         VALUES (%s, %s, %s, %s, %s, %s)""", (pubkey, kind, amount, balance_after, signature, url))
        self._enqueue(job)

    def record_hand(self, hand_id: str, pot: int, board: list[str], showdown_hash: str, signature: str, url: str,
                    rows: list[dict]) -> None:
        """rows: [{pubkey, name, seat, delta, balance_after, hand_name, won}] - one transaction for the whole hand."""
        def job(c):
            c.execute("""INSERT INTO hands (hand_id, pot, board, showdown_hash, signature, explorer_url)
                         VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (hand_id) DO NOTHING""",
                      (hand_id, pot, " ".join(board), showdown_hash, signature, url))
            for r in rows:
                c.execute("INSERT INTO players (pubkey, name) VALUES (%s, %s) ON CONFLICT (pubkey) DO NOTHING",
                          (r["pubkey"], r["name"]))
                c.execute("""INSERT INTO ledger (pubkey, kind, hand_id, seat, delta, balance_after, hand_name, won, signature, explorer_url)
                             VALUES (%s, 'hand', %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                          (r["pubkey"], hand_id, r["seat"], r["delta"], r["balance_after"], r.get("hand_name"),
                           r["won"], signature, url))
        self._enqueue(job)

    # ----- read (one round trip for the chart + list + stats) -----
    def history(self, pubkey: str) -> dict:
        if not self.ready.is_set():
            return {"enabled": True, "stats": _empty_stats(), "series": [], "recent": [], "error": "database starting"}
        with self.pool.connection() as c:
            rows = c.execute("""SELECT id, extract(epoch FROM ts)::float8, kind, hand_id, delta, balance_after,
                                       hand_name, won, signature, explorer_url
                                FROM (SELECT * FROM ledger WHERE pubkey = %s ORDER BY id DESC LIMIT 300) t
                                ORDER BY id""", (pubkey,)).fetchall()
            agg = c.execute("""SELECT count(*) FILTER (WHERE kind='hand'), count(*) FILTER (WHERE kind='hand' AND won),
                                      coalesce(sum(delta) FILTER (WHERE kind='hand'), 0),
                                      coalesce(max(delta) FILTER (WHERE kind='hand'), 0)
                               FROM ledger WHERE pubkey = %s""", (pubkey,)).fetchone()
        entries = [{"id": r[0], "ts": r[1], "kind": r[2], "handId": r[3], "delta": r[4], "balance": r[5],
                    "handName": r[6], "won": r[7], "signature": r[8], "explorerUrl": r[9]} for r in rows]
        # x axis = turns (hands played); point 0 is the bankroll before the first hand. Grants only show as a jump.
        hands = [e for e in entries if e["kind"] == "hand"]
        series = []
        if hands:
            series.append({"n": 0, "balance": hands[0]["balance"] - hands[0]["delta"]})
        for i, e in enumerate(hands, 1):
            series.append({"n": i, "balance": e["balance"], "delta": e["delta"], "won": e["won"], "handName": e["handName"]})
        stats = {"hands": agg[0], "wins": agg[1], "net": int(agg[2]), "best": int(agg[3]),
                 "balance": entries[-1]["balance"] if entries else None}
        return {"enabled": True, "stats": stats, "series": series, "recent": list(reversed(entries))[:40]}

    def close(self) -> None:
        self.q.put(None)


def make_db(url: str | None) -> "Db | NullDb":
    return Db(url) if url else NullDb()
