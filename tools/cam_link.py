"""One command to get an iPhone (or any phone) camera working over HTTPS.

    python tools/cam_link.py

Starts a free Cloudflare quick tunnel to the local server (cloudflared is downloaded once into
tools/bin, no system install), then opens a QR for  <https-url>/cam  and one for  <https-url>/dealer.
Scan the /cam QR with the camera phone, enter the dealer PIN, tap Start camera. Leave this running.

The tunnel makes the server reachable from the internet at a random URL. Dealer endpoints still need
the PIN (5 wrong tries -> 5 minute lockout); close this window to shut the tunnel.
"""
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import qrcode

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "tools" / "bin" / "cloudflared.exe"
URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

if not EXE.exists():
    EXE.parent.mkdir(parents=True, exist_ok=True)
    print("Downloading cloudflared (once)...")
    urllib.request.urlretrieve(URL, EXE)

proc = subprocess.Popen([str(EXE), "tunnel", "--url", "http://localhost:8000", "--no-autoupdate"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = None
for line in proc.stdout:                                   # the URL is printed in the log banner
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
    if m:
        base = m.group(0)
        break
if not base:
    sys.exit("Could not get a tunnel URL. Is the server running on :8000?")

out = ROOT / "data"
out.mkdir(exist_ok=True)
for name in ("cam", "dealer"):
    img = qrcode.make(f"{base}/{name}")
    try:
        img.save(out / f"{name}_qr.png")
    except OSError:                                        # old QR still open in an image viewer (Windows lock)
        img.save(out / f"{name}_qr_new.png")
        print(f"(could not overwrite data/{name}_qr.png; saved data/{name}_qr_new.png)")
(out / "public_url.txt").write_text(base)        # the game server builds phone-reachable seat QRs from this
print(f"\nCamera phone : {base}/cam      (QR: data/cam_qr.png)")
print(f"Dealer phone : {base}/dealer   (QR: data/dealer_qr.png)")
print(f"Vision source: web:http://127.0.0.1:8000")
if os.name == "nt":
    os.startfile(out / "cam_qr.png")
print("\nTunnel running. Leave this window open. Ctrl+C to stop.")
try:
    proc.wait()
except KeyboardInterrupt:
    proc.terminate()
