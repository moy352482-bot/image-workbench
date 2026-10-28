"""图片 API 协议和真实工作台队列/账本回归；外部模型由 HTTP Mock 替代。"""

import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import StorageConfig, StorageManager, TaskRunner
from image_workbench.accounts import CreditCostCalculator
from image_workbench.cli import _parser, main
from image_workbench.contracts.image_queue import Claim
from image_workbench.image_api_backend import ImageAPIBackend
from image_workbench.remote_access import RemoteAccess
from image_workbench.remote_worker import RemoteWorker
from image_workbench.scheduling import SchedulingPolicy
from image_workbench.web import create_app
from image_workbench.worker import confirm_stopped
from image_workbench.workbench_state import WorkbenchState


def png(color="coral"):
    data = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(data, "PNG")
    return data.getvalue()


class ImageAPIBackendTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data", max_image_bytes=2048))
        self.state = WorkbenchState(self.storage, backend="image-api")
        self.project = self.state.create_project("API 测试", uuid4().hex)
        self.runner = TaskRunner(self.storage)
        self.calls = []
        self.image = png()
        self.key = "test-api-key-not-a-real-credential"
        self.response = {"data": [{"b64_json": base64.b64encode(self.image).decode()}], "usage": {"total_tokens": 2}}
        self.status = 200
        self.error = None
        self.responses = []
        self.sleep = Mock()
        clock = patch('image_workbench.image_api_backend.time', SimpleNamespace(monotonic=time.monotonic, sleep=self.sleep))
        clock.start()
        self.addCleanup(clock.stop)

        def handler(request):
            self.calls.append(request)
            if self.responses:
                return self.responses.pop(0)
            if self.error:
                raise self.error
            headers = {"Location": "https://other.test/image.png"} if self.status == 307 else {}
            return httpx.Response(self.status, json=self.response, headers=headers)

        self.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.client.close)
        self.backend = ImageAPIBackend("https://images.test/v1/", self.key, client=self.client)

    def run_task(self, prompt="生成一个产品图", *, base=None, inputs=(), aspect_ratio="auto", timeout=600):
        task = self.state.create_task(self.project.project_id, request_id=uuid4().hex,
                                      operation="edit" if base else "generate", prompt=prompt,
                                      base_artifact_id=base, inputs=inputs, aspect_ratio=aspect_ratio)
        record = self.runner.run(task.project_id, task.task_id, self.backend, idempotency_key=uuid4().hex, timeout_seconds=timeout)
        return task, record

    def run_path(self, record):
        return self.runner.get_execution_directory(record.project_id, record.task_id, record.execution_id)

    def test_generation_is_archived_once_with_auth_dimensions_and_timing(self):
        task, record = self.run_task("产品照", aspect_ratio="3:2")
        self.assertEqual(record.status, "succeeded")
        request = self.calls[0]
        self.assertEqual(str(request.url), "https://images.test/v1/images/generations")
        self.assertEqual(request.headers["Authorization"], "Bearer " + self.key)
        body = json.loads(request.content)
        self.assertEqual((body["size"], body["n"], body["response_format"]), ("1536x1024", 1, "b64_json"))
        self.assertTrue(body["history_disabled"])
        self.assertIn("本次视觉需求", body["prompt"])
        artifact = record.output_artifact_ids[0]
        self.assertEqual(self.storage.get_artifact_path(artifact).read_bytes(), self.image)
        self.assertGreater(record.backend_seconds, 0)
        self.assertIsNone(record.codex_seconds)
        replay = self.runner.run(task.project_id, task.task_id, self.backend, idempotency_key=record.execution_id)
        self.assertEqual(replay.output_artifact_ids, record.output_artifact_ids)
        self.assertEqual(len(self.calls), 1)
        for path in self.run_path(record).rglob("*.json"):
            self.assertNotIn(self.key, path.read_text(encoding="utf-8"))

    def test_edit_sends_base_first_and_additional_reference_preserving_lineage(self):
        _, original = self.run_task()
        aid = original.output_artifact_ids[0]
        reference = self.root / "reference.png"
        reference.write_bytes(png("blue"))
        _, edited = self.run_task("把杯子改成蓝色", base=aid, inputs=[reference], aspect_ratio="2:3")
        self.assertEqual(edited.status, "succeeded")
        self.assertEqual(self.calls[-1].url.path, "/v1/images/edits")
        body = json.loads(self.calls[-1].content)
        self.assertEqual(body["size"], "1024x1536")
        self.assertEqual(len(body["images"]), 2)
        self.assertTrue(body["images"][0]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(body["images"][0]["image_url"].split(",", 1)[1]), self.image)
        self.assertEqual(base64.b64decode(body["images"][1]["image_url"].split(",", 1)[1]), reference.read_bytes())
        output = self.storage.get_artifact(edited.output_artifact_ids[0])
        self.assertEqual(output.source_artifact_id, aid)
        self.assertEqual(self.storage.get_artifact_path(aid).read_bytes(), self.image)

    def test_generate_with_multiple_references_uses_edits_endpoint(self):
        sources = []
        for color in ("red", "green"):
            path = self.root / (color + ".png")
            path.write_bytes(png(color))
            sources.append(path)
        _, record = self.run_task(inputs=sources)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(self.calls[0].url.path, "/v1/images/edits")
        body = json.loads(self.calls[0].content)
        self.assertEqual(len(body["images"]), 2)
        self.assertNotIn("size", body)

    def test_http_errors_retry_only_recoverable_statuses_and_redact_key(self):
        for code in (400, 401, 403, 422, 429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.status = code
                self.response = {"error": {"message": "rejected Bearer " + self.key, "code": str(code)}}
                previous = len(self.calls)
                _, record = self.run_task()
                self.assertEqual(record.status, "execution_failed")
                self.assertEqual(record.failure_type, "image_api_failed")
                expected = 3 if code in {429, 500, 502, 503, 504} else 1
                self.assertEqual(len(self.calls), previous + expected)
                self.assertEqual(record.output_artifact_ids, [])
                diagnostic = (self.run_path(record) / "work" / "api-response.json").read_text(encoding="utf-8")
                self.assertNotIn(self.key, diagnostic)
                self.assertNotIn(self.key, record.error)
                self.assertEqual(json.loads(diagnostic)["status_code"], code)
                self.assertEqual(json.loads(diagnostic)["attempt_count"], expected)

    def test_unknown_outcomes_do_not_repeat_and_failed_connections_are_bounded(self):
        for error, status, failure in ((httpx.ReadTimeout("secret " + self.key), "timed_out", "image_api_timeout"),
                                       (httpx.WriteTimeout("secret " + self.key), "timed_out", "image_api_timeout"),
                                       (httpx.ReadError("secret " + self.key), "execution_failed", "image_api_failed"),
                                       (httpx.ConnectTimeout("secret " + self.key), "execution_failed", "image_api_failed"),
                                       (httpx.ConnectError("secret " + self.key), "execution_failed", "image_api_failed")):
            with self.subTest(status=status):
                self.error = error
                previous = len(self.calls)
                _, record = self.run_task()
                self.assertEqual((record.status, record.failure_type), (status, failure))
                self.assertNotIn(self.key, record.error)
                expected = 3 if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)) else 1
                self.assertEqual(len(self.calls), previous + expected)

    def test_content_policy_refusal_stops_after_three_attempts_with_safe_error(self):
        self.status = 400
        self.response = {"error": {"code": "content_policy_violation", "message": "private reason " + self.key}}
        _, record = self.run_task("普通民众的历史生活场景", aspect_ratio="3:2")
        self.assertEqual(record.failure_type, "image_api_content_rejected")
        self.assertEqual(record.status, "execution_failed")
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn(self.key, record.error)
        self.assertEqual(record.output_artifact_ids, [])

    def test_retry_keeps_exact_body_and_archives_only_the_successful_result(self):
        self.responses = [httpx.Response(400, json={"error": {"code": "content_policy_violation"}}),
                          httpx.Response(503, json={"error": {"code": "busy"}}),
                          httpx.Response(200, json=self.response)]
        _, record = self.run_task("中世纪普通民众的生活场景", aspect_ratio="3:2")
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(len(record.output_artifact_ids), 1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({call.content for call in self.calls}), 1)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 5])
        info = json.loads((self.run_path(record) / 'work/api-response.json').read_text(encoding='utf-8'))
        self.assertEqual([attempt['status_code'] for attempt in info['attempts']], [400, 503, 200])

    def test_retry_preserves_edit_base_references_and_ratio(self):
        _, original = self.run_task()
        self.responses = [httpx.Response(503, json={"error": "busy"}), httpx.Response(200, json=self.response)]
        reference = self.root / 'reference.png'
        reference.write_bytes(png('green'))
        _, record = self.run_task('改成蓝色', base=original.output_artifact_ids[0], inputs=[reference], aspect_ratio='2:3')
        self.assertEqual(record.status, 'succeeded')
        self.assertEqual(self.calls[-1].content, self.calls[-2].content)
        payload = json.loads(self.calls[-1].content)
        self.assertEqual((payload['size'], len(payload['images'])), ('1024x1536', 2))

    def test_retry_respects_retry_after_and_overall_deadline(self):
        self.responses = [httpx.Response(429, headers={'Retry-After': '7'}, json={'error': 'busy'}),
                          httpx.Response(200, json=self.response)]
        _, record = self.run_task()
        self.assertEqual(record.status, 'succeeded')
        self.sleep.assert_called_once_with(7)
        self.sleep.reset_mock()
        self.status, self.response = 503, {'error': 'busy'}
        before = len(self.calls)
        _, record = self.run_task(timeout=1)
        self.assertEqual(record.status, 'execution_failed')
        self.assertEqual(len(self.calls), before + 1)
        self.sleep.assert_not_called()

    def test_malformed_missing_and_nonimage_results_never_become_success(self):
        cases = [({}, "missing_artifact"), ({"data": []}, "missing_artifact"),
                 ({"data": [{"url": "https://other.test/image.png"}]}, "missing_artifact"),
                 ({"data": [{"b64_json": "%%%%"}]}, "invalid_artifact"),
                 ({"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}, "invalid_artifact")]
        for response, status in cases:
            with self.subTest(response=response):
                self.response = response
                _, record = self.run_task()
                self.assertEqual(record.status, status)
                self.assertEqual(record.output_artifact_ids, [])

    def test_oversized_response_is_rejected(self):
        self.response = {"data": [{"b64_json": "a" * 100000}]}
        _, record = self.run_task()
        self.assertEqual(record.status, "invalid_artifact")
        self.assertEqual(len(self.calls), 1)

    def test_redirect_does_not_forward_authorization(self):
        self.status = 307
        self.response = {"error": "redirect"}
        _, record = self.run_task()
        self.assertEqual(record.failure_type, "image_api_failed")
        self.assertEqual(len(self.calls), 1)

    def test_private_config_and_environment_override_without_exposing_secrets(self):
        config = self.root / "private.json"
        config.write_text(json.dumps({"base_url": "https://api.test", "api_key": self.key}), encoding="utf-8")
        with patch.dict(os.environ, {"IMAGE_WORKBENCH_IMAGE_API_CONFIG": str(config),
                                     "IMAGE_WORKBENCH_IMAGE_API_MODEL": "gpt-image-2"}, clear=True):
            backend = ImageAPIBackend.from_environment()
        self.assertEqual(backend.base_url, "https://api.test")
        self.assertEqual(backend.model, "gpt-image-2")
        for url in ("https://your-domain.com", "https://name:password@example.com", "http://remote.test", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ImageAPIBackend(url, self.key)

    def test_cli_preserves_legacy_backend_and_requires_explicit_paid_run(self):
        self.assertEqual(_parser().parse_args(["web"]).backend, "codex")
        self.assertEqual(_parser().parse_args(["cloud", "--backend", "image-api"]).backend, "image-api")
        for backend in ("codex", "local", "image-api"):
            self.assertEqual(Claim(worker_id="a" * 32, backend=backend).backend, backend)
        with self.assertRaises(ValueError):
            Claim(worker_id="a" * 32, backend="unknown-provider")
        with self.assertRaises(SystemExit) as raised, patch("sys.stderr", new_callable=io.StringIO):
            main(["run", "--project", "a" * 32, "--task", "b" * 32, "--backend", "image-api"])
        self.assertEqual(raised.exception.code, 2)
        for operation, count in (("generate", 0), ("generate", 1), ("edit", 0)):
            self.assertEqual(CreditCostCalculator.calculate(operation, "image-api", reference_count=count),
                             CreditCostCalculator.calculate(operation, "codex", reference_count=count))

    def test_manual_recovery_works_without_provider_credentials(self):
        state = WorkbenchState(StorageManager(StorageConfig(data_root=self.root / "recovery")), backend="image-api")
        project = state.create_project("恢复测试", uuid4().hex)
        task = state.create_task(project.project_id, request_id=uuid4().hex, operation="generate", prompt="恢复测试")
        job = state.submit(task.project_id, task.task_id, request_id=uuid4().hex, confirmed=True)
        job["status"] = "uncertain"
        state.save_job(job)
        with patch.dict(os.environ, {}, clear=True):
            result = confirm_stopped(state, task.project_id, task.task_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.calls), 0)


class ImageAPIWorkbenchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.storage = StorageManager(StorageConfig(data_root=self.root / "server"))
        self.access = RemoteAccess("https://workbench.test", "worker-" + "w" * 40)
        self.app = create_app(self.storage, backend="image-api", remote_access=self.access)
        self.browser = TestClient(self.app, base_url=self.access.origin)
        self.addCleanup(self.browser.close)
        token = self.browser.get("/api/auth/config").json()["csrf_token"]
        credentials = {"username": "imageapiuser", "password": "test-image-password"}
        headers = {"X-Workbench-CSRF": token}
        self.assertEqual(self.browser.post("/api/auth/register", json=credentials, headers=headers).status_code, 200)
        self.assertEqual(self.browser.post("/api/auth/login", json=credentials, headers=headers).status_code, 200)
        self.browser.headers["X-Workbench-CSRF"] = self.browser.get("/api/config").json()["csrf_token"]
        response = self.browser.post("/api/main-projects", json={"name": "API 工作台回归", "request_id": uuid4().hex})
        self.assertEqual(response.status_code, 200)
        self.sid = response.json()["subprojects"][0]["subproject_id"]
        self.calls = []
        self.fail = False
        self.content_rejected = False
        self.failures_remaining = 0
        clock = patch('image_workbench.image_api_backend.time', SimpleNamespace(monotonic=time.monotonic, sleep=Mock()))
        clock.start()
        self.addCleanup(clock.stop)

        def handler(request):
            self.calls.append(request)
            if self.failures_remaining:
                self.failures_remaining -= 1
                return httpx.Response(503, json={"error": {"message": "暂不可用"}})
            if self.content_rejected:
                return httpx.Response(400, json={"error": {"code": "content_policy_violation", "message": "private-test-key"}})
            if self.fail:
                return httpx.Response(503, json={"error": {"message": "暂不可用"}})
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})

        provider = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(provider.close)
        backend = ImageAPIBackend("https://provider.test", "private-test-key", client=provider)
        transport = TestClient(self.app, base_url=self.access.origin)
        self.worker = RemoteWorker(self.access.origin, self.access.worker_token, self.root / "worker", backend, client=transport)
        self.addCleanup(self.worker.close)

    def task(self, base=None):
        data = {"request_id": uuid4().hex, "operation": "edit" if base else "generate", "prompt": "产品摄影",
                "aspect_ratio": "1:1"}
        if base:
            data["base_artifact_id"] = base
        response = self.browser.post(f"/api/subprojects/{self.sid}/tasks", data=data)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wallet(self):
        return self.browser.get("/api/credits").json()["credits"]

    def test_web_generate_edit_idempotency_credit_and_download(self):
        base = None
        for cost in (40, 60):
            task = self.task(base)
            tid = task["task_id"]
            body = {"request_id": uuid4().hex, "confirm_generation": True}
            before = self.wallet()["remaining_credit"]
            first = self.browser.post(f"/api/tasks/{tid}/run", json=body)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(self.browser.post(f"/api/tasks/{tid}/run", json=body).json(), first.json())
            self.assertEqual(self.worker.run_once(), "completed")
            self.assertEqual(self.worker.run_once(), "idle")
            detail = self.browser.get(f"/api/tasks/{tid}").json()
            self.assertEqual(detail["status"], "awaiting_review")
            output = detail["outputs"][0]
            self.assertEqual(self.browser.get(output["url"]).content, png())
            self.assertEqual(self.wallet()["remaining_credit"], before - cost)
            if base:
                self.assertEqual(self.storage.get_artifact(output["artifact_id"]).source_artifact_id, base)
            base = output["artifact_id"]
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([r.url.path for r in self.calls], ["/v1/images/generations", "/v1/images/edits"])
        self.assertTrue(all(json.loads(r.content)["size"] == "1024x1024" for r in self.calls))
        self.assertTrue(all("本次视觉需求" in json.loads(r.content)["prompt"] for r in self.calls))
        self.assertNotIn("private-test-key", self.browser.get("/api/config").text)

    def test_failed_api_job_releases_credit_and_survives_worker_restart_without_retry(self):
        self.fail = True
        task = self.task()
        before = self.wallet()["remaining_credit"]
        response = self.browser.post(f"/api/tasks/{task['task_id']}/run",
                                     json={"request_id": uuid4().hex, "confirm_generation": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.worker.run_once(), "failed")
        restarted = RemoteWorker(self.access.origin, self.access.worker_token, self.root / "worker", self.worker.backend,
                                 client=TestClient(self.app, base_url=self.access.origin))
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.run_once(), "idle")
        detail = self.browser.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["failure_type"], "image_api_failed")
        self.assertEqual(self.wallet()["remaining_credit"], before)
        self.assertEqual(len(self.calls), 3)

    def test_content_rejection_survives_remote_protocol_and_refunds_credit(self):
        self.content_rejected = True
        task = self.task()
        before = self.wallet()["remaining_credit"]
        response = self.browser.post(f"/api/tasks/{task['task_id']}/run",
                                     json={"request_id": uuid4().hex, "confirm_generation": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.worker.run_once(), "failed")
        detail = self.browser.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(detail["failure_type"], "image_api_content_rejected")
        self.assertIn("内容审核", detail["error_summary"])
        self.assertEqual(detail["credit_status"], "refunded")
        self.assertEqual(self.wallet()["remaining_credit"], before)
        self.assertNotIn("private-test-key", json.dumps(detail))
        self.assertEqual(self.worker.run_once(), "idle")
        self.assertEqual(len(self.calls), 3)

    def test_retry_success_settles_one_job_once_and_worker_restart_does_not_repeat(self):
        self.failures_remaining = 2
        task = self.task()
        before = self.wallet()['remaining_credit']
        response = self.browser.post(f"/api/tasks/{task['task_id']}/run",
                                     json={"request_id": uuid4().hex, "confirm_generation": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.wallet()['frozen_credit'], 40)
        self.assertEqual(self.worker.run_once(), 'completed')
        detail = self.browser.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(detail['status'], 'awaiting_review')
        self.assertEqual((len(detail['outputs']), len(detail['executions'])), (1, 1))
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertEqual(self.wallet()['remaining_credit'], before - 40)
        self.assertEqual(self.wallet()['frozen_credit'], 0)
        restarted = RemoteWorker(self.access.origin, self.access.worker_token, self.root / 'worker', self.worker.backend,
                                 client=TestClient(self.app, base_url=self.access.origin))
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.run_once(), 'idle')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({call.content for call in self.calls}), 1)

    def test_four_workers_share_six_multiuser_tasks_without_crossing_results_or_credit(self):
        self.app.state.workbench.scheduling_policy = SchedulingPolicy(4, 4)
        other = TestClient(self.app, base_url=self.access.origin)
        self.addCleanup(other.close)
        csrf = other.get('/api/auth/config').json()['csrf_token']
        credentials = {'username': 'secondimageuser', 'password': 'another-test-password'}
        for endpoint in ('register', 'login'):
            response = other.post('/api/auth/' + endpoint, json=credentials, headers={'X-Workbench-CSRF': csrf})
            self.assertEqual(response.status_code, 200)
        other.headers['X-Workbench-CSRF'] = other.get('/api/config').json()['csrf_token']
        response = other.post('/api/main-projects', json={'name': '另一个用户', 'request_id': uuid4().hex})
        self.assertEqual(response.status_code, 200)
        other_sid = response.json()['subprojects'][0]['subproject_id']
        clients, tasks, expected = [self.browser, other], [], {}
        colors = ['red', 'green', 'blue', 'yellow', 'purple', 'coral']
        for index in range(6):
            owner = index % 2
            client = clients[owner]
            sid = self.sid if owner == 0 else other_sid
            response = client.post(f'/api/subprojects/{sid}/tasks', data={
                'request_id': uuid4().hex, 'operation': 'generate', 'prompt': f'多用户并行任务 {index}', 'aspect_ratio': '1:1'})
            self.assertEqual(response.status_code, 200)
            task = response.json()
            tasks.append((owner, task))
            expected[task['image_prompt']['compiled_prompt']] = png(colors[index])
            response = client.post(f"/api/tasks/{task['task_id']}/run", json={'request_id': uuid4().hex, 'confirm_generation': True})
            self.assertEqual(response.status_code, 200)
        lock, four_active, release = threading.Lock(), threading.Event(), threading.Event()
        observed = {'active': 0, 'peak': 0, 'calls': 0}

        def handler(request):
            with lock:
                observed['active'] += 1
                observed['calls'] += 1
                observed['peak'] = max(observed['peak'], observed['active'])
                if observed['active'] == 4:
                    four_active.set()
            try:
                if not release.wait(timeout=15):
                    raise RuntimeError('parallel workers did not reach the provider together')
                raw = expected[json.loads(request.content)['prompt']]
                return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(raw).decode()}]})
            finally:
                with lock:
                    observed['active'] -= 1

        workers = []
        for index in range(5):
            provider = httpx.Client(transport=httpx.MockTransport(handler))
            self.addCleanup(provider.close)
            backend = ImageAPIBackend('https://provider.test', 'parallel-test-key', client=provider)
            worker = RemoteWorker(self.access.origin, self.access.worker_token, self.root / f'parallel-{index}', backend,
                                  client=TestClient(self.app, base_url=self.access.origin))
            self.addCleanup(worker.close)
            workers.append(worker)
        self.assertEqual(len({worker.worker_id for worker in workers}), 5)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker.run_once) for worker in workers[:4]]
            try:
                self.assertTrue(four_active.wait(timeout=15))
                jobs = self.app.state.workbench.list_jobs()
                self.assertEqual(sum(job['status'] == 'running' for job in jobs), 4)
                self.assertEqual(sum(job['status'] == 'queued' for job in jobs), 2)
                self.assertEqual(workers[4].run_once(), 'idle')
                for client in clients:
                    self.assertEqual(client.get('/api/credits').json()['credits']['frozen_credit'], 120)
            finally:
                release.set()
            self.assertEqual([future.result(timeout=30) for future in futures], ['completed'] * 4)
            next_results = list(pool.map(lambda worker: worker.run_once(), workers[:4]))
        self.assertEqual(next_results.count('completed'), 2)
        self.assertEqual(next_results.count('idle'), 2)
        self.assertEqual(observed, {'active': 0, 'peak': 4, 'calls': 6})
        for owner, task in tasks:
            detail = clients[owner].get(f"/api/tasks/{task['task_id']}").json()
            self.assertEqual(detail['status'], 'awaiting_review')
            self.assertEqual(len(detail['outputs']), 1)
            output = detail['outputs'][0]
            self.assertEqual(clients[owner].get(output['url']).content, expected[task['image_prompt']['compiled_prompt']])
            self.assertEqual(clients[1 - owner].get(f"/api/tasks/{task['task_id']}").status_code, 404)
            self.assertEqual(clients[1 - owner].get(output['url']).status_code, 404)
        for client in clients:
            wallet = client.get('/api/credits').json()['credits']
            self.assertEqual((wallet['remaining_credit'], wallet['frozen_credit']), (80, 0))
