"""Real browser -> stored assets -> queued Mock image execution -> immutable versions."""

import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from playwright.sync_api import expect, sync_playwright

from image_workbench import BackendResult
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

    def generate(self, request):
        self.requests.append(request)
        output = request.work_dir / "products" / "mock-result.png"
        if not self.missing_output:
            output.write_bytes(png("blue" if len(self.requests) == 1 else "green"))
        return BackendResult((output,))


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
        card = self.page.locator(f'#pipelineImageReferences input[value="{stored.artifact_id}"]').locator("..")
        expect(card).to_be_visible()
        self.evidence["paste_timing"] = self.page.evaluate("""() => ({
            preview_ms:window.assetTiming.preview_ms,
            registered_and_visible_ms:performance.now()-window.assetTiming.start
        })""")
        self.assertIsNotNone(self.evidence["paste_timing"]["preview_ms"])
        # Remove and re-add through the current task, keeping the same stored asset.
        card.get_by_role("button", name="移除参考图 red.png", exact=True).click()
        expect(card).to_have_count(0)
        reference_started = time.perf_counter()
        self.assertTrue(self.event(colors=("red",)))
        self.idle()
        expect(self.page.locator(f'#pipelineImageReferences input[value="{stored.artifact_id}"]')).to_be_checked()
        self.evidence["direct_reference_ms"] = round((time.perf_counter() - reference_started) * 1000, 2)
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
        with self.page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url.endswith(f"/api/tasks/{edit['task_id']}/run")) as submitted:
            self.page.get_by_role("button", name="开始演练", exact=True).click()
        self.assertEqual(submitted.value.status, 200, submitted.value.text())
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
        expect(self.page.locator(".artifact-card")).to_have_count(1)
        expect(self.page.locator(".artifact-card")).to_contain_text("来源版本")
        expect(self.page.locator(".artifact-card")).to_contain_text(first.artifact_id)
        self.page.wait_for_function("() => [...document.querySelectorAll('.artifact-card img')].every(img => img.complete && img.naturalWidth > 0)")
        self.assertEqual(self.get(f"/api/tasks/{edit['task_id']}")["outputs"][0]["artifact_id"], second.artifact_id)
        self.evidence["version_chain"] = [first.artifact_id, second.artifact_id]
        self.evidence["source_files_unchanged"] = True
        self.assertEqual(len(self.backend.requests), 2)

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
