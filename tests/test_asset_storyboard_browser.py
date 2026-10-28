"""Storyboard assets and script menus through real Chromium and temporary HTTP storage."""

import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from tests import test_asset_browser as asset_helpers
from tests.test_script_service import TEXT
from tests.test_video import png


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/goals/asset-ux/browser-storyboard.json"
SOURCES = (
    "image_workbench/web_static/asset-input.js",
    "image_workbench/web_static/asset-actions.js",
    "image_workbench/web_static/storyboard.js",
    "image_workbench/web_static/pipeline.js",
    "image_workbench/web_static/scripts.js",
)


def fingerprint():
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SOURCES}


class AssetStoryboardBrowserTests(unittest.TestCase):
    # Borrow infrastructure, not the test class, so phase A is not run twice.
    stop_server = asset_helpers.AssetBrowserTests.stop_server
    post = asset_helpers.AssetBrowserTests.post
    get = asset_helpers.AssetBrowserTests.get
    event = asset_helpers.AssetBrowserTests.event
    idle = asset_helpers.AssetBrowserTests.idle

    @classmethod
    def setUpClass(cls):
        cls.started = time.perf_counter()
        cls.results = []
        cls.source_before = fingerprint()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        after = fingerprint()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({
            "runner": "Real Chromium + uvicorn + temporary SQLite/files + deterministic Mock storyboard planner",
            "real_model_calls": 0,
            "mock_storyboard_calls": sum(case["mock_storyboard_calls"] for case in cls.results),
            "elapsed_seconds": round(time.perf_counter() - cls.started, 3),
            "source_sha256": after,
            "source_changed_during_run": cls.source_before != after,
            "cases": cls.results,
            "note": "Clipboard images and drag references use browser File/DataTransfer events and the actual app listeners. Script menu actions use real APIs; no Codex or external model is invoked.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def setUp(self):
        asset_helpers.AssetBrowserTests.setUp(self)
        self.page.set_default_timeout(10000)
        self.interactions = []
        self.model.generate.side_effect = AssertionError("This suite must not generate a script with a real model")
        self.classifier = Mock(side_effect=AssertionError("External demand classification is forbidden"))
        self.app.script_service.router.fallback = self.classifier
        self.app.storyboard_service.registry = self.app.script_service.registry
        self.planner_adapter = Mock()
        self.planner_adapter.generate.side_effect = AssertionError("Real storyboard inference is forbidden")
        self.app.storyboard_service.planner.adapter = self.planner_adapter
        self.mock_planner = Mock(wraps=self.app.storyboard_service.planner.mock)
        self.app.storyboard_service.planner.mock = self.mock_planner

    def tearDown(self):
        result = self._outcome.result
        failures = [trace for test, trace in result.failures + result.errors if test is self]
        forbidden = self.model.generate.call_count + self.planner_adapter.generate.call_count + self.classifier.call_count
        self.results.append({
            "name": self._testMethodName,
            "status": "failed" if failures or self.errors or forbidden else "passed",
            "elapsed_seconds": round(time.perf_counter() - self.case_started, 3),
            "mock_storyboard_calls": self.mock_planner.generate.call_count,
            "forbidden_model_attempts": forbidden,
            "page_errors": self.errors,
            "interactions": self.interactions,
            **({"failure": failures[0][-6000:]} if failures else {}),
        })
        self.model.generate.assert_not_called()
        self.planner_adapter.generate.assert_not_called()
        self.classifier.assert_not_called()
        self.assertEqual(self.errors, [], "Uncaught browser exceptions")

    def record(self, name, started):
        self.interactions.append({"name": name, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})

    def script(self):
        value = self.post(f"/api/video/projects/{self.pid}/script/upload",
                          data={"request_id": uuid4().hex},
                          files={"file": ("原创脚本.txt", TEXT.encode("utf-8"), "text/plain")})
        self.assertEqual(value["status"], "completed")
        return value

    def revision(self, source):
        return self.post(f"/api/scripts/tasks/{source['task_id']}/revisions", json={
            "request_id": uuid4().hex,
            "text": TEXT.replace("回家吧", "新版对白，不应成为旧版菜单的来源"),
            "change_reason": "验证菜单精确使用所选历史版本",
        })

    def plan(self, source=None):
        source = source or self.script()
        body = {"mode": "mock", "selection_mode": "recommended", "target_duration_ms": 12000,
                "aspect_ratio": "16:9", "video_model_id": "h3", "video_project_id": self.pid,
                "source_script_version": source["version_id"]}
        quote = self.post("/api/storyboards/quote", json=body)
        self.assertTrue(quote["can_generate"], quote)
        task = self.post("/api/storyboards/tasks", json={
            **body, "request_id": uuid4().hex, "quote_signature": quote["quote_signature"], "confirmed": True})
        task = self.get("/api/storyboards/tasks/" + task["task_id"])
        self.assertEqual(task["status"], "completed", task)
        return self.get("/api/storyboards/" + task["storyboard_id"])

    def open_stage(self, stage):
        fragment = "pipelineVideos" if stage == "video" else "pipelineScripts"
        self.page.goto(self.origin + f"/video?project={self.pid}#{fragment}", wait_until="networkidle")
        self.page.wait_for_function("(id) => window.IWVideo?.snapshot().project?.project_id === id", arg=self.pid)
        self.page.locator(f'[data-pipeline-step="{stage}"]').click()
        self.page.locator(f'[data-pipeline-tab="{stage}"][data-mode="generate"]').click()
        if stage == "video":
            self.board_panel = self.page.locator("#pipelineVideos > .pipeline-stage-body .storyboard-panel").filter(
                has=self.page.get_by_role("heading", name="AI 分镜规划与审核", exact=True))
            expect(self.board_panel.get_by_role("button", name="保存选中镜头", exact=True)).to_be_visible()

    def assets(self):
        return self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]

    def image_files(self):
        return {str(path.relative_to(self.storage.data_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.storage.data_root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}}

    def save_shot(self, board):
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(f"/api/storyboards/{board['storyboard_id']}/edit")) as saved:
            self.board_panel.get_by_role("button", name="保存选中镜头", exact=True).click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        self.page.wait_for_function("() => window.IWStoryboard.assetTarget()?.version > 1")
        return self.get("/api/storyboards/" + board["storyboard_id"])

    def script_card(self, source):
        return self.page.locator("#pipelineScriptTasks .pipeline-task").filter(
            has=self.page.locator(f'a[href*="task_id={source["task_id"]}"]'))

    def more(self, source):
        card = self.script_card(source)
        expect(card).to_have_count(1)
        details = card.locator(".asset-actions-menu")
        details.locator("summary").click()
        return details

    def test_default_selected_storyboard_paste_preserves_text_and_persists_same_artifact(self):
        board = self.plan()
        first = board["shots"][0]
        self.open_stage("video")
        expect(self.board_panel.locator(".storyboard-shot button[aria-pressed=true]")).to_have_count(1)
        self.assertEqual(self.page.evaluate("() => window.IWStoryboard.assetTarget()?.shot_id"), first["shot_id"])
        # Do not click the shot button: the visible default selection is the target.
        field = self.board_panel.locator('textarea[name="visual_description"]')
        draft_text = "尚未保存的画面描述：灯塔旁海浪翻涌。"
        field.fill(draft_text)
        started = time.perf_counter()
        self.assertTrue(self.event(colors=("red",), selector='#pipelineVideos textarea[name="visual_description"]'))
        self.idle()
        expect(self.page.locator("dialog.asset-choice-dialog[open]")).to_have_count(0)
        uploaded = self.assets()
        self.assertEqual(len(uploaded), 1)
        aid = uploaded[0]["artifact_id"]
        expect(self.board_panel.locator(f'input[type=checkbox][value="{aid}"]')).to_be_checked()
        expect(field).to_have_value(draft_text)
        self.assertEqual(self.storage.get_artifact(aid).format, "PNG")
        self.record("default_shot_paste_to_reference", started)
        saved = self.save_shot(board)
        shot = next(item for item in saved["shots"] if item["shot_id"] == first["shot_id"])
        self.assertEqual(shot["reference_artifact_ids"], [aid])
        self.assertEqual(shot["visual_description"], draft_text)
        self.assertTrue(all(not item["reference_artifact_ids"] for item in saved["shots"] if item["shot_id"] != first["shot_id"]))
        self.page.reload(wait_until="networkidle")
        expect(self.board_panel.locator(f'input[type=checkbox][value="{aid}"]')).to_be_checked()
        expect(field).to_have_value(draft_text)
        self.assertEqual([item["artifact_id"] for item in self.assets()], [aid])
        self.assertEqual(self.get("/api/storyboards/" + board["storyboard_id"] + "?version=1")["shots"][0]["reference_artifact_ids"], [])

    def test_tray_drag_to_storyboard_reuses_artifact_without_new_image_file(self):
        board = self.plan()
        uploaded = self.post(f"/api/video/projects/{self.pid}/assets",
                             files={"files": ("blue.png", png("blue"), "image/png")})["assets"][0]
        aid = uploaded["artifact_id"]
        before = self.image_files()
        self.open_stage("video")
        target = board["shots"][-1]
        tray = self.page.locator(f'#assetTrayGrid [data-artifact-id="{aid}"]')
        expect(tray).to_have_count(1)
        row = self.board_panel.locator(f'.storyboard-shot[data-shot-id="{target["shot_id"]}"]')
        started = time.perf_counter()
        payload = row.evaluate("""(target, aid) => {
            const source = document.querySelector(`#assetTrayGrid [data-artifact-id="${aid}"]`);
            const transfer = new DataTransfer();
            source.dispatchEvent(new DragEvent('dragstart', {dataTransfer:transfer,bubbles:true,cancelable:true}));
            target.dispatchEvent(new DragEvent('dragover', {dataTransfer:transfer,bubbles:true,cancelable:true}));
            const drop = new DragEvent('drop', {dataTransfer:transfer,bubbles:true,cancelable:true});
            target.dispatchEvent(drop);
            return {prevented:drop.defaultPrevented,payload:transfer.getData('application/x-image-workbench-asset')};
        }""", aid)
        self.assertTrue(payload["prevented"])
        self.assertEqual(json.loads(payload["payload"])["artifact_id"], aid)
        expect(self.board_panel.locator(f'input[type=checkbox][value="{aid}"]')).to_be_checked()
        self.assertEqual(self.page.evaluate("() => window.IWStoryboard.assetTarget()?.shot_id"), target["shot_id"])
        saved = self.save_shot(board)
        self.record("tray_drag_and_save_shot", started)
        shot = next(item for item in saved["shots"] if item["shot_id"] == target["shot_id"])
        self.assertEqual(shot["reference_artifact_ids"], [aid])
        self.assertEqual(self.image_files(), before)
        self.assertEqual([item["artifact_id"] for item in self.assets()], [aid])
        self.page.reload(wait_until="networkidle")
        row.locator("button[aria-pressed]").click()
        expect(self.board_panel.locator(f'input[type=checkbox][value="{aid}"]')).to_be_checked()
        self.assertEqual(self.image_files(), before)

    def test_script_more_generate_storyboard_completes_mock_for_clicked_version(self):
        source = self.script()
        latest = self.revision(source)
        self.assertNotEqual(source["version_id"], latest["version_id"])
        self.open_stage("script")
        panel = self.page.locator("#pipelineScriptForm .storyboard-panel")
        expect(panel.locator('select[name="mode"]')).to_have_value("api")
        panel.locator("summary").click()
        panel.locator('select[name="mode"]').select_option("mock")
        started = time.perf_counter()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith("/api/storyboards/quote")) as quote:
            self.more(source).get_by_role("button", name="生成分镜", exact=True).click()
        self.assertEqual(quote.value.status, 200, quote.value.text())
        self.assertEqual(quote.value.request.post_data_json["source_script_version"], source["version_id"])
        expect(panel).to_have_attribute("open", "")
        confirm = panel.get_by_role("button", name="确认此版本分镜报价并提交", exact=True)
        expect(confirm).to_be_enabled()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith("/api/storyboards/tasks")) as submitted:
            confirm.click()
        self.assertEqual(submitted.value.status, 200, submitted.value.text())
        request = submitted.value.request.post_data_json
        self.assertEqual((request["mode"], request["source_script_version"], request["video_project_id"]),
                         ("mock", source["version_id"], self.pid))
        self.assertTrue(request["confirmed"])
        task = submitted.value.json()
        expect(panel.get_by_role("link", name="在视频工作台查看此分镜", exact=True)).to_be_visible(timeout=15000)
        finished = self.get("/api/storyboards/tasks/" + task["task_id"])
        self.assertEqual(finished["status"], "completed", finished)
        board = self.get("/api/storyboards/" + finished["storyboard_id"])
        self.assertEqual(board["source_script_version"], source["version_id"])
        self.assertEqual(board["source_script_artifact_id"], source["output_artifact_id"])
        self.assertEqual((board["video_project_id"], board["subproject_id"]), (self.pid, self.link["subproject_id"]))
        self.assertEqual(self.mock_planner.generate.call_count, 1)
        self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 0)
        self.record("script_menu_quote_to_mock_storyboard", started)

    def test_script_more_save_template_opens_correct_version_and_saves_private_template(self):
        source = self.script()
        self.revision(source)
        self.open_stage("script")
        started = time.perf_counter()
        menu = self.more(source)
        link = menu.get_by_role("link", name="保存为模板", exact=True)
        query = parse_qs(urlparse(link.get_attribute("href")).query)
        self.assertEqual(query["task_id"], [source["task_id"]])
        self.assertEqual(query["action"], ["template"])
        self.assertEqual(query["workspace_id"], [self.wid])
        self.assertEqual(query["main_project_id"], [self.link["main_project_id"]])
        self.assertEqual(query["subproject_id"], [self.link["subproject_id"]])
        link.click()
        dialog = self.page.locator("#scriptTemplateDialog")
        expect(dialog).to_be_visible()
        expect(self.page.locator("#scriptText")).to_have_text(TEXT)
        expect(dialog.locator("#scriptTemplateSource")).to_have_value("existing_artifact")
        expect(dialog.locator("#scriptTemplateSourceInfo")).to_contain_text("v1")
        expect(self.page.locator("#scriptSubproject")).to_have_value(self.link["subproject_id"])
        dialog.locator("#scriptDraftName").fill("菜单导出的海边故事模板")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith("/api/scripts/template-drafts")) as draft:
            dialog.get_by_role("button", name="生成结构化草稿", exact=True).click()
        self.assertEqual(draft.value.status, 200, draft.value.text())
        self.assertEqual(draft.value.request.post_data_json["task_id"], source["task_id"])
        self.assertEqual(draft.value.json()["source_preview"], TEXT)
        expect(dialog.locator("#scriptTemplateEditor")).to_be_visible()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith("/api/scripts/templates")) as saved:
            dialog.get_by_role("button", name="确认并保存模板", exact=True).click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        value = saved.value.json()
        self.assertEqual(value["source_artifact_id"], source["output_artifact_id"])
        self.assertEqual(value["source_version"], 1)
        self.assertEqual(value["owner_account_id"], self.aid)
        self.assertEqual(value["workspace_id"], self.wid)
        self.assertEqual(value["visibility"], "private")
        self.assertEqual(value["name"], "菜单导出的海边故事模板")
        persisted = self.get("/api/scripts/templates/" + value["template_id"])
        self.assertEqual(persisted["source_artifact_id"], source["output_artifact_id"])
        expect(dialog.locator("#scriptPersonalTemplates")).to_contain_text("菜单导出的海边故事模板")
        self.assertEqual(self.mock_planner.generate.call_count, 0)
        self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 0)
        self.record("script_menu_to_saved_private_template", started)


if __name__ == "__main__":
    unittest.main()
