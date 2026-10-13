import importlib.util,json,sys,tempfile,uuid,unittest
from pathlib import Path

class PrivacyPersistence(unittest.TestCase):
 def run_version(self,path):
  spec=importlib.util.spec_from_file_location('state_test',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  fake='ghp_'+('A'*36);pem='-----BEGIN PRIVATE KEY-----\n'+'B'*64+'\n-----END PRIVATE KEY-----'
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);config={'root':str(root/'db'),'exchange':str(root/'exchange'),'heartbeat_seconds':180,'pool_limits':{'local':2,'cloud':None}}
   if path.name=='state.py':
    cp=root/'config.json';m.bootstrap(cp,config['root'],config['exchange'],None);config=json.loads(cp.read_text())
   store=m.Store(config);store.export()
   r={'schema_version':1,'request_id':str(uuid.uuid4()),'actor':'codex','op':'create','execution_pool':'cloud','resources':[{'kind':'service','mode':'write','name':'test-service'}],'fields':{'title':'Privacy regression','goal':fake+' '+pem}}
   task=store.apply(r)['task'];self.assertEqual(store.apply(r)['task']['task_id'],task['task_id'])
   claim={'schema_version':1,'request_id':str(uuid.uuid4()),'actor':'codex','op':'claim','task_id':task['task_id'],'expected_revision':task['revision'],'execution_pool':'cloud','resources':[{'kind':'service','mode':'write','name':'test-service'}]}
   task=store.apply(claim)['task'];finish=dict(claim,request_id=str(uuid.uuid4()),op='finish',expected_revision=task['revision'],lease_id=task['lease_id'],exit_code=0,report=fake+' '+pem);store.apply(finish);store.export()
   for key,value in [('source_key',fake),('actor',fake),('resources',[{'kind':'service','mode':'write','name':fake}])]:
    bad=dict(r,request_id=str(uuid.uuid4()));bad[key]=value
    with self.assertRaises(m.StateError):store.apply(bad)
   store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');store.db.close()
   for file in root.rglob('*'):
    if file.is_file():
     raw=file.read_bytes();self.assertNotIn(fake.encode(),raw,str(file));self.assertNotIn(('B'*64).encode(),raw,str(file))
   config['allow_file_requests']=True;store=m.Store(config);store.export()
   bad=dict(r,request_id=fake)
   inbox=Path(config['exchange'])/'inbox'/(fake+'.json');inbox.write_text(json.dumps(bad),encoding='utf-8')
   self.assertFalse(store.process_inbox()[0]['ok']);self.assertFalse(inbox.exists())
   store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');store.db.close()
   for file in root.rglob('*'):
    if file.is_file():self.assertNotIn(fake.encode(),file.read_bytes(),str(file))
   config['privacy_scanner']=[sys.executable,'-c','raise SystemExit(2)'];store=m.Store(config)
   with self.assertRaises(m.StateError):store.apply(dict(r,request_id=str(uuid.uuid4())))
   store.db.close()
 def test_direct_event_metadata_uses_scanner(self):
  path=Path(__file__).resolve().parents[1]/'scripts/state.py'
  spec=importlib.util.spec_from_file_location('direct_event_test',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  with tempfile.TemporaryDirectory() as temp:
   cp=Path(temp)/'config.json';m.bootstrap(cp);cfg=json.loads(cp.read_text());store=m.Store(cfg)
   try:
    r={'schema_version':1,'request_id':str(uuid.uuid4()),'actor':'worker','op':'create','execution_pool':'cloud','read_only':True,'resources':[],'fields':{'title':'safe','goal':'safe'}}
    task=store.apply(r)['task'];revision=task['revision']
    store.config['privacy_scanner']=[sys.executable,'-c',"import sys,json;v=json.load(sys.stdin);raise SystemExit(1 if v.get('actor')=='opaque-event-fixture' else 0)"]
    with self.assertRaises(m.StateError):
     with store.transaction():store.save(task,'opaque-event-fixture','test')
    self.assertEqual(store.get(task['task_id'])['revision'],revision)
   finally:store.db.close()
 def test_public(self):self.run_version(Path(__file__).resolve().parents[1]/'scripts/state.py')

if __name__=='__main__':unittest.main()
