"""Browser/HTTP/real queue coverage for direct and selected image generation."""
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

from image_workbench.image_api_backend import ImageAPIBackend
from image_workbench.web import create_app
from image_workbench.worker import LocalWorker
from tests import test_asset_browser as helpers
from tests.test_video import png


SCREENSHOTS = Path(__file__).resolve().parents[1] / "work/image-actions/screenshots"


class ImageActionsBrowserTests(unittest.TestCase):
    stop_server = helpers.AssetBrowserTests.stop_server
    post = helpers.AssetBrowserTests.post
    get = helpers.AssetBrowserTests.get
    root_page = helpers.AssetBrowserTests.root_page
    save_root_task = helpers.AssetBrowserTests.save_root_task

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        with patch.object(helpers, "create_app", side_effect=lambda storage, **kwargs: create_app(storage, backend="image-api")):
            helpers.AssetBrowserTests.setUp(self)
        self.provider_calls = []
        self.policy_rejected = False

        def handler(request):
            self.provider_calls.append(json.loads(request.content))
            if self.policy_rejected:
                return httpx.Response(400, json={"error": {"code": "content_policy_violation", "message": "provider-private-detail"}})
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png("blue")).decode()}]})

        provider = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(provider.close)
        backend = ImageAPIBackend("https://provider.test", "browser-test-secret", client=provider)
        self.worker = LocalWorker(self.app.state.workbench, backend)

    def tearDown(self):
        if any(test is self for test, _ in self._outcome.result.failures + self._outcome.result.errors):
            self.page.screenshot(path=str(SCREENSHOTS / (self._testMethodName + "-failure.png")), full_page=True)
            (SCREENSHOTS / (self._testMethodName + "-failure.html")).write_text(self.page.content(), encoding="utf-8")
        self.model.generate.assert_not_called()
        self.assertEqual(self.errors, [], "Uncaught browser errors")

    def save(self, text, ratio="1:1", style=""):
        self.page.locator("#promptInput").fill(text)
        self.page.locator("#ratioSelect").select_option(ratio)
        self.page.locator("#imageStyleSelect").select_option(style)
        task = self.save_root_task()
        expect(self.page.locator("#saveTaskButton")).to_be_enabled()
        return task

    def row(self, task):
        return self.page.locator(".task-list-item").filter(has_text=task["name"] or task["prompt"])

    def test_direct_generation_keeps_style_ratio_and_double_click_is_one_job(self):
        self.root_page()
        self.page.locator("#promptInput").fill('蓝色纸盒产品摄影，文字“蓝盒”')
        self.page.locator("#ratioSelect").select_option("2:3")
        self.page.locator("#imageStyleSelect").select_option("commercial-product")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/run")) as submitted:
            self.page.locator("#generateTaskButton").evaluate("button => { button.click(); button.click(); }")
        self.assertEqual(submitted.value.status, 200)
        expect(self.page.locator("#generateTaskButton")).to_be_enabled()
        tasks = self.get(f"/api/subprojects/{self.link['subproject_id']}/tasks")["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertEqual(self.get("/api/credits")["credits"]["frozen_credit"], 40)
        self.worker.serve(once=True)
        expect(self.page.locator("#resultState")).to_have_text("等待审核", timeout=10000)
        self.assertEqual(len(self.provider_calls), 1)
        self.assertEqual(self.provider_calls[0]["size"], "1024x1536")
        detail = self.get(f"/api/tasks/{tasks[0]['task_id']}")
        self.assertEqual(detail["image_prompt"]["style_id"], "commercial-product")
        self.assertEqual(self.provider_calls[0]["prompt"], detail["image_prompt"]["compiled_prompt"])
        self.assertEqual(detail["image_prompt"]["source_prompt"], '蓝色纸盒产品摄影，文字“蓝盒”')

    def test_saved_selection_batches_only_chosen_drafts_and_all_rows_refresh(self):
        self.root_page()
        tasks = [self.save("画面一：海边灯塔", "1:1", "watercolor"),
                 self.save("画面二：复古街道", "3:2", "retro-film"),
                 self.save("画面三：竖版海报", "2:3", "graphic-poster")]
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.assertEqual(self.get("/api/credits")["credits"]["frozen_credit"], 0)
        expect(self.page.locator("#batchSelectionSummary")).to_have_text("已选 3 项 · 预计 120 积分")
        self.row(tasks[1]).locator("input[type=checkbox]").uncheck()
        expect(self.page.locator("#batchSelectionSummary")).to_have_text("已选 2 项 · 预计 80 积分")
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.screenshot(path=str(SCREENSHOTS / "desktop.png"), full_page=True)
        self.page.locator("#batchGenerateButton").evaluate("button => { button.click(); button.click(); }")
        expect(self.page.locator("#batchMessage")).to_have_text("已提交 2 项任务，系统将自动安排生成。")
        jobs = self.app.state.workbench.list_jobs()
        self.assertEqual([job["task_id"] for job in jobs], [tasks[0]["task_id"], tasks[2]["task_id"]])
        self.assertEqual(self.get("/api/credits")["credits"]["frozen_credit"], 80)
        self.worker.serve(once=True)
        self.worker.serve(once=True)
        for index in (0, 2):
            expect(self.row(tasks[index]).locator(".status-pill")).to_have_text("等待审核", timeout=10000)
            expect(self.row(tasks[index]).locator("input[type=checkbox]")).to_be_disabled()
        expect(self.row(tasks[1]).locator(".status-pill")).to_have_text("待开始")
        self.assertEqual([call["size"] for call in self.provider_calls], ["1024x1024", "1024x1536"])
        self.page.locator("#selectDraftTasks").check()
        expect(self.page.locator("#batchGenerateButton")).to_have_text("批量生成 (1)")
        self.page.reload(wait_until="domcontentloaded")
        expect(self.page.locator("#taskCount")).to_have_text("3 个任务")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 2)

    def test_batch_insufficient_total_credit_leaves_every_draft_unsubmitted(self):
        self.root_page()
        for index in range(6):
            self.save(f"批量场景 {index + 1}")
        self.page.locator("#batchGenerateButton").click()
        expect(self.page.locator("#batchMessage")).to_contain_text("共需 240 积分，当前可用 200，本次尚未提交")
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.assertEqual(self.get("/api/credits")["credits"]["frozen_credit"], 0)

    def test_batch_stops_on_submission_failure_and_keeps_remaining_selection(self):
        self.root_page()
        tasks = [self.save(f"批量构图 {index + 1}") for index in range(3)]
        self.page.route(f"**/api/tasks/{tasks[1]['task_id']}/run", lambda route: route.fulfill(
            status=503, content_type="application/json", body=json.dumps({"detail": "暂时无法提交"})), times=1)
        self.page.locator("#batchGenerateButton").click()
        expect(self.page.locator("#batchMessage")).to_contain_text("已确认提交 1 项；后续提交已暂停")
        expect(self.page.locator("#batchSelectionSummary")).to_have_text("已选 2 项 · 预计 80 积分")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.page.locator("#batchGenerateButton").click()
        expect(self.page.locator("#batchMessage")).to_have_text("已提交 2 项任务，系统将自动安排生成。")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 3)
        self.assertEqual(self.get("/api/credits")["credits"]["frozen_credit"], 120)

    def test_failed_direct_submission_keeps_saved_task_without_creating_duplicate(self):
        self.root_page()
        self.page.locator("#promptInput").fill("直接生成一个木屋")
        submissions = []

        def fail_first_submission(route):
            submissions.append(route.request.url)
            if len(submissions) == 1:
                route.abort()
            else:
                route.continue_()

        # Keep interception enabled while the UI immediately reloads the task list.
        # Removing the last route during abort can strand that GET in Chromium.
        self.page.route("**/api/tasks/*/run", fail_first_submission)
        self.page.locator("#generateTaskButton").click()
        expect(self.page.locator("#formMessage")).to_contain_text("任务已保存，生成提交未确认")
        expect(self.page.locator("#promptInput")).to_have_value("")
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.page.get_by_role("button", name="开始生成", exact=True).click()
        expect(self.page.locator("#resultState")).to_have_text("等待执行")
        self.assertEqual(len(self.get(f"/api/subprojects/{self.link['subproject_id']}/tasks")["tasks"]), 1)
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertEqual(len(submissions), 2)

    def test_late_initial_list_cannot_replace_a_saved_task_after_submission_failure(self):
        delayed = []

        def delay_first_list(route):
            if not delayed:
                delayed.append(route)
            else:
                route.continue_()

        self.page.route(f"**/api/subprojects/{self.link['subproject_id']}/tasks", lambda route:
                        delay_first_list(route) if route.request.method == "GET" else route.continue_())
        self.root_page()
        self.page.locator("#promptInput").fill("迟到列表不能覆盖新任务")
        self.page.route("**/api/tasks/*/run", lambda route: route.abort(), times=1)
        self.page.locator("#generateTaskButton").click()
        expect(self.page.get_by_role("button", name="开始生成", exact=True)).to_be_visible()
        delayed[0].fulfill(status=200, content_type="application/json", body='{"tasks":[]}')
        self.page.wait_for_timeout(200)
        expect(self.page.locator("#taskCount")).to_have_text("1 个任务")
        expect(self.page.get_by_role("button", name="开始生成", exact=True)).to_be_visible()

    def test_content_rejection_explains_reason_and_reloads_parameters_without_running(self):
        self.root_page()
        task = self.save("中世纪普通民众的生活场景", "3:2", "retro-film")
        self.policy_rejected = True
        self.page.get_by_role("button", name="开始生成", exact=True).click()
        expect(self.page.locator("#resultState")).to_have_text("等待执行")
        self.worker.serve(once=True)
        expect(self.page.locator("#taskDetails")).to_contain_text("内容审核拒绝", timeout=10000)
        expect(self.page.get_by_role("button", name="重试生成", exact=True)).to_be_visible()
        self.assertEqual(self.get("/api/credits")["credits"]["remaining_credit"], 200)
        self.page.get_by_role("button", name="修改需求后新建", exact=True).click()
        expect(self.page.locator("#promptInput")).to_have_value("中世纪普通民众的生活场景")
        expect(self.page.locator("#ratioSelect")).to_have_value("3:2")
        expect(self.page.locator("#imageStyleSelect")).to_have_value("retro-film")
        self.assertEqual(self.get(f"/api/tasks/{task['task_id']}")["status"], "failed")
        self.assertEqual(len(self.provider_calls), 3)
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertNotIn("provider-private-detail", self.page.locator("body").inner_text())

    def test_mobile_actions_are_parallel_and_switching_projects_clears_batch_selection(self):
        self.root_page()
        self.save("移动端第一张")
        self.save("移动端第二张")
        self.page.set_viewport_size({"width": 390, "height": 844})
        save = self.page.locator("#saveTaskButton").bounding_box()
        generate = self.page.locator("#generateTaskButton").bounding_box()
        self.assertLess(abs(save["y"] - generate["y"]), 2)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth - innerWidth"), 1)
        self.page.screenshot(path=str(SCREENSHOTS / "mobile.png"), full_page=True)
        self.page.set_viewport_size({"width": 1440, "height": 1100})
        main = self.post("/api/main-projects", json={"name": "另一个项目", "request_id": uuid4().hex})
        self.page.locator("#refreshButton").click()
        self.page.locator(".project-item").filter(has_text="另一个项目").click()
        group = self.page.locator(".project-tree-group").filter(has=self.page.locator(".project-item").filter(has_text="另一个项目"))
        group.locator(".project-child").filter(has_text=main["subprojects"][0]["name"]).click()
        expect(self.page.locator("#taskCount")).to_have_text("0 个任务")
        expect(self.page.locator("#batchGenerateButton")).to_be_disabled()
