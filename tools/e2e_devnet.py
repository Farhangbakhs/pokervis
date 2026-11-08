"""Real devnet check of the custodial chain service: 2 wallets join, one hand settles, one cashes out.

    python tools/e2e_devnet.py      (chain service must be running: cd chain/service && npm start)
"""
import hashlib
import sys
import time
from pathlib import Path

import base58
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import config  # noqa: E402
from server.chain_client import ChainError, HttpChain  # noqa: E402

chain = HttpChain(config.get("CHAIN_URL"), config.get("CHAIN_API_KEY"))
mk = lambda: base58.b58encode(bytes(SigningKey.generate().verify_key)).decode()  # noqa: E731
alice, bob = mk(), mk()
hand = f"e2e-{int(time.time())}"
print("alice", alice, "\nbob  ", bob)

for name, pk in (("alice", alice), ("bob", bob)):
    r = chain.join(pk, 1000)
    print(f"join {name}: balance {r['balance']}  {r['explorer_url']}")

chain.begin_hand(hand)
h = hashlib.sha256(b"showdown").hexdigest()
for bad, why in (({alice: 50, bob: -40}, "non-zero-sum"), ({alice: 2000, bob: -2000}, "overdraw")):
    try:
        chain.settle(hand, bad, h)
        sys.exit(f"FAIL: {why} settlement was accepted!")
    except ChainError as e:
        print(f"rejected {why}: {e}")

t0 = time.time()
r = chain.settle(hand, {alice: 150, bob: -150}, h)
print(f"settled in {time.time() - t0:.1f}s: {r['explorer_url']}")
r2 = chain.settle(hand, {alice: 150, bob: -150}, h)
assert r2.get("replay") and r2["signature"] == r["signature"], "replay must be idempotent"
print("replay returns same signature (idempotent)")

bal = chain.balances()
print("balances:", bal[alice], bal[bob])
assert bal[alice] == 1150 and bal[bob] == 850
c = chain.cashout(alice, None)
print(f"alice cashed out {c['amount']} CHIP to her wallet: {c['explorer_url']}")
print("\nALL DEVNET CHECKS PASSED")
