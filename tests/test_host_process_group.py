import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import evidence
original_evidence_error=evidence.StateError
host_source=Path(os.environ.get('STILLRELAY_HOST_SOURCE',str(ROOT/'integrations/macos-host/shared_state.py')))
spec=importlib.util.spec_from_file_location('host_state',host_source)
state=importlib.util.module_from_spec(spec);spec.loader.exec_module(state)
evidence.StateError=original_evidence_error
previous_shared=sys.modules.get('shared_state');sys.modules['shared_state']=state
adapter_source=Path(os.environ.get('STILLRELAY_HOST_ADAPTER',str(ROOT/'integrations/macos-host/slack_adapter.py')))
adapter_spec=importlib.util.spec_from_file_location('host_slack_adapter',adapter_source)
adapter=importlib.util.module_from_spec(adapter_spec);adapter_spec.loader.exec_module(adapter)
if previous_shared is None:del sys.modules['shared_state']
else:sys.modules['shared_state']=previous_shared

@unittest.skipIf(os.name!='posix','POSIX process group required')
class HostGroupTest(unittest.TestCase):
 def test_group_inspection_error_is_conservative_for_both_host_helpers(self):
  spec=importlib.util.spec_from_file_location('host_lifecycle',ROOT/'integrations/macos-host/lifecycle_runtime.py');legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
  with patch.object(os,'killpg',side_effect=PermissionError()):
   self.assertEqual(state.process_group_state(12345),'unknown')
   self.assertTrue(state.legacy_process_alive({'process_group':12345}))
   self.assertTrue(legacy.lease_process_alive({'process_group':12345}))
 def test_real_orphaned_child_keeps_resource_and_local_capacity(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);store=state.Store({'root':str(root/'db'),'exchange':str(root/'exchange'),'legacy_leases':str(root/'leases'),'heartbeat_seconds':5,'pool_limits':{'local':1,'cloud':None}})
   script="import os,time\np=os.fork()\nif p: print('ready',flush=True)\ntime.sleep(30)"
   worker=subprocess.Popen([sys.executable,'-c',script],start_new_session=True,stdout=subprocess.PIPE,text=True)
   def req(op,task=None,**kw):
    r={'schema_version':1,'request_id':str(uuid.uuid4()),'actor':'worker','op':op,**kw}
    if task:r.update(task_id=task['task_id'],expected_revision=task['revision'],lease_id=task.get('lease_id'))
    return r
   try:
    self.assertEqual(worker.stdout.readline().strip(),'ready')
    target=[{'kind':'path','path':str(root/'x'),'mode':'write'}]
    task=store.apply(req('create',execution_pool='local',resources=target,fields={'title':'t','goal':'g'}))['task']
    task=store.apply(req('claim',task))['task']
    task=store.apply(req('heartbeat',task,pid=worker.pid,process_group=worker.pid))['task']
    worker.terminate();worker.wait(timeout=5)
    with store.transaction():task['heartbeat_epoch']=0;store.save(task,'test','expire')
    store.reconcile();task=store.get(task['task_id']);self.assertEqual(task['status'],'running')
    with self.assertRaisesRegex(state.StateError,'group'):store.apply(req('release',task,stop_confirmed=True,stop_evidence='test'))
    other=store.apply(req('create',execution_pool='local',resources=target,fields={'title':'other','goal':'g'}))['task']
    with self.assertRaisesRegex(state.StateError,'conflict'):store.apply(req('claim',other))
    self.assertTrue(state.legacy_process_alive(json.loads(store.lease_file(task).read_text())))
    spec=importlib.util.spec_from_file_location('host_lifecycle',ROOT/'integrations/macos-host/lifecycle_runtime.py');legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
    self.assertTrue(legacy.lease_process_alive(json.loads(store.lease_file(task).read_text())))
   finally:
    try:os.killpg(worker.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    if worker.poll() is None:worker.wait(timeout=5)
    worker.stdout.close();store.db.close()
 def test_group_remains_reserved_after_leader_exits_and_heartbeat_expires(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);store=state.Store({'root':str(root/'db'),'exchange':str(root/'exchange'),'legacy_leases':str(root/'leases'),'heartbeat_seconds':5,'pool_limits':{'local':2,'cloud':None}})
   worker=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
   def req(op,task=None,**kw):
    r={'schema_version':1,'request_id':str(uuid.uuid4()),'actor':'worker','op':op,**kw}
    if task:r.update(task_id=task['task_id'],expected_revision=task['revision'],lease_id=task.get('lease_id'))
    return r
   try:
    task=store.apply(req('create',execution_pool='local',resources=[{'kind':'path','path':str(root/'x'),'mode':'write'}],fields={'title':'t','goal':'g'}))['task']
    task=store.apply(req('claim',task))['task']
    task=adapter.perform(store,{'op':'heartbeat','actor':'worker','task_id':task['task_id'],'lease_id':task['lease_id'],'pid':worker.pid,'process_group':worker.pid})['task']
    self.assertEqual(task['process_group'],worker.pid)
    with self.assertRaisesRegex(state.StateError,'group'):store.apply(req('finish',task,exit_code=0))
    with patch.object(state,'pid_identity',return_value=None),patch.object(state,'process_group_state',return_value='alive'):
     with store.transaction():task['heartbeat_epoch']=0;store.save(task,'test','expire')
     store.reconcile()
    self.assertEqual(store.get(task['task_id'])['status'],'running')
   finally:
    worker.terminate();worker.wait(timeout=5);store.db.close()
