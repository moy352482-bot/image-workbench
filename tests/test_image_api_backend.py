"""图片 API 协议和真实工作台队列/账本回归；外部模型由 HTTP Mock 替代。"""

import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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
        self.project = self.storage.create_project("API 测试")
        self.runner = TaskRunner(self.storage)
        self.calls = []
        self.image = png()
        self.key = "test-api-key-not-a-real-credential"
        self.response = {"data": [{"b64_json": base64.b64encode(self.image).decode()}], "usage": {"total_tokens": 2}}
        self.status = 200
        self.error = None

        def handler(request):
            self.calls.append(request)
            if self.error:
                raise self.error
            headers = {"Location": "https://other.test/image.png"} if self.status == 307 else {}
            return httpx.Response(self.status, json=self.response, headers=headers)

        self.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.client.close)
        self.backend = ImageAPIBackend("https://images.test/v1/", self.key, client=self.client)

    def run_task(self, prompt="生成一个产品图", *, base=None, inputs=()):
        task = self.storage.create_task(self.project.project_id, "edit" if base else "generate", prompt, base)
        for path in inputs:
            self.storage.add_input_image(task.project_id, task.task_id, path)
        record = self.runner.run(task.project_id, task.task_id, self.backend, idempotency_key=uuid4().hex)
        return task, record

    def run_path(self, record):
        return self.runner.get_execution_directory(record.project_id, record.task_id, record.execution_id)

    def test_generation_is_archived_once_with_auth_dimensions_and_timing(self):
        task, record = self.run_task("产品照\n画面宽高比 3:2。")
        self.assertEqual(record.status, "succeeded")
        request = self.calls[0]
        self.assertEqual(str(request.url), "https://images.test/v1/images/generations")
        self.assertEqual(request.headers["Authorization"], "Bearer " + self.key)
        body = json.loads(request.content)
        self.assertEqual((body["size"], body["n"], body["response_format"]), ("1536x1024", 1, "b64_json"))
        self.assertTrue(body["history_disabled"])
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
        _, edited = self.run_task("把杯子改成蓝色\n画面宽高比 2:3。", base=aid, inputs=[reference])
        self.assertEqual(edited.status, "succeeded")
        self.assertEqual(self.calls[-1].url.path, "/v1/images/edits")
        body = json.loads(self.calls[-1].content)
        self.assertEqual(body["size"], "1024x1536")
        self.assertEqual(len(body["images"]), 2)
        self.assertEqual(base64.b64decode(body["images"][0]["base64"]), self.image)
        self.assertEqual(base64.b64decode(body["images"][1]["base64"]), reference.read_bytes())
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

    def test_http_errors_are_not_retried_and_diagnostics_redact_key(self):
        for code in (401, 429, 502, 503, 504):
            with self.subTest(code=code):
                self.status = code
                self.response = {"error": {"message": "rejected Bearer " + self.key, "code": str(code)}}
                previous = len(self.calls)
                _, record = self.run_task()
                self.assertEqual(record.status, "execution_failed")
                self.assertEqual(record.failure_type, "image_api_failed")
                self.assertEqual(len(self.calls), previous + 1)
                self.assertEqual(record.output_artifact_ids, [])
                diagnostic = (self.run_path(record) / "work" / "api-response.json").read_text(encoding="utf-8")
                self.assertNotIn(self.key, diagnostic)
                self.assertNotIn(self.key, record.error)
                self.assertEqual(json.loads(diagnostic)["status_code"], code)

    def test_timeout_and_connection_failure_do_not_repeat_model_call(self):
        for error, status, failure in ((httpx.ReadTimeout("secret " + self.key), "timed_out", "image_api_timeout"),
                                       (httpx.ConnectError("secret " + self.key), "execution_failed", "image_api_failed")):
            with self.subTest(status=status):
                self.error = error
                previous = len(self.calls)
                _, record = self.run_task()
                self.assertEqual((record.status, record.failure_type), (status, failure))
                self.assertNotIn(self.key, record.error)
                self.assertEqual(len(self.calls), previous + 1)

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

        def handler(request):
            self.calls.append(request)
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
        self.assertEqual(len(self.calls), 1)
