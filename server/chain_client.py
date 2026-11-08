"""Chain clients with one interface: MockChain (in-memory, for tests/fallback) and HttpChain (the TS service)."""
from __future__ import annotations

import hashlib
from typing import Protocol

import httpx


class ChainError(Exception):
    pass


class Chain(Protocol):
    mode: str

    def join(self, pubkey: str, amount: int) -> dict: ...
    def cashout(self, pubkey: str, amount: int | None = None) -> dict: ...
    def begin_hand(self, hand_id: str) -> dict: ...
    def abort_hand(self, hand_id: str) -> dict: ...
    def settle(self, hand_id: str, deltas: dict[str, int], showdown_hash: str) -> dict: ...
    def balances(self) -> dict[str, int]: ...


class MockChain:
    """Same contract as the real service, entirely in memory. Enforces the same invariants."""
    mode = "mock"

    def __init__(self) -> None:
        self.bal: dict[str, int] = {}
        self.begun: str | None = None
        self.settled: dict[str, dict] = {}

    def join(self, pubkey: str, amount: int) -> dict:
        self.bal[pubkey] = self.bal.get(pubkey, 0) + amount
        return {"balance": self.bal[pubkey], "signature": "mock-join", "explorer_url": ""}

    def cashout(self, pubkey: str, amount: int | None = None) -> dict:
        if self.begun:
            raise ChainError("hand in progress")
        amt = self.bal.get(pubkey, 0) if amount is None else amount
        if not 1 <= amt <= self.bal.get(pubkey, 0):
            raise ChainError("bad cash-out amount")
        self.bal[pubkey] -= amt
        return {"amount": amt, "balance": self.bal[pubkey], "signature": "mock-cashout", "explorer_url": ""}

    def begin_hand(self, hand_id: str) -> dict:
        if hand_id in self.settled:
            raise ChainError("hand_id already settled")
        if self.begun and self.begun != hand_id:
            raise ChainError(f"hand {self.begun} still open")           # same rule as the real chain service
        self.begun = hand_id
        return {"ok": True}

    def abort_hand(self, hand_id: str) -> dict:
        if self.begun != hand_id:
            raise ChainError("that hand is not open")
        self.begun = None
        return {"ok": True}

    def settle(self, hand_id: str, deltas: dict[str, int], showdown_hash: str) -> dict:
        if hand_id in self.settled:
            return {**self.settled[hand_id], "replay": True}
        if self.begun != hand_id:
            raise ChainError("hand not begun")
        if sum(deltas.values()) != 0:
            raise ChainError("deltas must sum to 0")
        for k, v in deltas.items():
            if self.bal.get(k, 0) + v < 0:
                raise ChainError(f"insufficient balance for {k}")
        for k, v in deltas.items():
            self.bal[k] = self.bal.get(k, 0) + v
        sig = "mock-" + hashlib.sha256(f"{hand_id}{showdown_hash}".encode()).hexdigest()[:16]
        rec = {"signature": sig, "explorer_url": f"https://explorer.solana.com/tx/{sig}?cluster=devnet"}
        self.settled[hand_id] = rec
        self.begun = None
        return rec

    def balances(self) -> dict[str, int]:
        return dict(self.bal)


class HttpChain:
    """Talks to chain/service (loopback, shared-secret header)."""
    mode = "custodial"

    def __init__(self, url: str, api_key: str, timeout: float = 60.0):
        self.c = httpx.Client(base_url=url, headers={"x-api-key": api_key}, timeout=timeout)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            r = self.c.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise ChainError(f"chain service unreachable: {e}") from e
        if r.status_code != 200:
            raise ChainError(r.json().get("error", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text)
        return r.json()

    def join(self, pubkey: str, amount: int) -> dict:
        return self._call("POST", "/join", {"pubkey": pubkey, "amount": amount})

    def cashout(self, pubkey: str, amount: int | None = None) -> dict:
        return self._call("POST", "/cashout", {"pubkey": pubkey, "amount": amount})

    def begin_hand(self, hand_id: str) -> dict:
        return self._call("POST", "/begin_hand", {"hand_id": hand_id})

    def abort_hand(self, hand_id: str) -> dict:
        return self._call("POST", "/abort_hand", {"hand_id": hand_id})

    def settle(self, hand_id: str, deltas: dict[str, int], showdown_hash: str) -> dict:
        return self._call("POST", "/settle", {"hand_id": hand_id, "deltas": deltas, "showdown_hash": showdown_hash})

    def balances(self) -> dict[str, int]:
        return self._call("GET", "/balances")["balances"]
