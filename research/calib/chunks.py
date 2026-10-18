import sys,struct
def parse(b):
    hdr=b[:5]; op,bpl,req=struct.unpack('<BHH',hdr)
    out=[]; off=5
    while off<len(b):
        if off+4>len(b): out.append(('TRAIL',off,b[off:])); break
        t,sz=struct.unpack('<HH',b[off:off+4])
        out.append((t,off,b[off+4:off+4+sz])); off+=4+sz
    return (op,bpl,req),out
if __name__=="__main__":
 for fn in sys.argv[1:]:
     b=open(fn,'rb').read()
     h,cs=parse(b)
     print(fn,len(b),'op=%02x bpl=0x%x req=%d'%h)
     for t,o,p in cs:
         if t=='TRAIL': print('  TRAIL at',o,len(p)); continue
         print('  chunk 0x%02x @%5d (data @%5d) len %5d  %s'%(t,o,o+4,len(p),p[:16].hex()))
