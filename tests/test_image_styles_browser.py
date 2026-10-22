"""Real browser use of style previews, reuse and queued image execution."""

from pathlib import Path
import unittest

from playwright.sync_api import expect, sync_playwright

from image_workbench.worker import LocalWorker
from tests import test_asset_browser as helpers
from tests.test_image_prompting import RecordingBackend
from tests.test_video import png


SCREENSHOTS = Path(__file__).resolve().parents[1] / "work/image-generation-skill/screenshots"


class ImageStylesBrowserTests(unittest.TestCase):
    setUp = helpers.AssetBrowserTests.setUp
    stop_server = helpers.AssetBrowserTests.stop_server
    post = helpers.AssetBrowserTests.post
    get = helpers.AssetBrowserTests.get
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

    def tearDown(self):
        self.model.generate.assert_not_called()
        self.assertEqual(self.errors, [], "Uncaught browser exceptions")

    def root_page(self):
        self.page.goto(self.origin + "/", wait_until="domcontentloaded")
        self.page.locator(".project-child").first.click()
        expect(self.page.locator("#taskForm")).to_be_visible()
        expect(self.page.locator("#saveTaskButton")).to_be_enabled()

    def gallery(self):
        self.page.locator("#browseImageStyles").click()
        expect(self.page.locator("#imageStylesDialog")).to_be_visible()
        expect(self.page.locator("#imageStyleCards button")).to_have_count(8)

    def test_previews_application_copy_reuse_and_undo(self):
        self.root_page()
        prompt = '原创要求：白色圆桌，标题“山水之间”，保留所有文字。'
        self.page.locator("#promptInput").fill(prompt)
        self.page.locator("#ratioSelect").select_option("3:2")
        self.gallery()
        for card in self.page.locator("#imageStyleCards button").all():
            card.scroll_into_view_if_needed()
            expect(card.locator("img")).to_be_visible()
            self.page.wait_for_function("img => img.complete && img.naturalWidth > 0", arg=card.locator("img").element_handle())
        self.page.get_by_role("button", name="查看平面海报风格案例").click()
        expect(self.page.get_by_role("button", name="查看平面海报风格案例")).to_have_attribute("aria-pressed", "true")
        expect(self.page.locator("#imageStyleName")).to_have_text("平面海报")
        self.page.locator("#imageStylesDialog").evaluate("dialog => { dialog.scrollTop = 0; }")
        self.page.screenshot(path=str(SCREENSHOTS / "style-gallery-desktop.png"))
        self.page.locator("#applyImageStyle").click()
        expect(self.page.locator("#imageStyleSelect")).to_have_value("graphic-poster")
        expect(self.page.locator("#promptInput")).to_have_value(prompt)
        self.gallery()
        example = self.page.locator("#imageStyleExample").input_value()
        self.page.locator("#copyImageStyleExample").click()
        self.assertEqual(self.page.evaluate("navigator.clipboard.readText()"), example)
        self.page.locator("#useImageStyleExample").click()
        expect(self.page.locator("#promptInput")).to_have_value(example)
        expect(self.page.locator("#ratioSelect")).to_have_value("2:3")
        self.page.locator("#undoStyleExample").click()
        expect(self.page.locator("#promptInput")).to_have_value(prompt)
        expect(self.page.locator("#ratioSelect")).to_have_value("3:2")
        expect(self.page.locator("#imageStyleSelect")).to_have_value("graphic-poster")

    def test_saved_style_reaches_worker_without_preview_images_or_extra_calls(self):
        self.root_page()
        self.page.locator("#promptInput").fill('蓝色纸盒，仅写“蓝盒”')
        self.page.locator("#imageStyleSelect").select_option("commercial-product")
        task = self.save_root_task()
        self.assertEqual(task["image_prompt"]["style_id"], "commercial-product")
        self.assertEqual(task["image_prompt"]["source_prompt"], '蓝色纸盒，仅写“蓝盒”')
        self.assertEqual(task["input_count"], 0)
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        expect(self.page.locator(".prompt-skill-details summary")).to_contain_text("极简产品")
        self.page.locator(".prompt-skill-details summary").click()
        expect(self.page.locator(".prompt-skill-details pre")).to_contain_text("image-generation-default")
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/api/tasks/{task['task_id']}/run")) as submitted:
            self.page.get_by_role("button", name="开始演练", exact=True).click()
        self.assertEqual(submitted.value.status, 200)
        fixture = self.root / "preview-test.png"
        fixture.write_bytes(png("blue"))
        backend = RecordingBackend([fixture])
        LocalWorker(self.app.state.workbench, backend).serve(once=True)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.requests[0].task.prompt, task["image_prompt"]["compiled_prompt"])
        self.assertEqual(backend.requests[0].reference_images, ())
        self.assertEqual(self.get(f"/api/tasks/{task['task_id']}")["output_count"], 1)
        self.page.screenshot(path=str(SCREENSHOTS / "style-saved-task.png"))

    def test_catalog_failure_keeps_form_usable_and_gallery_can_retry(self):
        self.page.route("**/api/image-styles", lambda route: route.abort(), times=1)
        self.root_page()
        expect(self.page.locator("#saveTaskButton")).to_be_enabled()
        expect(self.page.locator("#imageStyleSelect option")).to_have_count(1)
        self.gallery()
        expect(self.page.locator("#imageStyleSelect option")).to_have_count(9)

    def test_mobile_gallery_has_no_horizontal_overflow_and_can_apply(self):
        self.root_page()
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.gallery()
        self.page.get_by_role("button", name="查看清透水彩风格案例").click()
        self.page.locator("#applyImageStyle").scroll_into_view_if_needed()
        overflow = self.page.locator("#imageStylesDialog").evaluate("dialog => dialog.scrollWidth - dialog.clientWidth")
        self.assertLessEqual(overflow, 1)
        self.page.screenshot(path=str(SCREENSHOTS / "style-gallery-mobile.png"))
        self.page.locator("#applyImageStyle").click()
        expect(self.page.locator("#imageStyleSelect")).to_have_value("watercolor")
        expect(self.page.locator("#imageStylesDialog")).not_to_be_visible()
