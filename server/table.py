"""Table orchestrator: seats <-> wallets, hand flow, vision readings, settlement.

Everything that mutates state goes through methods here (guarded by one lock), so the
FastAPI layer stays thin and the whole flow is testable without HTTP, camera, or chain.
"""
from __future__ import annotations

import json
import random
import re
import secrets
import threading
import time
from pathlib import Path

from game.engine import ALL_CARDS, EngineError, Player, PokerEngine
from server.chain_client import Chain, ChainError
from server.db import NullDb
from vision.zones import ZoneReading, is_board, seat_hole_cards, seat_of_zone

MAX_SEATS = 4
BOT_NAMES = {1: "Ada", 2: "Turing", 3: "Hopper", 4: "Lovelace"}
BOT_DELAY = 1.2                                     # seconds a bot "thinks", so the table is readable


def _distinct(items: list[str]) -> list[str]:
    out: list[str] = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


class TableError(Exception):
    """User-facing error (safe to show)."""


class Table:
    def __init__(self, chain: Chain, blinds: tuple[int, int] = (5, 10), buy_in: int = 1000,
                 auto_confirm: bool = True, min_conf: float = 0.35, state_path: Path | None = None, db=None, settle_async: bool = False,
                 hand_prefix: str = ""):
        self.chain = chain
        self.hand_prefix = hand_prefix              # hosted tables: "<room>." so their hands are told apart on chain
        self._refilling: set[int] = set()           # bots being topped up on chain
        self.bots: set[int] = set()                 # seats played by the server; their cards are dealt virtually at showdown
        self._bot_cards: tuple[str | None, dict[int, list[str]]] = (None, {})
        self._bot_turn: tuple[int | None, float] = (None, 0.0)
        self._cashing: set[int] = set()             # seats whose SOL cash-out is on its way
        self._sol_retry = 0.0                       # next time to ask an unreachable chain service for its info
        self._vision_sig = None                     # what the last vision message changed for the screens
        self.board_pending = 0                      # cards the camera has spotted but not confirmed yet
        self.db = db or NullDb()
        self.settle_async = settle_async            # real server: the payout confirms in the background, the table never freezes
        self._settling = False
        self.feed: list[dict] = []                  # recent REAL on-chain events (buy-ins, payouts, cash-outs) shown on the big screen
        self._sol: dict | None = None
        self.engine = PokerEngine(blinds)
        self.buy_in = buy_in
        self.auto_confirm = auto_confirm
        self.min_conf = min_conf
        self.manual_mode = False
        self.lock = threading.RLock()
        self.version = 0
        self.session = time.strftime("%m%d%H%M%S")
        self.seat_codes: dict[int, str] = {s: secrets.token_urlsafe(6) for s in range(1, MAX_SEATS + 1)}
        self.readings: dict[str, ZoneReading] = {}
        self.vision_warnings: list[str] = []
        self.vision_ts = 0.0
        self.overrides: dict[str, str] = {}
        self.last_settlement: dict | None = None
        self.settle_error: str | None = None
        self.hand_key: str | None = None
        self.prompts: list[str] = []
        self._state_path = state_path
        self._saved: dict | None = None
        self.last_action: dict[int, str] = {}       # seat -> "FOLD" / "CALL 20" ... shown on every screen
        self._board_order: list[str] = []           # cards the camera has seen in the board area, in arrival order
        self._board_now: set[str] = set()           # ...of which these are visible right now
        self._board_seen_at: dict[str, float] = {}  # last time each was seen (cards are held on screen briefly if they drop out)
        self._stale: set[str] = set()               # cards already lying on the table when a hand starts: ignored until picked up
        self._settled_at = 0.0                      # when the last payout landed
        self._last_join = 0.0                       # last time someone sat, left or re-bought
        self._settle_try = 0.0                      # last payout attempt (failed payouts are retried by tick())

    # ---------- helpers ----------
    def _bump(self) -> None:
        self.version += 1
        self._save()

    # ---------- persistence: seat codes and who is sitting where survive a server restart ----------
    def _snapshot(self) -> dict:
        return {"seat_codes": {str(k): v for k, v in self.seat_codes.items()},
                "players": {str(s): {"name": p.name, "pubkey": p.pubkey, "stack": p.stack}
                            for s, p in self.engine.players.items()}}

    def _save(self) -> None:
        if not self._state_path:
            return
        snap = self._snapshot()
        if snap == self._saved:
            return
        try:
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap), encoding="utf-8")
            tmp.replace(self._state_path)
            self._saved = snap
        except OSError:
            pass                                                   # never let a disk hiccup break the game

    def restore(self) -> None:
        """Reload seat codes and the roster. Stacks come from the chain (the source of truth for chips).
        A hand that was running is NOT resumed: it is simply gone and a fresh one starts."""
        if not self._state_path:
            return
        try:
            d = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for k, code in (d.get("seat_codes") or {}).items():
            if isinstance(code, str) and code and int(k) in self.seat_codes:
                self.seat_codes[int(k)] = code
        try:
            balances = self.chain.balances()
        except Exception:  # noqa: BLE001 - chain not reachable yet: fall back to the saved stacks
            balances = {}
        for k, pl in (d.get("players") or {}).items():
            try:
                stack = int(balances.get(pl["pubkey"], pl["stack"]))
                if stack > 0:
                    self.engine.add_player(int(k), pl["name"], pl["pubkey"], stack)
                else:                                              # busted: keep the seat so they can simply TOP UP
                    self.engine.players[int(k)] = Player(int(k), pl["name"], pl["pubkey"], 0)
            except (EngineError, KeyError, ValueError):
                continue
        self._last_join = time.time()
        self._saved = self._snapshot()

    def _seat_of_pubkey(self, pubkey: str) -> int | None:
        for s, p in self.engine.players.items():
            if p.pubkey == pubkey:
                return s
        return None

    # ---------- seating ----------
    def verify_seat_code(self, seat: int, code: str) -> None:
        if seat not in self.seat_codes or not secrets.compare_digest(self.seat_codes[seat], code or ""):
            raise TableError("Wrong seat code. Scan the QR printed at your seat.")

    def _check_can_join(self, seat: int, pubkey: str) -> dict | None:
        """Raise if this wallet cannot sit here. Returns the seat info when it is simply a player signing back in."""
        if not 1 <= seat <= MAX_SEATS:
            raise TableError("Invalid seat")
        if seat in self.engine.players and self.engine.players[seat].pubkey == pubkey:
            return {"seat": seat, "stack": self.engine.players[seat].stack}     # phone reload / lost token: always allowed, even mid-hand
        if self.engine.in_hand or self.engine.result is not None:
            raise TableError("Hand in progress, join between hands")
        if seat in self.engine.players:
            raise TableError(f"Seat {seat} is taken")
        if self._seat_of_pubkey(pubkey) is not None:
            raise TableError("This wallet already has a seat")
        return None

    def join(self, seat: int, name: str, pubkey: str) -> dict:
        with self.lock:
            back = self._check_can_join(seat, pubkey)
            if back:
                self.db.upsert_player(pubkey, self.engine.players[seat].name)
                return back
        # The wallet's on-chain bankroll IS the stack: asked outside the lock so a slow chain never freezes the table.
        try:
            stack = int(self.chain.balance(pubkey))
        except ChainError as e:
            raise TableError(str(e)) from e
        if stack <= 0:
            raise TableError("Buy in first: pay devnet SOL from your wallet to get chips")
        with self.lock:
            back = self._check_can_join(seat, pubkey)               # things may have changed while we asked
            if back:
                return back
            name = (name or "").strip()[:20] or f"Player {seat}"
            try:
                self.engine.add_player(seat, name, pubkey, stack)
            except EngineError as e:
                raise TableError(str(e)) from e
            self.db.upsert_player(pubkey, name)
            self._last_join = time.time()
            self._bump()
            return {"seat": seat, "stack": stack}

    # ---------- optional bots (only when a table's dealer asks for one) ----------
    def add_bot(self, pubkey: str, seat: int | None = None) -> dict:
        def free_seat() -> int:
            if self.engine.in_hand or self.engine.result is not None:
                raise TableError("Add bots between hands")
            free = [s for s in range(1, MAX_SEATS + 1) if s not in self.engine.players]
            if not free:
                raise TableError("The table is full")
            return seat if seat in free else free[-1]
        with self.lock:
            free_seat()
        try:
            stack = int(self.chain.bot_bankroll(pubkey, self.buy_in)["balance"])   # on chain, so never under the lock
        except ChainError as e:
            raise TableError(str(e)) from e
        with self.lock:
            s = free_seat()
            self.engine.add_player(s, BOT_NAMES[s], pubkey, stack)
            self.bots.add(s)
            self._last_join = time.time()
            self._bump()
            return {"seat": s}

    def _refill_bot(self, seat: int, pubkey: str) -> None:
        try:
            balance = int(self.chain.bot_bankroll(pubkey, self.buy_in)["balance"])
            with self.lock:
                p = self.engine.players.get(seat)
                if p is not None and p.pubkey == pubkey and not self.engine.in_hand and self.engine.result is None:
                    p.stack = balance
                    self._bump()
        except Exception as e:  # noqa: BLE001 - a bot that cannot refill just sits out
            print("bot refill failed:", e)
        finally:
            self._refilling.discard(seat)

    def _bots_play(self, now: float) -> None:
        e = self.engine
        for s in self.bots:                                         # a busted bot is topped up between rounds
            p = e.players.get(s)
            if p is not None and p.stack <= 0 and not e.in_hand and e.result is None and s not in self._refilling:
                self._refilling.add(s)
                threading.Thread(target=self._refill_bot, args=(s, p.pubkey), daemon=True, name="bot-refill").start()
        s = e.to_act
        if s is None or s not in self.bots or self.phase != "betting":
            self._bot_turn = (None, 0.0)
            return
        if self._bot_turn[0] != s:
            self._bot_turn = (s, now)
        if now - self._bot_turn[1] < BOT_DELAY:
            return
        self._bot_turn = (None, 0.0)
        call = e.to_call(s)
        limits = e.raise_limits()
        humans_live = [x for x in e.live_seats() if x not in self.bots]
        roll = random.random()
        if not humans_live and len(e.live_seats()) > 1:             # nobody is watching a bot-only pot: end it
            kind, amount = ("fold" if call else "check"), None
        elif call and roll < 0.18:
            kind, amount = "fold", None
        elif limits and roll > 0.9:
            kind, amount = "raise", min(limits[1], limits[0] + random.choice([0, 10, 20]))
        else:
            kind, amount = ("call" if call else "check"), None
        self.act(s, kind, amount)

    def _bot_hole_cards(self, known: set[str]) -> dict[int, list[str]]:
        """Virtual cards for the bots still in the hand, from whatever the camera has not seen. Decided once per hand."""
        seats = [s for s in self.engine.live_seats() if s in self.bots]
        key, cards = self._bot_cards
        used = {c for cs in cards.values() for c in cs}
        if key != self.hand_key or set(cards) != set(seats) or used & known:
            rng = secrets.SystemRandom()
            pool = rng.sample(sorted(ALL_CARDS - known), 2 * len(seats))
            cards = {s: pool[2 * i:2 * i + 2] for i, s in enumerate(seats)}
            self._bot_cards = (self.hand_key, cards)
        return cards

    def cash_out(self, seat: int) -> dict:
        """Log out: stand up between hands. The chips stay in this wallet's on-chain bankroll, so signing back in
        (same phone / same wallet) sits down with exactly the same stack."""
        with self.lock:
            p = self.engine.players.get(seat)
            if p is None:
                raise TableError("Empty seat")
            if self.engine.in_hand or self.engine.result is not None:
                raise TableError("Log out between hands")
            try:
                self.engine.remove_player(seat)
            except EngineError as e:
                raise TableError(str(e)) from e
            self._last_join = time.time()
            self._bump()
            return {"kept": p.stack}

    def _feed(self, kind: str, who: str, amount: int, res: dict) -> None:
        sig = res.get("deposit_signature") or res.get("signature") or ""
        if not sig or sig.startswith("mock"):
            return
        self.feed.insert(0, {"kind": kind, "who": who, "chips": amount, "sig": sig,
                             "url": res.get("explorer_url", ""), "ts": time.time()})
        del self.feed[12:]

    # ---------- real devnet SOL: buy in / cash out ----------
    def sol_info(self) -> dict | None:
        """House address + chip price (cached). None while the chain service is unreachable."""
        if self._sol is None and time.time() >= self._sol_retry:
            try:
                self._sol = self.chain.info()
            except Exception:  # noqa: BLE001
                self._sol_retry = time.time() + 10                 # do not make every state update wait on a dead service
        return self._sol

    def prepare_buyin(self, pubkey: str, chips: int) -> dict:
        try:
            return self.chain.buyin_tx(pubkey, int(chips))
        except (ChainError, ValueError) as e:
            raise TableError(str(e)) from e

    def confirm_buyin(self, pubkey: str, signature: str) -> dict:
        """The wallet paid the house in SOL: verify it on chain, credit the chips, refresh a seated stack."""
        try:
            res = self.chain.verify_buyin(signature, pubkey)
        except ChainError as e:
            raise TableError(str(e)) from e
        with self.lock:
            if not res.get("replay"):
                seat = self._seat_of_pubkey(pubkey)
                name = self.engine.players[seat].name if seat else f"{pubkey[:4]}...{pubkey[-4:]}"
                self.db.record_entry(pubkey, name, "deposit", int(res["chips"]), int(res["balance"]),
                                     res.get("deposit_signature", ""), res.get("explorer_url", ""))
                self._feed("buy-in", name, int(res["chips"]), res)
            seat = self._seat_of_pubkey(pubkey)
            if seat and not self.engine.in_hand and self.engine.result is None:
                self.engine.players[seat].stack = int(res["balance"])
                self._bump()
        return res

    def sync_stack(self, seat: int) -> dict:
        def check():
            p = self.engine.players.get(seat)
            if p is None or self.engine.in_hand or self.engine.result is not None:
                raise TableError("You can add chips between rounds")
            return p
        with self.lock:
            pubkey = check().pubkey
        try:
            balance = int(self.chain.balance(pubkey))               # outside the lock: this is a network call
        except ChainError as e:
            raise TableError(str(e)) from e
        with self.lock:
            p = check()
            p.stack = balance
            self._bump()
            return {"stack": p.stack}

    def cash_out_sol(self, seat: int) -> dict:
        """Log out AND take the winnings: one transaction returns the chips and pays the SOL to the player's wallet."""
        with self.lock:
            p = self.engine.players.get(seat)
            if p is None:
                raise TableError("Empty seat")
            if self.engine.in_hand or self.engine.result is not None:
                raise TableError("Cash out between hands")
            if p.stack <= 0:
                raise TableError("You have no chips to cash out")
            if seat in self._cashing:
                raise TableError("Your cash-out is already on its way")
            self._cashing.add(seat)                                 # blocks a new hand or a kick while the transaction confirms
            pubkey, stack, name = p.pubkey, p.stack, p.name
        try:
            try:
                res = self.chain.cashout_sol(pubkey, stack)         # can take many seconds: the table keeps running meanwhile
            except ChainError as e:
                raise TableError(str(e)) from e
            with self.lock:
                try:
                    self.engine.remove_player(seat)
                except EngineError as e:
                    raise TableError(str(e)) from e
                self.db.record_entry(pubkey, name, "cashout", -int(res["amount"]), int(res["balance"]),
                                     res.get("signature", ""), res.get("explorer_url", ""))
                self._feed("cash-out", name, int(res["amount"]), res)
                self._last_join = time.time()
                self._bump()
                return res
        finally:
            self._cashing.discard(seat)

    def kick(self, seat: int) -> dict:
        """Dealer removes a player. A hand in progress is cancelled first (stacks only move when a hand settles,
        so nobody loses chips); the player's chips are paid back to their wallet and the seat gets a new QR code."""
        with self.lock:
            p = self.engine.players.get(seat)
            if p is None:
                raise TableError("Empty seat")
            if self.settle_error or self._settling:
                raise TableError("Settlement not confirmed yet: wait or retry settlement first")
            if seat in self._cashing:
                raise TableError("That player's cash-out is on its way")
            name = p.name
            if self.engine.in_hand:
                self.abort_hand()
            self.engine.clear_result()
            try:
                self.engine.remove_player(seat)                    # chips stay in the wallet's on-chain bankroll
            except EngineError as e:
                raise TableError(str(e)) from e
            self.seat_codes[seat] = secrets.token_urlsafe(6)         # the old QR / link no longer sits down
            self.bots.discard(seat)
            self.last_action.pop(seat, None)
            self._last_join = time.time()
            self._bump()
            return {"kicked": seat, "name": name}

    # ---------- hand flow ----------
    @property
    def phase(self) -> str:
        e = self.engine
        if e.result is not None:
            return "settling" if self.settle_error else "done"
        if not e.in_hand:
            return "waiting"
        if len(self._fresh_codes()) >= 5:               # five cards are out: time to look at the players
            return "showdown"
        if e.to_act is not None:
            return "betting"
        if e.board_needed:
            return "need_board"
        return "betting"

    def start_hand(self) -> int:
        with self.lock:
            if self.engine.result is not None:
                raise TableError("Press Next hand first")
            if self._cashing:
                raise TableError("A cash-out is on its way: start the round in a moment")
            self.overrides.clear()
            prev_key, prev_id = self.hand_key, self.engine.hand_id
            try:
                hid = self.engine.start_hand()
                self.hand_key = f"{self.hand_prefix}{self.session}-{hid}"
                players = [self.engine.players[s].pubkey for s in self.engine._order]
                try:
                    self.chain.begin_hand(self.hand_key, players)
                except ChainError as e:
                    # a hand left open by an earlier session blocks every new one: close it, then retry once
                    m = re.search(r"hand (\S+) still open", str(e))
                    if not m or m.group(1) == self.hand_key:
                        raise
                    self.chain.abort_hand(m.group(1))
                    self.chain.begin_hand(self.hand_key, players)
            except (EngineError, ChainError) as e:
                self.engine.abort()
                self.engine.hand_id, self.hand_key = prev_id, prev_key    # a failed start must not burn a hand id
                raise TableError(str(e)) from e
            self.last_settlement = None
            self.settle_error = None
            self.last_action.clear()
            self.prompts = []
            # cards still on the table from the last hand are not part of this one until they are picked up
            self._stale = {rd.code for z, rd in self.readings.items() if is_board(z)}
            self.readings = {z: rd for z, rd in self.readings.items() if is_board(z) and rd.code not in self._stale}
            self._finish_if_done()
            self._bump()
            return hid

    def act(self, seat: int, kind: str, amount: int | None = None) -> None:
        with self.lock:
            to_call = self.engine.to_call(seat) if self.engine.in_hand and seat in self.engine._order else 0
            try:
                self.engine.act(seat, kind, amount)
            except EngineError as e:
                raise TableError(str(e)) from e
            self.last_action[seat] = {"fold": "FOLD", "check": "CHECK", "call": f"CALL {to_call}",
                                      "raise": f"RAISE TO {amount}"}.get(kind, kind.upper())
            self._finish_if_done()
            self._auto()
            self._bump()

    def abort_hand(self) -> None:
        with self.lock:
            if self.hand_key and self.engine.in_hand:
                try:
                    self.chain.abort_hand(self.hand_key)
                except ChainError as e:
                    raise TableError(str(e)) from e
            self.engine.abort()
            self.settle_error = None
            self._bump()

    def next_hand(self) -> None:
        with self.lock:
            if self.settle_error or self._settling:
                raise TableError("Settlement not confirmed yet: retry settlement")
            self.engine.clear_result()
            self.last_settlement = None
            self.overrides.clear()
            self.last_action.clear()
            self.prompts = []
            self._bump()

    # ---------- vision ----------
    def _effective_readings(self) -> dict[str, ZoneReading]:
        r = dict(self.readings)
        for z, code in self.overrides.items():
            r[z] = ZoneReading(code, 1.0, True)                    # dealer override is authoritative
        return r

    def on_vision(self, msg: dict) -> None:
        """msg: {ts, zones: {zone: {code, conf, stable} | None}, warnings}.

        The camera is the source of truth: whatever stable cards it sees in the board area are on the table and
        every screen shows exactly that, always. Once 5 board cards are out the players' areas are read too (the
        reader only scans them then, so nobody's private look at their own cards is ever recorded). When those
        5 cards are NEW to this hand and every remaining player's two cards are read, the winner is paid.
        """
        with self.lock:
            self.vision_ts = float(msg.get("ts", time.time()))
            self.vision_warnings = list(msg.get("warnings", []))
            zones = {z: v for z, v in (msg.get("zones") or {}).items() if v}
            board = sorted(((z, v) for z, v in zones.items() if is_board(z)), key=lambda kv: int(kv[0][1:]))
            self._stale &= {v["code"] for _, v in board}                     # picked up -> no longer a leftover
            now_codes = _distinct([v["code"] for _, v in board if v.get("stable")])
            now = time.time()
            self._board_now = set(now_codes)
            for c in now_codes:
                self._board_seen_at[c] = now
                if c not in self._board_order:
                    self._board_order.append(c)                          # new card: appended, existing cards keep their place
            self._board_order = [c for c in self._board_order if c in self._board_now or now - self._board_seen_at.get(c, 0) < self.BOARD_HOLD]
            five_out = len(self._board_view()) >= 5
            shown = set(self._board_view())
            self.board_pending = len({v["code"] for _, v in board if not v.get("stable") and v["code"] not in shown and v["code"] not in self._stale})
            self.readings = {
                z: ZoneReading(v["code"], float(v["conf"]), bool(v.get("stable")))
                for z, v in zones.items()
                if (is_board(z) and v["code"] not in self._stale) or (five_out and not is_board(z))
            }
            self._auto()
            self.tick()
            sig = (tuple(self._board_view()), self.board_pending, tuple(sorted((z, r.code, r.stable) for z, r in self.readings.items())),
                   tuple(self.vision_warnings), tuple(self.prompts), self.phase, tuple(self.engine.board))
            if sig != self._vision_sig:                          # an unchanged picture is not news: the screens are not redrawn
                self._vision_sig = sig
                self._bump()

    def seats_to_read(self) -> list[int]:
        """Seat areas the reader may look at right now: only once five new cards are out, only players still in, never bots.
        Nobody's private look at their own cards is ever read."""
        with self.lock:
            e = self.engine
            if not e.in_hand or len(self._fresh_codes()) < 5:
                return []
            return [s for s in e.live_seats() if s not in self.bots]

    def tick(self) -> None:
        """About once a second and on every vision message. Never starts a round (the dealer does that):
        it retries a failed payout, and a few seconds after a payout it returns the table to the waiting state
        so players can buy in and new players can sit down."""
        with self.lock:
            e, now = self.engine, time.time()
            if e.result is not None and self.settle_error and now - self._settle_try > 5:
                self._settle()                                     # payout failed earlier: keep trying
                self._bump()
            if e.result is not None and self.last_settlement is not None and not self.settle_error and now - self._settled_at >= 8:
                self.next_hand()                                   # result stays on screen for 8 s, then: waiting for the dealer
            if self.bots:
                self._bots_play(now)

    # ---------- starting a round (the dealer decides when) ----------
    def camera_live(self) -> bool:
        return bool(self.vision_ts and time.time() - self.vision_ts < 10
                    and not any("CAMERA" in w for w in self.vision_warnings))

    def can_start(self) -> dict:
        """Can the dealer start a round right now? If not, say why in plain words (shown on every screen)."""
        e = self.engine
        if e.in_hand or e.result is not None:
            return {"ok": False, "reason": "A round is in progress"}
        if len([s for s, p in e.players.items() if p.stack > 0]) < 2:
            return {"ok": False, "reason": "Need at least 2 players with chips"}
        cards = self._board_view()
        if self.camera_live() and cards:
            return {"ok": False, "reason": f"Clear the board first ({len(cards)} card{'s' if len(cards) != 1 else ''} on the table)"}
        return {"ok": True, "reason": ""}

    def request_start(self) -> int:
        """The dealer pressed START ROUND. Refuses (with the reason) unless there are enough players and the board is clear."""
        with self.lock:
            chk = self.can_start()
            if not chk["ok"]:
                raise TableError(chk["reason"])
            return self.start_hand()

    def override(self, zone: str, code: str) -> None:
        with self.lock:
            from game.engine import norm_cards
            try:
                code = norm_cards(code)[0]
            except EngineError as e:
                raise TableError(str(e)) from e
            self.overrides[zone] = code
            self._auto()
            self._bump()

    BOARD_HOLD = 3.0                                # seconds a card stays on screen after the camera stops seeing it

    def _board_view(self) -> list[str]:
        """What every screen shows on the board: the camera's cards, in arrival order, with a short grace period so a
        card that is briefly hidden (a hand reaching in, a missed frame) does not flicker off and on."""
        now = time.time()
        return [c for c in self._board_order if c in self._board_now or now - self._board_seen_at.get(c, 0) < self.BOARD_HOLD]

    def _fresh_codes(self) -> list[str]:
        """Stable board cards that belong to THIS hand (leftovers from the last hand excluded), in position order.
        Reads through dealer overrides, so a corrected card counts."""
        r = self._effective_readings()
        board = sorted(((z, rd) for z, rd in r.items() if is_board(z)), key=lambda kv: int(kv[0][1:]))
        return _distinct([rd.code for _, rd in board if rd.stable])

    def pending(self) -> dict:
        """What the camera currently proposes."""
        e, r = self.engine, self._effective_readings()
        out: dict = {"board": None, "reveal": None, "board5": None, "conflicts": []}
        if not e.in_hand:
            return out
        board = self._fresh_codes()
        if len(board) >= 5:
            # five NEW cards are out: read every remaining player's two cards and pay out
            out["board5"] = board[:5]
            humans = [s for s in e.live_seats() if s not in self.bots]
            reveal = seat_hole_cards(r, humans, self.min_conf)
            if reveal is not None and self.bots:
                known = set(board[:5]) | {c for cs in reveal.values() for c in cs}
                reveal = {**reveal, **self._bot_hole_cards(known)}
            out["reveal"] = reveal
        elif e.board_needed:
            # fewer than five: feed the cards to the game one betting round at a time
            new = [c for c in board if c not in e.board]
            if len(new) >= e.board_needed:
                out["board"] = new[: e.board_needed]
        return out

    def _auto(self) -> None:
        if self.manual_mode or not self.auto_confirm or not self.engine.in_hand:
            return
        p = self.pending()
        try:
            if p["board5"] and p["reveal"]:
                self._settle_now(p["board5"], p["reveal"])
            elif p["board"]:
                self._confirm_board(p["board"])
        except TableError as e:
            self.prompts = [str(e)]                                # e.g. a card read twice: shown, not guessed

    # ---------- dealer confirms ----------
    def _confirm_board(self, cards: list[str]) -> None:
        try:
            self.engine.deal_board(cards)
        except EngineError as e:
            raise TableError(str(e)) from e
        self.prompts = []
        # new street: the previous street's check/call/raise labels no longer apply (folds stay)
        self.last_action = {s: a for s, a in self.last_action.items() if a == "FOLD"}
        self._finish_if_done()

    def confirm_board(self, cards: list[str] | None = None) -> None:
        with self.lock:
            cards = cards or self.pending()["board"]
            if not cards:
                raise TableError("No stable board reading yet. Enter the cards manually.")
            self._confirm_board(cards)
            self._auto()
            self._bump()

    def _settle_now(self, board: list[str], reveal: dict) -> None:
        try:
            self.engine.settle_now(board, {int(s): c for s, c in reveal.items()})
        except EngineError as e:
            raise TableError(str(e)) from e
        self.prompts = []
        self._finish_if_done()

    def _confirm_showdown(self, reveal: dict[int, list[str] | str]) -> None:
        board = _distinct(self._fresh_codes() + list(self.engine.board))[:5]
        if len(board) < 5:
            raise TableError("5 board cards are not out yet")
        self._settle_now(board, reveal)

    def confirm_showdown(self, reveal: dict | None = None) -> None:
        with self.lock:
            reveal = reveal or self.pending()["reveal"]
            if not reveal:
                raise TableError("Not all hole cards are stable yet. Enter them manually.")
            self._confirm_showdown(reveal)
            self._bump()

    # ---------- settlement ----------
    def _finish_if_done(self) -> None:
        e = self.engine
        if e.result is not None and self.last_settlement is None and not self.settle_error and not self._settling:
            self._settle()

    def _settle(self) -> None:
        """Pay the winners on Solana. The result is already on every screen; the transaction confirms in the
        background (settle_async) so the game loop and websockets never wait on the network."""
        e = self.engine
        if self._settling:
            return
        self._settle_try = time.time()
        assert e.result is not None and self.hand_key
        args = (self.hand_key, dict(e.result.deltas), e.showdown_hash())
        if self.settle_async:
            self._settling = True
            threading.Thread(target=self._settle_worker, args=args, daemon=True, name="settle").start()
        else:
            self._settle_worker(*args)

    def _settle_worker(self, key: str, deltas: dict, showdown_hash: str) -> None:
        res, err = None, None
        try:
            res = self.chain.settle(key, deltas, showdown_hash)
        except ChainError as ex:
            err = str(ex)
        with self.lock:
            self._settling = False
            if self.hand_key != key or self.engine.result is None:
                return                                              # the hand was reset meanwhile: nothing to record
            if res is not None:
                self.last_settlement, self.settle_error = res, None
                self._settled_at = time.time()
                self._record_hand(key, res, showdown_hash)
                r = self.engine.result
                gain = max((d for d in r.deltas.values()), default=0)
                names = ", ".join(self.engine.players[x].name for x in r.winners if x in self.engine.players)
                self._feed("payout", names or "winner", gain, res)
            else:
                self.settle_error = err
            self._bump()

    def _record_hand(self, key: str, res: dict, showdown_hash: str) -> None:
        """Queue the finished hand for the database (never blocks, never raises into the game)."""
        try:
            e, r = self.engine, self.engine.result
            rows = []
            for seat in e._start:
                p = e.players.get(seat)
                if p is None:
                    continue
                d = r.deltas.get(p.pubkey, 0)
                rows.append({"pubkey": p.pubkey, "name": p.name, "seat": seat, "delta": d, "balance_after": p.stack,
                             "hand_name": r.hand_names.get(seat), "won": seat in r.winners and d > 0})
            pot = sum(x["delta"] for x in rows if x["delta"] > 0)
            self.db.record_hand(key, pot, list(r.board), showdown_hash, res.get("signature", ""), res.get("explorer_url", ""), rows)
        except Exception as ex:  # noqa: BLE001
            print("db record failed:", ex)

    def retry_settlement(self) -> None:
        with self.lock:
            if self.engine.result is None:
                raise TableError("Nothing to settle")
            self._settle()
            self._bump()
            if self.settle_error and not self._settling:
                raise TableError(self.settle_error)

    # ---------- public state (safe to broadcast: no codes, tokens, or hidden cards) ----------
    def _placed_cards(self) -> dict[int, list[str]]:
        """Face-up cards the camera sees in each seat's area (only read once 5 board cards are out)."""
        out: dict[int, list[str]] = {}
        r = self._effective_readings()
        for z in sorted(r):
            seat = seat_of_zone(z)
            if seat and r[z].stable:
                out.setdefault(seat, []).append(r[z].code)
        return {seat: cards[:2] for seat, cards in out.items()}

    def _status(self) -> dict:
        """One plain-language line (plus detail) that every screen shows: who must do what right now."""
        e, ph = self.engine, self.phase
        name = lambda s: e.players[s].name.upper()  # noqa: E731
        stage = {0: "PRE-FLOP", 3: "FLOP", 4: "TURN", 5: "RIVER"}.get(len(e.board), "")
        if ph == "waiting":
            funded = [s for s, p in e.players.items() if p.stack > 0]
            if len(funded) < 2:
                return {"code": "waiting_players", "headline": "WAITING FOR PLAYERS",
                        "detail": "Scan the QR at an empty seat to join. Short on chips? Buy in on your phone."}
            chk = self.can_start()
            if not chk["ok"]:
                return {"code": "clear_board", "headline": "CLEAR THE BOARD",
                        "detail": chk["reason"] + ". Then the dealer can start the round."}
            return {"code": "ready", "headline": "READY: START THE ROUND?",
                    "detail": "New players can still join and buy in. The dealer starts the round when everyone is seated."}
        if ph == "betting":
            s = e.to_act
            if s is None:
                return {"code": "wait", "headline": "ONE MOMENT", "detail": ""}
            call = e.to_call(s)
            return {"code": "turn", "seat": s, "headline": f"{name(s)}'S TURN",
                    "detail": f"{stage} · {'CALL ' + str(call) if call else 'CHECK OR BET'} · POT {self._pot()}"}
        if ph == "need_board":
            n = e.board_needed
            what = {0: "THE FLOP", 3: "THE TURN", 4: "THE RIVER"}.get(len(e.board), "THE BOARD")
            return {"code": "dealer_board", "headline": f"DEALER: PLACE {what}",
                    "detail": f"Burn a card, then put {n} card{'s' if n != 1 else ''} face up in the board area. Hands out of view."}
        if ph == "showdown":
            placed = self._placed_cards()
            waiting = [s for s in e.live_seats() if len(placed.get(s, [])) < 2 and s not in self.bots]
            if waiting:
                return {"code": "showdown", "headline": "5 CARDS OUT: PLACE YOUR CARDS",
                        "detail": "Waiting for " + ", ".join(name(s) for s in waiting) + ". Put both cards face up in your area."}
            return {"code": "showdown_reading", "headline": "READING THE CARDS...", "detail": "Hold still, hands out of view."}
        r = e.result
        if r is None:
            return {"code": "wait", "headline": "", "detail": ""}
        wins = ", ".join(name(s) for s in r.winners)
        gain = max((e.players[s].stack - e._start[s] for s in r.winners), default=0)
        hand = ", ".join(sorted({r.hand_names[s] for s in r.winners if s in r.hand_names}))
        if ph == "settling":
            return {"code": "settling", "headline": "PAYOUT PENDING", "detail": "Waiting for the Solana payout to confirm."}
        why = f"{hand.upper()} · " if hand else "EVERYONE ELSE FOLDED · "
        tail = "Clear the board. The dealer starts the next round."
        paid = "PAID ON SOLANA" if self.last_settlement else "PAYING OUT ON SOLANA..."
        return {"code": "winner", "headline": f"{wins} WIN{'S' if len(r.winners) == 1 else ''}!",
                "detail": f"{why}+{gain} CHIPS {paid}. {tail}", "winners": r.winners}

    def _pot(self) -> int:
        e = self.engine
        return int(e.state.total_pot_amount) if e.in_hand and e.state is not None else 0

    def public_state(self) -> dict:
        with self.lock:
            e = self.engine
            roles = {}
            if (e.in_hand or e.result is not None) and len(e._order) >= 2:
                roles = {"dealer": e.button, "sb": e._order[0], "bb": e._order[1]}
            seats = []
            for s in range(1, MAX_SEATS + 1):
                p = e.players.get(s)
                if not p:
                    seats.append({"seat": s, "occupied": False})
                    continue
                in_hand = e.in_hand and s in e._order
                pi = e._order.index(s) if in_hand else None
                st = e.state
                role = "/".join(k for k, v in (("D", roles.get("dealer")), ("SB", roles.get("sb")), ("BB", roles.get("bb"))) if v == s)
                seats.append({
                    "seat": s, "occupied": True, "name": p.name, "pubkey": p.pubkey, "stack": p.stack,
                    "bet": st.bets[pi] if in_hand and st else 0,
                    "folded": bool(in_hand and st and not st.statuses[pi]),
                    "is_button": e.button == s and (e.in_hand or e.result is not None),
                    "to_act": e.to_act == s, "role": role, "last_action": self.last_action.get(s, ""),
                })
            pot = self._pot()
            r = e.result
            if r is not None and r.revealed:
                shown = {s: list(c) for s, c in r.revealed.items()}
            else:
                shown = self._placed_cards()
            return {
                "version": self.version, "phase": self.phase, "mode": self.chain.mode, "sol": self.sol_info(), "feed": list(self.feed),
                "board_pending": self.board_pending, "bots": sorted(self.bots),
                "status": self._status(), "roles": roles, "showdown_cards": shown, "can_start": self.can_start(),
                "board_camera": self._board_view(),
                "manual_mode": self.manual_mode, "blinds": list(e.blinds), "buy_in": self.buy_in,
                "hand_id": self.hand_key, "seats": seats, "board": e.board, "pot": pot,
                "to_act": e.to_act, "board_needed": e.board_needed,
                "min_raise_to": (e.raise_limits() or (None, None))[0] if e.in_hand else None,
                "max_raise_to": (e.raise_limits() or (None, None))[1] if e.in_hand else None,
                "result": None if r is None else {
                    "winners": r.winners, "hand_names": r.hand_names, "showdown": r.showdown,
                    "revealed": r.revealed, "deltas_by_seat": {s: e.players[s].stack - e._start[s] for s in e._start},
                },
                "settlement": self.last_settlement, "settle_error": self.settle_error,
                "vision": {"age": round(time.time() - self.vision_ts, 1) if self.vision_ts else None,
                           "warnings": self.vision_warnings},
                "prompts": self.prompts,
            }

    def to_call_for(self, seat: int) -> int:
        with self.lock:
            return self.engine.to_call(seat) if self.engine.in_hand and seat in self.engine._order else 0

    def dealer_state(self) -> dict:
        """Extra info for the dealer console only (seat QR codes, pending reads)."""
        with self.lock:
            return {"seat_codes": dict(self.seat_codes), "pending": self.pending(),
                    "overrides": dict(self.overrides),
                    "readings": {z: {"code": r.code, "conf": r.conf, "stable": r.stable} for z, r in self.readings.items()}}
