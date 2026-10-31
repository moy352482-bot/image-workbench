import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import LocalFileBackend, StorageConfig, StorageManager
from image_workbench.web import create_workspace_app as create_app  # 内层工作台契约；账号边界见 test_accounts。
from image_workbench.worker import LocalWorker


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data", max_image_bytes=2048))
        self.fixture = self.root / "fixture.png"
        Image.new("RGB", (48, 32), "teal").save(self.fixture)
        self.app = create_app(self.storage, backend="local")
        self.state = self.app.state.workbench
        self.client = TestClient(self.app, base_url="http://127.0.0.1:7860", raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.headers = {"X-Workbench-CSRF": self.client.get("/api/config").json()["csrf_token"]}
        response = self.post("/api/projects", json={"request_id": uuid4().hex, "name": "中文测试项目"})
        self.assertEqual(response.status_code, 200, response.text)
        self.pid = response.json()["project_id"]

    def post(self, path, **kwargs):
        return self.client.post(path, headers=self.headers, **kwargs)

    def task(self, **changes):
        data = {"request_id": uuid4().hex, "operation": "generate", "prompt": "画一只鸟 <script>window.bad=1</script>"}
        data.update(changes)
        result = self.post(f"/api/projects/{self.pid}/tasks", data=data)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def execute(self, task):
        response = self.post(f"/api/projects/{self.pid}/tasks/{task['task_id']}/run",
                             json={"request_id": uuid4().hex, "confirm_generation": True})
        self.assertEqual(response.status_code, 200, response.text)
        LocalWorker(self.state, LocalFileBackend([self.fixture])).serve(once=True)
        return self.client.get(f"/api/projects/{self.pid}/tasks/{task['task_id']}").json()

    def test_home_static_and_guard_reject_cross_site_wrong_host_and_missing_csrf(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Image Workbench", home.text)
        self.assertIn("frame-ancestors 'none'", home.headers["content-security-policy"])
        self.assertEqual(self.client.get("/static/workbench.js").status_code, 200)
        body = {"request_id": uuid4().hex, "name": "bad"}
        self.assertEqual(self.client.post("/api/projects", json=body).status_code, 403)
        self.assertEqual(self.client.post("/api/projects", json=body, headers={**self.headers, "Origin": "https://example.org"}).status_code, 403)
        self.assertEqual(self.client.get("/api/projects", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/data/projects").status_code, 404)

    def test_project_and_task_duplicate_save_do_not_create_or_execute_twice(self):
        rid = uuid4().hex
        body = {"request_id": rid, "name": "重试项目"}
        first = self.post("/api/projects", json=body)
        self.assertEqual(first.json(), self.post("/api/projects", json=body).json())
        self.assertEqual(self.post("/api/projects", json={**body, "name": "不同名称"}).status_code, 409)
        tid = uuid4().hex
        first_task = self.task(request_id=tid)
        self.assertEqual(first_task, self.task(request_id=tid))
        self.assertEqual(len(self.storage.list_tasks(self.pid)), 1)
        self.assertEqual(self.state.runner.list_executions(self.pid, tid), [])
        self.assertEqual(self.state.list_jobs(), [])

    def test_task_source_metadata_is_optional_preserved_and_idempotent(self):
        source = {"kind": "prompt_enhancement", "enhancement_id": "e" * 32,
                  "skill_id": "image-generation", "skill_version": 1,
                  "mode": "standard", "task_type": "generate"}
        data = {"request_id": uuid4().hex, "operation": "generate", "prompt": "强化后的提示词",
                "original_prompt": "一只猫", "prompt_source": json.dumps(source, ensure_ascii=False)}
        response = self.post(f"/api/projects/{self.pid}/tasks", data=data)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual(task["original_prompt"], "一只猫")
        self.assertEqual(task["prompt_source"], source)
        options = self.state.task_options(self.pid, task["task_id"])
        self.assertEqual(options["original_prompt"], "一只猫")
        self.assertEqual(options["prompt_source"], source)
        self.assertEqual(self.post(f"/api/projects/{self.pid}/tasks", data=data).json(), task)
        self.assertEqual(self.post(f"/api/projects/{self.pid}/tasks", data={**data, "original_prompt": "另一只猫"}).status_code, 409)
        self.assertEqual(self.post(f"/api/projects/{self.pid}/tasks",
                                   data={**data, "request_id": uuid4().hex, "prompt_source": "not-json"}).status_code, 400)
        self.assertEqual(self.post(f"/api/projects/{self.pid}/tasks",
                                   data={**data, "request_id": uuid4().hex,
                                         "prompt_source": json.dumps({**source, "mode": "extreme"})}).status_code, 400)
        legacy = self.task()
        self.assertIsNone(legacy["original_prompt"])
        self.assertIsNone(legacy["prompt_source"])

    def test_multipart_png_jpeg_webp_content_and_saved_input_integrity(self):
        uploads = []
        expected = []
        for image_format in ("PNG", "JPEG", "WEBP"):
            stream = io.BytesIO()
            Image.new("RGB", (32, 24), "coral").save(stream, image_format)
            data = stream.getvalue()
            expected.append(hashlib.sha256(data).hexdigest())
            uploads.append(("files", ("../../outside.txt", data, "application/octet-stream")))
        response = self.post(f"/api/projects/{self.pid}/tasks", data={"request_id": uuid4().hex, "operation": "generate", "prompt": "参考图测试", "aspect_ratio": "3:2"}, files=uploads)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual(task["input_count"], 3)
        artifacts = self.storage.list_task_artifacts(self.pid, task["task_id"])
        self.assertCountEqual([item.sha256 for item in artifacts], expected)
        self.assertEqual({item.format for item in artifacts}, {"PNG", "JPEG", "WEBP"})
        self.assertIn("3:2", task["prompt"])
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertEqual(list((self.storage.data_root / ".workbench/uploads").iterdir()), [])

    def test_reject_bad_content_oversized_upload_unknown_paths_and_invalid_ids(self):
        target = f"/api/projects/{self.pid}/tasks"
        data = {"request_id": uuid4().hex, "operation": "generate", "prompt": "bad input"}
        self.assertEqual(self.post(target, data=data, files={"files": ("valid.png", b"not an image", "image/png")}).status_code, 400)
        self.assertEqual(self.post(target, data=data, files={"files": ("big.png", b"x" * 2049, "image/png")}).status_code, 413)
        self.assertEqual(self.post(target, data={**data, "source_path": "C:/secret.txt"}).status_code, 400)
        many = [("files", ("image.png", self.fixture.read_bytes(), "image/png"))] * 9
        self.assertEqual(self.post(target, data=data, files=many).status_code, 400)
        self.assertEqual(self.storage.list_tasks(self.pid), [])
        response = self.client.get("/api/artifacts/not-a-valid-id")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(str(self.storage.data_root), response.text)
        self.assertEqual(self.client.get("/api/artifacts/" + uuid4().hex).status_code, 404)
        self.assertEqual(self.client.post(target, content=b"x", headers={**self.headers, "Content-Length": str(42 * 1024 * 1024)}).status_code, 413)

    def test_manual_run_only_duplicate_clicks_one_job_and_approved_download_survives_restart(self):
        task = self.task()
        url = f"/api/projects/{self.pid}/tasks/{task['task_id']}"
        self.assertEqual(self.post(url + "/run", json={"request_id": uuid4().hex}).status_code, 400)
        jobs = [self.post(url + "/run", json={"request_id": uuid4().hex, "confirm_generation": True}).json() for _ in range(3)]
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        self.assertEqual(self.client.get(url).json()["status"], "queued")
        LocalWorker(self.state, LocalFileBackend([self.fixture])).serve(once=True)
        detail = self.client.get(url).json()
        self.assertEqual(detail["status"], "awaiting_review")
        self.assertEqual(len(detail["executions"]), 1)
        artifact = detail["outputs"][0]
        self.assertEqual(artifact["review_status"], "pending")
        for forbidden in ("relative_path", "original_filename", "sha256", "logs", "command", "stderr"):
            self.assertNotIn(f'"{forbidden}"', self.client.get(url).text)
        download = self.client.get(artifact["download_url"])
        self.assertEqual(download.content, self.fixture.read_bytes())
        self.assertIn("attachment", download.headers["content-disposition"])
        review = self.post(artifact["url"] + "/approve", json={})
        self.assertEqual(review.json()["review_status"], "approved")
        with TestClient(create_app(self.storage, backend="local"), base_url="http://localhost") as reopened:
            self.assertEqual(reopened.get(url).json()["status"], "approved")
            self.assertEqual(len(reopened.get(url).json()["executions"]), 1)
        self.assertEqual(self.post(url + "/run", json={"request_id": uuid4().hex, "confirm_generation": True}).json()["status"], "completed")
        self.assertEqual(len(self.state.runner.list_executions(self.pid, task["task_id"])), 1)

    def test_historical_branch_preserves_all_outputs_and_rejects_other_project_base(self):
        first = self.execute(self.task())
        base = first["outputs"][0]
        second = self.execute(self.task(operation="edit", base_artifact_id=base["artifact_id"], prompt="修改色调"))
        branch = self.execute(self.task(operation="edit", base_artifact_id=base["artifact_id"], prompt="从V1创建分支"))
        self.assertEqual(second["outputs"][0]["source_artifact_id"], base["artifact_id"])
        self.assertEqual(branch["outputs"][0]["source_artifact_id"], base["artifact_id"])
        self.assertEqual(len({base["artifact_id"], second["outputs"][0]["artifact_id"], branch["outputs"][0]["artifact_id"]}), 3)
        self.assertEqual(self.client.get(base["url"]).content, self.fixture.read_bytes())
        other = self.post("/api/projects", json={"request_id": uuid4().hex, "name": "另一个项目"}).json()
        self.assertEqual(self.post(f"/api/projects/{other['project_id']}/tasks", data={"request_id": uuid4().hex, "operation": "edit", "prompt": "不能跨项目", "base_artifact_id": base["artifact_id"]}).status_code, 400)

    def test_browser_disconnect_does_not_own_worker_lifetime_and_no_replay_on_app_recreation(self):
        started, finish = threading.Event(), threading.Event()
        fixture = self.fixture

        class SlowLocal(LocalFileBackend):
            def generate(self, request):
                started.set()
                if not finish.wait(5):
                    raise TimeoutError("test wait timed out")
                return super().generate(request)

        task = self.task()
        url = f"/api/projects/{self.pid}/tasks/{task['task_id']}"
        self.post(url + "/run", json={"request_id": uuid4().hex, "confirm_generation": True})
        worker = threading.Thread(target=LocalWorker(self.state, SlowLocal([fixture])).serve, kwargs={"once": True})
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            self.assertEqual(self.client.get(url).json()["status"], "generating")
            self.client.close()
            with TestClient(create_app(self.storage, backend="local"), base_url="http://localhost") as reopened:
                self.assertEqual(reopened.get(url).json()["status"], "generating")
                finish.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                detail = reopened.get(url).json()
                self.assertEqual(detail["status"], "awaiting_review")
                self.assertEqual(len(detail["executions"]), 1)
        finally:
            finish.set()
            worker.join(5)

    def test_changed_image_is_not_served_or_approved(self):
        detail = self.execute(self.task())
        artifact = detail["outputs"][0]
        path = self.storage.get_artifact_path(artifact["artifact_id"])
        tampered = bytearray(path.read_bytes())
        tampered[0] ^= 0xFF
        path.write_bytes(bytes(tampered))
        response = self.client.get(artifact["url"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "文件校验未通过，请刷新后重试。")
        self.assertEqual(self.post(artifact["url"] + "/approve", json={}).status_code, 400)

    def test_existing_registered_output_is_reviewable_without_a_runner_record(self):
        old = self.storage.create_task(self.pid, "generate", "以前直接登记的任务")
        artifact = self.storage.register_output_image(self.pid, old.task_id, self.fixture)
        detail = self.client.get(f"/api/projects/{self.pid}/tasks/{old.task_id}").json()
        self.assertEqual(detail["status"], "awaiting_review")
        self.assertFalse(detail["can_generate"])
        self.assertEqual(detail["outputs"][0]["artifact_id"], artifact.artifact_id)
        listed = self.client.get(f"/api/projects/{self.pid}/tasks").json()["tasks"]
        preview = next(item for item in listed if item["task_id"] == old.task_id)["preview_artifact"]
        self.assertEqual(preview["url"], f"/api/artifacts/{artifact.artifact_id}")
        self.assertNotIn("relative_path", preview)
        self.assertNotIn("sha256", preview)
        self.assertEqual(self.post(f"/api/projects/{self.pid}/tasks/{old.task_id}/run", json={"request_id": uuid4().hex, "confirm_generation": True}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
