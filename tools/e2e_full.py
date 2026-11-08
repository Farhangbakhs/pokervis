"""Full-stack hand against a RUNNING server + chain service (real devnet if the server uses it):
2 phone-style players sign in, play a hand, the dealer feeds board + showdown, the winner is paid on-chain.

    python tools/e2e_full.py [--server http://127.0.0.1:8000]
"""
import argparse, base64, sys, time
from pathlib import Path
import base58, httpx
from nacl.signing import SigningKey
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import config

ap = argparse.ArgumentParser(); ap.add_argument("--server", default="http://127.0.0.1:8000"); a = ap.parse_args()
c = httpx.Client(base_url=a.server, timeout=60)
dh = {"Authorization": "Bearer " + c.post("/api/dealer/login", json={"pin": config.get("DEALER_PIN")}).json()["token"]}
st = c.get("/api/dealer/state", headers=dh).json()
print("chain mode:", st["mode"])
if st["phase"] == "done":
    c.post("/api/dealer/next_hand", headers=dh)

def sit(seat, name):
    sk = SigningKey.generate(); pub = base58.b58encode(bytes(sk.verify_key)).decode()
    ch = c.post("/api/auth/challenge", json={"pubkey": pub}).json()
    sig = base64.b64encode(sk.sign(ch["message"].encode()).signature).decode()
    code = c.get("/api/dealer/state", headers=dh).json()["seat_codes"][str(seat)]
    r = c.post("/api/auth/login", json={"pubkey": pub, "nonce": ch["nonce"], "signature": sig, "seat": seat, "seat_code": code, "name": name})
    r.raise_for_status(); return {"h": {"Authorization": "Bearer " + r.json()["token"]}, "pub": pub, "seat": seat}

# use two free seats
free = [s["seat"] for s in st["seats"] if not s["occupied"]][:2]
A, B = sit(free[0], "Alice"), sit(free[1], "Bob")
by = {A["seat"]: A, B["seat"]: B}
print("seated:", free)
c.post("/api/dealer/start_hand", json={}, headers=dh).raise_for_status()

def checkdown():
    while True:
        s = c.get("/api/state").json()
        if s["to_act"] is None: return s
        p = by[s["to_act"]]; me = c.get("/api/me", headers=p["h"]).json()
        c.post("/api/action", json={"type": "call" if me["to_call"] else "check"}, headers=p["h"]).raise_for_status()

checkdown()
for cards in (["As", "Kd", "7c"], ["2h"], ["9s"]):
    c.post("/api/dealer/confirm_board", json={"cards": cards}, headers=dh).raise_for_status(); checkdown()
t0 = time.time()
r = c.post("/api/dealer/confirm_showdown", json={"reveal": {str(A["seat"]): "AdAh", str(B["seat"]): "QsQd"}}, headers=dh)
r.raise_for_status()
s = c.get("/api/state").json()
print(f"winner seat(s): {s['result']['winners']}  hand: {s['result']['hand_names']}  settled in {time.time()-t0:.1f}s")
while s["settlement"] is None and time.time() - t0 < 60:     # the payout confirms in the background
    assert not s["settle_error"], s["settle_error"]
    time.sleep(0.5); s = c.get("/api/state").json()
print(f"payout confirmed after {time.time()-t0:.1f}s: {s['settlement']['explorer_url']}")
assert s["result"]["winners"] == [A["seat"]], "Alice (aces) should win"
c.post("/api/dealer/next_hand", headers=dh)
for p, n in ((A, "Alice"), (B, "Bob")):
    c.post("/api/cashout", headers=p["h"])                 # log out: chips stay in the wallet
    h = c.get("/api/history", params={"pubkey": p["pub"]}).json()
    print(n, "history:", h["stats"], "points:", len(h["series"]))
print("both logged out. FULL STACK OK")
