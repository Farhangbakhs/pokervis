import cv2, glob, os, sys, numpy as np
root=sys.argv[1]; out=sys.argv[2]; os.makedirs(out,exist_ok=True)
tiles=[]
for p in sorted(glob.glob(os.path.join(root,"*","**","*.MOV"),recursive=True)):
    c=cv2.VideoCapture(p); n=int(c.get(7)); c.set(1,n//2); ok,f=c.read()
    hsv=cv2.cvtColor(f,cv2.COLOR_BGR2HSV)
    m=((hsv[...,1]<60)&(hsv[...,2]>170)).astype(np.uint8)*255
    m=cv2.morphologyEx(m,cv2.MORPH_CLOSE,np.ones((25,25),np.uint8))
    cs,_=cv2.findContours(m,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    if cs:
        x,y,w,h=cv2.boundingRect(max(cs,key=cv2.contourArea)); pad=int(0.08*max(w,h))
        f=f[max(0,y-pad):y+h+pad,max(0,x-pad):x+w+pad]
    if f.shape[1]>f.shape[0]: f=cv2.rotate(f,cv2.ROTATE_90_CLOCKWISE)
    h,w=f.shape[:2]; s=min(300/w,420/h); f=cv2.resize(f,None,fx=s,fy=s)
    t=np.zeros((420,300,3),np.uint8); t[:f.shape[0],:f.shape[1]]=f
    cv2.putText(t,p.replace("\\","/").split("/")[-2][:1].upper()+" "+os.path.basename(p)[4:8],(4,414),cv2.FONT_HERSHEY_SIMPLEX,0.8,(0,255,255),2); tiles.append(t)
while len(tiles)%18: tiles.append(np.zeros_like(tiles[0]))
for i in range(0,len(tiles),18):
    ch=tiles[i:i+18]; cv2.imwrite(f"{out}/card{i//18}.jpg",np.vstack([np.hstack(ch[j:j+6]) for j in range(0,18,6)]))
