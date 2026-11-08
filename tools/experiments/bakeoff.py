"""Score a pretrained card YOLO on our own footage. Usage: bakeoff.py <weights> <videos_root> <imgsz> [frames_per_video]"""
import cv2, glob, os, sys, json, collections
from ultralytics import YOLO
GT = {  # video -> card code (read by eye from contact sheets; unknown/ambiguous videos omitted)
 "6106":"Jc","6107":"8c","6109":"Kc","6112":"Tc","6115":"2c","6118":"Kc","6127":"Qc","6129":"8c","6132":"7c","6143":"Ac","6146":"Ac","6148":"Tc","6158":"5c",
 "6113":"Td","6117":"9d","6121":"Td","6122":"7d","6125":"8d","6133":"Qd","6135":"Ad","6136":"7d","6137":"4d","6138":"6d","6141":"Ad","6145":"5d","6147":"Jd","6159":"Kd",
 "6108":"Qh","6110":"7h","6123":"2h","6124":"5h","6126":"6h","6130":"3h","6131":"9h","6134":"2h","6139":"7h","6140":"8h","6142":"4h","6150":"5h","6151":"3h","6154":"8h",
 "6111":"Ts","6114":"8s","6116":"7s","6119":"As","6153":"8s","6157":"Ks"}
def norm(name):  # '10H','10 of hearts','Ah' etc -> 'Th'
    n = name.strip().lower().replace(" of ","").replace("_","")
    suits = {"hearts":"h","heart":"h","spades":"s","spade":"s","clubs":"c","club":"c","diamonds":"d","diamond":"d"}
    for k,v in suits.items():
        if n.endswith(k): n = n[:-len(k)]+v; break
    r, s = n[:-1].upper(), n[-1]
    r = {"10":"T","1":"A","ACE":"A","KING":"K","QUEEN":"Q","JACK":"J"}.get(r, r)
    return r+s
w, root, imgsz = sys.argv[1], sys.argv[2], int(sys.argv[3]); nf = int(sys.argv[4]) if len(sys.argv)>4 else 12
m = YOLO(w); print("classes:", list(m.names.values())[:8], len(m.names), flush=True)
res = {}; fr_ok = fr_tot = fr_none = 0
for p in sorted(glob.glob(os.path.join(root,"*","**","*.MOV"), recursive=True)):
    vid = os.path.basename(p)[4:8]
    if vid not in GT: continue
    c = cv2.VideoCapture(p); n = int(c.get(7)); votes = collections.Counter(); ok_f = tot = 0
    for i in range(nf):
        c.set(1, int(n*(i+0.5)/nf)); ok, f = c.read()
        if not ok: continue
        r = m.predict(f, imgsz=imgsz, conf=0.25, verbose=False, device="cpu")[0]
        tot += 1
        if len(r.boxes)==0: fr_none += 1; continue
        b = r.boxes[int(r.boxes.conf.argmax())]; code = norm(m.names[int(b.cls)])
        votes[code] += 1; ok_f += code == GT[vid]
    fr_ok += ok_f; fr_tot += tot
    top = votes.most_common(1)[0][0] if votes else None
    res[vid] = (GT[vid], top, ok_f, tot, dict(votes))
    print(vid, GT[vid], "->", top, f"{ok_f}/{tot}", "" if top==GT[vid] else "  <-- MISS", flush=True)
vid_ok = sum(1 for g,t,*_ in res.values() if g==t)
print(f"\nVIDEO vote accuracy {vid_ok}/{len(res)}  |  per-frame {fr_ok}/{fr_tot}  |  no-detection frames {fr_none}")
