"""Poker engine: thin wrapper over pokerkit's NoLimitTexasHoldem.

Hole cards are physically dealt and unknown until showdown, so we deal them as unknown
('??') and reveal the real cards at showdown via `show_or_muck_hole_cards` (verified
against pokerkit 0.7.7). The board is dealt as the camera/dealer confirms it.

A hand's output is `deltas {pubkey: int}` (sum == 0) plus winner info.
"""
from __future__ import annotations

import hashlib
import warnings
from dataclasses import dataclass, field

from pokerkit import Automation as A
from pokerkit import NoLimitTexasHoldem, StandardHighHand

AUTOMATIONS = (
    A.ANTE_POSTING, A.BET_COLLECTION, A.BLIND_OR_STRADDLE_POSTING, A.CARD_BURNING,
    A.HAND_KILLING, A.CHIPS_PUSHING, A.CHIPS_PULLING,
)
RANKS, SUITS = "23456789TJQKA", "shdc"
ALL_CARDS = {r + s for r in RANKS for s in SUITS}
# pokerkit warns about 'non-recommended' dealable cards; we validate uniqueness ourselves.
warnings.filterwarnings('ignore', message='A card being dealt')


class EngineError(ValueError):
    """Illegal action / bad input. Message is safe to show to the user."""


@dataclass
class Player:
    seat: int
    name: str
    pubkey: str
    stack: int


@dataclass
class HandResult:
    deltas: dict[str, int]                 # pubkey -> chip change, sums to 0
    winners: list[int]                     # seats with a positive delta
    hand_names: dict[int, str]             # seat -> e.g. "Flush"  (showdown only)
    showdown: bool
    board: list[str] = field(default_factory=list)
    revealed: dict[int, list[str]] = field(default_factory=dict)


def norm_cards(cards: str | list[str]) -> list[str]:
    """'AsKd' or ['As','Kd'] -> ['As','Kd'], validated."""
    if isinstance(cards, str):
        cards = [cards[i:i + 2] for i in range(0, len(cards), 2)]
    out = []
    for c in cards:
        c = c[0].upper() + c[1].lower()
        if c not in ALL_CARDS:
            raise EngineError(f"Invalid card '{c}'")
        out.append(c)
    return out


class PokerEngine:
    def __init__(self, blinds: tuple[int, int] = (5, 10)):
        self.blinds = blinds
        self.players: dict[int, Player] = {}        # seat -> Player
        self.button = -1
        self.hand_id = 0
        self.state = None
        self._order: list[int] = []                 # pokerkit player_index -> seat
        self._start: dict[int, int] = {}            # seat -> stack at hand start
        self.result: HandResult | None = None
        self._pre: list[int] = []                   # stacks before the pot was paid out
        self._board: list[str] = []

    # ---------- table setup ----------
    def add_player(self, seat: int, name: str, pubkey: str, stack: int) -> None:
        if self.in_hand:
            raise EngineError("Cannot join during a hand")
        if seat in self.players:
            raise EngineError(f"Seat {seat} is taken")
        if stack <= 0:
            raise EngineError("Buy-in must be positive")
        self.players[seat] = Player(seat, name, pubkey, stack)

    def remove_player(self, seat: int) -> Player:
        if self.in_hand:
            raise EngineError("Cannot leave during a hand")
        return self.players.pop(seat)

    # ---------- hand lifecycle ----------
    @property
    def in_hand(self) -> bool:
        return self.state is not None and self.result is None

    def start_hand(self) -> int:
        if self.in_hand:
            raise EngineError("Hand already in progress")
        seats = sorted(s for s, p in self.players.items() if p.stack > 0)
        if len(seats) < 2:
            raise EngineError("Need at least 2 players with chips")
        # rotate button to next occupied seat
        nxt = [s for s in seats if s > self.button]
        self.button = nxt[0] if nxt else seats[0]
        # pokerkit: player 0 = small blind ... last player = button
        i = seats.index(self.button)
        self._order = seats[i + 1:] + seats[:i + 1]
        self._start = {s: self.players[s].stack for s in self._order}
        self.state = NoLimitTexasHoldem.create_state(
            AUTOMATIONS, True, 0, self.blinds, self.blinds[1],
            tuple(self._start[s] for s in self._order), len(self._order))
        for pi in range(len(self._order)):
            self.state.deal_hole("????", pi)
        self.result = None
        self._board = []
        self.hand_id += 1
        self._pre = list(self.state.stacks)
        self._maybe_finish()
        return self.hand_id

    def abort(self) -> None:
        """Misdeal: discard the hand, no stack changes (stacks only move when a hand finishes)."""
        self.state = None
        self.result = None
        self._board = []
        self._order = []

    def clear_result(self) -> None:
        """Dealer acknowledged the finished hand; ready for the next one."""
        if self.result is not None:
            self.state = None
            self.result = None
            self._board = []
            self._order = []

    def seat_to_index(self, seat: int) -> int:
        try:
            return self._order.index(seat)
        except ValueError:
            raise EngineError(f"Seat {seat} is not in this hand")

    # ---------- betting ----------
    @property
    def to_act(self) -> int | None:
        if not self.in_hand or self.state.actor_index is None:
            return None
        return self._order[self.state.actor_index]

    def to_call(self, seat: int) -> int:
        st, pi = self.state, self.seat_to_index(seat)
        return max(0, max(st.bets) - st.bets[pi])

    def raise_limits(self) -> tuple[int, int] | None:
        st = self.state
        if st is None or not st.can_complete_bet_or_raise_to():
            return None
        return (st.min_completion_betting_or_raising_to_amount,
                st.max_completion_betting_or_raising_to_amount)

    def act(self, seat: int, kind: str, amount: int | None = None) -> None:
        if not self.in_hand:
            raise EngineError("No hand in progress")
        if self.to_act != seat:
            raise EngineError("Not your turn")
        st = self.state
        self._pre = list(st.stacks)
        if kind == "fold":
            st.fold()
        elif kind == "check":
            if self.to_call(seat) > 0:
                raise EngineError("Cannot check, there is a bet to call")
            st.check_or_call()
        elif kind == "call":
            st.check_or_call()
        elif kind == "raise":
            lim = self.raise_limits()
            if lim is None:
                raise EngineError("Cannot raise now")
            if amount is None or not (lim[0] <= amount <= lim[1]):
                raise EngineError(f"Raise must be between {lim[0]} and {lim[1]}")
            st.complete_bet_or_raise_to(amount)
        else:
            raise EngineError(f"Unknown action '{kind}'")
        self._maybe_finish()

    # ---------- board ----------
    # The engine tracks the board itself. pokerkit only gets the cards while it is
    # expecting them (normal betting). When everyone is all-in it jumps to its own
    # showdown ordering, which doesn't fit camera/dealer flow, so we run the board out
    # ourselves and pay the pots in `showdown()`.
    @property
    def board(self) -> list[str]:
        return list(self._board)

    @property
    def board_needed(self) -> int:
        """How many board cards the engine is waiting for (0 = none)."""
        if not self.in_hand or self.state.actor_index is not None:
            return 0
        if sum(self.state.statuses) < 2:
            return 0
        return {0: 3, 3: 1, 4: 1}.get(len(self._board), 0)

    def deal_board(self, cards: str | list[str]) -> None:
        cards = norm_cards(cards)
        need = self.board_needed
        if need == 0:
            raise EngineError("Board not expected right now")
        if len(cards) != need:
            raise EngineError(f"Expected {need} board card(s), got {len(cards)}")
        if len(set(cards)) != len(cards) or set(self._board) & set(cards):
            raise EngineError("Duplicate card")
        self._board.extend(cards)
        if self.state.can_deal_board():
            self.state.deal_board("".join(cards))
        self._maybe_finish()

    # ---------- showdown ----------
    @property
    def awaiting_showdown(self) -> bool:
        return (self.in_hand and self.state.actor_index is None
                and len(self._board) == 5 and sum(self.state.statuses) >= 2)

    def live_seats(self) -> list[int]:
        if self.state is None:
            return []
        return [self._order[i] for i, on in enumerate(self.state.statuses) if on]

    def showdown(self, reveal: dict[int, str | list[str]]) -> HandResult:
        """reveal: seat -> two hole cards for every non-folded player."""
        if not self.awaiting_showdown:
            raise EngineError("Not at showdown")
        live = self.live_seats()
        if set(reveal) != set(live):
            raise EngineError(f"Need hole cards for seats {sorted(live)}")
        hole = {s: norm_cards(c) for s, c in reveal.items()}
        flat = [c for cs in hole.values() for c in cs] + self._board
        if any(len(cs) != 2 for cs in hole.values()):
            raise EngineError("Each player needs exactly 2 hole cards")
        if len(set(flat)) != len(flat):
            raise EngineError("Duplicate card across hands/board")
        st = self.state
        assert not any(st.bets), "bets should be collected before showdown"
        board = "".join(self._board)
        hands = {pi: StandardHighHand.from_game("".join(hole[seat]), board)
                 for pi, seat in enumerate(self._order) if seat in hole}
        paid = [0] * len(self._order)
        for pot in st.pots:
            elig = [pi for pi in pot.player_indices if pi in hands]
            if not elig:
                continue
            best = max(hands[pi] for pi in elig)
            winners = [pi for pi in elig if hands[pi] == best]   # already in left-of-button order
            share, odd = divmod(pot.amount, len(winners))
            for k, pi in enumerate(winners):
                paid[pi] += share + (1 if k < odd else 0)       # odd chips: first seats left of button
        final = [st.stacks[pi] + paid[pi] for pi in range(len(self._order))]
        names = {seat: hands[pi].entry.label.value
                 for pi, seat in enumerate(self._order) if pi in hands}
        return self._finish(final, paid, showdown=True, names=names, revealed=hole)

    def settle_now(self, board: list[str] | str, reveal: dict[int, str | list[str]]) -> HandResult:
        """Pay out immediately from whatever has been put in so far (no waiting for betting streets).

        `board` = the 5 community cards the camera sees; `reveal` = seat -> 2 hole cards for EVERY player
        still in the hand. Every chip a player has committed (including this round's uncollected bets) goes
        into the pots; side pots are built from the commitments; the best hand among the players eligible
        for each pot wins it; ties split, odd chips to the first seat left of the button. Folded players'
        chips stay in the pot. Always zero-sum.
        """
        if not self.in_hand:
            raise EngineError("No hand in progress")
        board = norm_cards(board)
        if len(board) != 5:
            raise EngineError("Need 5 board cards")
        live = self.live_seats()
        if set(reveal) != set(live):
            raise EngineError(f"Need hole cards for seats {sorted(live)}")
        hole = {s: norm_cards(c) for s, c in reveal.items()}
        if any(len(cs) != 2 for cs in hole.values()):
            raise EngineError("Each player needs exactly 2 hole cards")
        flat = [c for cs in hole.values() for c in cs] + board
        if len(set(flat)) != len(flat):
            raise EngineError("Duplicate card across hands/board")

        st = self.state
        committed = {seat: self._start[seat] - st.stacks[pi] for pi, seat in enumerate(self._order)}
        hands = {seat: StandardHighHand.from_game("".join(hole[seat]), "".join(board)) for seat in live}
        position = {seat: i for i, seat in enumerate(self._order)}           # index 0 = first seat left of the button
        paid = {seat: 0 for seat in self._order}
        won: set[int] = set()                                                  # winners of a CONTESTED pot (not just an uncalled bet coming back)
        prev = 0
        for level in sorted({c for c in committed.values() if c > 0}):
            amount = sum(min(c, level) - min(c, prev) for c in committed.values())
            eligible = [s for s in live if committed[s] >= level] or list(live)   # live players who matched this level
            if amount:
                best = max(hands[s] for s in eligible)
                winners = sorted((s for s in eligible if hands[s] == best), key=position.get)
                share, odd = divmod(amount, len(winners))
                for k, s in enumerate(winners):
                    paid[s] += share + (1 if k < odd else 0)
                if len(eligible) > 1:
                    won.update(winners)
            prev = level
        final = [self._start[seat] - committed[seat] + paid[seat] for seat in self._order]
        paid_list = [paid[seat] if seat in won else 0 for seat in self._order]   # only used to decide who is a winner
        names = {seat: hands[seat].entry.label.value for seat in live}
        self._board = board
        return self._finish(final, paid_list, showdown=True, names=names, revealed=hole)

    # ---------- internals ----------
    def _maybe_finish(self) -> None:
        # pokerkit ends the hand itself only when everyone else folded (HAND_KILLING + CHIPS_PUSHING).
        if self.state is not None and not self.state.status and self.result is None:
            final = list(self.state.stacks)
            paid = [f - p for f, p in zip(final, self._pre)]
            self._finish(final, paid, showdown=False, names={}, revealed={})

    def _finish(self, final: list[int], paid: list[int], showdown: bool,
                names: dict, revealed: dict) -> HandResult:
        deltas, winners = {}, []
        for pi, seat in enumerate(self._order):
            self.players[seat].stack = final[pi]
            deltas[self.players[seat].pubkey] = final[pi] - self._start[seat]
            if paid[pi] > 0:                                    # received pot chips (includes chops)
                winners.append(seat)
        assert sum(deltas.values()) == 0, "zero-sum violated"
        self.result = HandResult(deltas, winners, names, showdown, self.board, revealed)
        return self.result

    def showdown_hash(self, frame_jpeg: bytes = b"") -> str:
        r = self.result
        if r is None:
            raise EngineError("Hand not finished")
        h = hashlib.sha256()
        h.update(str(self.hand_id).encode())
        h.update(",".join(r.board).encode())
        for seat in sorted(r.revealed):
            h.update(f"{seat}:{''.join(r.revealed[seat])}".encode())
        h.update(frame_jpeg)
        return h.hexdigest()


def _hand_name(hole: list[str], board: list[str]) -> str:
    hand = StandardHighHand.from_game("".join(hole), "".join(board))
    return hand.entry.label.value
