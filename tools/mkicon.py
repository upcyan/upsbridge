
import zlib, struct, sys

def png(path, size):
    # 深色圆角底 + 电池形状，蓝色
    W=H=size
    bg=(22,27,35); fg=(76,141,255); fg2=(63,185,80)
    rows=[]
    r=max(2, size//8)
    def rounded(x,y):
        # 圆角矩形距离判断
        cx = min(max(x, r), W-1-r); cy = min(max(y, r), H-1-r)
        return (x-cx)**2 + (y-cy)**2 <= r*r
    bx0,bx1 = int(W*0.24), int(W*0.72)
    by0,by1 = int(H*0.34), int(H*0.68)
    for y in range(H):
        row=bytearray([0])
        for x in range(W):
            if not rounded(x,y):
                row += bytes((0,0,0,0)); continue
            c = bg
            # 电池主体描边
            if bx0-2 <= x <= bx1+2 and by0-2 <= y <= by1+2:
                inside = bx0 <= x <= bx1 and by0 <= y <= by1
                if inside: c = fg
                else: c = (230,237,243)
            # 电池正极凸起
            if int(W*0.72) < x <= int(W*0.78) and int(H*0.44) <= y <= int(H*0.58):
                c = (230,237,243)
            row += bytes((c[0],c[1],c[2],255))
        rows.append(bytes(row))
    raw=b"".join(rows)
    def chunk(t,d):
        return struct.pack(">I",len(d))+t+d+struct.pack(">I", zlib.crc32(t+d)&0xffffffff)
    out=b"\x89PNG\r\n\x1a\n"
    out+=chunk(b"IHDR", struct.pack(">IIBBBBB", W,H,8,6,0,0,0))
    out+=chunk(b"IDAT", zlib.compress(raw,9))
    out+=chunk(b"IEND", b"")
    open(path,"wb").write(out)

png(sys.argv[1], int(sys.argv[2]))
