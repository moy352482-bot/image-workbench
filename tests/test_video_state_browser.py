"""Real browser coverage for scoped video state and late pipeline responses."""
import unittest
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

from tests import test_asset_browser as fixtures
from tests.test_video import png


class VideoStateBrowserTests(unittest.TestCase):
    setUp = fixtures.AssetBrowserTests.setUp
    stop_server = fixtures.AssetBrowserTests.stop_server
    post = fixtures.AssetBrowserTests.post
    get = fixtures.AssetBrowserTests.get
    video = fixtures.AssetBrowserTests.video
    event = fixtures.AssetBrowserTests.event
    idle = fixtures.AssetBrowserTests.idle

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.model.generate.assert_not_called()

    def other_project(self):
        pid = uuid4().hex
        self.post("/api/video/projects", json={"request_id": pid, "name": "项目 B"})
        self.post(f"/api/video/projects/{pid}/pipeline", json={})
        return pid

    def select(self, pid):
        self.page.evaluate("id => window.IWVideo.selectProject(id)", pid)
        self.page.wait_for_function("id => window.IWVideo.snapshot().project?.project_id === id", arg=pid)

    def test_project_tree_navigation_uses_shared_main_subprojects_without_starting_jobs(self):
        self.video()
        expect(self.page.locator("#videoProjectList")).to_contain_text("浏览器项目 A")
        expect(self.page.locator("#videoProjectList")).to_contain_text("未分类任务")

        with self.page.expect_response(lambda response: response.url.endswith("/api/main-projects")
                                       and response.request.method == "POST") as created:
            self.page.locator("#newVideoProject").click()
            self.page.locator("#videoProjectDialogName").fill("浏览器品牌 B")
            self.page.locator("#videoProjectDialogSubmit").click()
        main = created.value.json()
        main_id = main["main_project_id"]
        self.page.wait_for_function("id => window.IWVideo.snapshot().mainProjectId === id", arg=main_id)
        group = self.page.locator(".project-tree-group").filter(has_text="浏览器品牌 B")
        expect(group).to_be_visible()

        with self.page.expect_response(lambda response: response.url.endswith("/subprojects")
                                       and response.request.method == "POST") as sub_created:
            group.locator(".project-child-add").click()
            self.page.locator("#videoProjectDialogName").fill("秋季宣传片")
            self.page.locator("#videoProjectDialogSubmit").click()
        sub_id = sub_created.value.json()["subproject_id"]
        self.page.wait_for_function("id => window.IWVideo.snapshot().subprojectId === id", arg=sub_id)
        expect(self.page.locator("#videoActiveName")).to_have_text("秋季宣传片")
        self.assertIn("subproject=" + sub_id, self.page.url)

        sub_row = group.locator(".project-child-row").filter(has_text="秋季宣传片")
        with self.page.expect_response(lambda response: response.url.endswith(f"/api/subprojects/{sub_id}")
                                       and response.request.method == "PATCH"):
            sub_row.locator(".project-sub-options").click()
            self.page.locator("#videoProjectDialogName").fill("秋季成片")
            self.page.locator("#videoProjectDialogSubmit").click()
        expect(self.page.locator("#videoProjectList")).to_contain_text("秋季成片")
        self.page.wait_for_function("id => window.IWVideo.snapshot().subprojectId === id", arg=sub_id)

        with self.page.expect_response(lambda response: response.url.endswith(f"/api/main-projects/{main_id}")
                                       and response.request.method == "PATCH"):
            group.locator(".project-options").click()
            self.page.locator("#videoProjectDialogName").fill("浏览器品牌 2026")
            self.page.locator("#videoProjectDialogSubmit").click()
        expect(self.page.locator("#videoProjectList")).to_contain_text("浏览器品牌 2026")

        self.page.locator(".project-tree-group").filter(has_text="浏览器项目 A").locator(".project-item").click()
        self.page.wait_for_function("id => window.IWVideo.snapshot().project?.project_id === id", arg=self.pid)
        expect(self.page.locator("#videoActiveName")).to_have_text("浏览器项目 A")
        self.assertEqual(self.app.video_store._jobs(), [])
        self.assertEqual(self.app.identity.wallet(self.aid)["used_credit"], 0)

    def test_snapshots_and_events_cannot_mutate_state_and_reference_order_is_owned(self):
        assets = self.post(f"/api/video/projects/{self.pid}/assets", files=[
            ("files", (color + ".png", png(color), "image/png")) for color in ("red", "blue")])["assets"]
        self.video()
        ids = [asset["asset_id"] for asset in assets]
        result = self.page.evaluate("""async ids => {
            const host = window.IWVideo, view = host.snapshot();
            const name = view.project.name, workspace = view.account.workspace_id;
            view.project.name = 'corrupt'; view.project.assets.length = 0;
            view.account.workspace_id = 'foreign'; view.referenceOrder.push('fake');
            host.setReferences(ids, {context: host.captureContext(), mode:'I2V'});
            const wrongScope = {...host.captureContext(), workspace:'foreign'};
            const rejected = host.setReferences([...ids].reverse(), {context:wrongScope,mode:'I2V'});
            document.addEventListener('video-project-view', event => {event.detail.project.name='mutated event';}, {once:true});
            await host.refreshProject();
            return {rawState: Object.hasOwn(host,'state'), view:host.snapshot(), name, workspace, rejected};
        }""", ids)
        self.assertFalse(result["rawState"])
        self.assertFalse(result["rejected"])
        self.assertEqual(result["view"]["project"]["name"], result["name"])
        self.assertEqual(result["view"]["account"]["workspace_id"], result["workspace"])
        self.assertEqual(len(result["view"]["project"]["assets"]), 2)
        self.assertEqual(result["view"]["referenceOrder"], ids)

    def test_slow_selection_cannot_replace_a_newer_visit_to_the_same_project(self):
        self.video()
        other = self.other_project()
        old = self.get(f"/api/video/projects/{self.pid}")
        old["name"] = "过期项目响应"
        pending = []

        def delay_first(route):
            if not pending:
                pending.append(route)
            else:
                route.continue_()

        self.page.route(f"**/api/video/projects/{self.pid}", delay_first)
        with self.page.expect_request(lambda request: request.url.endswith(f"/api/video/projects/{self.pid}")):
            self.page.evaluate("id => {window.pendingSelection = window.IWVideo.selectProject(id);}", self.pid)
        self.select(other)
        self.select(self.pid)
        pending[0].fulfill(json=old)
        self.page.evaluate("() => window.pendingSelection")
        expect(self.page.locator("#videoActiveName")).to_have_text("浏览器项目 A")
        self.assertEqual(self.page.evaluate("() => window.IWVideo.snapshot().project.name"), "浏览器项目 A")
        self.assertIn("project=" + self.pid, self.page.url)

    def test_newer_refresh_wins_even_when_project_scope_did_not_change(self):
        self.video()
        old = self.get(f"/api/video/projects/{self.pid}")
        pending = []

        def delay_first(route):
            if not pending:
                pending.append(route)
            else:
                route.continue_()

        self.page.route(f"**/api/video/projects/{self.pid}", delay_first)
        with self.page.expect_request(lambda request: request.url.endswith(f"/api/video/projects/{self.pid}")):
            self.page.evaluate("() => {window.pendingRefresh = window.IWVideo.refreshProject();}")
        self.post(f"/api/video/projects/{self.pid}/assets", files=[("files", ("new.png", png(), "image/png"))])
        self.page.evaluate("() => window.IWVideo.refreshProject()")
        pending[0].fulfill(json=old)
        self.assertIsNone(self.page.evaluate("() => window.pendingRefresh"))
        self.assertEqual(len(self.page.evaluate("() => window.IWVideo.snapshot().project.assets")), 1)
        expect(self.page.locator("#videoAssetCount")).to_have_text("1 张")

    def test_late_shot_save_preserves_newer_form_edits(self):
        self.video()
        self.page.locator('[data-pipeline-step="video"]').click()
        self.page.locator('[data-pipeline-tab="video"][data-mode="generate"]').click()
        self.page.locator("#videoMode").select_option("T2V")
        self.page.locator("#videoShotName").fill("原镜头")
        self.page.locator("#videoShotPrompt").fill("原来的画面")
        pending = []
        self.page.route(f"**/api/video/projects/{self.pid}/shots", lambda route: pending.append(route))
        with self.page.expect_request(lambda request: request.url.endswith(f"/api/video/projects/{self.pid}/shots")):
            self.page.locator('#videoShotForm button[type="submit"]').click()
        self.page.locator("#videoShotName").fill("下一镜头")
        self.page.locator("#videoShotPrompt").fill("尚未保存的新画面")
        pending[0].continue_()
        expect(self.page.locator("#videoNotice")).to_contain_text("分镜已保存")
        expect(self.page.locator("#videoShotName")).to_have_value("下一镜头")
        expect(self.page.locator("#videoShotPrompt")).to_have_value("尚未保存的新画面")
        shots = self.get(f"/api/video/projects/{self.pid}")["shots"]
        self.assertEqual([shot["prompt"] for shot in shots], ["原来的画面"])

    def test_upload_returning_after_leaving_and_reentering_project_does_not_select_assets(self):
        self.video()
        other = self.other_project()
        pending = []
        self.page.route(f"**/api/video/projects/{self.pid}/assets", lambda route: pending.append(route))
        with self.page.expect_request(lambda request: request.url.endswith(f"/api/video/projects/{self.pid}/assets")):
            self.event(colors=("red",))
        self.select(other)
        self.select(self.pid)
        pending[0].continue_()
        self.idle()
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(0)
        self.assertEqual(self.page.evaluate("() => window.IWVideo.snapshot().referenceOrder"), [])
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["assets"]), 1)
        self.assertEqual(self.get(f"/api/video/projects/{other}")["assets"], [])

    def test_workspace_switch_invalidates_pending_reads_before_navigation(self):
        other = self.app.identity.create_workspace(self.aid, "第二空间")["workspace_id"]
        self.video()
        old = self.get(f"/api/video/projects/{self.pid}")
        old["name"] = "切换空间前的旧响应"
        reads, switches = [], []
        self.page.route(f"**/api/video/projects/{self.pid}", lambda route: reads.append(route))
        self.page.route("**/api/workspaces/select", lambda route: switches.append(route))
        with self.page.expect_request(lambda request: request.url.endswith(f"/api/video/projects/{self.pid}")):
            self.page.evaluate("() => {window.previousContext=window.IWVideo.captureContext();window.pendingRefresh=window.IWVideo.refreshProject();}")
        with self.page.expect_request(lambda request: request.url.endswith("/api/workspaces/select")):
            self.page.locator("#videoWorkspace").select_option(other)
        reads[0].fulfill(json=old)
        self.assertIsNone(self.page.evaluate("() => window.pendingRefresh"))
        self.assertFalse(self.page.evaluate("() => window.IWVideo.isCurrent(window.previousContext)"))
        expect(self.page.locator("#videoActiveName")).to_have_text("浏览器项目 A")
        switches[0].continue_()
        self.page.wait_for_function("id => window.IWVideo?.snapshot().account?.workspace_id === id", arg=other)
        self.assertIsNone(self.page.evaluate("() => window.IWVideo.snapshot().project"))
