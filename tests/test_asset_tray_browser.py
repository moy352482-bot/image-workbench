"""Asset tray/action tests against the real app, HTTP storage, and Chromium."""

import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from PIL import Image
from playwright.sync_api import sync_playwright, expect

from tests import test_asset_browser as asset_helpers
from tests.test_video import png


REPORT = Path(__file__).resolve().parents[1] / "docs/goals/asset-ux/browser-B.json"
SCREENSHOTS = REPORT.parent / "screenshots"


class AssetTrayBrowserTests(unittest.TestCase):
    # Reuse setup/helpers explicitly; inheriting the test class would rerun phase A.
    setUp = asset_helpers.AssetBrowserTests.setUp
    stop_server = asset_helpers.AssetBrowserTests.stop_server
    post = asset_helpers.AssetBrowserTests.post
    get = asset_helpers.AssetBrowserTests.get
    video = asset_helpers.AssetBrowserTests.video
    root_page = asset_helpers.AssetBrowserTests.root_page
    files = asset_helpers.AssetBrowserTests.files
    event = asset_helpers.AssetBrowserTests.event
    idle = asset_helpers.AssetBrowserTests.idle

    @classmethod
    def setUpClass(cls):
        cls.started = time.perf_counter()
        cls.results = []
        cls.fixture_temp = tempfile.TemporaryDirectory(prefix="iw-tray-fixture-")
        cls.clip_path = Path(cls.fixture_temp.name) / "two-color.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg is required for the real, decodable MP4 fixture")
        subprocess.run([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=red:s=64x48:r=10:d=0.5",
            "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10:d=0.5",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]",
            "-map", "[v]", "-c:v", "libx264", "-movflags", "+faststart", "-y", str(cls.clip_path),
        ], check=True, capture_output=True, timeout=30)
        cls.clip = cls.clip_path.read_bytes()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.fixture_temp.cleanup()
        REPORT.write_text(json.dumps({
            "runner": "Python Playwright Chromium + real uvicorn/SQLite/files + ffmpeg H.264 MP4 fixture",
            "model_calls": 0,
            "elapsed_seconds": round(time.perf_counter() - cls.started, 3),
            "cases": cls.results,
            "note": "All actions use the real API; frame extraction runs in browser video/canvas. Drag tests pass DataTransfer through actual dragstart/drop listeners.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def tearDown(self):
        result = self._outcome.result
        failed = any(test is self for test, _ in result.failures + result.errors)
        screenshot = SCREENSHOTS / (self._testMethodName + ".png")
        self.page.screenshot(path=str(screenshot), full_page=True)
        self.results.append({"name": self._testMethodName,
                             "status": "failed" if failed or self.errors else "passed",
                             "elapsed_seconds": round(time.perf_counter() - self.case_started, 3),
                             "page_errors": self.errors,
                             "screenshot": str(screenshot.relative_to(REPORT.parent)).replace("\\", "/")})
        self.model.generate.assert_not_called()
        self.assertEqual(self.errors, [], "Uncaught browser exceptions")

    def upload_images(self, *colors):
        return self.post(f"/api/video/projects/{self.pid}/assets",
                         files=[("files", (color + ".png", png(color), "image/png")) for color in colors])["assets"]

    def upload_video(self):
        return self.post(f"/api/video/projects/{self.pid}/videos/upload",
                         data={"request_id": uuid4().hex},
                         files={"file": ("two-color.mp4", self.clip, "video/mp4")})

    def output_image(self, color="purple"):
        task = self.post(f"/api/video/projects/{self.pid}/images",
                         json={"request_id": uuid4().hex, "prompt": "浏览器测试产物", "reference_ids": []})
        source = self.root / "synthetic-output.png"
        source.write_bytes(png(color))
        artifact = self.storage.register_output_image(task["project_id"], task["task_id"], source)
        return task, artifact

    def tray(self, aid):
        return self.page.locator(f'#assetTrayGrid [data-artifact-id="{aid}"]')

    def menu(self, container, label):
        details = container.locator(".asset-actions-menu").first
        if details.get_attribute("open") is None:
            details.locator("summary").click()
        details.get_by_role("button", name=label, exact=True).click()

    def drag(self, aid, target):
        return self.page.locator(target).evaluate("""(target, aid) => {
            const source = document.querySelector(`#assetTrayGrid [data-artifact-id="${aid}"]`);
            const transfer = new DataTransfer();
            source.dispatchEvent(new DragEvent('dragstart',{dataTransfer:transfer,bubbles:true,cancelable:true}));
            target.dispatchEvent(new DragEvent('dragover',{dataTransfer:transfer,bubbles:true,cancelable:true}));
            const drop = new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true});
            target.dispatchEvent(drop);
            return {prevented:drop.defaultPrevented,payload:transfer.getData('application/x-image-workbench-asset')};
        }""", aid)

    def test_tray_image_actions_use_same_artifact_and_remove_only_reference(self):
        asset = self.upload_images("red")[0]
        aid = asset["asset_id"]
        self.video()
        card = self.tray(aid)
        expect(card).to_be_visible()
        self.menu(card, "预览")
        expect(self.page.locator(".asset-preview-dialog img")).to_be_visible()
        self.page.wait_for_function("() => document.querySelector('.asset-preview-dialog img')?.naturalWidth > 0")
        self.page.get_by_role("button", name="关闭预览", exact=True).click()
        self.menu(card, "用于视频生成")
        expect(self.page.locator("#pipelineVideos")).to_be_visible()
        expect(self.page.locator(f'#videoReferenceChoices input[value="{aid}"]')).to_be_checked()
        self.assertEqual(self.page.evaluate("() => window.IWVideo.state.referenceOrder"), [aid])
        self.menu(card, "移出暂存区")
        expect(card).to_have_count(0)
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["assets"]), 1)
        self.assertEqual(self.client.get(asset["url"]).content, png("red"))
        self.page.reload(wait_until="networkidle")
        expect(self.tray(aid)).to_have_count(0)
        self.assertEqual(self.get(f"/api/video/projects/{self.pid}/tray")["assets"], [])

    def test_tray_drag_references_existing_artifact_without_copying(self):
        asset = self.upload_images("red")[0]
        aid = asset["asset_id"]
        self.video()
        before = self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]
        payload = self.drag(aid, "#pipelineImageReferences")
        self.assertTrue(payload["prevented"])
        self.assertEqual(json.loads(payload["payload"])["artifact_id"], aid)
        expect(self.page.locator(f'#pipelineImageReferences input[value="{aid}"]')).to_be_checked()
        self.drag(aid, "#pipelineImageReferences")
        expect(self.page.locator("#pipelineImageReferences input:checked")).to_have_count(1)
        self.page.locator('[data-pipeline-step="video"]').click()
        self.drag(aid, "#videoReferenceChoices")
        expect(self.page.locator(f'#videoReferenceChoices input[value="{aid}"]')).to_be_checked()
        self.drag(aid, "#videoReferenceChoices")
        expect(self.page.locator("#videoReferenceChoices input:checked")).to_have_count(1)
        after = self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]
        self.assertEqual([item["artifact_id"] for item in before], [item["artifact_id"] for item in after])
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["assets"]), 1)

    def test_tray_first_last_actions_preserve_occupied_frames(self):
        red, blue, green = self.upload_images("red", "blue", "green")
        self.video()
        self.menu(self.tray(blue["asset_id"]), "设为尾帧")
        expect(self.page.locator("#videoNotice")).to_contain_text("请先选择首帧")
        self.assertEqual(self.page.evaluate("() => window.IWVideo.state.referenceOrder"), [])
        self.menu(self.tray(red["asset_id"]), "设为首帧")
        expect(self.page.locator("#pipelineVideos")).to_be_visible()
        expect(self.page.locator("#videoMode")).to_have_value("I2V")
        self.menu(self.tray(blue["asset_id"]), "设为尾帧")
        chosen = [red["asset_id"], blue["asset_id"]]
        self.page.wait_for_function("(ids) => JSON.stringify(window.IWVideo.state.referenceOrder)===JSON.stringify(ids)",
                                    arg=chosen)
        expect(self.page.locator("#videoMode")).to_have_value("FL2V")
        expect(self.page.locator(".video-reference-order").filter(has_text="尾帧")).to_have_count(1)
        self.menu(self.tray(green["asset_id"]), "设为首帧")
        expect(self.page.locator("#videoNotice")).to_contain_text("已有图片不会覆盖")
        self.assertEqual(self.page.evaluate("() => window.IWVideo.state.referenceOrder"), chosen)
        expect(self.page.locator("#videoReferenceChoices input:checked")).to_have_count(2)

    def test_frame_modes_validate_counts_and_save_existing_i2v_contract(self):
        red, blue = self.upload_images("red", "blue")
        self.video()
        self.page.locator('[data-pipeline-step="video"]').click()
        mode = self.page.locator("#videoMode")
        self.assertEqual(mode.locator("option").all_text_contents(),
                         ["参考素材生视频", "首帧生视频", "首尾帧生视频", "文生视频"])
        first = self.page.locator(f'#videoReferenceChoices input[value="{blue["asset_id"]}"]')
        last = self.page.locator(f'#videoReferenceChoices input[value="{red["asset_id"]}"]')
        name = self.page.locator("#videoShotName")
        prompt = self.page.locator("#videoShotPrompt")
        save = self.page.locator('#videoShotForm button[type="submit"]')
        mode.select_option("I2V")
        name.fill("只有首帧")
        prompt.fill("画面中的主体缓慢向前移动")
        save.click()
        expect(self.page.locator("#videoNotice")).to_contain_text("首帧模式需要选择 1 张")
        self.assertEqual(self.get(f"/api/video/projects/{self.pid}")["shots"], [])
        first.check()
        save.click()
        expect(self.page.locator("#videoNotice")).to_contain_text("分镜已保存")
        shots = self.get(f"/api/video/projects/{self.pid}")["shots"]
        self.assertEqual((shots[0]["mode"], shots[0]["reference_ids"]), ("I2V", [blue["asset_id"]]))
        expect(self.page.locator(".video-shot-meta")).to_contain_text("首帧生视频")

        name.fill("从首帧到尾帧")
        prompt.fill("从蓝色画面平稳过渡到红色画面")
        mode.select_option("FL2V")
        save.click()
        expect(self.page.locator("#videoNotice")).to_contain_text("首尾帧模式需要选择 2 张")
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["shots"]), 1)
        last.check()
        mode.select_option("I2V")
        expect(self.page.locator("#videoReferenceChoices input:checked")).to_have_count(2)
        save.click()
        expect(self.page.locator("#videoNotice")).to_contain_text("首帧模式需要选择 1 张")
        expect(self.page.locator(".video-reference-order").filter(has_text="多余参考图")).to_have_count(1)
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["shots"]), 1)
        mode.select_option("FL2V")
        expect(first.locator("..").locator(".video-reference-order")).to_have_text("首帧")
        expect(last.locator("..").locator(".video-reference-order")).to_have_text("尾帧")
        save.click()
        expect(self.page.locator("#videoNotice")).to_contain_text("分镜已保存")
        shots = self.get(f"/api/video/projects/{self.pid}")["shots"]
        self.assertEqual(len(shots), 2)
        shot = next(item for item in shots if item["name"] == "从首帧到尾帧")
        self.assertEqual((shot["mode"], shot["reference_ids"]),
                         ("I2V", [blue["asset_id"], red["asset_id"]]))
        self.page.reload()
        expect(self.page.locator(".video-shot-meta").filter(has_text="首帧生视频")).to_have_count(1)
        expect(self.page.locator(".video-shot-meta").filter(has_text="首尾帧生视频")).to_have_count(1)
        self.assertTrue(all(item["status"] == "draft" for item in shots))

    def test_generated_result_and_gallery_continue_edit_keep_scope_and_base(self):
        _, artifact = self.output_image()
        self.post(f"/api/video/projects/{self.pid}/images/from-artifact",
                  json={"artifact_id": artifact.artifact_id})
        for selector in ("#pipelineImageTasks .pipeline-image-result", "#videoAssetGrid .video-asset"):
            with self.subTest(source=selector):
                self.video()
                container = self.page.locator(selector).first
                expect(container).to_be_visible()
                container.locator(".asset-actions-menu summary").click()
                container.get_by_role("button", name="继续修改", exact=True).click()
                self.page.wait_for_url(self.origin + "/?**")
                expect(self.page.locator("#baseImagePanel")).to_be_visible()
                expect(self.page.locator('input[name="operation"][value="edit"]')).to_be_checked()
                self.assertIn(artifact.artifact_id, self.page.locator("#baseImagePreview img").get_attribute("src"))
                query = parse_qs(urlparse(self.page.url).query)
                self.assertEqual(query["workspace_id"], [self.wid])
                self.assertEqual(query["main_project_id"], [self.link["main_project_id"]])
                self.assertEqual(query["subproject_id"], [self.link["subproject_id"]])
                self.assertEqual(query["asset_id"], [artifact.artifact_id])
                self.assertEqual(self.page.locator("#workspaceSelect").input_value(), self.wid)
                main = self.get(f"/api/main-projects/{self.link['main_project_id']}")
                sub = next(item for item in main["subprojects"]
                           if item["subproject_id"] == self.link["subproject_id"])
                expect(self.page.locator("#activeProjectName")).to_have_text(sub["name"])

    def test_root_output_menu_handoff_maps_same_subproject_and_artifact(self):
        task, artifact = self.output_image()
        self.root_page()
        self.page.locator("#taskList .task-row").filter(has_text="浏览器测试产物").click()
        card = self.page.locator("#taskDetails .artifact-card").first
        expect(card).to_be_visible()
        self.menu(card, "用于视频生成")
        self.page.wait_for_url(self.origin + "/video?**")
        expect(self.page.locator("#pipelineVideos")).to_be_visible()
        expect(self.page.locator(f'#videoReferenceChoices input[value="{artifact.artifact_id}"]')).to_be_checked()
        query = parse_qs(urlparse(self.page.url).query)
        self.assertEqual(query["workspace_id"], [self.wid])
        self.assertEqual(query["project"], [self.pid])
        self.assertEqual(query["asset_id"], [artifact.artifact_id])
        mapping = self.get(f"/api/video/projects/{query['project'][0]}/pipeline")
        self.assertEqual(mapping["subproject_id"], task["subproject_id"])
        self.assertEqual(mapping["main_project_id"], task["main_project_id"])
        self.assertEqual(len(self.get("/api/video/projects")["projects"]), 1)
        self.assertEqual(self.page.evaluate("() => window.IWVideo.state.referenceOrder"), [artifact.artifact_id])

    def test_video_tray_preview_extract_first_last_frames_and_hide_original_survives(self):
        shot = self.upload_video()
        self.video()
        video_asset = next(item for item in self.get(f"/api/video/projects/{self.pid}/tray")["assets"]
                           if item["media_type"] == "video")
        card = self.tray(video_asset["artifact_id"])
        expect(card).to_be_visible()
        self.menu(card, "预览")
        expect(self.page.locator(".asset-preview-dialog video")).to_be_visible()
        self.page.wait_for_function("() => document.querySelector('.asset-preview-dialog video')?.videoWidth === 64")
        self.page.get_by_role("button", name="关闭预览", exact=True).click()
        self.menu(card, "提取首帧")
        expect(self.page.locator("#assetTrayCount")).to_have_text("2", timeout=20000)
        self.menu(card, "提取尾帧")
        expect(self.page.locator("#assetTrayCount")).to_have_text("3", timeout=20000)
        images = self.get(f"/api/subprojects/{self.link['subproject_id']}/assets")["assets"]
        self.assertEqual(len(images), 2)
        pixels = {}
        for asset in images:
            data = self.client.get(asset["url"]).content
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, (64, 48))
                pixels[asset["filename"]] = image.convert("RGB").getpixel((32, 24))
        first = next(pixel for name, pixel in pixels.items() if "首帧" in name)
        last = next(pixel for name, pixel in pixels.items() if "尾帧" in name)
        self.assertGreater(first[0], 220)
        self.assertLess(first[2], 30)
        self.assertGreater(last[2], 220)
        self.assertLess(last[0], 30)
        self.menu(card, "移出暂存区")
        expect(card).to_have_count(0)
        self.assertEqual(self.client.get(shot["output_url"]).content, self.clip)
        self.page.reload(wait_until="networkidle")
        expect(self.tray(video_asset["artifact_id"])).to_have_count(0)
        expect(self.page.locator("#assetTrayCount")).to_have_text("2")

    def test_video_tray_binds_draft_shot_and_previews_same_clip(self):
        source = self.upload_video()
        target = self.post(f"/api/video/projects/{self.pid}/shots", json={
            "request_id": uuid4().hex, "name": "等待绑定的镜头", "prompt": "用于片段绑定",
            "mode": "T2V", "duration": 4, "aspect_ratio": "landscape", "reference_ids": []})
        self.video()
        asset = next(item for item in self.get(f"/api/video/projects/{self.pid}/tray")["assets"]
                     if item["media_type"] == "video")
        self.menu(self.tray(asset["artifact_id"]), "绑定镜头")
        dialog = self.page.locator(".asset-choice-dialog[open]")
        expect(dialog).to_contain_text("选择要绑定视频的镜头")
        dialog.get_by_role("button", name="等待绑定的镜头", exact=True).click()
        expect(self.page.locator("#videoNotice")).to_contain_text("视频片段已绑定")
        self.page.locator('[data-pipeline-step="video"]').click()
        card = self.page.locator("#videoShots .video-shot-card").filter(has_text="等待绑定的镜头")
        card.get_by_role("button", name="预览绑定视频", exact=True).click()
        expect(self.page.locator(".asset-preview-dialog video")).to_be_visible()
        self.page.wait_for_function("() => document.querySelector('.asset-preview-dialog video')?.videoWidth === 64")
        saved = next(shot for shot in self.get(f"/api/video/projects/{self.pid}")["shots"]
                     if shot["shot_id"] == target["shot_id"])
        self.assertEqual(saved["bound_clip"]["artifact_id"], asset["artifact_id"])
        self.assertEqual(saved["bound_clip"]["source_shot_id"], source["shot_id"])
        self.assertEqual(self.client.get(saved["bound_clip"]["url"]).content, self.clip)
        self.assertEqual(len(self.get(f"/api/video/projects/{self.pid}")["shots"]), 2)
        self.page.get_by_role("button", name="关闭预览", exact=True).click()


if __name__ == "__main__":
    unittest.main(verbosity=2)
