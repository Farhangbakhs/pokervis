"""A bot opponent so ONE real phone can play. It scans in at a seat like a phone would
(wallet signature + seat code), then calls/checks every street (never folds or raises).

    python tools/bot_player.py --seat 2 --name Botty [--server http://127.0.0.1:8000]
"""
import argparse
import base64
import sys
import time
from pathlib import Path

import base58
import httpx
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import config  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--seat", type=int, default=2)
ap.add_argument("--name", default="Botty")
ap.add_argument("--server", default="http://127.0.0.1:8000")
a = ap.parse_args()

c = httpx.Client(base_url=a.server, timeout=10)
dealer = c.post("/api/dealer/login", json={"pin": config.get("DEALER_PIN")}).json()["token"]
code = c.get("/api/dealer/state", headers={"Authorization": f"Bearer {dealer}"}).json()["seat_codes"][str(a.seat)]

keyfile = Path(__file__).resolve().parent.parent / "data" / f"bot_key_seat{a.seat}.txt"
if keyfile.exists():
    sk = SigningKey(base64.b64decode(keyfile.read_text().strip()))     # same wallet as last time: rejoins its own seat
else:
    sk = SigningKey.generate()
    keyfile.parent.mkdir(exist_ok=True)
    keyfile.write_text(base64.b64encode(bytes(sk)).decode())
pub = base58.b58encode(bytes(sk.verify_key)).decode()
ch = c.post("/api/auth/challenge", json={"pubkey": pub}).json()
sig = base64.b64encode(sk.sign(ch["message"].encode()).signature).decode()
r = c.post("/api/auth/login", json={"pubkey": pub, "nonce": ch["nonce"], "signature": sig,
                                    "seat": a.seat, "seat_code": code, "name": a.name})
r.raise_for_status()
h = {"Authorization": f"Bearer {r.json()['token']}"}
print(f"{a.name} seated at {a.seat} with {r.json()['stack']} CHIP (wallet {pub[:6]}...). Ctrl+C to leave.")

last = None
while True:
    try:
        me = c.get("/api/me", headers=h).json()
        if me["seat"]["to_act"]:
            kind = "call" if me["to_call"] else "check"
            c.post("/api/action", json={"type": kind}, headers=h)
            if (kind, me["phase"], me["pot"]) != last:
                print(f"{a.name}: {kind}")
                last = (kind, me["phase"], me["pot"])
    except httpx.HTTPError as e:
        print("server error:", e)
    time.sleep(0.7)
