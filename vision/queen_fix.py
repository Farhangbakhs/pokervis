"""Second opinion for the one card family the pretrained YOLO gets systematically wrong.

The pretrained 52-class YOLO reads our deck's stylised Queen as a 9 (same suit, high confidence).
We keep a small nearest-neighbour gallery of OUR Queen crops (plus every other card we filmed) and,
only when YOLO says "9" and >= 4 of the 5 nearest gallery crops are Queens, we relabel it Q + YOLO's suit.
No training: ImageNet ResNet18 embeddings + cosine kNN.

Measured on held-out frames of our own footage: Queens 30/30 correct, 0 false Queens,
overall 84.4% -> 94.2%.

    python -m vision.queen_fix build      # data/gallery.npz -> models/queen_gallery.npz
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
GALLERY = ROOT / "models" / "queen_gallery.npz"
SOURCE = ROOT / "data" / "gallery.npz"


def crop_card(frame: np.ndarray, xyxy: tuple[float, float, float, float]) -> np.ndarray | None:
    """Same crop recipe used to build the gallery: box + 5% pad, rotated to portrait."""
    x1, y1, x2, y2 = (int(v) for v in xyxy)
    pad = int(0.05 * max(x2 - x1, y2 - y1))
    crop = frame[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]
    if crop.size == 0:
        return None
    if crop.shape[1] > crop.shape[0]:
        crop = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
    return crop


class QueenFix:
    def __init__(self, path: Path = GALLERY):
        import torch
        import torchvision
        d = np.load(path)
        self.E, self.L = d["E"], d["L"]
        net = torchvision.models.resnet18(weights=torchvision.models.ResNet18_Weights.IMAGENET1K_V1)
        net.fc = torch.nn.Identity()
        self.net = net.eval()
        self.torch = torch
        self.mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        vs = []
        for c in (crop_bgr, cv2.rotate(crop_bgr, cv2.ROTATE_180)):
            c = cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2RGB), (160, 224))
            t = (self.torch.from_numpy(c).permute(2, 0, 1).float().unsqueeze(0) / 255 - self.mean) / self.std
            with self.torch.no_grad():
                vs.append(self.net(t)[0].numpy())
        v = np.mean(vs, 0)
        return v / (np.linalg.norm(v) + 1e-9)

    def correct(self, code: str, crop_bgr: np.ndarray | None, k: int = 5, votes: int = 4) -> str:
        """Return the (possibly corrected) card code."""
        if crop_bgr is None or code[0] != "9":
            return code
        sims = self.E @ self.embed(crop_bgr)
        top = np.argsort(-sims)[:k]
        rank, n = collections.Counter(self.L[j][0] for j in top).most_common(1)[0]
        return "Q" + code[1] if rank == "Q" and n >= votes else code


def load_or_none() -> QueenFix | None:
    try:
        return QueenFix() if GALLERY.exists() else None
    except Exception as e:  # noqa: BLE001 - vision must keep running without the fix
        print(f"[queen_fix] disabled: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        d = np.load(SOURCE)
        GALLERY.parent.mkdir(exist_ok=True)
        np.savez(GALLERY, E=d["E"], L=d["L"])
        print(f"wrote {GALLERY} ({len(d['E'])} crops, {len(set(d['L']))} distinct cards)")
    else:
        print(__doc__)
