import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import LocalFileBackend, StorageConfig, StorageManager
from image_workbench.remote_access import RemoteAccess, validate_origin
from image_workbench.remote_queue import Complete
from image_workbench.remote_worker import RemoteWorker
from image_workbench.web import create_workspace_app as create_app  # 原传输协议；账号入口另有端到端覆盖。
from image_workbench.worker import confirm_stopped


class CountingBackend(LocalFileBackend):
    def __init__(self, sources):
        super().__init__(sources)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return super().generate(request)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = StorageManager(StorageConfig(data_root=self.root / "server", max_image_bytes=2048))
        self.image = self.root / "fixture.png"
        Image.new("RGB", (48, 32), "coral").save(self.image)
        self.access = RemoteAccess("https://images.example", "worker-" + "a" * 40, "owner", "browser-" + "b" * 32)
        self.app = create_app(self.storage, backend="local", remote_access=self.access)
        self.state = self.app.state.workbench
        self.queue = self.app.state.remote_queue
        self.client = self.new_client()
        self.browser = {"Authorization": self.access.browser_authorization.decode()}
        self.csrf = self.client.get("/api/config", headers=self.browser).json()["csrf_token"]
        self.browser["X-Workbench-CSRF"] = self.csrf
        self.pid = self.state.create_project("云端传递测试", uuid4().hex).project_id
        self.backend = CountingBackend([self.image])

    def new_client(self, app=None):
        client = TestClient(app or self.app, base_url=self.access.origin, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def worker(self, cache="client", backend=None, client=None):
        return RemoteWorker(self.access.origin, self.access.worker_token, self.root / cache,
                            backend or self.backend, client=client or self.client)

    def submit(self, **kwargs):
        task = self.state.create_task(self.pid, request_id=uuid4().hex, operation=kwargs.pop("operation", "generate"),
            prompt="把需求从云端传回本机", **kwargs)
        job = self.state.submit(task.project_id, task.task_id, request_id=uuid4().hex, confirmed=True)
        return task, job

    def test_auth_separates_browser_worker_and_checks_host_origin_csrf(self):
        for path in ("/", "/static/workbench.js", "/api/config", "/api/projects"):
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get(path, headers=self.browser).status_code, 200)
            self.assertEqual(self.client.get(path, headers={"Authorization": self.access.worker_authorization.decode()}).status_code, 401)
        self.assertEqual(self.client.post("/api/worker/claim", headers=self.browser,
            json={"worker_id": uuid4().hex, "backend": "local"}).status_code, 401)
        body = {"request_id": uuid4().hex, "name": "test"}
        self.assertEqual(self.client.post("/api/projects", headers={"Authorization": self.browser["Authorization"]}, json=body).status_code, 403)
        self.assertEqual(self.client.post("/api/projects", headers={**self.browser, "Origin": "https://evil.example"}, json=body).status_code, 403)
        self.assertEqual(self.client.get("/api/config", headers={**self.browser, "Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/projects", headers={**self.browser, "Origin": self.access.origin}, json=body).status_code, 200)
        config = self.client.get("/api/config", headers=self.browser)
        self.assertNotIn(self.access.worker_token, config.text)
        self.assertNotIn(self.access.password, config.text)

    def test_offline_queue_generate_return_approval_and_download(self):
        task, job = self.submit(inputs=[self.image], aspect_ratio="3:2")
        self.assertEqual(self.state.task_view(task.project_id, task.task_id)["status"], "queued")
        self.assertFalse(self.state.worker_running())
        worker = self.worker()
        self.assertEqual(worker.run_once(), "completed")
        self.assertEqual(worker.run_once(), "idle")
        self.assertEqual(len(self.backend.requests), 1)
        request = self.backend.requests[0]
        self.assertEqual(request.reference_images[0].read_bytes(), self.image.read_bytes())
        self.assertIn("3:2", request.task.prompt)
        self.assertNotIn(str(self.storage.data_root), str(request.work_dir))
        view = self.state.task_view(task.project_id, task.task_id)
        self.assertEqual(view["status"], "awaiting_review")
        self.assertTrue(self.state.worker_running())
        self.assertEqual(len(view["outputs"]), 1)
        aid = view["outputs"][0]["artifact_id"]
        self.state.approve(aid)
        self.assertEqual(self.state.task_view(task.project_id, task.task_id)["status"], "approved")
        self.assertEqual(self.client.get(f"/api/artifacts/{aid}", headers=self.browser).content, self.image.read_bytes())

    def test_edit_transfers_base_and_preserves_lineage(self):
        original, _ = self.submit()
        worker = self.worker()
        worker.run_once()
        base = self.state.task_view(original.project_id, original.task_id)["outputs"][0]["artifact_id"]
        edit, _ = self.submit(operation="edit", base_artifact_id=base, inputs=[self.image])
        self.assertEqual(worker.run_once(), "completed")
        self.assertEqual(self.backend.requests[-1].edit_image.read_bytes(), self.image.read_bytes())
        self.assertEqual(len(self.backend.requests[-1].reference_images), 1)
        output = self.state.task_view(edit.project_id, edit.task_id)["outputs"][0]
        self.assertEqual(self.storage.get_artifact(output["artifact_id"]).source_artifact_id, base)

    def _lose_response(self, suffix, *, after=True):
        original = self.client.request
        lost = False
        def request(method, url, **kwargs):
            nonlocal lost
            if str(url).endswith(suffix) and not lost:
                lost = True
                if after:
                    response = original(method, url, **kwargs)
                    self.assertEqual(response.status_code, 200, response.text)
                raise httpx.ReadError("simulated connection loss")
            return original(method, url, **kwargs)
        return mock.patch.object(self.client, "request", side_effect=request)

    def test_claim_response_loss_returns_same_job_after_client_restart(self):
        task, job = self.submit()
        with self._lose_response("/claim"):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        self.assertEqual(len(self.backend.requests), 0)
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(self.state.get_job(task.project_id, task.task_id)["job_id"], job["job_id"])

    def test_upload_response_loss_retries_only_upload_after_restart(self):
        self.submit()
        with self._lose_response("/outputs/0"):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(len(self.backend.requests), 1)

    def test_start_permission_response_loss_retries_same_attempt(self):
        self.submit()
        with self._lose_response("/start"):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        self.assertEqual(len(self.backend.requests), 0)
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(len(self.backend.requests), 1)

    def test_server_refuses_new_attempt_if_local_job_checkpoint_is_missing(self):
        task, job = self.submit()
        worker = self.worker()
        manifest = self.queue.claim(worker.worker_id, "local")
        self.queue.start(job["job_id"], worker.worker_id, manifest["claim_id"], uuid4().hex)
        with self.assertRaises(httpx.HTTPStatusError) as raised:
            worker.run_once()
        self.assertEqual(raised.exception.response.status_code, 409)
        self.assertEqual(len(self.backend.requests), 0)

    def test_completion_response_loss_is_idempotent(self):
        task, _ = self.submit()
        with self._lose_response("/complete"):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(len(self.state.task_view(task.project_id, task.task_id)["outputs"]), 1)

    def test_cloud_restart_retains_claim_and_pending_result(self):
        self.submit()
        with self._lose_response("/outputs/0", after=False):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        restarted_app = create_app(self.storage, backend="local", remote_access=self.access)
        self.assertEqual(self.worker(client=self.new_client(restarted_app)).run_once(), "completed")
        self.assertEqual(len(self.backend.requests), 1)

    def test_second_worker_cannot_steal_expired_claim_or_fake_online(self):
        task, job = self.submit()
        first = self.queue.claim(uuid4().hex, "local")
        hb = self.queue.root / "heartbeat.json"
        info = self.storage._read_json(hb)
        info["time"] = time.time() - 120
        self.storage._write_json(hb, info)
        self.storage._write_json(self.queue.workers / (info["worker_id"] + ".json"), info)
        self.assertIsNone(self.queue.claim(uuid4().hex, "local"))
        self.assertFalse(self.state.worker_running())
        self.assertEqual(self.state.task_view(task.project_id, task.task_id)["status"], "uncertain")
        self.assertEqual(self.state.get_job(task.project_id, task.task_id)["job_id"], first["job_id"])

    def test_concurrent_claims_have_only_one_owner(self):
        self.submit()
        self.submit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda wid: self.queue.claim(wid, "local"), [uuid4().hex, uuid4().hex]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(job["status"] == "running" for job in self.state.list_jobs()), 1)

    def test_explicit_server_confirmation_blocks_old_worker_start(self):
        task, job = self.submit()
        wid = uuid4().hex
        manifest = self.queue.claim(wid, "local")
        confirmed = confirm_stopped(self.state, task.project_id, task.task_id)
        self.assertEqual(confirmed["status"], "failed")
        from image_workbench.workbench_state import ConflictError
        with self.assertRaises(ConflictError):
            self.queue.start(job["job_id"], wid, manifest["claim_id"], uuid4().hex)

    def test_process_crash_before_result_pauses_without_regeneration(self):
        task, _ = self.submit()
        backend = self.backend
        with mock.patch.object(backend, "generate", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.worker().run_once()
        worker = self.worker()
        self.assertEqual(worker.run_once(), "uncertain")
        self.assertEqual(len(backend.requests), 0)
        job = self.state.get_job(task.project_id, task.task_id)
        worker.confirm_stopped(job["job_id"])
        self.assertEqual(worker.run_once(), "failed")
        self.assertEqual(len(backend.requests), 0)
        self.state.submit(task.project_id, task.task_id, request_id=uuid4().hex, confirmed=True, retry_of=job["job_id"])
        self.assertEqual(worker.run_once(), "completed")
        self.assertEqual(len(backend.requests), 1)

    def test_bad_output_hash_cross_job_asset_and_foreign_owner_rejected(self):
        task, job = self.submit(inputs=[self.image])
        other, _ = self.submit(inputs=[self.image])
        wid = uuid4().hex
        manifest = self.queue.claim(wid, "local")
        auth = {"Authorization": self.access.worker_authorization.decode(), "X-Worker-ID": wid,
                "X-Claim-ID": manifest["claim_id"]}
        base = f"/api/worker/jobs/{job['job_id']}"
        foreign_asset = self.storage.list_task_artifacts(other.project_id, other.task_id)[0].artifact_id
        self.assertEqual(self.client.get(base + "/assets/" + foreign_asset, headers=auth).status_code, 404)
        self.assertEqual(self.client.post(base + "/heartbeat", headers={**auth, "X-Worker-ID": uuid4().hex}).status_code, 409)
        self.assertEqual(self.client.put(base + "/outputs/0", headers=auth, content=b"not an image").status_code, 400)
        self.assertEqual(self.client.put(base + "/outputs/0", headers=auth, content=b"x" * 2049).status_code, 400)
        self.assertEqual(self.client.put(base + "/outputs/0", headers=auth, content=self.image.read_bytes()).status_code, 200)
        self.assertEqual(self.client.put(base + "/outputs/0", headers=auth, content=b"changed").status_code, 409)
        self.assertEqual(self.client.post(base + "/complete", headers=auth, json={"status": "succeeded", "hashes": ["0" * 64]}).status_code, 400)
        self.assertEqual(self.client.post(base + "/complete", headers=auth, json={"status": "succeeded", "hashes": []}).status_code, 400)

    def test_server_crash_during_multi_image_archive_resumes_with_same_ids(self):
        task, job = self.submit()
        wid = uuid4().hex
        manifest = self.queue.claim(wid, "local")
        digests = [self.queue.upload(job["job_id"], wid, manifest["claim_id"], i, self.image.read_bytes()) for i in range(2)]
        result = Complete(status="succeeded", hashes=digests)
        register = self.storage.register_output_image
        calls = 0
        def crash(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SystemExit("simulated server crash")
            return register(*args, **kwargs)
        with mock.patch.object(self.storage, "register_output_image", side_effect=crash):
            with self.assertRaises(SystemExit):
                self.queue.complete(job["job_id"], wid, manifest["claim_id"], result)
        self.assertEqual(self.queue.complete(job["job_id"], wid, manifest["claim_id"], result)["status"], "completed")
        self.assertEqual(len(self.state.task_view(task.project_id, task.task_id)["outputs"]), 2)
        self.assertEqual(self.queue.complete(job["job_id"], wid, manifest["claim_id"], result)["status"], "completed")
        self.assertEqual(len(self.state.task_view(task.project_id, task.task_id)["outputs"]), 2)

    def test_archive_failure_restore_does_not_call_backend_again(self):
        task, job = self.submit()
        worker = self.worker()
        with mock.patch.object(self.storage, "register_output_image", side_effect=OSError("disk temporarily unavailable")):
            self.assertEqual(worker.run_once(), "failed")
        record = self.state.runner.get_execution(task.project_id, task.task_id)
        self.assertTrue(record.candidates)
        self.state.submit(task.project_id, task.task_id, request_id=uuid4().hex, restore_execution_id=record.execution_id)
        self.assertEqual(worker.run_once(), "completed")
        self.assertEqual(len(self.backend.requests), 1)

    def test_plain_http_remote_and_reused_cache_with_other_server_rejected(self):
        for value in ("http://images.example", "https://user:pass@images.example", "https://images.example/path"):
            with self.assertRaises(ValueError):
                validate_origin(value)
        self.assertEqual(validate_origin("http://127.0.0.1:7860/"), "http://127.0.0.1:7860")
        self.worker()
        with self.assertRaises(ValueError):
            RemoteWorker("https://different.example", self.access.worker_token, self.root / "client", self.backend, client=self.client)

    def test_backend_mismatch_cannot_claim_task(self):
        task, _ = self.submit()
        response = self.client.post("/api/worker/claim", headers={"Authorization": self.access.worker_authorization.decode()},
            json={"worker_id": uuid4().hex, "backend": "codex"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state.get_job(task.project_id, task.task_id)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
