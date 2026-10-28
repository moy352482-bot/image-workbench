import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import digest
from image_workbench.script_models import ScriptModelRegistry
from image_workbench.storage import _now
from image_workbench.web import create_app
from tests.test_video import png, mp4
from tests.test_script_service import TEXT


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.app = create_app(self.storage, backend="local")
        self.app.video_store.enabled_backends = ("h3", "online-2.0")  # Legacy queue compatibility fixture.
        self.identity = self.app.identity
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "pipeline_alice", None)
            self.bid, self.bwid = self.identity._account(db, "pipeline_bobby", None)
        self.other_wid = self.identity.create_workspace(self.aid, "其他空间")["workspace_id"]
        self.alice = self.client(self.aid, self.wid)
        self.bobby = self.client(self.bid, self.bwid)
        self.anon = self.client()
        self.pid = uuid4().hex
        result = self.alice.post("/api/video/projects", json={"request_id": self.pid, "name": "三步创作", "script": ""})
        self.assertEqual(result.status_code, 200, result.text)
        self.prefix = "/api/video/projects/" + self.pid
        result = self.alice.post(self.prefix + "/pipeline", json={})
        self.assertEqual(result.status_code, 200, result.text)
        self.link = result.json()
        self.adapter = Mock()
        self.adapter.generate.return_value = TEXT
        self.app.script_service.adapter = self.adapter
        registry = ScriptModelRegistry(catalog={"models": [{"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "high"}]}]})
        self.app.script_service.registry = registry
        self.app.script_service.router.registry = registry

    def client(self, aid=None, wid=None):
        client = TestClient(self.app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(client.close)
        if aid:
            token, csrf = uuid4().hex, uuid4().hex
            with self.identity.transaction() as db:
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (digest(token), aid, csrf, wid, _now(), time.time()+3600, "pipeline-test"))
            client.cookies.set("iw_session", token)
            client.headers["X-Workbench-CSRF"] = csrf
        return client

    def upload_script(self, text=TEXT, rid=None):
        return self.alice.post(self.prefix + "/script/upload", data={"request_id": rid or uuid4().hex}, files={"file": ("原创脚本.txt", text.encode(), "text/plain")})

    def test_page_has_three_generate_upload_steps_and_visible_reference_input(self):
        page = self.alice.get("/video")
        self.assertEqual(page.status_code, 200)
        for value in ("pipelineImages", "pipelineScripts", "pipelineVideos", "pipelineImageReferenceFiles", "pipelineScriptUpload", "pipelineVideoUpload", "上传参考图", "生成参考图", "生成脚本", "生成视频", "SD2.0", "Seedance 2.0"):
            self.assertIn(value, page.text)
        self.assertNotIn('id="videoProjectDetail" hidden', page.text)
        self.assertNotIn('data-pipeline-pane="image:upload" hidden', page.text)
        self.assertNotIn('id="videoPricingForm"', page.text)
        self.assertNotIn('id="videoProjectForm"', page.text)
        self.assertEqual(self.alice.get("/static/pipeline.js").status_code, 200)
        self.assertEqual(self.anon.get(self.prefix + "/pipeline").status_code, 401)
        self.assertEqual(self.anon.post(self.prefix + "/pipeline", json={}).status_code, 403)
        self.assertEqual(self.alice.post(self.prefix + "/pipeline", json={}, headers={"X-Workbench-CSRF": ""}).status_code, 403)

    def test_project_binding_is_idempotent_and_bounded_to_owner(self):
        again = self.alice.post(self.prefix + "/pipeline", json={}).json()
        self.assertEqual(again, self.link)
        self.assertEqual(len(self.alice.get("/api/main-projects").json()["main_projects"]), 1)
        self.assertEqual(self.bobby.get(self.prefix + "/pipeline").status_code, 404)
        self.assertEqual(self.alice.get(self.prefix + "/pipeline", headers={"X-Workspace-ID": self.other_wid}).status_code, 404)

    def test_uploaded_reference_enters_existing_image_interface_and_generated_artifact_can_return(self):
        response = self.alice.post(self.prefix + "/assets", files=[("files", ("reference.png", png(), "image/png"))])
        self.assertEqual(response.status_code, 200, response.text)
        rid = response.json()["assets"][0]["asset_id"]
        body = {"request_id": uuid4().hex, "prompt": "原创人物在咖啡馆，保持参考人物外观", "reference_ids": [rid]}
        draft = self.alice.post(self.prefix + "/images", json=body)
        self.assertEqual(draft.status_code, 200, draft.text)
        task = draft.json()
        self.assertEqual((task["input_count"], task["estimated_credit"], task["subproject_id"]), (1, 60, self.link["subproject_id"]))
        self.assertEqual(self.alice.post(self.prefix + "/images", json=body).json()["task_id"], task["task_id"])
        self.assertEqual(self.alice.get(task["inputs"][0]["url"]).content, png())
        # Register a local synthetic output: no image model is called by this test.
        source = self.root / "generated.png"; source.write_bytes(png("blue"))
        artifact = self.storage.register_output_image(task["project_id"], task["task_id"], source)
        imported = self.alice.post(self.prefix + "/images/from-artifact", json={"artifact_id": artifact.artifact_id})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(len(self.alice.get(self.prefix).json()["assets"]), 2)
        self.assertEqual(self.alice.post(self.prefix + "/images/from-artifact", json={"artifact_id": artifact.artifact_id}).status_code, 200)
        self.assertEqual(len(self.alice.get(self.prefix).json()["assets"]), 2)
        self.assertEqual(self.bobby.post(self.prefix + "/images/from-artifact", json={"artifact_id": artifact.artifact_id}).status_code, 404)

    def test_reference_cannot_be_taken_from_another_video_project(self):
        pid = uuid4().hex
        self.alice.post("/api/video/projects", json={"request_id": pid, "name": "其他项目"})
        asset = self.alice.post(f"/api/video/projects/{pid}/assets", files=[("files", ("x.png", png(), "image/png"))]).json()["assets"][0]
        response = self.alice.post(self.prefix + "/images", json={"request_id": uuid4().hex, "prompt": "示意图", "reference_ids": [asset["asset_id"]]})
        self.assertEqual(response.status_code, 404)

    def test_deleted_linked_project_is_hidden_and_restore_recovers_original_assets(self):
        asset = self.alice.post(self.prefix + "/assets", files=[
            ("files", ("reference.png", png(), "image/png"))]).json()["assets"][0]
        original_assets = self.alice.get(self.prefix).json()["assets"]
        mid = self.link["main_project_id"]
        self.assertEqual(self.alice.delete(f"/api/project-actions/{mid}").status_code, 200)
        self.assertEqual(self.alice.get("/api/video/projects").json()["projects"], [])
        for path in (self.prefix, self.prefix + "/pipeline", asset["url"]):
            self.assertEqual(self.alice.get(path).status_code, 404, path)
        self.assertEqual(self.alice.post(self.prefix + "/assets", files=[
            ("files", ("new.png", png("blue"), "image/png"))]).status_code, 404)
        self.assertEqual(self.alice.post(f"/api/project-actions/{mid}/restore").status_code, 200)
        self.assertEqual(self.alice.get("/api/video/projects").json()["projects"][0]["project_id"], self.pid)
        self.assertEqual(self.alice.get(asset["url"]).content, png())
        self.assertEqual(self.alice.get(self.prefix).json()["assets"], original_assets)
        self.adapter.generate.assert_not_called()

    def test_linked_project_moved_out_of_workspace_is_not_listed_or_readable(self):
        mid = self.link["main_project_id"]
        self.assertEqual(self.alice.post(f"/api/project-actions/{mid}/move",
                                       json={"workspace_id": self.other_wid}).status_code, 200)
        self.assertEqual(self.alice.get("/api/video/projects").json()["projects"], [])
        self.assertEqual(self.alice.get(self.prefix).status_code, 404)
        self.assertEqual(self.bobby.get(self.prefix).status_code, 404)

    def test_script_upload_versions_and_generation_are_saved_and_selected(self):
        rid = uuid4().hex
        response = self.upload_script(rid=rid)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual(task["source"], "upload")
        self.assertEqual(task["output_version"], 1)
        self.assertEqual(self.alice.get(self.prefix).json()["script"], TEXT)
        self.assertEqual(self.upload_script(rid=rid).json()["task_id"], task["task_id"])
        self.assertEqual(self.upload_script(TEXT + "新增一句\n", rid).status_code, 409)
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)
        self.adapter.generate.assert_not_called()
        body = {"subproject_id": self.link["subproject_id"], "generation_type": "script", "input_request": "写原创台灯产品短视频",
                "selection_mode": "manual", "model_id": "gpt-6-luna", "thinking_level": "high"}
        quote = self.alice.post("/api/scripts/quote", json=body)
        self.assertEqual(quote.status_code, 200, quote.text)
        generated = self.alice.post("/api/scripts/tasks", json={**body, "request_id": uuid4().hex, "quote_signature": quote.json()["quote_signature"]})
        self.assertEqual(generated.status_code, 200, generated.text)
        tid = generated.json()["task_id"]
        self.assertEqual(self.alice.post(self.prefix + "/script/from-task", json={"task_id": tid}).status_code, 200)
        self.assertEqual(self.alice.get(self.prefix + "/pipeline").json()["active_script_task_id"], tid)
        self.assertEqual(self.alice.get(f"/api/scripts/tasks/{tid}/download").content.decode(), TEXT)
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_uploaded_text_must_be_utf8_txt_and_has_no_cross_account_access(self):
        for name, data in (("bad.pdf", b"text"), ("bad.txt", b"\xff\xfe"), ("empty.txt", b""), ("huge.txt", b"x"*(256*1024+1))):
            response = self.alice.post(self.prefix + "/script/upload", data={"request_id": uuid4().hex}, files={"file": (name, data)})
            self.assertEqual(response.status_code, 400, response.text)
        uploaded = self.upload_script().json()
        self.assertEqual(self.bobby.get(f"/api/scripts/tasks/{uploaded['task_id']}/download").status_code, 404)
        self.assertEqual(self.bobby.post(self.prefix + "/script/from-task", json={"task_id": uploaded["task_id"]}).status_code, 404)

    def test_mp4_upload_preview_download_idempotency_and_no_charge(self):
        rid = uuid4().hex
        def upload(data=mp4()):
            return self.alice.post(self.prefix + "/videos/upload", data={"request_id": rid}, files={"file": ("已有成片.mp4", data, "video/mp4")})
        response = upload()
        self.assertEqual(response.status_code, 200, response.text)
        shot = response.json()
        self.assertEqual((shot["source"], shot["status"], shot["estimated_credit"]), ("upload", "completed", 0))
        self.assertEqual(self.alice.get(shot["output_url"]).content, mp4())
        self.assertEqual(self.alice.get(shot["download_url"]).content, mp4())
        self.assertEqual(upload().status_code, 200)
        self.assertEqual(len(self.alice.get(self.prefix).json()["shots"]), 1)
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)
        self.assertEqual(self.bobby.get(shot["output_url"]).status_code, 404)
        self.assertEqual(self.alice.post(self.prefix + f"/shots/{rid}/run", json={"request_id": uuid4().hex, "confirm_generation": True, "expected_credit": 200}).status_code, 409)
        self.assertEqual(upload(b"invalid MP4").status_code, 400)

    def test_h3_generation_still_receives_uploaded_images_and_selected_script(self):
        self.upload_script()
        asset = self.alice.post(self.prefix + "/assets", files=[("files", ("hero.png", png(), "image/png"))]).json()["assets"][0]
        shot = self.alice.post(self.prefix + "/shots", json={"request_id": uuid4().hex, "name": "镜头一", "prompt": "主角挥手",
            "mode": "I2V", "duration": 4, "aspect_ratio": "landscape", "reference_ids": [asset["asset_id"]]}).json()
        response = self.alice.post(self.prefix + f"/shots/{shot['shot_id']}/run", json={"request_id": uuid4().hex, "confirm_generation": True, "expected_credit": 160})
        self.assertEqual(response.status_code, 200, response.text)
        manifest = self.app.video_store.claim(uuid4().hex)
        self.assertEqual(manifest["project"]["script"], TEXT)
        self.assertEqual(manifest["assets"][0]["sha256"], hashlib.sha256(png()).hexdigest())
        self.assertEqual(manifest["task"]["reference_ids"], [asset["asset_id"]])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 160)

