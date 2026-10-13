import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/privacy_scan_service.py'

@unittest.skipIf(sys.platform=='win32' or not hasattr(socket,'AF_UNIX'),'Optional Unix socket broker is for macOS/Linux')
class ScannerServiceTests(unittest.TestCase):
 def test_broker_pass_reject_restart_and_fail_closed(self):
  with tempfile.TemporaryDirectory() as temp:
   base=Path(temp);address=str(base/'scan.sock');cp=base/'service.json'
   scanner=[sys.executable,'-c',"import sys;data=sys.stdin.read();raise SystemExit(1 if 'opaque-fixture' in data else 0)"]
   cp.write_text(json.dumps({'scanner':scanner}),encoding='utf-8')
   def start():
    p=subprocess.Popen([sys.executable,'-B',str(SCRIPT),'--socket',address,'--serve-config',str(cp)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    for _ in range(50):
     if Path(address).exists():
      with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
       try:s.connect(address);return p
       except (ConnectionRefusedError,FileNotFoundError):pass
     time.sleep(.05)
    p.terminate();p.communicate(timeout=5);self.fail('Broker did not start')
   def call(value):return subprocess.run([sys.executable,'-B',str(SCRIPT),'--socket',address],input=json.dumps(value),encoding='utf-8',capture_output=True,timeout=5)
   p=start()
   try:
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as raw:
     raw.connect(address);raw.sendall(b'['*5000+b'0'+b']'*5000);raw.shutdown(socket.SHUT_WR);self.assertEqual(raw.recv(16),b'REJECT\n')
    self.assertEqual(call({'summary':'ordinary progress'}).returncode,0)
    r=call({'summary':'opaque-fixture'});self.assertEqual(r.returncode,1);self.assertEqual(r.stdout+r.stderr,'')
    r=call({'summary':'ghp_'+'A'*36});self.assertEqual(r.returncode,1)
   finally:p.terminate();p.communicate(timeout=5)
   self.assertEqual(call({'summary':'ordinary'}).returncode,2)
   p=start()
   try:self.assertEqual(call({'summary':'ordinary'}).returncode,0)
   finally:p.terminate();p.communicate(timeout=5)
   self.assertEqual(set(x.name for x in base.iterdir()),{'scan.sock','service.json'})

if __name__=='__main__':unittest.main()
