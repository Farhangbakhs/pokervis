"""Build a kNN rank-gallery from our footage and test it leave-one-video-out (LOVO).

Idea: pretrained YOLO is good at suit + most ranks but reads our stylised Queen as a 9.
We crop the YOLO box, embed it (ImageNet ResNet18, no training), and use nearest neighbours
from our own labelled footage to decide the RANK. Suit stays with YOLO.

Usage: gallery_eval.py <weights> <videos_root> <out_npz> [frames_per_video]
"""
import cv2, glob, os, sys, collections
import numpy as np, torch, torchvision
from ultralytics import YOLO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bakeoff_labels import GT, norm  # shared labels

w, root, out = sys.argv[1], sys.argv[2], sys.argv[3]
nf = int(sys.argv[4]) if len(sys.argv) > 4 else 12
yolo = YOLO(w)
net = torchvision.models.resnet18(weights=torchvision.models.ResNet18_Weights.IMAGENET1K_V1)
net.fc = torch.nn.Identity(); net.eval()
mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1); std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)


def embed(crop_bgr):
    """Upright-ish card crop -> L2-normalised 512-d vector. Averages the crop and its 180 deg rotation."""
    vs = []
    for c in (crop_bgr, cv2.rotate(crop_bgr, cv2.ROTATE_180)):
        c = cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2RGB), (160, 224))
        t = (torch.from_numpy(c).permute(2, 0, 1).float().unsqueeze(0) / 255 - mean) / std
        with torch.no_grad():
            vs.append(net(t)[0].numpy())
    v = np.mean(vs, 0)
    return v / (np.linalg.norm(v) + 1e-9)


vids, seen = [], set()
for p in sorted(glob.glob(os.path.join(root, "*", "**", "*.MOV"), recursive=True)):
    vid = os.path.basename(p)[4:8]
    if vid in GT and vid not in seen:
        seen.add(vid); vids.append((vid, p))

E, V, L, YC = [], [], [], []
for vid, p in vids:
    c = cv2.VideoCapture(p); n = int(c.get(7))
    for i in range(nf):
        c.set(1, int(n * (i + 0.5) / nf)); ok, f = c.read()
        if not ok: continue
        r = yolo.predict(f, imgsz=640, conf=0.25, verbose=False, device="cpu")[0]
        if len(r.boxes) == 0: continue
        b = r.boxes[int(r.boxes.conf.argmax())]
        x1, y1, x2, y2 = [int(v) for v in b.xyxy[0].tolist()]
        pad = int(0.05 * max(x2 - x1, y2 - y1))
        crop = f[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]
        if crop.size == 0: continue
        if crop.shape[1] > crop.shape[0]: crop = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
        E.append(embed(crop)); V.append(vid); L.append(GT[vid]); YC.append(norm(yolo.names[int(b.cls)]))
    print("done", vid, GT[vid], flush=True)

E = np.array(E); np.savez(out, E=E, V=np.array(V), L=np.array(L), YC=np.array(YC))

# ---- leave-one-video-out kNN on RANK; suit from YOLO ----
V, L, YC = np.array(V), np.array(L), np.array(YC)
ok_yolo = ok_hyb = 0; miss = []
for i in range(len(E)):
    m = V != V[i]                                  # exclude the whole held-out video
    sims = E[m] @ E[i]; idx = np.argsort(-sims)[:5]
    rank = collections.Counter(L[m][j][0] for j in idx).most_common(1)[0][0]
    pred = rank + YC[i][1]                         # kNN rank + YOLO suit
    ok_yolo += YC[i] == L[i]; ok_hyb += pred == L[i]
    if pred != L[i]: miss.append((V[i], L[i], YC[i], pred, round(float(sims[idx[0]]), 3)))
print(f"\nframes {len(E)} | YOLO alone {ok_yolo/len(E):.1%} | YOLO-suit + kNN-rank (LOVO) {ok_hyb/len(E):.1%}")
by = collections.Counter((m[0], m[1]) for m in miss)
print("hybrid misses by (video, truth):", dict(by))
