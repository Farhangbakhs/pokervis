import cv2, glob, os, sys
root = sys.argv[1]; out = sys.argv[2]
os.makedirs(out, exist_ok=True)
for p in sorted(glob.glob(os.path.join(root, "*", "**", "*.MOV"), recursive=True)):
    c = cv2.VideoCapture(p)
    n = int(c.get(cv2.CAP_PROP_FRAME_COUNT)); fps = c.get(cv2.CAP_PROP_FPS)
    w = int(c.get(3)); h = int(c.get(4))
    c.set(cv2.CAP_PROP_POS_FRAMES, n//2); ok, f = c.read()
    suit = p.replace("\\","/").split("/")[-3 if p.count("/")+p.count("\\")>3 else -2]
    name = os.path.splitext(os.path.basename(p))[0]
    print(suit, name, n, round(fps,1), f"{w}x{h}", round(n/fps,1), "s", flush=True)
    if ok: cv2.imwrite(os.path.join(out, f"{name}.jpg"), cv2.resize(f, None, fx=0.25, fy=0.25))
