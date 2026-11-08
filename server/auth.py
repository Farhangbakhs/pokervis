"""Player sign-in by wallet signature + signed session tokens.

Flow (no passwords, nothing to install on the phone):
  1. phone has an ed25519 keypair (guest burner in localStorage, or a wallet that can signMessage)
  2. POST /api/auth/challenge {pubkey}  -> {nonce, message}
  3. phone signs `message`               -> POST /api/auth/login {pubkey, nonce, signature, seat, seat_code, name}
  4. server verifies the signature (proves key ownership), checks the physical seat code,
     and returns a short-lived HMAC token bound to (pubkey, seat). All later calls send it as a Bearer token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


class AuthError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode_pubkey(pubkey: str) -> bytes:
    try:
        raw = base58.b58decode(pubkey)
    except Exception as e:  # noqa: BLE001
        raise AuthError("invalid pubkey") from e
    if len(raw) != 32:
        raise AuthError("invalid pubkey")
    return raw


def decode_signature(sig: str) -> bytes:
    """Accept base64 / base64url / base58 signatures (wallets differ)."""
    for fn in (lambda s: base64.b64decode(s + "=" * (-len(s) % 4), validate=True),
               _unb64, base58.b58decode):
        try:
            raw = fn(sig)
            if len(raw) == 64:
                return raw
        except Exception:  # noqa: BLE001
            continue
    raise AuthError("invalid signature encoding")


class Auth:
    CHALLENGE_TTL = 120
    TOKEN_TTL = 24 * 3600

    def __init__(self, secret: str):
        if len(secret) < 24:
            raise ValueError("SESSION_SECRET too short")
        self._key = secret.encode()
        self._challenges: dict[str, tuple[str, float]] = {}   # nonce -> (pubkey, expiry)

    # ----- challenge / response -----
    def challenge(self, pubkey: str) -> dict:
        decode_pubkey(pubkey)
        self._gc()
        nonce = secrets.token_hex(16)
        self._challenges[nonce] = (pubkey, time.time() + self.CHALLENGE_TTL)
        return {"nonce": nonce, "message": self.message(nonce, pubkey)}

    @staticmethod
    def message(nonce: str, pubkey: str) -> str:
        return f"AutoPoker sign-in\nnonce:{nonce}\npubkey:{pubkey}"

    def verify_login(self, pubkey: str, nonce: str, signature: str) -> None:
        """Single-use nonce; signature must be over the exact challenge message."""
        entry = self._challenges.pop(nonce, None)
        if entry is None or entry[0] != pubkey or entry[1] < time.time():
            raise AuthError("challenge expired or unknown")
        try:
            VerifyKey(decode_pubkey(pubkey)).verify(self.message(nonce, pubkey).encode(), decode_signature(signature))
        except BadSignatureError as e:
            raise AuthError("bad signature") from e

    def _gc(self) -> None:
        now = time.time()
        for n in [n for n, (_, exp) in self._challenges.items() if exp < now]:
            del self._challenges[n]

    # ----- tokens -----
    def issue(self, claims: dict, ttl: int | None = None) -> str:
        body = dict(claims, exp=int(time.time() + (ttl or self.TOKEN_TTL)))
        payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
        sig = _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())
        return f"{payload}.{sig}"

    def check(self, token: str | None) -> dict:
        if not token or "." not in token:
            raise AuthError("missing token")
        payload, sig = token.rsplit(".", 1)
        good = _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            raise AuthError("bad token")
        claims = json.loads(_unb64(payload))
        if claims.get("exp", 0) < time.time():
            raise AuthError("token expired")
        return claims
