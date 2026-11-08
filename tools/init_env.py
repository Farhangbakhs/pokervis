"""Create .env with random secrets if missing (never overwrites existing values).

    python tools/init_env.py

Keys: CHAIN_API_KEY (game server <-> chain service), SESSION_SECRET (signs player tokens),
DEALER_PIN (dealer console login). .env is gitignored.
"""
import secrets
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
defaults = {
    "CHAIN_API_KEY": lambda: secrets.token_urlsafe(32),
    "SESSION_SECRET": lambda: secrets.token_urlsafe(48),
    "DEALER_PIN": lambda: str(secrets.randbelow(900000) + 100000),
    "CHAIN_URL": lambda: "http://127.0.0.1:8787",
    "SOLANA_RPC": lambda: "https://api.devnet.solana.com",
    "BUY_IN": lambda: "1000",
    "MAX_BUYIN": lambda: "5000",
}
cur = {}
if ENV.exists():
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
added = [k for k in defaults if k not in cur]
for k in added:
    cur[k] = defaults[k]()
ENV.write_text("\n".join(f"{k}={v}" for k, v in cur.items()) + "\n")
print(f"{ENV}: added {added or 'nothing (already complete)'}")
if "DEALER_PIN" in added:
    print(f"Dealer PIN for /dealer login: {cur['DEALER_PIN']}")
