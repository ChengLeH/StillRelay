"""Wait for the owning runner to bind this process group before exec."""
import os
import sys

def main():
    if len(sys.argv)<3:raise SystemExit(2)
    fd=int(sys.argv[1]);argv=sys.argv[2:]
    allowed=os.read(fd,1);os.close(fd)
    if allowed!=b'1':raise SystemExit(125)
    os.execvpe(argv[0],argv,os.environ)

if __name__=='__main__':main()
