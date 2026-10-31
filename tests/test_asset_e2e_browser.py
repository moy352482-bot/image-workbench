"""Real browser -> stored assets -> queued Mock image execution -> immutable versions."""

import hashlib
import json
from pathlib import Path
import re
import time
import unittest
from unittest import mock
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from image_workbench import BackendResult
from image_workbench.accounts import CreditCostCalculator
from image_workbench.worker import LocalWorker
from tests import test_asset_browser as asset_helpers
from tests.test_video import png


REPORT = Path(__file__).resolve().parents[1] / "docs/goals/asset-ux/browser-D.json"
SCREENSHOTS = REPORT.parent / "screenshots"


class MockImageBackend:
    name = "local"

    def __init__(self):
        self.requests = []
        self.missing_output = False
        self.output_count = 1

    def generate(self, request):
        self.requests.append(request)
        if self.missing_output:
            return BackendResult(())
        if self.output_count == 1:
            output = request.work_dir / "products" / "mock-result.png"
            output.write_bytes(png("blue" if len(self.requests) == 1 else "green"))
            return BackendResult((output,))
        colors = ("blue", "green", "red", "yellow")
        outputs = []
        for index in range(self.output_count):
            output = request.work_dir / "products" / f"mock-result-{index}.png"
            output.write_bytes(png(colors[index % len(colors)]))
            outputs.append(output)
        return BackendResult(tuple(outputs))


class AssetEndToEndBrowserTests(unittest.TestCase):
    stop_server = asset_helpers.AssetBrowserTests.stop_server
    post = asset_helpers.AssetBrowserTests.post
    get = asset_helpers.AssetBrowserTests.get
    video = asset_helpers.AssetBrowserTests.video
    root_page = asset_helpers.AssetBrowserTests.root_page
    event = asset_helpers.AssetBrowserTests.event
    idle = asset_helpers.AssetBrowserTests.idle
    save_root_task = asset_helpers.AssetBrowserTests.save_root_task

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
            "runner": "Python Playwright Chromium + real uvicorn/SQLite/files + LocalWorker/TaskRunner",
            "real_model_calls": 0,
            "mock_image_calls": sum(case["mock_image_calls"] for case in cls.results),
            "elapsed_seconds": round(time.perf_counter() - cls.started, 3),
            "cases": cls.results,
            "note": (
                "Image paste uses browser File/DataTransfer/ClipboardEvent. All task, artifact, "
                "queue and version records use the real app in temporary data roots. "
                "Only the image backend is replaced with deterministic PNG output; "
                "LocalWorker and TaskRunner execute normally. No production files or paid model calls."
            ),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def setUp(self):
        asset_helpers.AssetBrowserTests.setUp(self)
        forbidden = mock.patch(
            "image_workbench.codex_backend.CodexImageBackend.generate",
            side_effect=AssertionError("Real image generation is forbidden in this browser suite"),
        )
        self.real_image = forbidden.start()
        self.addCleanup(forbidden.stop)
        self.backend = MockImageBackend()
        self.worker = LocalWorker(self.app.state.workbench, self.backend)
        self.evidence = {}

    def tearDown(self):
        result = self._outcome.result
        failed = any(test is self for test, _ in result.failures + result.errors)
        screenshot = SCREENSHOTS / (self._testMethodName + ".png")
        self.page.screenshot(path=str(screenshot), full_page=True)
        self.results.append({
            "name": self._testMethodName,
            "status": "failed" if failed or self.errors else "passed",
            "elapsed_seconds": round(time.perf_counter() - self.case_started, 3),
            "page_errors": self.errors,
            "mock_image_calls": len(self.backend.requests),
            "evidence": self.evidence,
            "screenshot": str(screenshot.relative_to(REPORT.parent)).replace("\\", "/"),
        })
        self.real_image.assert_not_called()
        self.model.generate.assert_not_called()
        self.assertEqual(self.errors, [], "Uncaught browser exceptions")

    def menu(self, container, label):
        menu = container.locator(".asset-actions-menu").first
        if menu.get_attribute("open") is None:
            menu.locator("summary").click()
        menu.get_by_role("button", name=label, exact=True).click()

    def paste_and_prepare(self, prompt):
        self.video()
        self.page.evaluate("""() => {
            window.assetTiming={start:performance.now()};
            const observer=new MutationObserver(() => {
                if(document.querySelector('.asset-input-progress img')) {
                    window.assetTiming.preview_ms=performance.now()-window.assetTiming.start;
                    observer.disconnect();
                }
            });
            observer.observe(document.body,{childList:true,subtree:true});
        }""")
        self.assertTrue(self.event(colors=("red",)))
        self.idle()
        assets = self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]
        self.assertEqual(len(assets), 1)
        asset = assets[0]
        stored = self.storage.get_artifact(asset["artifact_id"])
        self.assertEqual(stored.sha256, hashlib.sha256(png("red")).hexdigest())
        self.assertEqual(self.storage.get_artifact_path(stored.artifact_id).read_bytes(), png("red"))
        self.assertEqual(self.client.get(asset["url"]).content, png("red"))
        card = self.page.locator(f'#assetTrayGrid [data-artifact-id="{stored.artifact_id}"]')
        expect(card).to_be_visible()
        self.evidence["paste_timing"] = self.page.evaluate("""() => ({
            preview_ms:window.assetTiming.preview_ms,
            registered_and_visible_ms:performance.now()-window.assetTiming.start
        })""")
        self.assertIsNotNone(self.evidence["paste_timing"]["preview_ms"])
        # Exercise the shared action menu, not only paste's automatic selection.
        self.page.locator(f'#pipelineImageReferences input[value="{stored.artifact_id}"]').uncheck()
        reference_started = time.perf_counter()
        self.menu(card, "设为参考图")
        expect(self.page.locator(f'#pipelineImageReferences input[value="{stored.artifact_id}"]')).to_be_checked()
        self.evidence["tray_reference_ms"] = round((time.perf_counter() - reference_started) * 1000, 2)
        self.page.locator("#pipelineImagePrompt").fill(prompt)
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(f"/api/video/projects/{self.pid}/images")) as saved:
            self.page.get_by_role("button", name="准备图片生成", exact=True).click()
        response = saved.value
        self.assertEqual(response.status, 200, response.text())
        task = response.json()
        self.assertEqual([item["artifact_id"] for item in task["inputs"]], [stored.artifact_id])
        self.assertEqual(task["input_count"], 1)
        self.assertEqual(self.backend.requests, [])
        self.evidence.update(input_artifact_id=stored.artifact_id, input_sha256=stored.sha256,
                             task_id=task["task_id"], subproject_id=task["subproject_id"])
        return stored, task

    def task_card(self, task):
        return self.page.locator("#pipelineImageTasks .pipeline-task").filter(has_text=task["prompt"])

    def enqueue(self, task):
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(f"/api/tasks/{task['task_id']}/run")) as submitted:
            self.task_card(task).get_by_role("button", name="生成图片", exact=False).click()
        response = submitted.value
        self.assertEqual(response.status, 200, response.text())
        self.assertEqual(response.json()["status"], "queued")
        return response.json()

    def run_worker(self, task):
        started = time.perf_counter()
        self.assertTrue(self.worker.run_next())
        state = self.app.state.workbench
        job = state.get_job(task["project_id"], task["task_id"])
        record = state.runner.get_execution(task["project_id"], task["task_id"], job["execution_id"])
        self.evidence.setdefault("executions", []).append({
            "task_id": task["task_id"], "execution_id": record.execution_id,
            "job_status": job["status"], "execution_status": record.status,
            "output_artifact_ids": record.output_artifact_ids,
            "worker_elapsed_seconds": round(time.perf_counter() - started, 4),
            "record_total_seconds": record.total_seconds,
        })
        self.assertFalse(self.worker.run_next(), "A finished task must not be silently replayed")
        return record

    def refresh_video(self):
        self.page.locator("#videoRefresh").click()
        expect(self.page.locator("#videoNotice")).to_contain_text("状态已刷新")

    def test_paste_reference_mock_generation_and_edit_preserve_artifact_versions(self):
        source, task = self.paste_and_prepare("保持参考图构图，生成蓝色海报")
        self.enqueue(task)
        record = self.run_worker(task)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(len(record.output_artifact_ids), 1)
        request = self.backend.requests[0]
        self.assertEqual([path.read_bytes() for path in request.reference_images], [png("red")])
        self.assertIsNone(request.edit_image)
        self.assertNotEqual(request.reference_images[0], self.storage.get_artifact_path(source.artifact_id))
        first = self.storage.get_artifact(record.output_artifact_ids[0])
        self.assertEqual(first.kind, "output")
        self.assertIsNone(first.source_artifact_id)
        self.assertEqual(self.storage.get_artifact_path(first.artifact_id).read_bytes(), png("blue"))
        self.refresh_video()
        result = self.task_card(task).locator(".pipeline-image-result")
        expect(result).to_have_count(1)
        expect(result.locator("img")).to_have_attribute("src", self.get(f"/api/tasks/{task['task_id']}")["outputs"][0]["url"])
        self.page.wait_for_function("() => [...document.querySelectorAll('.pipeline-image-result img')].every(img => img.complete && img.naturalWidth > 0)")
        self.menu(result, "继续修改")
        self.page.wait_for_url(self.origin + "/?**")
        expect(self.page.locator("#baseImagePanel")).to_be_visible()
        self.assertIn(first.artifact_id, self.page.locator("#baseImagePreview img").get_attribute("src"))
        self.page.locator("#promptInput").fill("保留上一版构图，改成绿色")
        edit = self.save_root_task()
        self.assertEqual(edit["base_artifact_id"], first.artifact_id)
        self.assertEqual(edit["subproject_id"], task["subproject_id"])
        self.assertEqual(self.last_run["status"], "queued")
        revised = self.run_worker(edit)
        self.assertEqual(revised.status, "succeeded")
        self.assertEqual(len(revised.output_artifact_ids), 1)
        second = self.storage.get_artifact(revised.output_artifact_ids[0])
        self.assertEqual(second.source_artifact_id, first.artifact_id)
        self.assertNotEqual(second.artifact_id, first.artifact_id)
        self.assertEqual(self.backend.requests[1].edit_image.read_bytes(), png("blue"))
        self.assertEqual(self.storage.get_artifact_path(first.artifact_id).read_bytes(), png("blue"))
        self.assertEqual(self.storage.get_artifact_path(second.artifact_id).read_bytes(), png("green"))
        self.assertEqual(self.storage.get_artifact_path(source.artifact_id).read_bytes(), png("red"))
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=edit["prompt"]).click()
        card = self.page.locator(".artifact-card")
        expect(card).to_have_count(1)
        expect(card).to_have_attribute("data-artifact-id", second.artifact_id)
        expect(card).to_have_attribute("data-source-artifact-id", first.artifact_id)
        detail_after = self.get(f"/api/tasks/{edit['task_id']}")
        self.assertEqual(detail_after["outputs"][0]["artifact_id"], second.artifact_id)
        self.assertEqual(detail_after["outputs"][0]["source_artifact_id"], first.artifact_id)
        expect(card.get_by_role("link", name="下载图片", exact=True)).to_have_attribute("href", detail_after["outputs"][0]["download_url"])
        expect(card.get_by_role("button", name="修改图片", exact=True)).to_be_visible()
        self.page.wait_for_function("() => [...document.querySelectorAll('.artifact-card img')].every(img => img.complete && img.naturalWidth > 0)")
        expect(self.page.locator("#taskDetails")).not_to_contain_text("任务 ID")
        self.evidence["version_chain"] = [first.artifact_id, second.artifact_id]
        self.evidence["source_files_unchanged"] = True
        self.assertEqual(len(self.backend.requests), 2)

    def test_result_panel_draft_has_no_duplicate_start_entry(self):
        self.root_page()
        # Legacy draft created through the API keeps the documented draft-only create contract.
        draft = self.post(f"/api/subprojects/{self.link['subproject_id']}/tasks", data={
            "request_id": uuid4().hex,
            "operation": "generate",
            "prompt": "草稿状态的结果面板",
            "aspect_ratio": "auto",
            "reference_artifact_ids": "[]",
        })
        self.assertEqual(draft["status"], "draft")
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=draft["prompt"]).click()
        details = self.page.locator("#taskDetails")
        expect(details).to_contain_text("尚未生成图片")
        expect(details).not_to_contain_text("任务 ID")
        expect(details.locator(".artifact-card")).to_have_count(0)
        expect(details.locator('[data-step="started"]')).to_have_attribute("data-state", "pending")
        expect(details.locator('[data-step="generating"]')).to_have_attribute("data-state", "pending")
        expect(details.locator('[data-step="generated"]')).to_have_attribute("data-state", "pending")
        expect(details.locator(".result-run-button")).to_have_count(0)
        expect(details.get_by_role("button", name="开始生成", exact=True)).to_have_count(0)
        expect(details.get_by_role("button", name="开始演练", exact=True)).to_have_count(0)
        expect(details.locator(".generation-estimate-value")).to_have_text("暂无足够历史数据")
        expect(details.locator(".result-credit")).to_have_attribute("data-credit-status", "waiting_credit")
        expect(details.locator(".result-credit")).to_have_attribute("data-credit-amount", "40")
        expect(details.locator(".result-credit-state")).to_have_text("· 预计消耗")
        expect(self.page.locator("#saveTaskButton")).to_have_text("开始生成")
        expect(self.page.locator("#saveModeHint")).to_have_text("点击后立即生成")
        detail = self.get(f"/api/tasks/{draft['task_id']}")
        self.assertEqual(detail["status"], "draft")
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.evidence["draft_has_no_duplicate_start"] = True

    def test_result_panel_failure_shows_refund_without_generated_card(self):
        self.root_page()
        self.page.locator("#promptInput").fill("失败状态的结果面板")
        self.backend.missing_output = True
        task = self.save_root_task()
        self.assertEqual(self.last_run["status"], "queued")
        record = self.run_worker(task)
        self.assertEqual(record.status, "missing_artifact")
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=task["prompt"]).click()
        details = self.page.locator("#taskDetails")
        expect(details).to_contain_text("这次没有生成图片")
        expect(details).to_contain_text("未找到本次生成的图片文件")
        expect(details.locator(".artifact-card")).to_have_count(0)
        expect(details.get_by_role("button", name="重新演练", exact=True)).to_be_visible()
        expect(details).to_contain_text("已退回")
        expect(details.locator(".result-credit")).to_have_attribute("data-credit-status", "refunded")
        expect(details.locator(".result-credit-state")).to_have_text("· 已退回")
        expect(details.locator('[data-step="started"]')).to_have_attribute("data-state", "done")
        expect(details.locator('[data-step="generating"]')).to_have_attribute("data-state", "error")
        expect(details.locator('[data-step="generated"]')).to_have_attribute("data-state", "pending")
        detail = self.get(f"/api/tasks/{task['task_id']}")
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["credit_status"], "refunded")
        self.evidence["failure_without_generated_card"] = True

    def test_save_auto_runs_exactly_once_and_marks_queue_node(self):
        self.root_page()
        self.page.locator("#promptInput").fill("保存即开始演练")
        runs = []
        self.page.on("request", lambda request: runs.append(request.url)
                    if request.method == "POST" and request.url.endswith("/run") else None)
        task = self.save_root_task()
        self.assertEqual(task["status"], "draft")
        self.assertEqual(self.last_run["status"], "queued")
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertEqual(self.backend.requests, [])
        details = self.page.locator("#taskDetails")
        expect(self.page.locator("#saveTaskButton")).to_have_text("开始生成")
        expect(details).to_contain_text("已开始生成，正在等待执行")
        expect(details.locator('[data-step="started"]')).to_have_attribute("data-state", "done")
        expect(details.locator('[data-step="generating"]')).to_have_attribute("data-state", "active")
        expect(details.locator('[data-step="generated"]')).to_have_attribute("data-state", "pending")
        expect(details.locator(".result-run-button")).to_have_count(0)
        expect(details.locator(".generation-estimate-value")).to_have_text("暂无足够历史数据")
        expect(details.locator(".result-credit")).to_have_attribute("data-credit-status", "credit_frozen")
        expect(details.locator(".result-credit-state")).to_have_text("· 已冻结")
        expect(details).to_contain_text("已冻结")
        expect(details).not_to_contain_text("已扣除")
        self.evidence["auto_run_requests"] = len(runs)
        self.evidence["single_job_after_save"] = True

    def test_run_submission_failure_keeps_form_and_left_button_retries_same_task(self):
        self.root_page()
        sid = self.link["subproject_id"]
        prompt = "执行提交失败后左侧重试"
        self.page.locator("#promptInput").fill(prompt)
        calls = []

        def intercept(route):
            calls.append(route.request.post_data_json)
            if len(calls) == 1:
                route.abort("failed")
            else:
                route.continue_()

        self.page.route("**/api/tasks/*/run", intercept)
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(f"/api/subprojects/{sid}/tasks")) as saved:
            self.page.locator("#saveTaskButton").click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        task = saved.value.json()
        expect(self.page.locator("#formMessage")).to_contain_text(
            "可直接再次点击“开始生成”重试")
        expect(self.page.locator("#promptInput")).to_have_value(prompt)
        expect(self.page.locator(".result-run-button")).to_have_count(0)
        expect(self.page.locator('[data-step="started"]')).to_have_attribute("data-state", "pending")
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.assertEqual(len(self.get(f"/api/subprojects/{sid}/tasks")["tasks"]), 1)

        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(f"/api/subprojects/{sid}/tasks")) as saved_again:
            with self.page.expect_response(lambda response: response.request.method == "POST"
                                           and response.url.endswith(f"/api/tasks/{task['task_id']}/run")) as submitted:
                self.page.locator("#saveTaskButton").click()
        self.assertEqual(saved_again.value.json()["task_id"], task["task_id"])
        self.last_run = submitted.value.json()
        self.assertEqual(self.last_run["status"], "queued")
        expect(self.page.locator("#promptInput")).to_have_value("")
        expect(self.page.locator('[data-step="generating"]')).to_have_attribute("data-state", "active")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.get(f"/api/subprojects/{sid}/tasks")["tasks"]), 1)
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertEqual(self.backend.requests, [])
        self.evidence["run_failure_retry_same_task"] = True

    def test_estimate_uses_same_type_history_above_progress_and_survives_reload(self):
        self.root_page()
        sid = self.link["subproject_id"]
        draft = self.post(f"/api/subprojects/{sid}/tasks", data={
            "request_id": uuid4().hex,
            "operation": "generate",
            "prompt": "历史耗时估算预览",
            "aspect_ratio": "auto",
            "reference_artifact_ids": "[]",
        })
        payload = self.get(f"/api/subprojects/{sid}/tasks")
        draft_record = next(task for task in payload["tasks"] if task["task_id"] == draft["task_id"])
        samples = [{
            "task_id": f"{index:032x}",
            "project_id": draft["project_id"],
            "main_project_id": draft["main_project_id"],
            "subproject_id": sid,
            "operation": "generate",
            "status": "awaiting_review",
            "status_label": "等待审核",
            "prompt": f"历史同类任务 {index}",
            "name": f"历史同类任务 {index}",
            "created_at": draft["created_at"],
            "updated_at": draft["updated_at"],
            "input_count": 0,
            "output_count": 1,
            "base_artifact_id": None,
            "execution_seconds": seconds,
            "execution_timing_source": "execution_record",
            "execution_backend": "local",
            "preview_artifact": None,
        } for index, seconds in enumerate((36, 38, 40), start=1)]
        payload["tasks"] = [draft_record, *samples]
        pattern = re.compile(rf"/api/subprojects/{sid}/tasks$")
        self.page.route(pattern, lambda route: route.fulfill(json=payload))
        self.page.reload(wait_until="networkidle")
        details = self.page.locator("#taskDetails")
        estimate = details.locator(".generation-estimate")
        expect(estimate.locator(".generation-estimate-label")).to_have_text("预计生成时间：")
        expect(estimate.locator(".generation-estimate-value")).to_have_text("约 38 秒")
        expect(estimate.locator(".generation-estimate-source")).to_have_text(
            "基于历史同类任务 · 3 次")
        self.assertEqual(estimate.get_attribute("data-samples"), "3")
        self.assertTrue(estimate.evaluate(
            "el => el.nextElementSibling && el.nextElementSibling.classList.contains('generation-steps')"))
        expect(details.locator('[data-step="started"]')).to_have_attribute("data-state", "pending")
        self.page.reload(wait_until="networkidle")
        expect(details.locator(".generation-estimate-value")).to_have_text("约 38 秒")
        expect(details.locator(".generation-estimate-source")).to_have_text(
            "基于历史同类任务 · 3 次")
        self.assertEqual(self.backend.requests, [])
        self.evidence["estimate_samples"] = 3
        self.evidence["estimate_restored_after_reload"] = True

    def test_generating_node_follows_live_task_status(self):
        self.root_page()
        self.page.locator("#promptInput").fill("生成中节点的实时状态")
        task = self.save_root_task()
        detail = self.get(f"/api/tasks/{task['task_id']}")
        detail.update(status="generating", status_label="生成中", can_generate=False, can_retry=False)
        pattern = re.compile(rf"/api/tasks/{task['task_id']}$")

        def intercept(route):
            if route.request.method == "GET":
                route.fulfill(json=detail)
            else:
                route.continue_()

        self.page.route(pattern, intercept)
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=task["prompt"]).click()
        details = self.page.locator("#taskDetails")
        expect(details.locator('[data-step="started"]')).to_have_attribute("data-state", "done")
        expect(details.locator('[data-step="generating"]')).to_have_attribute("data-state", "active")
        expect(details.locator('[data-step="generated"]')).to_have_attribute("data-state", "pending")
        expect(details.get_by_role("button", name="开始演练", exact=True)).to_have_count(0)
        self.assertEqual(self.backend.requests, [])
        self.evidence["generating_node_from_live_status"] = True

    def test_generated_node_and_stepper_survive_reload(self):
        self.root_page()
        self.page.locator("#promptInput").fill("三节点完成并刷新恢复")
        task = self.save_root_task()
        record = self.run_worker(task)
        self.assertEqual(record.status, "succeeded")
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=task["prompt"]).click()
        details = self.page.locator("#taskDetails")
        expect(details.locator(".artifact-card")).to_have_count(1)
        expect(details.locator('[data-step="started"]')).to_have_attribute("data-state", "done")
        expect(details.locator('[data-step="generating"]')).to_have_attribute("data-state", "done")
        expect(details.locator('[data-step="generated"]')).to_have_attribute("data-state", "done")
        expect(details.locator('[data-step="generated"]')).to_have_attribute("aria-label", "已生成：等待审核")
        expect(details).to_contain_text("已扣除")
        expect(details).not_to_contain_text("已冻结")
        expect(details.locator(".result-credit")).to_have_attribute("data-credit-status", "completed")
        expect(details.locator(".result-credit-state")).to_have_text("· 已扣除")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertEqual(len(self.backend.requests), 1)
        self.evidence["stepper_restored_after_reload"] = True

    def test_download_modify_and_credit_bind_the_selected_artifact(self):
        self.root_page()
        self.backend.output_count = 2
        self.page.locator("#promptInput").fill("两版结果下载与修改绑定")
        task = self.save_root_task()
        record = self.run_worker(task)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(len(record.output_artifact_ids), 2)
        first, second = self.get(f"/api/tasks/{task['task_id']}")["outputs"]
        self.assertEqual(self.storage.get_artifact_path(first["artifact_id"]).read_bytes(), png("blue"))
        self.assertEqual(self.storage.get_artifact_path(second["artifact_id"]).read_bytes(), png("green"))

        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=task["prompt"]).click()
        details = self.page.locator("#taskDetails")
        card = details.locator(".artifact-card")
        expect(card).to_have_count(1)
        expect(details.locator(".result-version")).to_have_count(2)
        expect(card).to_have_attribute("data-artifact-id", second["artifact_id"])
        expect(details.locator(".result-credit")).to_have_attribute("data-credit-status", "completed")
        expect(details.locator(".result-credit")).to_have_attribute("data-credit-amount", "40")
        expect(details.locator(".result-credit")).to_contain_text("本次任务积分：40")
        expect(details.locator(".result-credit-state")).to_have_text("· 已扣除")
        self.page.wait_for_function(
            "() => [...document.querySelectorAll('#taskDetails img')].every(img => img.complete && img.naturalWidth > 0)")

        with self.page.expect_download() as completed_download:
            card.get_by_role("link", name="下载图片", exact=True).click()
        download = completed_download.value
        self.assertEqual(download.suggested_filename, f"image-{second['artifact_id']}.png")
        self.assertEqual(Path(download.path()).read_bytes(), png("green"))

        details.locator(".result-version").nth(0).click()
        expect(card).to_have_attribute("data-artifact-id", first["artifact_id"])
        with self.page.expect_download() as selected_download:
            card.get_by_role("link", name="下载图片", exact=True).click()
        download = selected_download.value
        self.assertEqual(download.suggested_filename, f"image-{first['artifact_id']}.png")
        self.assertEqual(Path(download.path()).read_bytes(), png("blue"))

        download_link = card.get_by_role("link", name="下载图片", exact=True)
        first_path = self.storage.get_artifact_path(first["artifact_id"])
        original_bytes = first_path.read_bytes()
        tampered = bytearray(original_bytes)
        tampered[0] ^= 0xFF
        first_path.write_bytes(bytes(tampered))
        download_link.click()
        expect(self.page.locator("#toastRegion")).to_contain_text("下载失败：文件校验未通过，请刷新后重试。")
        first_path.write_bytes(original_bytes)

        download_route = f"**/api/artifacts/{first['artifact_id']}*"
        self.page.route(download_route, lambda route: route.fulfill(
            status=401, json={"detail": "请先登录"}))
        download_link.click()
        expect(self.page.locator("#toastRegion")).to_contain_text("下载失败：登录状态已失效，请重新登录。")
        self.page.unroute(download_route)

        self.page.route(download_route, lambda route: route.abort("failed"))
        download_link.click()
        expect(self.page.locator("#toastRegion")).to_contain_text(
            "下载失败：无法连接本地工作台，请确认服务仍在运行后重试。")
        self.page.unroute(download_route)

        run_requests = []
        self.page.on("request", lambda request: run_requests.append(request.url)
                    if request.method == "POST" and request.url.endswith("/run") else None)
        tasks_before = len(self.get(f"/api/subprojects/{self.link['subproject_id']}/tasks")["tasks"])
        jobs_before = len(self.app.state.workbench.list_jobs())
        card.get_by_role("button", name="修改图片", exact=True).click()
        expect(self.page.locator("#baseImagePanel")).to_be_visible()
        self.assertIn(first["artifact_id"], self.page.locator("#baseImagePreview img").get_attribute("src"))
        self.assertTrue(self.page.locator('input[name="operation"][value="edit"]').is_checked())
        expect(self.page.locator("#formMessage")).to_contain_text("已选定基底图片")
        self.assertEqual(run_requests, [])
        self.assertEqual(len(self.app.state.workbench.list_jobs()), jobs_before)
        self.assertEqual(len(self.get(f"/api/subprojects/{self.link['subproject_id']}/tasks")["tasks"]), tasks_before)
        self.assertEqual(len(self.backend.requests), 1)

        self.page.locator("#promptInput").fill("只改背景颜色")
        self.backend.output_count = 1
        edit = self.save_root_task()
        self.assertEqual(edit["base_artifact_id"], first["artifact_id"])
        self.assertEqual(edit["subproject_id"], task["subproject_id"])
        expect(self.page.locator("#taskDetails .result-credit")).to_have_attribute("data-credit-status", "credit_frozen")
        expect(self.page.locator("#taskDetails .result-credit-state")).to_have_text("· 已冻结")
        expect(self.page.locator("#taskDetails .result-credit-state")).not_to_contain_text("已扣除")
        revised = self.run_worker(edit)
        self.assertEqual(revised.status, "succeeded")
        revised_artifact = self.storage.get_artifact(revised.output_artifact_ids[0])
        self.assertEqual(revised_artifact.source_artifact_id, first["artifact_id"])
        self.assertEqual(self.storage.get_artifact_path(first["artifact_id"]).read_bytes(), png("blue"))
        self.assertEqual(self.storage.get_artifact_path(second["artifact_id"]).read_bytes(), png("green"))
        self.evidence.update(selected_downloads=[first["artifact_id"], second["artifact_id"]],
                             modify_base=first["artifact_id"], credit_state="completed")

    def test_credit_panel_uses_settled_amount_instead_of_new_quote(self):
        self.root_page()
        self.page.locator("#promptInput").fill("结算金额不随新报价变化")
        task = self.save_root_task()
        self.run_worker(task)
        detail = self.get(f"/api/tasks/{task['task_id']}")
        self.assertEqual((detail["reserved_credit"], detail["estimated_credit"]), (40, 40))
        with mock.patch.dict(CreditCostCalculator.policies, {"local": (80, 100)}):
            self.page.reload(wait_until="networkidle")
            self.page.locator("#taskList .task-row").filter(has_text=task["prompt"]).click()
            detail = self.get(f"/api/tasks/{task['task_id']}")
            self.assertEqual((detail["reserved_credit"], detail["estimated_credit"]), (40, 80))
            credit = self.page.locator("#taskDetails .result-credit")
            expect(credit).to_have_attribute("data-credit-status", "completed")
            expect(credit).to_have_attribute("data-credit-amount", "40")
            expect(credit).to_contain_text("本次任务积分：40")
            expect(credit).not_to_contain_text("本次任务积分：80")
        self.evidence["settled_amount_used"] = True

    def test_modify_click_during_submit_does_not_corrupt_pending_task(self):
        self.root_page()
        self.page.locator("#promptInput").fill("提交前已有结果")
        previous = self.save_root_task()
        self.run_worker(previous)
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=previous["prompt"]).click()
        details = self.page.locator("#taskDetails")
        expect(details.locator(".artifact-card")).to_have_count(1)

        self.page.locator("#promptInput").fill("提交中的新任务")
        original = self.app.state.workbench.create_task

        def slow_create(*args, **kwargs):
            time.sleep(1.2)
            return original(*args, **kwargs)

        run_requests = []
        self.page.on("request", lambda request: run_requests.append(request.url)
                    if request.method == "POST" and request.url.endswith("/run") else None)
        with mock.patch.object(self.app.state.workbench, "create_task", side_effect=slow_create):
            self.page.locator("#saveTaskButton").click()
            self.page.wait_for_timeout(100)
            details.get_by_role("button", name="修改图片", exact=True).click()
            expect(self.page.locator("#toastRegion")).to_contain_text("正在提交任务，请稍候。")
            expect(self.page.locator("#baseImagePanel")).to_be_hidden()
            expect(self.page.locator("#promptInput")).to_have_value("", timeout=5000)

        expect(self.page.locator("#taskList .task-row").filter(has_text="提交中的新任务")).to_have_count(1)
        expect(self.page.locator("#formMessage")).not_to_contain_text("当前页面仍停留在你刚选择的子项目")
        self.assertEqual(len(run_requests), 1)
        self.evidence["modify_blocked_while_busy"] = True

    def test_missing_download_url_disables_download_with_clear_message(self):
        self.root_page()
        self.page.locator("#promptInput").fill("没有下载地址时禁止下载")
        task = self.save_root_task()
        self.run_worker(task)
        detail = self.get(f"/api/tasks/{task['task_id']}")
        detail["outputs"][0]["download_url"] = ""
        pattern = re.compile(rf"/api/tasks/{task['task_id']}$")

        def intercept(route):
            if route.request.method == "GET":
                route.fulfill(json=detail)
            else:
                route.continue_()

        self.page.route(pattern, intercept)
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=task["prompt"]).click()
        link = self.page.locator("#taskDetails .artifact-card").get_by_role("link", name="下载图片", exact=True)
        expect(link).to_have_attribute("aria-disabled", "true")
        self.assertIsNone(link.get_attribute("href"))
        link.click(force=True)
        expect(self.page.locator("#toastRegion")).to_contain_text("当前没有可下载的原始图片")
        self.assertEqual(len(self.backend.requests), 1)
        self.evidence["download_without_url_blocked"] = True

    def test_edit_help_names_the_actual_modify_control(self):
        self.root_page()
        self.page.locator('input[name="operation"][value="edit"]').check(force=True)
        help_text = self.page.locator("#editHelp")
        expect(help_text).to_be_visible()
        expect(help_text).to_contain_text("修改图片")
        expect(help_text).not_to_contain_text("基于此图继续修改")
        self.evidence["edit_help_names_modify"] = True

    def test_legacy_task_without_credit_record_shows_unknown_amount(self):
        self.root_page()
        self.page.locator("#promptInput").fill("历史任务没有积分流水")
        task = self.save_root_task()
        self.run_worker(task)
        detail = self.get(f"/api/tasks/{task['task_id']}")
        detail["credit_status"] = "credit_unknown"
        detail.pop("reserved_credit", None)
        pattern = re.compile(rf"/api/tasks/{task['task_id']}$")

        def intercept(route):
            if route.request.method == "GET":
                route.fulfill(json=detail)
            else:
                route.continue_()

        self.page.route(pattern, intercept)
        self.page.reload(wait_until="networkidle")
        self.page.locator("#taskList .task-row").filter(has_text=task["prompt"]).click()
        credit = self.page.locator("#taskDetails .result-credit")
        expect(credit).to_have_attribute("data-credit-status", "credit_unknown")
        expect(credit).to_have_attribute("data-credit-amount", "待确认")
        expect(credit).to_contain_text("本次任务积分：待确认")
        expect(credit.locator(".result-credit-state")).to_have_text("· 扣费状态待确认")
        expect(credit).not_to_contain_text("预计消耗")
        expect(self.page.locator("#toastRegion")).not_to_contain_text("下载失败")
        self.evidence["legacy_credit_unknown"] = True

    def test_missing_mock_output_keeps_reference_and_reports_failure_without_replay(self):
        source, task = self.paste_and_prepare("缺失产物仍保留输入与任务")
        self.backend.missing_output = True
        self.enqueue(task)
        record = self.run_worker(task)
        self.assertEqual(record.status, "missing_artifact")
        self.assertEqual(record.output_artifact_ids, [])
        self.assertEqual(self.storage.get_artifact_path(source.artifact_id).read_bytes(), png("red"))
        self.refresh_video()
        card = self.task_card(task)
        expect(card).to_contain_text("未找到本次生成的图片文件")
        expect(card.get_by_role("button", name="重试生成", exact=False)).to_be_visible()
        expect(card.locator(".pipeline-image-result")).to_have_count(0)
        expect(self.page.locator("#pipelineImagePrompt")).to_have_value(task["prompt"])
        expect(self.page.locator(f'#pipelineImageReferences input[value="{source.artifact_id}"]')).to_be_checked()
        detail = self.get(f"/api/tasks/{task['task_id']}")
        self.assertEqual(detail["failure_type"], "artifact_not_found")
        self.assertEqual(detail["credit_status"], "refunded")
        self.assertEqual(len(detail["executions"]), 1)
        self.assertEqual(len(self.backend.requests), 1)
        self.evidence["credit_status"] = detail["credit_status"]

    def test_submit_network_failure_retries_same_request_and_runs_once(self):
        source, task = self.paste_and_prepare("断网重试仍引用同一张图片")
        calls = []
        pattern = f"**/api/tasks/{task['task_id']}/run"

        def intercept(route):
            calls.append(route.request.post_data_json)
            if len(calls) == 1:
                route.abort("failed")
            else:
                route.continue_()

        self.page.route(pattern, intercept)
        self.task_card(task).get_by_role("button", name="生成图片", exact=False).click()
        expect(self.page.locator("#videoNotice")).to_contain_text("Failed to fetch")
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.assertEqual(self.backend.requests, [])
        expect(self.page.locator("#pipelineImagePrompt")).to_have_value(task["prompt"])
        expect(self.page.locator(f'#pipelineImageReferences input[value="{source.artifact_id}"]')).to_be_checked()
        self.enqueue(task)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["request_id"], calls[1]["request_id"])
        record = self.run_worker(task)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(len(self.app.state.workbench.list_jobs()), 1)
        self.assertEqual(len(self.backend.requests), 1)
        self.refresh_video()
        expect(self.task_card(task).locator(".pipeline-image-result")).to_have_count(1)
        self.evidence["same_submission_request_id"] = calls[0]["request_id"]
        self.evidence["single_job_and_execution"] = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
