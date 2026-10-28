"""Image execution timer in Chromium; temporary tasks and mocked status only."""
import unittest
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from tests import test_asset_browser as fixtures


class ImageTimerBrowserTests(unittest.TestCase):
    setUp = fixtures.AssetBrowserTests.setUp
    stop_server = fixtures.AssetBrowserTests.stop_server
    post = fixtures.AssetBrowserTests.post
    get = fixtures.AssetBrowserTests.get

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def tearDown(self):
        self.page.unroute_all(behavior="ignoreErrors")
        self.model.generate.assert_not_called()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.app.state.workbench.list_jobs(), [])

    def task(self, name="逐秒计时"):
        return self.post(f"/api/subprojects/{self.link['subproject_id']}/tasks", data={
            "request_id": uuid4().hex, "name": name, "operation": "generate", "prompt": "不调用模型",
        })

    def show(self, detail, handler=None):
        self.path = f"/api/tasks/{detail['task_id']}"
        self.detail = {**detail, "status": "generating", "elapsed_seconds": 10,
                       "execution_seconds": 10, "execution_timing_source": "running"}
        self.page.route("**" + self.path, handler or (lambda route: route.fulfill(json=self.detail)))
        self.page.clock.install(time=1000000)
        self.page.clock.pause_at(1001000)
        self.page.goto(self.origin + "/", wait_until="domcontentloaded")
        self.page.locator(".project-child").first.click()
        self.page.locator(".task-row").filter(has_text=detail["name"]).click()
        self.timer = self.page.locator(".detail-progress span")
        expect(self.timer).to_have_text("生成中 · 已运行 10 秒")

    def poll(self):
        with self.page.expect_response(lambda response: response.url.endswith(self.path)):
            self.page.clock.run_for(3000)

    def hold(self, route, pending):
        pending.append(route)
        self.page.evaluate("count => { window.heldTimerRequests = count; }", len(pending))

    def test_seconds_advance_between_polls_without_requests_or_rebuilding_details(self):
        requests, pending = [], []

        def respond(route):
            requests.append(route.request)
            if len(requests) == 1:
                route.fulfill(json=self.detail)
            else:
                self.hold(route, pending)

        self.show(self.task(), respond)
        self.page.evaluate("window.originalTiming = document.querySelector('.detail-progress')")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("生成中 · 已运行 11 秒")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("生成中 · 已运行 12 秒")
        self.assertEqual(len(requests), 1, "display ticks must not add HTTP polling")
        self.assertTrue(self.page.evaluate("window.originalTiming === document.querySelector('.detail-progress')"))
        with self.page.expect_request(lambda request: request.url.endswith(self.path)):
            self.page.clock.run_for(1000)
        self.page.wait_for_function("() => window.heldTimerRequests === 1")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("生成中 · 已运行 14 秒")
        self.assertEqual(len(pending), 1, "a slow response must not stop local seconds")
        with self.page.expect_event("requestfailed", predicate=lambda request: request.url.endswith(self.path)):
            pending[0].abort()

    def test_late_poll_cannot_restart_a_finished_timer_after_manual_refresh(self):
        pending = []

        def respond(route):
            if self.detail.get("delay"):
                self.hold(route, pending)
            else:
                route.fulfill(json=self.detail)

        self.show(self.task(), respond)
        self.detail["delay"] = True
        with self.page.expect_request(lambda request: request.url.endswith(self.path)):
            self.page.clock.run_for(3000)
        self.page.wait_for_function("() => window.heldTimerRequests === 1")
        self.page.clock.run_for(3000)
        self.assertEqual(len(pending), 1, "automatic polls must not overlap a slow request")
        # A user refresh can supersede an in-flight poll even though timed polls
        # are serialized. Its final result must survive the older response.
        with self.page.expect_request(lambda request: request.url.endswith(self.path)):
            self.page.locator("#refreshButton").click()
        self.page.wait_for_function("() => window.heldTimerRequests === 2")
        finished = {**self.detail, "status": "failed", "execution_seconds": 15.2}
        pending[1].fulfill(json=finished)
        expect(self.timer).to_have_text("执行失败 · 本次执行耗时 15 秒")
        with self.page.expect_response(lambda response: response.url.endswith(self.path)):
            pending[0].fulfill(json=self.detail)
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("执行失败 · 本次执行耗时 15 秒")

    def test_poll_calibrates_clock_and_terminal_result_stops_it(self):
        self.show(self.task())
        self.detail.update(status="checking", elapsed_seconds=59, execution_seconds=59)
        self.poll()
        expect(self.timer).to_have_text("检查产物 · 已运行 59 秒")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("检查产物 · 已运行 1 分 0 秒")
        self.detail.update(status="awaiting_review", elapsed_seconds=63.2, execution_seconds=63.2)
        self.poll()
        expect(self.timer).to_have_text("本次执行完成 · 本次执行耗时 1 分 3 秒")
        self.page.clock.run_for(10000)
        expect(self.timer).to_have_text("本次执行完成 · 本次执行耗时 1 分 3 秒")

    def test_queue_unknown_and_new_attempt_do_not_accumulate_old_seconds(self):
        self.show(self.task())
        self.detail.update(status="queued", elapsed_seconds=None, execution_seconds=None)
        self.poll()
        expect(self.timer).to_have_text("等待执行 · 尚未开始计时")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("等待执行 · 尚未开始计时")
        self.detail.update(status="generating", elapsed_seconds=0, execution_seconds=0)
        self.poll()
        expect(self.timer).to_have_text("生成中 · 已运行 0 秒")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("生成中 · 已运行 1 秒")
        self.detail.update(status="uncertain", elapsed_seconds=None, execution_seconds=None)
        self.poll()
        expect(self.timer).to_have_text("执行状态待确认 · 最终耗时暂不可用")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("执行状态待确认 · 最终耗时暂不可用")

    def test_switch_and_reload_use_selected_task_and_ignore_late_old_response(self):
        first = self.task("任务 A")
        other = self.task("任务 B")
        other.update(status="generating", elapsed_seconds=40, execution_seconds=40)
        self.page.route(f"**/api/tasks/{other['task_id']}", lambda route: route.fulfill(json=other))
        pending = []

        def respond(route):
            if not self.detail.get("delay"):
                route.fulfill(json=self.detail)
            else:
                self.hold(route, pending)

        self.show(first, respond)
        self.detail["delay"] = True
        with self.page.expect_request(lambda request: request.url.endswith(self.path)):
            self.page.clock.run_for(3000)
        self.page.wait_for_function("window.heldTimerRequests === 1")
        self.page.locator(".task-row").filter(has_text="任务 B").click()
        expect(self.timer).to_have_text("生成中 · 已运行 40 秒")
        pending[0].fulfill(json=self.detail)
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("生成中 · 已运行 41 秒")
        self.page.reload(wait_until="domcontentloaded")
        expect(self.timer).to_have_text("生成中 · 已运行 40 秒")
        self.page.clock.run_for(1000)
        expect(self.timer).to_have_text("生成中 · 已运行 41 秒")


if __name__ == "__main__":
    unittest.main()
