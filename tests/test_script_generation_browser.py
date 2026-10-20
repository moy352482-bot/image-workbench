"""Both script entry points through real HTTP; only generation adapters are mocked."""

import json
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from playwright.sync_api import sync_playwright, expect

from image_workbench.script_models import CONFIG_ROOT, ScriptModelRegistry
from tests import test_asset_browser as asset_helpers
from tests.test_script_service import TEXT


REPORT = Path(__file__).resolve().parents[1] / "docs/goals/asset-ux/browser-C.json"
SCREENSHOTS = REPORT.parent / "screenshots"
ENTRIES = ("scripts", "pipeline")
REQUEST = "写一条原创台灯产品介绍短视频，展示书桌、开灯动作和温暖的结尾。"


class ScriptGenerationBrowserTests(unittest.TestCase):
    stop_server = asset_helpers.AssetBrowserTests.stop_server
    post = asset_helpers.AssetBrowserTests.post
    get = asset_helpers.AssetBrowserTests.get

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
        REPORT.write_text(json.dumps({
            "runner": "Real Chromium + uvicorn + temporary SQLite/files + Mock text model",
            "real_model_calls": 0,
            "mock_model_calls": sum(case["mock_model_calls"] for case in cls.results),
            "elapsed_seconds": round(time.perf_counter() - cls.started, 3),
            "interaction_elapsed_seconds": round(sum(
                interaction["elapsed_ms"] for case in cls.results
                for interaction in case["interactions"]) / 1000, 3),
            "cases": cls.results,
            "note": "Prices use a temporary copy of the real Pricing Registry; no production pricing is modified. Recorded interaction times include the actual UI action and its awaited observable outcome.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def setUp(self):
        asset_helpers.AssetBrowserTests.setUp(self)
        self._cleanups = [cleanup for cleanup in self._cleanups if cleanup[0] != self.temp.cleanup]
        self.temp._finalizer.detach()
        self.keep_temp = False
        self._cleanups.insert(0, (lambda: None if self.keep_temp else self.temp.cleanup(), (), {}))
        self.interactions = []
        self.config_root = self.root / "registry"
        self.config_root.mkdir()
        for name in ("script_models.json", "script_pricing.json", "script_routing.json"):
            shutil.copyfile(CONFIG_ROOT / name, self.config_root / name)
        self.catalog = {"models": [
            {"slug": name, "supported_reasoning_levels": [{"effort": level}
             for level in ("low", "medium", "high", "xhigh", "max", "ultra")]}
            for name in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")]}
        self.registry = ScriptModelRegistry(self.config_root, catalog=self.catalog)
        self.app.script_service.registry = self.registry
        self.app.script_service.router.registry = self.registry
        self.classifier = Mock(side_effect=AssertionError("No external demand classification is allowed"))
        self.app.script_service.router.fallback = self.classifier
        self.model.generate.return_value = TEXT
        self.page.set_default_timeout(10000)
        self.submissions = []
        self.page.on("request", lambda request: self.submissions.append(request.post_data_json)
                     if request.method == "POST" and request.url.endswith("/api/scripts/tasks") else None)

    def tearDown(self):
        result = self._outcome.result
        failed = any(test is self or getattr(test, "test_case", None) is self
                     for test, _ in result.failures + result.errors)
        failed = failed or bool(self.errors) or self.classifier.called
        self.keep_temp = failed
        screenshot = SCREENSHOTS / (self._testMethodName + ".png")
        self.page.screenshot(path=str(screenshot), full_page=True)
        self.results.append({"name": self._testMethodName,
                             "status": "failed" if failed else "passed",
                             "elapsed_seconds": round(time.perf_counter() - self.case_started, 3),
                             "mock_model_calls": self.model.generate.call_count,
                             "page_errors": self.errors,
                             "interactions": self.interactions,
                             **({"failure_data_root": str(self.root)} if self.keep_temp else {}),
                             "screenshot": str(screenshot.relative_to(REPORT.parent)).replace("\\", "/")})
        self.classifier.assert_not_called()
        self.assertEqual(self.errors, [], "Uncaught browser exceptions")

    def ui(self, kind, field):
        return self.page.locator("#" + ("script" if kind == "scripts" else "pipelineScript") + field)

    def entry(self, kind):
        if kind == "scripts":
            self.page.goto(self.origin + f"/scripts?workspace_id={self.wid}&main_project_id={self.link['main_project_id']}&subproject_id={self.link['subproject_id']}",
                           wait_until="networkidle")
            expect(self.ui(kind, "Subproject")).to_have_value(self.link["subproject_id"])
        else:
            self.page.goto(self.origin + f"/video?project={self.pid}#pipelineScripts", wait_until="networkidle")
            self.page.locator('[data-pipeline-step="script"]').click()
            self.page.locator('[data-pipeline-tab="script"][data-mode="generate"]').click()
        expect(self.ui(kind, "Model").locator("option")).to_have_count(4)
        expect(self.ui(kind, "Generate")).to_have_text("开始生成")
        expect(self.ui(kind, "Quote")).to_have_count(0)

    def ready(self, kind, model="gpt-6.1-sol", effort="high", request=REQUEST):
        self.entry(kind)
        self.ui(kind, "Type").select_option("script")
        self.ui(kind, "Template").select_option("basic_video_script")
        self.ui(kind, "Model").select_option(model)
        if model != "recommended":
            self.ui(kind, "Thinking").select_option(effort)
        self.ui(kind, "Request").fill(request)

    def record(self, label, started):
        self.interactions.append({"name": label, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})

    def tasks(self):
        return self.get(f"/api/scripts/tasks?subproject_id={self.link['subproject_id']}")["tasks"]

    def completed(self, tid, kind):
        # Poll the real task endpoint through the browser request client.
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = self.context.request.get(self.origin + f"/api/scripts/tasks/{tid}").json()
            if result["status"] not in {"queued", "running", "archiving", "checking"}:
                self.assertEqual(result["status"], "completed", result)
                return result
            self.page.wait_for_timeout(50)
        self.fail(f"{kind} task did not complete: {tid}")

    def submit(self, kind):
        expect(self.ui(kind, "Generate")).to_be_enabled()
        started = time.perf_counter()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith("/api/scripts/tasks")) as submitted:
            self.ui(kind, "Generate").click()
        response = submitted.value
        self.assertEqual(response.status, 200, response.text())
        detail = self.completed(response.json()["task_id"], kind)
        if kind == "scripts":
            expect(self.page.locator("#scriptResultStatus")).to_have_text("已完成", timeout=12000)
            expect(self.page.locator("#scriptText")).to_have_text(TEXT)
        else:
            expect(self.page.locator("#pipelineScriptTasks .pipeline-task").first).to_contain_text("已完成", timeout=12000)
        self.record(kind + "_generate_to_txt", started)
        return detail

    def assert_output(self, detail):
        self.assertEqual(detail["text"], TEXT)
        self.assertEqual(detail["output_version"], 1)
        self.assertTrue(detail["version_id"])
        self.assertTrue(detail["output_artifact_id"])
        self.assertEqual(detail["subproject_id"], self.link["subproject_id"])
        artifact = self.storage.get_artifact(detail["output_artifact_id"])
        self.assertEqual(artifact.format, "TXT")
        self.assertEqual(self.client.get(f"/api/scripts/tasks/{detail['task_id']}/download").content.decode(), TEXT)

    def test_scripts_template_style_txt_artifact_version_and_refresh(self):
        kind = "scripts"
        self.ready(kind)
        self.page.locator("details.scripts-options").filter(has=self.page.locator("#scriptStyles")).locator("summary").click()
        first_style = self.page.locator("#scriptStyles input[type=checkbox]").first
        first_style.check()
        template = self.ui(kind, "Template").input_value()
        detail = self.submit(kind)
        self.assert_output(detail)
        self.assertEqual(detail["template_id"], template)
        self.assertEqual(len(detail["style_ids"]), 1)
        expect(self.page.locator("#scriptText")).to_have_text(TEXT, timeout=12000)
        expect(first_style).to_be_checked()
        expect(self.ui(kind, "Model")).to_have_value("gpt-6.1-sol")
        expect(self.ui(kind, "Thinking")).to_have_value("high")
        started = time.perf_counter()
        self.page.locator("#scriptEdit").click()
        self.page.locator("#scriptEditedText").fill(TEXT + "\n旁白：欢迎回家。\n")
        self.page.locator("#scriptEditReason").fill("补充一句旁白")
        self.page.locator("#scriptEditSave").click()
        expect(self.page.locator("#scriptResultTitle")).to_contain_text("v2")
        self.record("scripts_edit_to_v2", started)
        revisions = self.tasks()
        latest = next(item for item in revisions if item.get("output_version") == 2)
        self.assertEqual(latest["parent_version_id"], detail["version_id"])
        self.assertEqual(self.model.generate.call_count, 1)
        started = time.perf_counter()
        self.page.reload(wait_until="networkidle")
        expect(self.page.locator("#scriptResultTitle")).to_contain_text("v2")
        expect(self.page.locator("#scriptText")).to_contain_text("欢迎回家")
        self.record("scripts_refresh_preserves_v2", started)
        self.assertEqual(self.client.get(f"/api/scripts/tasks/{detail['task_id']}/download").content.decode(), TEXT)

    def test_pipeline_template_txt_artifact_version_and_use_in_project(self):
        kind = "pipeline"
        self.ready(kind, model="gpt-6-luna", effort="high")
        expect(self.ui(kind, "Price")).to_contain_text("免费")
        detail = self.submit(kind)
        self.assert_output(detail)
        card = self.page.locator("#pipelineScriptTasks .pipeline-task").filter(has_text="v1")
        expect(card).to_be_visible(timeout=12000)
        card.get_by_role("button", name="预览脚本", exact=True).click()
        expect(card.locator("pre")).to_have_text(TEXT)
        started = time.perf_counter()
        card.get_by_role("button", name="用于当前项目", exact=True).click()
        expect(self.page.locator("#videoActiveScript")).to_have_text(TEXT)
        self.record("pipeline_apply_script_to_project", started)
        self.assertEqual(self.get(f"/api/video/projects/{self.pid}/pipeline")["active_script_task_id"], detail["task_id"])
        started = time.perf_counter()
        self.page.reload(wait_until="networkidle")
        expect(self.page.locator("#videoActiveScript")).to_have_text(TEXT)
        self.record("pipeline_refresh_preserves_active_script", started)
        expect(self.ui(kind, "Quote")).to_have_count(0)
        self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 0)

    def test_both_entries_show_registry_prices_immediately_without_model_calls(self):
        pricing = json.loads((self.config_root / "script_pricing.json").read_text(encoding="utf-8"))["models"]
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                self.entry(kind)
                for model, levels in pricing.items():
                    self.ui(kind, "Model").select_option(model)
                    for effort, price in levels.items():
                        started = time.perf_counter()
                        self.ui(kind, "Thinking").select_option(effort)
                        text = "免费" if price == 0 else f"{price} 积分"
                        expect(self.ui(kind, "Price")).to_contain_text(text)
                        self.assertIn(text, self.ui(kind, "Thinking").locator(f'option[value="{effort}"]').inner_text())
                        self.record(f"{kind}_{model}_{effort}_local_price", started)
                expect(self.ui(kind, "Generate")).to_be_disabled()
        self.model.generate.assert_not_called()
        self.assertEqual(self.submissions, [])

    def test_both_entries_read_changed_temporary_registry_instead_of_hardcoding(self):
        path = self.config_root / "script_pricing.json"
        pricing = json.loads(path.read_text(encoding="utf-8"))
        pricing["models"]["gpt-6-astra"]["high"] = 43
        path.write_text(json.dumps(pricing), encoding="utf-8")
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                started = time.perf_counter()
                self.ready(kind, model="gpt-6-astra")
                expect(self.ui(kind, "Price")).to_contain_text("43 积分")
                expect(self.ui(kind, "Generate")).to_be_enabled()
                self.record(kind + "_changed_registry_to_display", started)
        self.model.generate.assert_not_called()

    def test_manual_generation_survives_quote_network_failure_both_entries(self):
        self.page.route("**/api/scripts/quote", lambda route: route.abort("failed"))
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                started = time.perf_counter()
                with self.page.expect_event("requestfailed", predicate=lambda request: request.url.endswith("/api/scripts/quote")):
                    self.ready(kind, model="gpt-6-luna", effort="high")
                expect(self.ui(kind, "Price")).to_contain_text("免费")
                self.record(kind + "_failed_quote_preserves_local_price", started)
                detail = self.submit(kind)
                self.assert_output(detail)
                self.assertNotIn("quote_signature", self.submissions[-1])
                expect(self.ui(kind, "Request")).to_have_value(REQUEST)
        self.assertEqual(self.model.generate.call_count, 2)
        self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 0)

    def test_double_click_creates_one_task_and_one_charge_per_entry(self):
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                def delayed(**kwargs):
                    entered.set()
                    if not release.wait(8):
                        raise RuntimeError("Test did not release the model")
                    return TEXT
                self.model.generate.side_effect = delayed
                self.ready(kind)
                before = len(self.submissions)
                started = time.perf_counter()
                with self.page.expect_response(lambda response: response.request.method == "POST"
                                               and response.url.endswith("/api/scripts/tasks")) as submitted:
                    self.ui(kind, "Generate").evaluate("(button) => { button.click(); button.click(); }")
                self.assertTrue(entered.wait(5), "The single mock model invocation did not start")
                self.assertEqual(self.app.identity.wallet(self.aid)["frozen_credit"], 20)
                self.assertEqual(len(self.submissions) - before, 1)
                release.set()
                self.completed(submitted.value.json()["task_id"], kind)
                self.record(kind + "_double_click_to_single_task", started)
        self.assertEqual(len(self.tasks()), 2)
        self.assertEqual(self.model.generate.call_count, 2)
        self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.app.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_insufficient_balance_blocks_ui_and_server_both_entries(self):
        with self.app.identity.transaction() as db:
            db.execute("UPDATE credit_accounts SET total_credit=9 WHERE account_id=?", (self.aid,))
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                started = time.perf_counter()
                self.ready(kind, model="gpt-6-astra", effort="high")
                text = self.ui(kind, "QuoteDetail") if kind == "scripts" else self.ui(kind, "Price")
                expect(text).to_contain_text("积分不足")
                expect(text).to_contain_text("40")
                expect(text).to_contain_text("9")
                expect(self.ui(kind, "Generate")).to_be_disabled()
                self.record(kind + "_insufficient_balance_blocks_ui", started)
                response = self.client.post("/api/scripts/tasks", json={
                    "request_id": uuid4().hex, "subproject_id": self.link["subproject_id"],
                    "generation_type": "script", "template_id": "basic_video_script", "input_request": REQUEST,
                    "selection_mode": "manual", "model_id": "gpt-6-astra", "thinking_level": "high"})
                self.assertEqual(response.status_code, 402, response.text)
                self.assertEqual(response.json()["expected_credit"], 40)
        self.model.generate.assert_not_called()
        self.assertEqual(self.app.identity.wallet(self.aid)["remaining_credit"], 9)
        self.assertEqual(self.app.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_unavailable_models_and_unpriced_efforts_disabled_both_entries(self):
        self.registry.catalog = {"models": [item for item in self.catalog["models"] if item["slug"] != "gpt-6-astra"]}
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                started = time.perf_counter()
                self.ready(kind, model="gpt-6-luna", effort="high")
                option = self.ui(kind, "Model").locator('option[value="gpt-6-astra"]')
                expect(option).to_be_disabled()
                expect(option).to_contain_text("不可用")
                expect(self.ui(kind, "Thinking").locator('option[value="max"]')).to_be_disabled()
                expect(self.ui(kind, "Thinking").locator('option[value="max"]')).to_contain_text("未定价")
                expect(self.ui(kind, "Model")).to_have_value("gpt-6-luna")
                self.record(kind + "_unavailable_options_disabled", started)
        self.model.generate.assert_not_called()

    def test_recommended_model_is_automatically_quoted_and_generates_both_entries(self):
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                started = time.perf_counter()
                self.ready(kind, model="recommended")
                expect(self.ui(kind, "Thinking")).to_be_disabled()
                expect(self.ui(kind, "Generate")).to_be_enabled(timeout=8000)
                self.record(kind + "_recommended_auto_quote", started)
                detail = self.submit(kind)
                self.assert_output(detail)
                self.assertTrue(self.submissions[-1]["quote_signature"])
                self.assertEqual(detail["selection_mode"], "recommended")
        self.assertEqual(self.model.generate.call_count, 2)

    def test_delayed_old_quote_cannot_replace_new_model_price_both_entries(self):
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                pending = []
                def hold_first(route):
                    if not pending:
                        pending.append(route)
                    else:
                        route.continue_()
                self.page.route("**/api/scripts/quote", hold_first)
                with self.page.expect_request(lambda request: request.url.endswith("/api/scripts/quote")):
                    self.ready(kind, model="gpt-6-luna", effort="high")
                # The request event precedes dispatch to a sync route callback.
                deadline = time.monotonic() + 5
                while not pending and time.monotonic() < deadline:
                    self.page.wait_for_timeout(10)
                self.assertEqual(len(pending), 1)
                started = time.perf_counter()
                self.ui(kind, "Model").select_option("gpt-6-astra")
                self.ui(kind, "Thinking").select_option("max")
                expect(self.ui(kind, "Price")).to_contain_text("50 积分")
                with self.page.expect_response(lambda response: response.request == pending[0].request) as released, \
                        self.page.expect_request_finished(lambda request: request == pending[0].request):
                    pending[0].continue_()
                self.assertEqual(released.value.status, 200)
                self.assertEqual(released.value.json()["price"], 0)
                self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                expect(self.ui(kind, "Price")).to_contain_text("50 积分")
                expect(self.ui(kind, "Model")).to_have_value("gpt-6-astra")
                expect(self.ui(kind, "Thinking")).to_have_value("max")
                self.record(kind + "_old_quote_cannot_replace_new_selection", started)
                self.page.unroute("**/api/scripts/quote", hold_first)
        self.model.generate.assert_not_called()

    def test_submit_network_failure_preserves_selection_and_reuses_request_id(self):
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                self.page.route("**/api/scripts/tasks", lambda route: route.abort("failed") if route.request.method == "POST" else route.continue_())
                self.ready(kind)
                started = time.perf_counter()
                with self.page.expect_event("requestfailed", predicate=lambda request: request.method == "POST" and request.url.endswith("/api/scripts/tasks")):
                    self.ui(kind, "Generate").click()
                notice = self.page.locator("#scriptNotice" if kind == "scripts" else "#videoNotice")
                expect(notice).to_be_visible()
                expect(self.ui(kind, "Request")).to_have_value(REQUEST)
                expect(self.ui(kind, "Model")).to_have_value("gpt-6.1-sol")
                expect(self.ui(kind, "Thinking")).to_have_value("high")
                expect(self.ui(kind, "Template")).to_have_value("basic_video_script")
                self.record(kind + "_failed_submit_preserves_draft", started)
                first_id = self.submissions[-1]["request_id"]
                self.page.unroute("**/api/scripts/tasks")
                detail = self.submit(kind)
                self.assert_output(detail)
                self.assertEqual(self.submissions[-1]["request_id"], first_id)
        self.assertEqual(len(self.tasks()), 2)
        self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 40)

    def test_mock_model_failure_refunds_without_losing_request_both_entries(self):
        self.model.generate.side_effect = RuntimeError("测试模型明确失败")
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                self.ready(kind)
                detail_failures = []
                if kind == "scripts":
                    def fail_first_detail(route):
                        if route.request.method == "GET" and not detail_failures:
                            detail_failures.append(route.request.url)
                            route.fulfill(status=503, content_type="application/json",
                                          body=json.dumps({"detail": "任务详情暂时不可用，请稍后重试"}))
                        else:
                            route.continue_()
                    self.page.route("**/api/scripts/tasks/*", fail_first_detail)
                before = len(self.submissions)
                started = time.perf_counter()
                with self.page.expect_response(lambda response: response.request.method == "POST"
                                               and response.url.endswith("/api/scripts/tasks")) as submitted:
                    self.ui(kind, "Generate").click()
                tid = submitted.value.json()["task_id"]
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    detail = self.get(f"/api/scripts/tasks/{tid}")
                    if detail["status"] == "failed":
                        break
                    self.page.wait_for_timeout(100)
                self.assertEqual(detail["status"], "failed")
                self.assertEqual(detail["error"], "测试模型明确失败")
                error = self.page.locator("#scriptTaskError" if kind == "scripts" else "#pipelineScriptTasks")
                expect(error).to_contain_text("测试模型明确失败", timeout=12000)
                expect(self.ui(kind, "Request")).to_have_value(REQUEST)
                self.assertEqual(self.app.identity.wallet(self.aid)["remaining_credit"], 200)
                self.assertEqual(self.app.identity.wallet(self.aid)["frozen_credit"], 0)
                self.assertEqual(len(self.submissions) - before, 1)
                if kind == "scripts":
                    self.assertEqual(len(detail_failures), 1)
                    self.page.unroute("**/api/scripts/tasks/*", fail_first_detail)
                self.record(kind + "_model_failure_to_visible_error_and_refund", started)
        self.assertEqual(self.model.generate.call_count, 2)

    def test_storyboard_optional_mock_is_explicit_and_real_mode_disabled(self):
        for kind in ENTRIES:
            with self.subTest(entry=kind):
                self.ready(kind, model="gpt-6-luna", effort="high")
                form = self.page.locator("#scriptForm" if kind == "scripts" else "#pipelineScriptForm")
                panel = form.locator(".storyboard-panel")
                expect(panel).to_have_count(1)
                panel.locator("summary").click()
                toggle = panel.locator(".storyboard-check input[type=checkbox]").first
                expect(toggle).not_to_be_checked()
                expect(panel.locator('select[name="mode"] option[value="codex"]')).to_have_js_property("disabled", True, timeout=5000)
                expect(panel.locator('select[name="mode"] option[value="mock"]')).to_have_js_property("disabled", False)
                expect(panel.locator('select[name="mode"] option[value="mock"]')).to_contain_text("Mock")
                started = time.perf_counter()
                toggle.check()
                expect(self.ui(kind, "Generate")).to_be_enabled(timeout=8000)
                self.record(kind + "_enable_mock_storyboard_to_quote", started)
                detail = self.submit(kind)
                self.assert_output(detail)
                self.assertEqual(self.submissions[-1]["storyboard_options"]["mode"], "mock")
                self.assertTrue(self.submissions[-1]["storyboard_confirmed"])
                self.assertEqual(detail["storyboard_quote"]["price"], 0)
                self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 0)
        self.assertEqual(self.model.generate.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
