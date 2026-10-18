# SPDX-License-Identifier: MIT
# bitpack() is ported from python-validity (validitysensor/sensor.py,
# https://github.com/uunicorn/python-validity), MIT licence:
#   Copyright (c) 2020 uunicorn
# The port and the rest of this file: Copyright (C) 2026 Raul Dias <raul@dias.com.br>, MIT.
# Both are under the MIT terms in LICENSE (same permission notice).
"""Build 06cb:0078's per-unit 02 (capture) command from CalibrationData_decrypted.bin.
Template for the fixed parts: a reference 02 command (iusearch). Usage: build_ours.py template.bin CalibrationData_decrypted.bin out.bin"""
import os,sys,struct
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))  # chunks.py sits next to this script
from chunks import parse
tmpl,cdp,outp=sys.argv[1:4]
t=open(tmpl,'rb').read(); cd=open(cdp,'rb').read()
# --- CalibrationData layout (file offsets); C struct = file 0x2c
assert struct.unpack_from('<I',cd,0xc)[0]==0x2449
C=0x2c
fact_len=struct.unpack_from('<I',cd,C+0x70)[0]; frame_len=struct.unpack_from('<I',cd,C+0x88)[0]
assert (fact_len,frame_len)==(56,8512),(fact_len,frame_len)
fact=cd[0xEC:0xEC+56]; frame=cd[0x124:0x124+8512]
bw,bh=struct.unpack_from('<II',cd,0x2294+4)  # baseline frame descriptor {1,w,h,0,ptr,ptr}
assert (bw,bh)==(152,56)
# chunk 0x48 copies the frame of descriptor ctx+0xd8 = second record of section 2
base=cd[0x22d4+57*152:0x22d4+57*152+bw*bh]
assert struct.unpack_from("<II",cd,0x22b4+4)==(152,56)
LINES=56; BPL=152
def rescale(b,quirk):
    v=(b-0x80)*10; q=-(-v//4) if v<0 else v//4; q=max(-128,min(127,q))
    if quirk and q==5: q=4
    return q&0xff
def bitpack(b):
    m=min(b); x=max(b)-m; u=0
    while x>0: x>>=1; u+=1
    v=0
    for i,val in enumerate(b): v|=(val-m)<<(u*i)
    return u,m,v.to_bytes((u*len(b)+7)//8,'little')
hdr=t[:5]; cs=parse(t)[1]
out=[]
for typ,off,p in cs:
    p=bytearray(p)
    if typ==0x34:
        key=b''.join(bytes(rescale(x,True) for x in frame[r*BPL+8+band*48: r*BPL+8+band*48+48]) for band,r in enumerate((18,28,38)))
        p[0:144]=key
    elif typ==0x30:
        n,=struct.unpack_from('<L',p,0); d0=4+8*n
        cols=b''
        for k in range(36):
            for j in range(LINES):
                cols+=bytes(rescale(x,False) for x in frame[j*BPL+8+4*k:j*BPL+8+4*k+4])
        assert len(cols)==len(p)-d0
        p[d0:]=cols
    elif typ==0x43:
        u,m,bits=bitpack(list(fact)); assert u==5,u
        q=60
        for _ in range(2):
            p[q:q+4]=struct.pack('<BBH',(u-1)|8,m,0); p[q+4:q+4+len(bits)]=bits; q+=4+len(bits)
    elif typ==0x48:
        p=bytearray(struct.pack('<HH',bw,bh)+base)
    out.append(struct.pack('<HH',typ,len(p))+bytes(p))
res=hdr+b''.join(out)
open(outp,'wb').write(res); print('wrote',outp,len(res))
