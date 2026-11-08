import cv2, sys, numpy as np
p, out, k = sys.argv[1], sys.argv[2], int(sys.argv[3])
c = cv2.VideoCapture(p); n = int(c.get(7)); t = []
for i in range(k):
    c.set(1, int(n*(i+0.5)/k)); ok, f = c.read()
    if ok:
        h, w = f.shape[:2]; s = 420/max(h, w); t.append(cv2.resize(f, None, fx=s, fy=s))
H = max(x.shape[0] for x in t); t = [cv2.copyMakeBorder(x, 0, H-x.shape[0], 0, 0, cv2.BORDER_CONSTANT) for x in t]
cv2.imwrite(out, np.hstack(t))
