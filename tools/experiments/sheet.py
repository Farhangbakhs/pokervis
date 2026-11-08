import cv2, glob, os, sys, numpy as np
d = sys.argv[1]; files = sorted(glob.glob(d+"/*.jpg"))
T=(240,320)
tiles=[]
for f in files:
    im=cv2.imread(f); h,w=im.shape[:2]; s=min(T[0]/w,T[1]/h)
    im=cv2.resize(im,None,fx=s,fy=s); c=np.zeros((T[1],T[0],3),np.uint8); c[:im.shape[0],:im.shape[1]]=im
    cv2.putText(c,os.path.basename(f)[4:8],(4,T[1]-6),cv2.FONT_HERSHEY_SIMPLEX,0.7,(0,255,255),2); tiles.append(c)
while len(tiles)%9: tiles.append(np.zeros_like(tiles[0]))
for i in range(0,len(tiles),27):
    ch=tiles[i:i+27]; rows=[np.hstack(ch[j:j+9]) for j in range(0,len(ch),9)]
    cv2.imwrite(f"{d}/sheet{i//27}.jpg",np.vstack(rows))
print(len(files))
