"""QR code that opens the dealer console on a phone (same Wi-Fi as the laptop).

    python tools/dealer_qr.py [--base https://my-tunnel.trycloudflare.com]

The QR holds only the URL. The phone still asks for the dealer PIN, so a stray photo of the QR
gives nobody access.
"""
import argparse
import os
import socket
from pathlib import Path

import qrcode


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
ap.add_argument("--base", default=None, help="public base URL (default http://<LAN IP>:8000)")
a = ap.parse_args()
base = (a.base or f"http://{lan_ip()}:8000").rstrip("/")
url = f"{base}/dealer"
out = Path(__file__).resolve().parent.parent / "data" / "dealer_qr.png"
out.parent.mkdir(exist_ok=True)
qrcode.make(url).save(out)
print(f"Dealer URL: {url}\nQR image:   {out}")
if os.name == "nt":
    os.startfile(out)  # opens in the default image viewer
