"""Real Chromium/HTTP coverage for shared image input; no model is invoked."""

import base64
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
import uvicorn

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import digest
from image_workbench.script_models import ScriptModelRegistry
from image_workbench.storage import _now
from image_workbench.web import create_app
from tests.test_video import png


REPORT = Path(__file__).resolve().parents[1] / "docs/goals/asset-ux/browser-A.json"
SCREENSHOTS = REPORT.parent / "screenshots"


class AssetBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.started = time.perf_counter()
        cls.results = []
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({
            "runner": "Python Playwright Chromium + real uvicorn + temporary SQLite/files",
            "model_calls": 0,
            "elapsed_seconds": round(time.perf_counter() - cls.started, 3),
            "cases": cls.results,
            "note": "Clipboard image/drop events carry browser File/DataTransfer objects. Text paste also uses the real browser clipboard and Ctrl+V.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def setUp(self):
        self.case_started = time.perf_counter()
        self.temp = tempfile.TemporaryDirectory(prefix="iw-asset-browser-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.app = create_app(self.storage, backend="local")
        identity = self.app.identity
        with identity.transaction() as db:
            self.aid, self.wid = identity._account(db, "asset_browser", None)
            self.token, self.csrf = uuid4().hex, uuid4().hex
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                       (digest(self.token), self.aid, self.csrf, self.wid, _now(), time.time() + 3600, "browser-test"))
        self.model = Mock()
        self.app.script_service.adapter = self.model
        registry = ScriptModelRegistry(catalog={"models": [
            {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "high"}]}]})
        self.app.script_service.registry = registry
        self.app.script_service.router.registry = registry
        self.client = TestClient(self.app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.client.cookies.set("iw_session", self.token)
        self.client.headers["X-Workbench-CSRF"] = self.csrf
        self.pid = uuid4().hex
        self.post("/api/video/projects", json={"request_id": self.pid, "name": "浏览器项目 A"})
        self.link = self.post(f"/api/video/projects/{self.pid}/pipeline", json={})
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", lifespan="off"))
        self.server_thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                self.fail("uvicorn failed to start")
            time.sleep(.01)
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 1100},
                                                permissions=["clipboard-read", "clipboard-write"])
        self.context.add_cookies([{"name": "iw_session", "value": self.token, "url": self.origin}])
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(6000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def stop_server(self):
        self.server.should_exit = True
        self.server_thread.join(timeout=5)

    def tearDown(self):
        result = self._outcome.result
        failed = any(test is self for test, _ in result.failures + result.errors)
        self.model.generate.assert_not_called()
        screenshot = SCREENSHOTS / (self._testMethodName + ".png")
        self.page.screenshot(path=str(screenshot), full_page=True)
        self.results.append({"name": self._testMethodName,
                             "status": "failed" if failed else "passed",
                             "elapsed_seconds": round(time.perf_counter() - self.case_started, 3),
                             "page_errors": self.errors,
                             "screenshot": str(screenshot.relative_to(REPORT.parent)).replace("\\", "/")})
        self.assertEqual(self.errors, [], "Uncaught browser exceptions")

    def post(self, path, **kwargs):
        response = self.client.post(path, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def video(self):
        self.page.goto(self.origin + "/video", wait_until="networkidle")
        self.page.wait_for_function("(id) => window.IWVideo?.snapshot().project?.project_id === id", arg=self.pid)
        self.page.locator('[data-pipeline-tab="image"][data-mode="generate"]').click()

    def root_page(self):
        self.page.goto(self.origin + "/", wait_until="networkidle")
        self.page.locator(".project-child").first.click()
        expect(self.page.locator("#taskForm")).to_be_visible()
        expect(self.page.locator("#saveTaskButton")).to_be_enabled()

    def files(self, *colors):
        return [{"name": color + ".png", "mimeType": "image/png", "buffer": png(color)} for color in colors]

    def event(self, kind="paste", colors=("red",), selector="body", text=""):
        files = [{"name": color + ".png", "type": "image/png",
                  "data": base64.b64encode(png(color)).decode()} for color in colors]
        return self.page.locator(selector).evaluate("""(target, args) => {
            const transfer = new DataTransfer();
            for (const value of args.files) {
                const bytes = Uint8Array.from(atob(value.data), c => c.charCodeAt(0));
                transfer.items.add(new File([bytes], value.name, {type: value.type}));
            }
            if (args.text) transfer.setData('text/plain', args.text);
            const event = args.kind === 'paste'
                ? new ClipboardEvent('paste', {clipboardData:transfer, bubbles:true, cancelable:true})
                : new DragEvent('drop', {dataTransfer:transfer, bubbles:true, cancelable:true});
            target.dispatchEvent(event);
            return event.defaultPrevented;
        }""", {"kind": kind, "files": files, "text": text})

    def idle(self):
        self.page.wait_for_function("() => !window.IWImageInput.uploading")

    def selected_root_assets(self, count):
        expect(self.page.locator("#filePreviewList .file-tile")).to_have_count(count)

    def save_root_task(self):
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(f"/api/subprojects/{self.link['subproject_id']}/tasks")) as saved:
            with self.page.expect_response(lambda response: response.request.method == "POST"
                                           and "/api/tasks/" in response.url
                                           and response.url.endswith("/run")) as submitted:
                self.page.locator("#saveTaskButton").click()
        response = saved.value
        self.assertEqual(response.status, 200, response.text())
        run_response = submitted.value
        self.assertEqual(run_response.status, 200, run_response.text())
        self.last_run = run_response.json()
        expect(self.page.locator("#promptInput")).to_have_value("")
        return response.json()

    def test_video_paste_upload_drop_deduplicate_and_artifact(self):
        self.video()
        self.assertTrue(self.event(colors=("red",)))
        self.idle()
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(1)
        self.assertTrue(self.event(colors=("red", "blue")))
        self.idle()
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(2)
        self.assertTrue(self.event("drop", ("green",), "#pipelineImageForm"))
        self.idle()
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(3)
        self.page.locator("#pipelineImageReferenceFiles").set_input_files(self.files("red", "yellow"))
        self.idle()
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(4)
        project = self.get(f"/api/video/projects/{self.pid}")
        assets = self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]
        self.assertEqual(len(project["assets"]), 4)
        self.assertEqual(len(assets), 4)
        self.assertEqual({item["artifact_id"] for item in assets}, {item["asset_id"] for item in project["assets"]})
        self.page.wait_for_function("""() => [...document.querySelectorAll('#pipelineImageReferences img')]
            .every(img => img.complete && img.naturalWidth > 0)""")

    def test_video_text_paste_invalid_input_and_network_failure_preserve_draft(self):
        self.video()
        prompt = self.page.locator("#pipelineImagePrompt")
        prompt.fill("保留已有需求")
        self.assertFalse(self.event(colors=(), selector="#pipelineImagePrompt", text="普通文字"))
        self.assertFalse(self.event(colors=("red",), selector="#pipelineImagePrompt", text="有文字的剪贴板"))
        self.assertEqual(self.get(f"/api/video/projects/{self.pid}")["assets"], [])
        self.page.evaluate("() => navigator.clipboard.writeText(' + 正常粘贴')")
        prompt.focus()
        prompt.press("End")
        prompt.press("Control+V")
        expect(prompt).to_have_value("保留已有需求 + 正常粘贴")
        self.event()
        self.idle()
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(1)
        self.page.locator("#pipelineImageReferenceFiles").set_input_files(
            {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"plain text"})
        expect(self.page.locator("#videoNotice")).to_contain_text("只支持")
        self.page.locator("#pipelineImageReferenceFiles").set_input_files(
            {"name": "pretend.png", "mimeType": "image/png", "buffer": b"not an image"})
        self.idle()
        expect(self.page.locator("#videoNotice")).to_contain_text("已填写的内容已保留")
        self.page.locator("#pipelineImageReferenceFiles").set_input_files(self.files(*(["blue"] * 10)))
        expect(self.page.locator("#videoNotice")).to_contain_text("最多添加")
        self.page.route(f"**/api/video/projects/{self.pid}/assets", lambda route: route.abort("failed"))
        self.event(colors=("blue",))
        self.idle()
        expect(prompt).to_have_value("保留已有需求 + 正常粘贴")
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(1)
        expect(self.page.locator("#videoNotice")).to_contain_text("已填写的内容已保留")
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["assets"]), 1)

    def test_video_stage_paste_uses_frames_and_script_stage_asks_purpose(self):
        self.video()
        self.page.locator('[data-pipeline-step="video"]').click()
        self.page.locator("#videoMode").select_option("I2V")
        self.event(colors=("red",))
        self.idle()
        self.event(colors=("blue",))
        self.idle()
        expect(self.page.locator("#videoReferenceChoices input:checked")).to_have_count(2)
        expect(self.page.locator(".video-reference-order").filter(has_text="首帧")).to_have_count(1)
        expect(self.page.locator(".video-reference-order").filter(has_text="末帧")).to_have_count(1)
        original = self.page.evaluate("() => [...window.IWVideo.snapshot().referenceOrder]")
        self.event(colors=("green",))
        self.idle()
        expect(self.page.locator("#videoNotice")).to_contain_text("最多选择 2 张")
        self.assertEqual(self.page.evaluate("() => window.IWVideo.snapshot().referenceOrder"), original)
        self.page.locator('[data-pipeline-step="script"]').click()
        self.event(colors=("yellow",))
        dialog = self.page.locator("dialog[open]")
        expect(dialog).to_contain_text("请选择用途")
        dialog.get_by_role("button", name="保存到项目素材库").click()
        self.idle()
        self.assertEqual(self.page.evaluate("() => window.IWVideo.snapshot().referenceOrder"), original)
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["assets"]), 4)

    def test_root_upload_paste_drop_are_direct_references_and_save(self):
        self.root_page()
        self.page.locator("#promptInput").fill("根据三张参考图创作海报")
        self.page.locator("#imageInput").set_input_files(self.files("red"))
        self.idle()
        self.selected_root_assets(1)
        self.event(colors=("red", "blue"))
        self.idle()
        self.selected_root_assets(2)
        self.event("drop", ("green",), "#referenceUploadPanel")
        self.idle()
        self.selected_root_assets(3)
        self.page.wait_for_function("""() => [...document.querySelectorAll('#filePreviewList img')]
            .every(img => img.complete && img.naturalWidth > 0)""")
        assets = self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]
        self.assertEqual(len(assets), 3)
        detail = self.save_root_task()
        self.assertEqual(detail["subproject_id"], self.link["subproject_id"])
        self.assertEqual(detail["input_count"], 3)
        self.assertEqual(detail["operation"], "generate")
        self.assertEqual(len(detail["inputs"]), 3)
        self.assertEqual({self.client.get(item["url"]).content for item in detail["inputs"]},
                         {png("red"), png("blue"), png("green")})
        self.assertEqual(self.last_run["status"], "queued")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.selected_root_assets(0)

    def test_root_edit_paste_sets_base_preserves_references_and_saves(self):
        self.root_page()
        self.event(colors=("red",))
        self.idle()
        self.selected_root_assets(1)
        self.page.locator(".operation-picker label").filter(has_text="修改图片").click()
        self.page.locator("#promptInput").fill("修改基底色彩并参考已有素材")
        self.event(colors=("blue",))
        self.idle()
        expect(self.page.locator("#baseImagePanel")).to_be_visible()
        expect(self.page.locator("#baseImagePreview img")).to_have_count(1)
        self.selected_root_assets(1)
        self.event(colors=("green",))
        dialog = self.page.locator("dialog[open]")
        expect(dialog).to_contain_text("待编辑图片已有内容")
        dialog.get_by_role("button", name="追加为参考图片").click()
        self.idle()
        self.selected_root_assets(2)
        base_url = self.page.locator("#baseImagePreview img").get_attribute("src")
        detail = self.save_root_task()
        self.assertEqual(detail["operation"], "edit")
        self.assertTrue(detail["base_artifact_id"])
        self.assertIn(detail["base_artifact_id"], base_url)
        self.assertEqual(detail["input_count"], 2)
        self.assertEqual(self.last_run["status"], "queued")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)

    def test_root_upload_and_save_network_failures_retain_draft_and_retry(self):
        self.root_page()
        self.page.locator("#promptInput").fill("网络失败后仍要保留")
        self.event(colors=("red",))
        self.idle()
        asset_route = f"**/api/subprojects/{self.link['subproject_id']}/assets"
        self.page.route(asset_route, lambda route: route.abort("failed"))
        self.event(colors=("blue",))
        self.idle()
        self.selected_root_assets(1)
        expect(self.page.locator("#promptInput")).to_have_value("网络失败后仍要保留")
        expect(self.page.locator("#toastRegion")).to_contain_text("已填写的内容已保留")
        self.page.unroute(asset_route)
        task_route = f"**/api/subprojects/{self.link['subproject_id']}/tasks"
        self.page.route(task_route, lambda route: route.abort("failed") if route.request.method == "POST" else route.continue_())
        self.page.locator("#saveTaskButton").click()
        expect(self.page.locator("#formMessage")).to_contain_text("可再次提交以重试")
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.selected_root_assets(1)
        expect(self.page.locator("#promptInput")).to_have_value("网络失败后仍要保留")
        self.page.unroute(task_route)
        task = self.save_root_task()
        self.assertEqual(task["input_count"], 1)
        self.assertEqual(task["prompt"], "网络失败后仍要保留")
        self.assertEqual(self.last_run["status"], "queued")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        saved = self.get(f"/api/subprojects/{self.link['subproject_id']}/tasks")["tasks"]
        self.assertEqual(len([item for item in saved if item["prompt"] == task["prompt"]]), 1)

    def test_video_inflight_upload_stays_in_original_project(self):
        other_pid = uuid4().hex
        self.post("/api/video/projects", json={"request_id": other_pid, "name": "浏览器项目 B"})
        other_link = self.post(f"/api/video/projects/{other_pid}/pipeline", json={})
        self.page.goto(self.origin + f"/video?project={self.pid}", wait_until="networkidle")
        self.page.locator("#videoProjectList").get_by_role("button", name="浏览器项目 A", exact=True).click()
        self.page.wait_for_function("(id) => window.IWVideo?.snapshot().project?.project_id === id", arg=self.pid)
        self.page.locator('[data-pipeline-tab="image"][data-mode="generate"]').click()
        pending = []
        self.page.route(f"**/api/video/projects/{self.pid}/assets", lambda route: pending.append(route))
        with self.page.expect_request(f"**/api/video/projects/{self.pid}/assets"):
            self.event(colors=("red",))
        expect(self.page.locator(".asset-input-progress")).to_be_visible()
        self.page.locator("#videoProjectList").get_by_role("button", name="浏览器项目 B", exact=True).click()
        self.page.wait_for_function("(id) => window.IWVideo.snapshot().project?.project_id === id", arg=other_pid)
        self.assertEqual(len(pending), 1)
        pending[0].continue_()
        self.idle()
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(0)
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["assets"]), 1)
        self.assertEqual(self.get(f"/api/video/projects/{other_pid}")["assets"], [])
        self.assertEqual(self.get(f"/api/subprojects/{other_link['subproject_id']}/assets")["assets"], [])

    def test_root_inflight_upload_stays_in_original_subproject(self):
        other = self.post(f"/api/main-projects/{self.link['main_project_id']}/subprojects",
                          json={"request_id": uuid4().hex, "name": "第二子项目"})
        self.root_page()
        original_sub = self.page.locator("#activeProjectName").inner_text()
        self.assertNotEqual(original_sub, "第二子项目")
        pending = []
        self.page.route(f"**/api/subprojects/{self.link['subproject_id']}/assets", lambda route: pending.append(route))
        with self.page.expect_request(f"**/api/subprojects/{self.link['subproject_id']}/assets"):
            self.event(colors=("red",))
        expect(self.page.locator(".asset-input-progress")).to_be_visible()
        self.page.locator(".project-child").filter(has_text="第二子项目").click()
        expect(self.page.locator("#activeProjectName")).to_have_text("第二子项目")
        self.page.locator("#promptInput").fill("新子项目的需求")
        self.assertEqual(len(pending), 1)
        pending[0].continue_()
        self.idle()
        self.selected_root_assets(0)
        expect(self.page.locator("#promptInput")).to_have_value("新子项目的需求")
        self.assertEqual(len(self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]), 1)
        self.assertEqual(self.get(f"/api/subprojects/{other['subproject_id']}/assets")["assets"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
