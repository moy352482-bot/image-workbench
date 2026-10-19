"""脚本页面、账号边界和真实服务的离线 HTTP 合同；不调用 Codex。"""

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import digest
from image_workbench.script_models import ScriptModelRegistry
from image_workbench.script_service import ScriptService
from image_workbench.storage import _now
from image_workbench.web import create_app


TEXT = "《灯光》\n\n1-1 夜 内 书房\n人物：小林\n▲小林打开台灯，看到桌面的信。\n小林：我回来了。\n【完】\n"


class ScriptWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = StorageManager(StorageConfig(data_root=Path(self.temp.name) / "data"))
        self.app = create_app(self.storage, backend="local")
        self.identity = self.app.identity
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "script_alice", None)
            self.bid, self.bwid = self.identity._account(db, "script_bobby", None)
        self.other_wid = self.identity.create_workspace(self.aid, "另一个空间")["workspace_id"]
        catalog = {"models": [
            {"slug": model, "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max", "ultra")]}
            for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")
        ]}
        self.registry = ScriptModelRegistry(catalog=catalog)
        self.adapter = Mock()
        self.adapter.generate.return_value = TEXT
        self.app.script_service = ScriptService(self.storage, self.identity, registry=self.registry, adapter=self.adapter)
        self.service = self.app.script_service
        self.alice = self.client(self.aid, self.wid)
        self.bobby = self.client(self.bid, self.bwid)
        self.anon = self.client()
        response = self.alice.post("/api/main-projects", json={"request_id": uuid4().hex, "name": "剧本项目"})
        self.assertEqual(response.status_code, 200, response.text)
        self.main = response.json()
        self.mid = self.main["main_project_id"]
        self.sid = self.main["subprojects"][0]["subproject_id"]

    def client(self, aid=None, wid=None):
        client = TestClient(self.app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(client.close)
        if aid:
            token, csrf = uuid4().hex, uuid4().hex
            with self.identity.transaction() as db:
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (digest(token), aid, csrf, wid, _now(), time.time() + 3600, "script-http-test"))
            client.cookies.set("iw_session", token)
            client.headers["X-Workbench-CSRF"] = csrf
        return client

    def body(self, **changes):
        value = {
            "subproject_id": self.sid, "generation_type": "screenplay", "template_id": "episodic_screenplay_plaintext",
            "input_request": "写一位年轻人回家读信的原创短片剧本", "selection_mode": "manual",
            "model_id": "gpt-6-astra", "thinking_level": "high",
        }
        value.update(changes)
        return value

    def quoted(self, body=None):
        body = body or self.body()
        response = self.alice.post("/api/scripts/quote", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return {**body, "request_id": uuid4().hex, "quote_signature": response.json()["quote_signature"]}, response.json()

    def submit(self, body=None):
        payload, _ = self.quoted(body)
        response = self.alice.post("/api/scripts/tasks", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), payload

    def test_page_static_navigation_and_authentication(self):
        self.assertEqual(self.anon.get("/scripts", follow_redirects=False).status_code, 303)
        self.assertEqual(self.anon.get("/api/scripts/config").status_code, 401)
        response = self.alice.get("/scripts")
        self.assertEqual(response.status_code, 200)
        self.assertIn("脚本与剧本", response.text)
        self.assertIn("scriptModel", response.text)
        self.assertIn("scriptThinking", response.text)
        self.assertNotIn("{{ asset_version }}", response.text)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        for filename in ("scripts.js", "scripts.css"):
            self.assertEqual(self.alice.get("/static/" + filename).status_code, 200)
        self.assertIn('href="/scripts"', self.alice.get("/").text)
        self.adapter.generate.assert_not_called()

    def test_config_exposes_dynamic_efforts_templates_balance_and_no_inference(self):
        response = self.alice.get("/api/scripts/config")
        self.assertEqual(response.status_code, 200)
        config = response.json()
        self.assertEqual(config["csrf_token"], self.alice.headers["X-Workbench-CSRF"])
        self.assertEqual(config["credits"]["remaining_credit"], 200)
        luna = next(item for item in config["models"] if item["model_id"] == "gpt-6-luna")
        self.assertEqual(next(item for item in luna["thinking_levels"] if item["id"] == "max")["pricing_status"], "unpriced")
        self.assertIn("episodic_screenplay_plaintext", [item["template_id"] for item in config["templates"]])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.adapter.generate.assert_not_called()

    def test_quote_csrf_origin_and_strict_fields_do_not_generate(self):
        self.assertEqual(self.alice.post("/api/scripts/quote", json=self.body(), headers={"X-Workbench-CSRF": ""}).status_code, 403)
        self.assertEqual(self.alice.post("/api/scripts/quote", json=self.body(), headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.alice.post("/api/scripts/quote", json=self.body(source_path="C:/private.txt")).status_code, 422)
        self.assertEqual(self.alice.post("/api/scripts/quote", json=self.body(duration=True)).status_code, 422)
        payload, quote = self.quoted()
        self.assertEqual((quote["price"], quote["shortfall"], quote["can_generate"]), (40, 0, True))
        self.assertEqual(self.alice.post("/api/scripts/tasks", json={**payload, "warnings_confirmed": "true"}).status_code, 422)
        self.adapter.generate.assert_not_called()

    def test_generation_download_exact_model_idempotency_and_manual_versions(self):
        task, payload = self.submit()
        tid = task["task_id"]
        response = self.alice.get("/api/scripts/tasks/" + tid)
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        self.assertEqual((detail["status"], detail["output_version"], detail["text"]), ("completed", 1, TEXT))
        self.assertEqual(self.adapter.generate.call_count, 1)
        self.assertEqual(self.adapter.generate.call_args.kwargs["model_id"], "gpt-6-astra")
        self.assertEqual(self.adapter.generate.call_args.kwargs["thinking_level"], "high")
        self.assertEqual(self.alice.post("/api/scripts/tasks", json=payload).json()["task_id"], tid)
        self.assertEqual(self.adapter.generate.call_count, 1)
        download = self.alice.get("/api/scripts/tasks/" + tid + "/download")
        self.assertEqual(download.content.decode("utf-8"), TEXT)
        self.assertIn("text/plain", download.headers["content-type"])
        self.assertIn("utf-8", download.headers["content-type"])
        self.assertIn(".txt", download.headers["content-disposition"])
        self.assertNotIn("generation_prompt", detail)
        new_text = TEXT.replace("我回来了", "我终于回来了")
        revision = {"request_id": uuid4().hex, "text": new_text, "change_reason": "调整最后一句对白"}
        saved = self.alice.post("/api/scripts/tasks/" + tid + "/revisions", json=revision)
        self.assertEqual(saved.status_code, 200, saved.text)
        child = self.alice.get("/api/scripts/tasks/" + saved.json()["task_id"]).json()
        self.assertEqual(child["output_version"], 2)
        self.assertEqual(child["text"], new_text)
        self.assertEqual(len(child["versions"]), 2)
        self.assertEqual(child["parent_version_id"], detail["version_id"])
        self.assertEqual(self.alice.get("/api/scripts/tasks/" + tid + "/download").content, download.content)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_cross_account_workspace_and_download_boundaries(self):
        task, _ = self.submit()
        tid = task["task_id"]
        for route in ("/api/scripts/tasks/" + tid, "/api/scripts/tasks/" + tid + "/download"):
            self.assertEqual(self.bobby.get(route).status_code, 404)
            self.assertEqual(self.alice.get(route, headers={"X-Workspace-ID": self.other_wid}).status_code, 404)
            self.assertEqual(self.alice.get(route + "?workspace_id=" + self.other_wid).status_code, 404)
        self.assertEqual(self.bobby.get("/api/scripts/tasks", params={"subproject_id": self.sid}).status_code, 404)
        self.assertEqual(self.bobby.post("/api/scripts/quote", json=self.body()).status_code, 404)
        for action in ("run", "recover"):
            self.assertEqual(self.bobby.post("/api/scripts/tasks/" + tid + "/" + action, json={}).status_code, 404)
        revision = {"request_id": uuid4().hex, "text": TEXT, "change_reason": "尝试修改"}
        self.assertEqual(self.bobby.post("/api/scripts/tasks/" + tid + "/revisions", json=revision).status_code, 404)
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_free_quote_unpriced_unavailable_and_insufficient_credit(self):
        free, quote = self.quoted(self.body(model_id="gpt-6-luna", thinking_level="high"))
        self.assertEqual(quote["price"], 0)
        self.assertEqual(self.alice.post("/api/scripts/quote", json=self.body(model_id="gpt-6-luna", thinking_level="max")).status_code, 400)
        with self.identity.transaction() as db:
            db.execute("UPDATE credit_accounts SET total_credit=9 WHERE account_id=?", (self.aid,))
        payload, quote = self.quoted()
        self.assertEqual(quote["shortfall"], 31)
        response = self.alice.post("/api/scripts/tasks", json=payload)
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["remaining_credit"], 9)
        self.assertEqual(response.json()["expected_credit"], 40)
        self.adapter.generate.assert_not_called()
        response = self.alice.post("/api/scripts/tasks", json=free)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 9)
        self.registry.catalog = {"models": []}
        self.assertEqual(self.alice.post("/api/scripts/quote", json=self.body()).status_code, 400)

    def test_quality_warning_requires_explicit_acknowledgement_without_model_substitution(self):
        payload, quote = self.quoted(self.body(input_request="最高质量，多线叙事，复杂人物关系的短片剧本", thinking_level="low"))
        self.assertTrue(quote["warnings"])
        self.assertEqual(self.alice.post("/api/scripts/tasks", json=payload).status_code, 409)
        self.adapter.generate.assert_not_called()
        response = self.alice.post("/api/scripts/tasks", json={**payload, "warnings_confirmed": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.adapter.generate.call_args.kwargs["thinking_level"], "low")

    def test_adapter_failure_refunds_and_provides_history(self):
        self.adapter.generate.side_effect = RuntimeError("测试中的明确生成失败")
        task, _ = self.submit()
        detail = self.alice.get("/api/scripts/tasks/" + task["task_id"]).json()
        self.assertEqual(detail["status"], "failed")
        self.assertIn("生成失败", detail["error"])
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        tasks = self.alice.get("/api/scripts/tasks", params={"subproject_id": self.sid}).json()["tasks"]
        self.assertEqual([item["task_id"] for item in tasks], [task["task_id"]])
        self.assertEqual(self.alice.get("/api/scripts/tasks/" + task["task_id"] + "/download").status_code, 409)

    def test_saved_candidate_recovers_without_model_replay(self):
        with patch.object(self.storage, "register_output_text", side_effect=OSError("模拟归档暂时失败")):
            task, _ = self.submit()
        tid = task["task_id"]
        detail = self.alice.get("/api/scripts/tasks/" + tid).json()
        self.assertTrue(detail["can_recover"])
        self.assertEqual(detail["status"], "uncertain")
        response = self.alice.post("/api/scripts/tasks/" + tid + "/recover", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(self.adapter.generate.call_count, 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)

    def test_queued_task_can_resume_and_blocks_project_move_and_delete(self):
        payload, _ = self.quoted()
        task = self.service.submit(self.aid, self.wid, payload)
        tid = task["task_id"]
        self.assertEqual(self.alice.delete("/api/project-actions/" + self.mid).status_code, 409)
        self.assertEqual(self.alice.post("/api/project-actions/" + self.mid + "/move", json={"workspace_id": self.other_wid}).status_code, 409)
        response = self.alice.post("/api/scripts/tasks/" + tid + "/run", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.alice.get("/api/scripts/tasks/" + tid).json()["status"], "completed")
        self.assertEqual(self.alice.post("/api/scripts/tasks/" + tid + "/run", json={}).status_code, 200)
        self.assertEqual(self.adapter.generate.call_count, 1)
        moved = self.alice.post("/api/project-actions/" + self.mid + "/move", json={"workspace_id": self.other_wid})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.alice.get("/api/scripts/tasks/" + tid).status_code, 404)
        self.assertEqual(self.alice.get("/api/scripts/tasks/" + tid + "?workspace_id=" + self.other_wid).status_code, 200)

    def test_unexpected_service_error_is_logged_without_internal_detail(self):
        with patch.object(self.service, "config", side_effect=RuntimeError("private diagnostic text")):
            response = self.alice.get("/api/scripts/config")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private diagnostic text", response.text)
        self.assertIn("private diagnostic text", self.storage._path(".workbench", "web-errors.log").read_text(encoding="utf-8"))
        self.adapter.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
