import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/state.py'
spec=importlib.util.spec_from_file_location('public_state',SCRIPT);state=importlib.util.module_from_spec(spec);spec.loader.exec_module(state)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='shared-state-');self.base=Path(self.tmp.name);self.cp=self.base/'設定'/'config.json'
    def tearDown(self):self.tmp.cleanup()
    def cli(self,*args):
        p=subprocess.run([sys.executable,'-B',str(SCRIPT),'--config',str(self.cp),*args],capture_output=True,text=True,encoding='utf-8')
        return p,json.loads(p.stdout or p.stderr)
    def config(self):return json.loads(self.cp.read_text(encoding='utf-8'))
    def request(self,s,op,t=None,**kw):
        r={'schema_version':1,'request_id':uuid.uuid4().hex,'actor':'worker-a','op':op,**kw}
        if t:r.update(task_id=t['task_id'],expected_revision=t['revision']);r.update({'lease_id':t['lease_id']} if t.get('lease_id') else {})
        return s.apply(r)['task']
    def create(self,s,target):return self.request(s,'create',execution_pool='cloud',resources=[{'kind':'path','path':str(target),'mode':'write'}],fields={'title':'fixture','goal':'verify'})

    def test_bootstrap_and_join_share_one_identity(self):
        first=state.bootstrap(self.cp);before=self.cp.read_bytes();second=state.bootstrap(self.cp);third=state.join(self.cp)
        self.assertTrue(first['created']);self.assertFalse(second['created']);self.assertEqual({x['state_id'] for x in [first,second,third]},{first['state_id']});self.assertEqual(before,self.cp.read_bytes())
        db=sqlite3.connect(Path(first['root'])/'state.sqlite');self.assertEqual(db.execute('select count(*) from tasks').fetchone()[0],0);db.close()
    def test_concurrent_first_agents_create_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:results=list(ex.map(lambda _:self.cli('bootstrap'),range(6)))
        self.assertTrue(all(p.returncode==0 for p,r in results),results);self.assertEqual(sum(r['created'] for p,r in results),1);self.assertEqual(len({r['state_id'] for p,r in results}),1)
    def test_two_registries_cannot_overwrite_one_exchange(self):
        exchange=self.base/'common'
        configs=[self.base/'a/config.json',self.base/'b/config.json']
        def run(cp):
            p=subprocess.run([sys.executable,'-B',str(SCRIPT),'--config',str(cp),'bootstrap','--exchange',str(exchange)],capture_output=True,text=True,encoding='utf-8')
            return p,json.loads(p.stdout or p.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:results=list(ex.map(run,configs))
        winners=[r for p,r in results if p.returncode==0]
        self.assertEqual(len(winners),1,results)
        self.assertEqual(state.join(winners[0]['config'])['state_id'],winners[0]['state_id'])
        self.assertEqual(json.loads((exchange/'state.json').read_text())['state_id'],winners[0]['state_id'])

    def test_join_missing_does_not_create_configuration(self):
        with self.assertRaisesRegex(state.StateError,'not found'):state.join(self.cp)
        self.assertFalse(self.cp.parent.exists())
    def test_existing_state_cannot_redirect(self):
        state.bootstrap(self.cp)
        with self.assertRaisesRegex(state.StateError,'differs'):state.bootstrap(self.cp,root=self.base/'other')
        self.assertFalse((self.base/'other').exists())
    def test_missing_database_not_silently_recreated(self):
        r=state.bootstrap(self.cp);db=Path(r['root'])/'state.sqlite';db.unlink()
        with self.assertRaisesRegex(state.StateError,'missing'):state.bootstrap(self.cp)
        self.assertFalse(db.exists())
    def test_missing_exchange_marker_not_recreated(self):
        r=state.bootstrap(self.cp);marker=Path(r['exchange'])/'state.json';marker.unlink()
        with self.assertRaisesRegex(state.StateError,'marker missing'):state.bootstrap(self.cp)
        self.assertFalse(marker.exists())
    def test_state_id_mismatch_refused(self):
        r=state.bootstrap(self.cp);c=self.config();c['state_id']=str(uuid.uuid4());self.cp.write_text(json.dumps(c))
        with self.assertRaisesRegex(state.StateError,'identity mismatch'):state.join(self.cp)
    def test_exchange_id_mismatch_refused(self):
        r=state.bootstrap(self.cp);marker=Path(r['exchange'])/'state.json';marker.write_text(json.dumps({'state_id':str(uuid.uuid4())}))
        with self.assertRaisesRegex(state.StateError,'identity mismatch'):state.join(self.cp)
    def test_explicit_expected_id_refused(self):
        state.bootstrap(self.cp)
        with self.assertRaisesRegex(state.StateError,'identity mismatch'):state.join(self.cp,str(uuid.uuid4()))
    def test_legacy_configuration_not_reinitialized(self):
        self.cp.parent.mkdir();self.cp.write_text(json.dumps({'schema_version':2,'root':str(self.base/'legacy')}));before=self.cp.read_bytes()
        with self.assertRaisesRegex(state.StateError,'migration'):state.bootstrap(self.cp)
        self.assertEqual(before,self.cp.read_bytes());self.assertFalse((self.base/'legacy').exists())
    def test_unknown_existing_database_not_overwritten(self):
        root=self.base/'data';root.mkdir();db=sqlite3.connect(root/'state.sqlite');db.execute('create table evidence(v text)');db.execute('insert into evidence values(?)',('preserve',));db.commit();db.close()
        with self.assertRaisesRegex(state.StateError,'unregistered'):state.bootstrap(self.cp,root=root)
        db=sqlite3.connect(root/'state.sqlite');self.assertEqual(db.execute('select v from evidence').fetchone()[0],'preserve');db.close()
    def test_interrupted_initialization_recovers_same_identity(self):
        real=state.write_atomic
        def fail_config(path,value):
            if Path(path)==self.cp.resolve():raise OSError('simulated interruption before registry commit')
            return real(path,value)
        with patch.object(state,'write_atomic',side_effect=fail_config):
            with self.assertRaises(OSError):state.bootstrap(self.cp)
        pending=json.loads(self.cp.with_name('.bootstrap.pending.json').read_text());expected=pending['settings']['state_id']
        r=state.bootstrap(self.cp);self.assertEqual(r['state_id'],expected);self.assertFalse(self.cp.with_name('.bootstrap.pending.json').exists())
    def test_invalid_capacity_does_not_create_state(self):
        with self.assertRaisesRegex(state.StateError,'pool'):state.bootstrap(self.cp,local_capacity=0)
        self.assertFalse(self.cp.exists());self.assertFalse((self.cp.parent/'data/state.sqlite').exists())
    def test_platform_defaults(self):
        mac=state.default_layout('darwin','/Users/example',{});self.assertEqual(mac['config'],'/Users/example/Library/Application Support/shared-task-state/config.json')
        linux=state.default_layout('linux','/home/example',{'XDG_CONFIG_HOME':'/tmp/conf','XDG_DATA_HOME':'/tmp/data'});self.assertEqual(linux['config'],'/tmp/conf/shared-task-state/config.json');self.assertEqual(linux['root'],'/tmp/data/shared-task-state')
        win=state.default_layout('win32',r'C:\Users\Example',{'LOCALAPPDATA':r'D:\User Data'});self.assertEqual(win['config'],r'D:\User Data\shared-task-state\config.json')
        fallback=state.default_layout('win32',r'C:\Users\Example',{});self.assertEqual(fallback['config'],r'C:\Users\Example\AppData\Local\shared-task-state\config.json')
    def test_relative_platform_environment_does_not_split_by_cwd(self):
        linux=state.default_layout('linux','/home/example',{'XDG_CONFIG_HOME':'relative','XDG_DATA_HOME':'relative'})
        self.assertEqual(linux['config'],'/home/example/.config/shared-task-state/config.json')
        win=state.default_layout('win32',r'C:\Users\Example',{'LOCALAPPDATA':'relative'})
        self.assertEqual(win['config'],r'C:\Users\Example\AppData\Local\shared-task-state\config.json')
        with patch.dict(os.environ,{'SHARED_TASK_STATE_CONFIG':'relative/config.json'}):
            with self.assertRaisesRegex(state.StateError,'absolute'):state.default_config()

    def test_direct_cli_requests_need_no_watch(self):
        r=state.bootstrap(self.cp);path=self.base/'request.json';path.write_text(json.dumps({'schema_version':1,'request_id':'first','actor':'arbitrary-agent','op':'create','execution_pool':'cloud','read_only':True,'resources':[],'fields':{'title':'fixture','goal':'verify'}}))
        p,out=self.cli('request','--file',str(path));self.assertEqual(p.returncode,0,out);self.assertTrue(out['ok']);self.assertTrue((Path(r['exchange'])/'tasks'/(out['task']['task_id']+'.json')).exists());self.assertFalse((Path(r['root'])/'processor.json').exists())
    def test_doctor_requires_file_processor_for_file_only(self):
        state.bootstrap(self.cp);d=state.doctor(self.cp);self.assertFalse(d['capabilities']['recent_file_processor_activity']);s=state.Store(self.config());state.processor_tick(s);s.db.close();self.assertTrue(state.doctor(self.cp)['capabilities']['recent_file_processor_activity'])
    def test_silent_worker_retains_lock_until_confirmed_stopped(self):
        state.bootstrap(self.cp);config=self.config();config['heartbeat_seconds']=5;s=state.Store(config)
        try:
            t=self.request(s,'claim',self.create(s,self.base/'target'));t=self.request(s,'update',t,fields={'last_confirmed_step':'A verified','next_step':'B pending'})
            with s.transaction():t['heartbeat_epoch']=0;s.save(t,'test','simulate-silence')
            s.reconcile();t=s.get(t['task_id']);self.assertEqual(t['status'],'interrupted');self.assertTrue(t['reservation_active']);self.assertTrue(t['lease_id']);self.assertEqual(t['last_confirmed_step'],'A verified')
            other=self.create(s,self.base/'target')
            with self.assertRaisesRegex(state.StateError,'resource conflict'):self.request(s,'claim',other)
            with self.assertRaisesRegex(state.StateError,'stop_confirmed'):self.request(s,'requeue',t,resume_evidence='read prior evidence')
            self.request(s,'release',t,stop_confirmed=True,stop_evidence='operator verified execution stopped')
            self.request(s,'claim',s.get(other['task_id']))
        finally:s.db.close()
    def test_unknown_process_inspection_does_not_release(self):
        state.bootstrap(self.cp);s=state.Store(self.config())
        try:
            t=self.request(s,'claim',self.create(s,self.base/'target'))
            with s.transaction():t.update(pid=123,pid_birth='birth',heartbeat_epoch=0);s.save(t,'test','simulate-pid')
            with patch.object(state,'process_probe',return_value=('unknown',None)):
                s.reconcile();self.assertTrue(s.get(t['task_id'])['reservation_active'])
        finally:s.db.close()
    def test_original_live_pid_cannot_be_released_by_evidence(self):
        state.bootstrap(self.cp);s=state.Store(self.config())
        try:
            t=self.request(s,'claim',self.create(s,self.base/'target'),pid=os.getpid())
            with self.assertRaisesRegex(state.StateError,'still alive'):self.request(s,'release',t,stop_confirmed=True,stop_evidence='not enough while live')
        finally:s.db.close()
    def test_file_watch_restart_keeps_identity_and_receipts(self):
        r=state.bootstrap(self.cp);ex=Path(r['exchange']);rid='file-only-fixture'
        (ex/'inbox'/(rid+'.json')).write_text(json.dumps({'schema_version':1,'request_id':rid,'actor':'file-controller','op':'create','execution_pool':'cloud','read_only':True,'resources':[],'fields':{'title':'fixture','goal':'verify'}}))
        for i in range(2):
            p=subprocess.Popen([sys.executable,'-B',str(SCRIPT),'--config',str(self.cp),'watch','--interval','0.5'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+5
                while not (ex/'receipts'/(rid+'.json')).exists() and time.monotonic()<deadline:time.sleep(.05)
                self.assertTrue((ex/'receipts'/(rid+'.json')).exists());self.assertEqual(state.join(self.cp)['state_id'],r['state_id'])
            finally:p.terminate();p.communicate(timeout=5)
        self.assertTrue(json.loads((ex/'receipts'/(rid+'.json')).read_text())['ok'])


if __name__=='__main__':unittest.main()
