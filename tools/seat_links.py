"""Print (and save as QR PNGs) the per-seat join links. Print one QR and tape it at each seat.

    python tools/seat_links.py [--base https://my-tunnel.trycloudflare.com] [--server http://127.0.0.1:8000]

The seat code in each link is the "physical presence" check: only someone who can see the
QR at that seat can claim it. Codes rotate when a player cashes out.
"""
import argparse
import socket
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import config  # noqa: E402


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


ap = argparse.ArgumentParser()
ap.add_argument("--server", default="http://127.0.0.1:8000")
ap.add_argument("--base", default=None, help="public base URL phones use (default: http://<LAN IP>:8000)")
a = ap.parse_args()

tok = httpx.post(f"{a.server}/api/dealer/login", json={"pin": config.get("DEALER_PIN")}).json()["token"]
codes = httpx.get(f"{a.server}/api/dealer/state", headers={"Authorization": f"Bearer {tok}"}).json()["seat_codes"]
base = (a.base or f"http://{lan_ip()}:8000").rstrip("/")
out = Path(__file__).resolve().parent.parent / "data" / "seat_qr"
out.mkdir(parents=True, exist_ok=True)
try:
    import qrcode
except ImportError:
    qrcode = None
for seat, code in sorted(codes.items(), key=lambda kv: int(kv[0])):
    url = f"{base}/play/{seat}?code={code}"
    print(f"Seat {seat}: {url}")
    if qrcode:
        qrcode.make(url).save(out / f"seat{seat}.png")
if qrcode:
    print(f"QR images: {out}")
print("Note: wallet-capable browsers want HTTPS; use `cloudflared tunnel --url http://localhost:8000` and pass --base.")
