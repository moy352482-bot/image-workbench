import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import digest
from image_workbench.h3_worker import _json_read, _json_write
from image_workbench.qwen_worker import ComfyQwenAdapter, QwenTransport, QwenWorker, workflow
from image_workbench.remote_access import RemoteAccess
from image_workbench.storage import _now
from image_workbench.web import create_app


def png(color='red'):
    stream = io.BytesIO()
    Image.new('RGB', (64, 64), color).save(stream, format='PNG')
    return stream.getvalue()


class QwenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        storage = StorageManager(StorageConfig(data_root=self.root/'data'))
        self.app = create_app(storage, backend='local', remote_access=RemoteAccess('http://localhost', 'worker-test-token-'*3))
        self.store = self.app.qwen_store
        with self.app.identity.transaction() as db:
            self.aid = db.execute("SELECT account_id FROM accounts WHERE username='moy'").fetchone()[0]
            self.wid = db.execute('SELECT workspace_id FROM workspaces WHERE account_id=?',(self.aid,)).fetchone()[0]
            self.bid,self.bwid = self.app.identity._account(db, 'qwen_other', None)
        self.other_wid = self.app.identity.create_workspace(self.aid, '另一空间')['workspace_id']
        self.admin = self.client(self.aid,self.wid)
        self.other = self.client(self.bid,self.bwid)
        self.anon = self.client()
        self.worker_client = self.client()
        self.worker_client.headers['Authorization'] = 'Bearer ' + 'worker-test-token-'*3
        self.transport = QwenTransport('http://localhost','worker-test-token-'*3,client=self.worker_client)
        self.adapter = Mock()
        self.adapter.url = 'http://127.0.0.1:8190'
        self.adapter.lock_path = self.root/'gpu.lock'
        self.adapter.timeout = 30
        self.adapter.poll_seconds = .01
        self.adapter.idle.return_value = True
        self.adapter.prepare_gpu.return_value = True
        self.adapter.upload.side_effect = lambda jid,index,data:f'{jid}/{index}.png'
        self.adapter.submit.return_value = str(uuid4())
        self.adapter.history.return_value = {'status':{'completed':True,'status_str':'success'},'outputs':{}}
        self.adapter.download.return_value = png('blue')

    def client(self, aid=None, wid=None):
        client = TestClient(self.app, base_url='http://localhost', raise_server_exceptions=False)
        self.addCleanup(client.close)
        if aid:
            token,csrf = uuid4().hex,uuid4().hex
            with self.app.identity.transaction() as db:
                db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',(digest(token),aid,csrf,wid,_now(),time.time()+3600,'qwen-test'))
            client.cookies.set('iw_session',token)
            client.headers['X-Workbench-CSRF'] = csrf
        return client

    def submit(self, *, files=None, client=None, **values):
        body = {'request_id':uuid4().hex,'mode':'generate','model':'fp8','prompt':'测试画面','seed':42,'resolution':1024,**values}
        return (client or self.admin).post('/api/qwen/jobs',data={'payload':json.dumps(body)},files=[('images',('input.png',data,'image/png')) for data in (files or [])])

    def engine(self):
        return QwenWorker(self.transport,self.adapter,self.root/'cache')

    def test_auth_admin_csrf_and_origin(self):
        self.assertEqual(self.anon.get('/qwen-test',follow_redirects=False).status_code,303)
        self.assertEqual(self.anon.get('/api/qwen/jobs').status_code,401)
        self.assertEqual(self.other.get('/qwen-test').status_code,403)
        self.assertEqual(self.other.get('/api/qwen/jobs').status_code,403)
        self.assertEqual(self.admin.get('/qwen-test').status_code,200)
        self.assertEqual(self.anon.post('/api/worker/qwen/claim',json={'worker_id':uuid4().hex}).status_code,401)
        self.assertEqual(self.admin.post('/api/qwen/jobs',headers={'X-Workbench-CSRF':'wrong'}).status_code,403)
        self.assertEqual(self.admin.post('/api/qwen/jobs',headers={'Origin':'https://foreign.invalid'}).status_code,403)

    def test_submit_replay_fingerprint_and_no_credit_charge(self):
        before = self.app.identity.wallet(self.aid)
        rid = uuid4().hex
        first = self.submit(request_id=rid,mode='edit',files=[png()])
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(self.submit(request_id=rid,mode='edit',files=[png()]).json()['job_id'],rid)
        self.assertEqual(self.submit(request_id=rid,mode='edit',files=[png('green')]).status_code,409)
        self.assertEqual(len(self.store.list(self.aid,self.wid)),1)
        self.assertEqual(self.app.identity.wallet(self.aid),before)

    def test_worker_availability_is_readable_online_offline_and_expired(self):
        wid = uuid4().hex
        self.assertFalse(self.admin.get('/api/qwen/config').json()['worker']['online'])
        self.worker_client.post('/api/worker/qwen/heartbeat',json={'worker_id':wid,'ready':True})
        response = self.admin.get('/api/qwen/config')
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['worker']['ready'])
        self.worker_client.post('/api/worker/qwen/heartbeat',json={'worker_id':wid,'ready':False,'message':'模型离线'})
        self.assertFalse(self.admin.get('/api/qwen/config').json()['worker']['ready'])
        path=self.store.workers/(wid+'.json')
        value=_json_read(path);value['time']=time.time()-60;_json_write(path,value)
        self.assertFalse(self.admin.get('/api/qwen/config').json()['worker']['online'])

    def test_upload_modes_corruption_and_untrusted_fields(self):
        for body in ({'mode':'edit'}, {'mode':'reference','files':[png()]}, {'mode':'generate','files':[png()]},
                     {'mode':'edit','files':[b'not an image']}, {'seed':True}, {'model':'arbitrary.safetensors'},
                     {'mode':'edit','files':[png()],'resolution':1536}, {'nodes':{}}):
            with self.subTest(body=body):
                self.assertEqual(self.submit(**body).status_code,400)
        self.assertEqual(self.store.list(self.aid,self.wid),[])

    def test_workspace_and_account_isolation_for_jobs_images(self):
        jid = self.submit(mode='edit',files=[png()]).json()['job_id']
        self.assertEqual(self.admin.get(f'/api/qwen/jobs/{jid}',headers={'X-Workspace-ID':self.other_wid}).status_code,404)
        self.assertEqual(self.admin.get(f'/api/qwen/jobs/{jid}/images/input-1',headers={'X-Workspace-ID':self.other_wid}).status_code,404)
        with self.app.identity.transaction() as db:
            db.execute('UPDATE accounts SET is_admin=1 WHERE account_id=?',(self.bid,))
        self.assertEqual(self.other.get(f'/api/qwen/jobs/{jid}').status_code,404)
        self.assertEqual(self.other.get(f'/api/qwen/jobs/{jid}/images/input-1').status_code,404)
        self.assertEqual(self.admin.get(f'/api/qwen/jobs/{jid}/images/output').status_code,404)

    def test_queued_cancel_capacity_and_worker_busy(self):
        ids = [self.submit().json()['job_id'] for _ in range(3)]
        self.assertEqual(self.submit().status_code,409)
        self.assertEqual(self.admin.post(f'/api/qwen/jobs/{ids[0]}/cancel').json()['status'],'cancelled')
        self.assertEqual(self.submit().status_code,200)
        self.adapter.idle.return_value = False
        self.assertEqual(self.engine().run_once(),'busy')
        self.assertEqual(self.store.get(self.aid,self.wid,ids[1])['status'],'queued')
        self.assertEqual(self.adapter.submit.call_count,0)

    def test_full_edit_roundtrip_hash_download_and_repeat_delivery(self):
        jid = self.submit(mode='edit',files=[png()]).json()['job_id']
        engine = self.engine()
        self.assertEqual(engine.run_once(),'completed')
        job = self.store.get(self.aid,self.wid,jid)
        self.assertEqual(job['output']['sha256'],hashlib.sha256(png('blue')).hexdigest())
        output = self.admin.get(f'/api/qwen/jobs/{jid}/images/output?download=true')
        self.assertEqual(output.content,png('blue'))
        self.assertIn('attachment',output.headers['content-disposition'])
        journal = _json_read(self.root/'cache'/jid/'journal.json')
        self.assertEqual(engine.deliver(journal),'completed')
        self.assertEqual(self.adapter.submit.call_count,1)
        self.assertEqual(engine.run_once(),'idle')
        self.assertEqual(self.adapter.submit.call_count,1)

    def test_wrong_claim_hash_and_late_failure_are_rejected(self):
        jid = self.submit().json()['job_id']
        worker = uuid4().hex
        job = self.store.claim(worker)
        headers={'X-Worker-ID':worker,'X-Claim-ID':job['claim_id']}
        base=f'/api/worker/qwen/jobs/{jid}'
        self.assertEqual(self.worker_client.post(base+'/start',headers={**headers,'X-Claim-ID':uuid4().hex},json={'attempt_id':uuid4().hex}).status_code,409)
        self.assertEqual(self.worker_client.post(base+'/start',headers=headers,json={'attempt_id':uuid4().hex}).status_code,200)
        self.assertEqual(self.worker_client.put(base+'/output',headers={**headers,'X-Content-SHA256':'a'*64},content=png()).status_code,400)
        sha=hashlib.sha256(png()).hexdigest()
        self.assertEqual(self.worker_client.put(base+'/output',headers={**headers,'X-Content-SHA256':sha},content=png()).status_code,200)
        self.assertEqual(self.worker_client.post(base+'/complete',headers=headers,json={'status':'succeeded','sha256':sha}).status_code,200)
        self.assertEqual(self.worker_client.post(base+'/complete',headers=headers,json={'status':'failed'}).status_code,409)
        self.assertEqual(self.admin.post(f'/api/qwen/jobs/{jid}/cancel').status_code,409)
        changed=png('green')
        self.assertEqual(self.worker_client.put(base+'/output',headers={**headers,'X-Content-SHA256':hashlib.sha256(changed).hexdigest()},content=changed).status_code,409)

    def test_submission_response_loss_reconciles_without_duplicate(self):
        self.submit()
        self.adapter.submit.side_effect = httpx.ReadTimeout('response lost')
        self.adapter.reconcile.return_value = [str(uuid4())]
        self.assertEqual(self.engine().run_once(),'completed')
        self.assertEqual(self.adapter.submit.call_count,1)

    def test_uncertain_submission_is_not_automatically_replayed(self):
        jid=self.submit().json()['job_id']
        self.adapter.submit.side_effect=httpx.ReadTimeout('unknown')
        self.adapter.reconcile.return_value=[]
        engine=self.engine()
        self.assertEqual(engine.run_once(),'uncertain')
        self.assertEqual(engine.run_once(),'uncertain')
        self.assertEqual(self.adapter.submit.call_count,1)
        self.assertEqual(self.store.get(self.aid,self.wid,jid)['status'],'uncertain')

    def test_delivery_response_loss_recovers_from_local_result(self):
        jid=self.submit().json()['job_id']
        real=self.transport.request
        first=[True]
        def request(method,suffix,*args,**kwargs):
            result=real(method,suffix,*args,**kwargs)
            if suffix=='/complete' and first[0]:
                first[0]=False
                raise httpx.ReadTimeout('ack lost')
            return result
        self.transport.request=request
        engine=self.engine()
        with self.assertRaises(httpx.ReadTimeout): engine.run_once()
        self.assertEqual(self.store.get(self.aid,self.wid,jid)['status'],'completed')
        self.assertEqual(self.engine().run_once(),'completed')
        self.assertEqual(self.adapter.submit.call_count,1)

    def test_completed_cloud_receipt_recovers_without_original_local_journal(self):
        jid=self.submit().json()['job_id']
        engine=self.engine()
        job=self.store.claim(engine.worker_id)
        self.store.start(jid,engine.worker_id,job['claim_id'],uuid4().hex,str(uuid4()))
        self.assertEqual(engine.run_once(),'completed')
        self.assertEqual(self.adapter.submit.call_count,0)

    def test_model_failure_does_not_publish_image(self):
        jid=self.submit().json()['job_id']
        self.adapter.history.return_value={'status':{'status_str':'error','completed':False}}
        self.assertEqual(self.engine().run_once(),'failed')
        self.assertEqual(self.admin.get(f'/api/qwen/jobs/{jid}/images/output').status_code,404)
        self.assertEqual(self.adapter.download.call_count,0)

    def test_corrupt_downloaded_input_is_never_submitted(self):
        self.submit(mode='edit',files=[png()])
        self.transport.input=lambda *args:png('green')
        self.assertEqual(self.engine().run_once(),'failed')
        self.assertEqual(self.adapter.submit.call_count,0)

    def test_workflow_binds_model_seed_and_two_images(self):
        task={'mode':'reference','model':'q5','prompt':'只改文字','seed':12345,'resolution':1024}
        graph=workflow(task,['a.png','b.png'],'safe/output')
        self.assertEqual(graph['1']['class_type'],'UnetLoaderGGUF')
        self.assertEqual(graph['4']['inputs']['images.image_2'],['15',0])
        self.assertEqual(graph['11']['inputs']['latent_image'],['4',2])
        self.assertEqual(graph['8']['inputs']['noise_seed'],12345)
        self.assertEqual(graph['13']['inputs']['filename_prefix'],'safe/output')
        self.assertNotIn('5',graph)

    def test_peer_gpu_busy_prevents_freeing_or_submission(self):
        calls=[]
        def handler(request):
            calls.append((request.url.port,request.url.path,request.method))
            busy=request.url.port==8189
            return httpx.Response(200,json={'queue_running':[1] if busy else [],'queue_pending':[]})
        with httpx.Client(base_url='http://127.0.0.1:8190',transport=httpx.MockTransport(handler)) as client:
            adapter=ComfyQwenAdapter('http://127.0.0.1:8190',self.root/'gpu.lock',peer_urls=['http://127.0.0.1:8189'],client=client)
            self.assertFalse(adapter.prepare_gpu())
        self.assertTrue(all(method=='GET' for _,_,method in calls))


if __name__=='__main__':
    unittest.main()
