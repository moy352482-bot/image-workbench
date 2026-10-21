"""两级项目 HTTP 合同与旧路由回归；仅使用临时目录及本地图片后端。"""

import hashlib
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import LocalFileBackend, StorageConfig, StorageManager
from image_workbench.web import create_workspace_app as create_app  # 内层层级兼容性。
from image_workbench.worker import LocalWorker


class ProjectWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.fixture = self.root / "local-output.png"
        Image.new("RGB", (12, 8), (90, 30, 120)).save(self.fixture, "PNG")
        self.client = TestClient(create_app(self.storage, backend="local"),
                                 base_url="http://127.0.0.1:7860", raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.headers = {"X-Workbench-CSRF": self.client.get("/api/config").json()["csrf_token"]}

    def post(self, path, **kwargs):
        return self.client.post(path, headers=self.headers, **kwargs)

    def test_empty_root_main_subproject_task_edit_move_and_counts(self):
        config = self.client.get("/api/config").json()
        self.assertIn("project_migration_required", config)
        self.assertFalse(config["project_migration_required"])

        created = self.post("/api/main-projects", json={"request_id": uuid4().hex,
                                                        "name": "新主项目", "description": "主描述"})
        self.assertEqual(created.status_code, 200, created.text)
        main = created.json()
        self.assertEqual(main["name"], "新主项目")
        self.assertIn("subproject_count", main)
        main_id = main["main_project_id"]
        subproject_response = self.post(f"/api/main-projects/{main_id}/subprojects",
                                        json={"request_id": uuid4().hex, "name": "第一批"})
        self.assertEqual(subproject_response.status_code, 200, subproject_response.text)
        subproject = subproject_response.json()
        sid = subproject["subproject_id"]
        target_response = self.post(f"/api/main-projects/{main_id}/subprojects",
                                    json={"request_id": uuid4().hex, "name": "归档整理"})
        self.assertEqual(target_response.status_code, 200, target_response.text)
        target_sid = target_response.json()["subproject_id"]

        task_response = self.post(f"/api/subprojects/{sid}/tasks", data={
            "request_id": uuid4().hex, "operation": "generate", "prompt": "保留完整提示词",
            "name": "第一版", "description": "初始说明",
        })
        self.assertEqual(task_response.status_code, 200, task_response.text)
        task = task_response.json()
        tid = task["task_id"]
        self.assertEqual(task["subproject_id"], sid)
        self.assertEqual(task["main_project_id"], main_id)
        self.assertEqual(task["name"], "第一版")

        edit = self.client.patch(f"/api/tasks/{tid}", headers=self.headers,
                                 json={"name": "已改标题", "description": "已改说明"})
        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual(edit.json()["name"], "已改标题")
        self.assertEqual(edit.json()["prompt"], "保留完整提示词")

        moved = self.post(f"/api/tasks/{tid}/move", json={"target_subproject_id": target_sid,
                                                         "expected_subproject_id": sid})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["task_id"], tid)
        self.assertEqual(moved.json()["subproject_id"], target_sid)
        stale = self.post(f"/api/tasks/{tid}/move", json={"target_subproject_id": sid,
                                                         "expected_subproject_id": sid})
        self.assertEqual(stale.status_code, 409, stale.text)
        old_list = self.client.get(f"/api/subprojects/{sid}/tasks").json()
        new_list = self.client.get(f"/api/subprojects/{target_sid}/tasks").json()
        self.assertEqual(old_list["tasks"], [])
        self.assertEqual([item["task_id"] for item in new_list["tasks"]], [tid])
        detail = self.client.get(f"/api/main-projects/{main_id}").json()
        self.assertEqual(detail["main_project"]["task_count"], 1)
        self.assertEqual(detail["main_project"]["subproject_count"], 3)
        self.assertEqual([item["task_id"] for item in detail["recent_tasks"]], [tid])
        self.assertIn("counts_by_status", detail["main_project"])

    def test_local_queue_path_keeps_artifact_identity_and_never_calls_codex(self):
        main = self.post("/api/main-projects", json={"request_id": uuid4().hex, "name": "本地验证"}).json()
        sub = main["subprojects"][0]
        task = self.post(f"/api/subprojects/{sub['subproject_id']}/tasks", data={
            "request_id": uuid4().hex, "operation": "generate", "prompt": "仅使用本地夹具",
        }).json()
        queued = self.post(f"/api/tasks/{task['task_id']}/run", json={
            "request_id": uuid4().hex, "confirm_generation": True,
        })
        self.assertEqual(queued.status_code, 200, queued.text)
        LocalWorker(self.client.app.state.workbench, LocalFileBackend([self.fixture])).serve(once=True)
        detail = self.client.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(detail["status"], "awaiting_review")
        output = detail["outputs"][0]
        stored = self.storage.get_artifact(output["artifact_id"])
        self.assertEqual(stored.sha256, hashlib.sha256(self.fixture.read_bytes()).hexdigest())
        self.assertEqual(output["main_project_id"], main["main_project_id"])
        self.assertEqual(output["subproject_id"], sub["subproject_id"])
        self.assertNotIn("relative_path", output)

        old_project = self.post("/api/projects", json={"request_id": uuid4().hex, "name": "旧 API 项目"})
        self.assertEqual(old_project.status_code, 200, old_project.text)
        old_id = old_project.json()["project_id"]
        old_list = self.client.get("/api/projects").json()["projects"]
        self.assertIn(old_id, [item["project_id"] for item in old_list])
        old_task = self.post(f"/api/projects/{old_id}/tasks", data={"request_id": uuid4().hex,
                         "operation": "generate", "prompt": "旧路由兼容"})
        self.assertEqual(old_task.status_code, 200, old_task.text)
        self.assertEqual(self.client.get(f"/api/projects/{old_id}/tasks/{old_task.json()['task_id']}").status_code, 200)

    def test_legacy_data_is_read_only_projection_until_explicit_migration(self):
        legacy = self.storage.create_project("迁移前旧项目")
        legacy_task = self.storage.create_task(legacy.project_id, "generate", "历史任务")
        self.client.close()
        self.client = TestClient(create_app(self.storage, backend="local"),
                                 base_url="http://127.0.0.1:7860", raise_server_exceptions=False)
        self.headers = {"X-Workbench-CSRF": self.client.get("/api/config").json()["csrf_token"]}

        config = self.client.get("/api/config").json()
        self.assertTrue(config["project_migration_required"])
        projected = self.client.get("/api/main-projects")
        self.assertEqual(projected.status_code, 200, projected.text)
        self.assertIn(legacy.project_id, [item["main_project_id"] for item in projected.json()["main_projects"]])
        rejected = self.post("/api/main-projects", json={"request_id": uuid4().hex, "name": "不可写"})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        tasks = self.client.get(f"/api/main-projects/{legacy.project_id}").json()["recent_tasks"]
        self.assertIn(legacy_task.task_id, [item["task_id"] for item in tasks])

    def test_old_task_api_and_artifact_preview_contract_remain_compatible(self):
        project = self.post("/api/projects", json={"request_id": uuid4().hex, "name": "兼容项目"})
        self.assertEqual(project.status_code, 200, project.text)
        pid = project.json()["project_id"]
        created = self.post(f"/api/projects/{pid}/tasks", data={"request_id": uuid4().hex,
                          "operation": "generate", "prompt": "旧任务表单"})
        self.assertEqual(created.status_code, 200, created.text)
        tid = created.json()["task_id"]
        self.assertEqual(self.client.get(f"/api/projects/{pid}/tasks").status_code, 200)
        self.assertEqual(self.client.get(f"/api/projects/{pid}/tasks/{tid}").json()["task_id"], tid)


if __name__ == "__main__":
    unittest.main()
