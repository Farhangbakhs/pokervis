"""A bot opponent that is a REAL Solana devnet wallet. Nothing is faked:

  1. it keeps its own ed25519 keypair (data/bot_key_<name>.txt) - its address is its identity and its Tiger history key
  2. if the wallet has no devnet SOL, the HOUSE wallet sends it some (a real, visible devnet transfer)
  3. it BUYS IN: the server builds the SOL transfer, the bot signs it with its own key and sends it to devnet;
     the server verifies that transaction on chain and only then credits chips
  4. it signs the login challenge, sits down, and plays: mostly check/call, sometimes raise, sometimes fold
  5. when busted it buys in again with a new payment

    python tools/bot_player.py                       # takes the first free seat
    python tools/bot_player.py --seat 2 --name Botty --style loose [--server http://127.0.0.1:8000]

Cards are physical: at showdown put the bot's two cards face up in ITS seat area (or the dealer overrides).
"""
import argparse
import base64
import os
import random
import sys
import time
from pathlib import Path

import base58
import httpx
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import config  # noqa: E402

STYLES = {  # (fold prob when facing a bet, raise prob when it may raise)
    "station": (0.0, 0.0),
    "normal": (0.30, 0.12),
    "loose": (0.10, 0.25),
}

ap = argparse.ArgumentParser()
ap.add_argument("--seat", type=int, default=0, help="0 = first free seat")
ap.add_argument("--name", default="Botty")
ap.add_argument("--style", choices=STYLES, default="normal")
ap.add_argument("--server", default="http://127.0.0.1:8000")
a = ap.parse_args()
fold_p, raise_p = STYLES[a.style]

RPC = os.environ.get("SOLANA_RPC", "https://api.devnet.solana.com")
if "devnet" not in RPC:
    sys.exit("devnet only")
c = httpx.Client(base_url=a.server, timeout=60)
rpc = httpx.Client(timeout=30)
chain = httpx.Client(base_url=config.get("CHAIN_URL", "http://127.0.0.1:8787"), headers={"x-api-key": config.get("CHAIN_API_KEY")}, timeout=90)

data_dir = Path(__file__).resolve().parent.parent / "data"
data_dir.mkdir(exist_ok=True)
keyfile = data_dir / f"bot_key_{a.name.lower()}.txt"
if keyfile.exists():
    sk = SigningKey(base64.b64decode(keyfile.read_text().strip()))
else:
    sk = SigningKey.generate()
    keyfile.write_text(base64.b64encode(bytes(sk)).decode())
pub = base58.b58encode(bytes(sk.verify_key)).decode()


def rpc_call(method: str, params: list):
    r = rpc.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()
    if "error" in r:
        raise RuntimeError(r["error"].get("message", str(r["error"])))
    return r["result"]


def dealer_headers() -> dict:
    tok = c.post("/api/dealer/login", json={"pin": config.get("DEALER_PIN")}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def bankroll() -> int:
    return int(c.get("/api/bankroll", params={"pubkey": pub}).json()["chips"])


def buy_in(chips: int) -> None:
    """Pay devnet SOL to the table from THIS wallet, then have the server verify the transaction on chain."""
    need = chips * 10_000 + 50_000
    try:
        need = chips * int(c.get("/api/state").json()["sol"]["lamports_per_chip"]) + 50_000
    except Exception:  # noqa: BLE001
        pass
    have = rpc_call("getBalance", [pub, {"commitment": "confirmed"}])["value"]
    if have < need:
        f = chain.post("/fund", json={"pubkey": pub, "lamports": max(need * 2, 30_000_000)})
        if f.status_code != 200:
            raise RuntimeError(f"cannot fund the bot wallet {pub}: {f.json().get('error')}. Send it devnet SOL from faucet.solana.com")
        print(f"{a.name}: house funded the bot wallet -> {f.json()['explorer_url']}")
    prep = c.post("/api/sol/prepare", json={"pubkey": pub, "chips": chips}).json()
    raw = base64.b64decode(prep["tx"])
    n = raw[0]                                           # number of signatures (1: the bot is fee payer and sender)
    message = raw[1 + 64 * n:]
    signed = raw[:1] + sk.sign(message).signature + raw[1 + 64:]
    sig = rpc_call("sendTransaction", [base64.b64encode(signed).decode(), {"encoding": "base64", "preflightCommitment": "confirmed"}])
    print(f"{a.name}: BUY-IN {chips} CHIP for {prep['lamports'] / 1e9} SOL -> https://explorer.solana.com/tx/{sig}?cluster=devnet")
    r = c.post("/api/sol/confirm", json={"pubkey": pub, "signature": sig})
    if r.status_code != 200:
        raise RuntimeError(r.json().get("detail", r.text))
    print(f"{a.name}: verified on chain, bankroll {r.json()['balance']} CHIP")


def sit() -> dict | None:
    """Make sure the wallet has paid for chips, sign the login challenge, take a seat."""
    dh = dealer_headers()
    st = c.get("/api/dealer/state", headers=dh).json()
    mine = next((s["seat"] for s in st["seats"] if s.get("occupied") and s.get("pubkey") == pub), None)
    seat = mine or a.seat or next((s["seat"] for s in st["seats"] if not s["occupied"]), 0)
    if not seat or (not mine and st["seats"][seat - 1]["occupied"]):
        return None
    if not mine and bankroll() <= 0:
        buy_in(st["buy_in"])
    ch = c.post("/api/auth/challenge", json={"pubkey": pub}).json()
    sig = base64.b64encode(sk.sign(ch["message"].encode()).signature).decode()
    r = c.post("/api/auth/login", json={"pubkey": pub, "nonce": ch["nonce"], "signature": sig, "seat": seat,
                                        "seat_code": st["seat_codes"][str(seat)], "name": a.name})
    if r.status_code != 200:
        print("cannot sit yet:", r.json().get("detail", r.text))
        return None
    print(f"{a.name} seated at {seat} with {r.json()['stack']} CHIP · wallet {pub}")
    return {"Authorization": f"Bearer {r.json()['token']}"}


def decide(me: dict) -> tuple[str, int | None]:
    to_call, seat = me["to_call"], me["seat"]
    can_raise = me["min_raise_to"] is not None and seat["stack"] > to_call
    if to_call and random.random() < fold_p:
        return "fold", None
    if can_raise and random.random() < raise_p:
        lo, hi = me["min_raise_to"], me["max_raise_to"]
        return "raise", min(hi, lo + random.choice([0, 0, 10, 20, 40]))
    return ("call" if to_call else "check"), None


h, last = None, None
print(f"{a.name} wallet: {pub}  (https://explorer.solana.com/address/{pub}?cluster=devnet)")
while True:
    try:
        if h is None:
            h = sit()
            if h is None:
                time.sleep(3)
                continue
        r = c.get("/api/me", headers=h)
        if r.status_code in (401, 404):           # kicked / table restarted / token expired: sit down again
            h = None
            time.sleep(2)
            continue
        me = r.json()
        if me["phase"] == "waiting" and me["seat"]["stack"] == 0:
            buy_in(c.get("/api/state").json()["buy_in"])        # busted: a new, real payment
            c.post("/api/sync", headers=h)
        elif me["seat"]["to_act"] and me["phase"] == "betting":
            kind, amt = decide(me)
            resp = c.post("/api/action", json={"type": kind, "amount": amt}, headers=h)
            if resp.status_code == 400 and kind == "raise":
                c.post("/api/action", json={"type": "call" if me["to_call"] else "check"}, headers=h)
            if (kind, me["phase"], me["pot"]) != last:
                print(f"{a.name}: {kind}{' ' + str(amt) if amt else ''}   (stack {me['seat']['stack']}, pot {me['pot']})")
                last = (kind, me["phase"], me["pot"])
    except (httpx.HTTPError, KeyError, ValueError, RuntimeError) as e:
        print("hiccup:", e)
        if isinstance(e, (KeyError, RuntimeError)):
            h = None
            time.sleep(5)
    time.sleep(0.7)
