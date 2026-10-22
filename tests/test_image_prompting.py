"""Image Skill snapshots reach both execution paths without changing task identity."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from PIL import Image

from image_workbench import LocalFileBackend
from image_workbench.image_prompting import ImagePromptCompiler, SKILL_ROOT
from image_workbench.worker import LocalWorker
from tests import test_remote, test_web


class RecordingBackend(LocalFileBackend):
    def __init__(self, sources):
        super().__init__(sources)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return super().generate(request)


class ImagePromptCompilerTests(unittest.TestCase):
    def setUp(self):
        self.compiler = ImagePromptCompiler()

    def test_templates_preserve_exact_user_text_and_do_not_import_example_subject(self):
        prompt = '画一辆白色卡车，车门上逐字写“早安 · 你好”，不要改变白色，不要花朵。'
        for style in self.compiler.catalog()["styles"]:
            with self.subTest(style=style["id"]):
                packet = self.compiler.compile(prompt, style_id=style["id"], aspect_ratio="3:2")
                self.assertEqual(packet["source_prompt"], prompt)
                self.assertEqual(packet["spec"]["primary_request"], prompt)
                self.assertEqual(packet["spec"]["aspect_ratio"], "3:2")
                self.assertIn(prompt, packet["compiled_prompt"])
                self.assertNotIn(style["example_prompt"], packet["compiled_prompt"])
                self.assertNotIn(style["preview_url"], packet["compiled_prompt"])
                self.assertEqual(packet["spec"]["input_images"], [])

    def test_no_template_and_edit_input_roles(self):
        packet = self.compiler.compile("只把杯子变成红色，文字和背景保持", operation="edit", reference_count=2)
        self.assertIsNone(packet["style_id"])
        self.assertIsNone(packet["spec"]["style"])
        images = packet["spec"]["input_images"]
        self.assertEqual([item["image"] for item in images], [1, 2, 3])
        self.assertIn("编辑基底", images[0]["role"])
        self.assertIn("参考图 1", images[1]["role"])
        self.assertIn("参考图 2", images[2]["role"])

    def test_skill_source_changes_are_actually_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shutil.copyfile(SKILL_ROOT / "styles.json", root / "styles.json")
            rule = "必须保留本次指定的三角形图案，不可变成圆形。"
            skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8") + "\n" + rule
            (root / "SKILL.md").write_text(skill, encoding="utf-8")
            packet = ImagePromptCompiler(root).compile("图案包装盒")
            self.assertIn(rule, packet["compiled_prompt"])
            self.assertNotEqual(packet["skill_sha256"], self.compiler.digest)

    def test_invalid_style_and_operation_are_rejected(self):
        for kwargs in ({"style_id": "../../outside"}, {"operation": "inpaint"}, {"reference_count": 9}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.compiler.compile("一张图", **kwargs)


class ImagePromptHttpTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp
    post = test_web.WebTests.post
    task = test_web.WebTests.task

    def test_catalog_previews_and_default_skill_are_available(self):
        response = self.client.get("/api/image-styles")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["default_enabled"])
        styles = response.json()["styles"]
        self.assertEqual(len({style["id"] for style in styles}), 8)
        for style in styles:
            with self.subTest(style=style["id"]):
                preview = self.client.get(style["preview_url"])
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.headers["content-type"], "image/png")
                path = Path(__file__).resolve().parents[1] / "image_workbench/web_static/image-styles" / (style["id"] + ".png")
                with Image.open(path) as image:
                    image.verify()
        detail = self.task(prompt="自定义线描风格，保留“山水”二字")
        self.assertIsNone(detail["image_prompt"]["style_id"])
        self.assertEqual(detail["image_prompt"]["source_prompt"], detail["prompt"])
        self.assertEqual(self.state.list_jobs(), [])

    def test_style_is_idempotent_and_snapshot_survives_skill_update(self):
        request_id = uuid4().hex
        fields = {"request_id": request_id, "prompt": "只画红色杯子", "style_id": "commercial-product"}
        first = self.task(**fields)
        with mock.patch("image_workbench.workbench_state.default_image_skill", side_effect=AssertionError("Replay must reuse saved snapshot")):
            replay = self.task(**fields)
        self.assertEqual(first["image_prompt"], replay["image_prompt"])
        changed = self.post(f"/api/projects/{self.pid}/tasks", data={**fields, "operation": "generate", "style_id": "watercolor"})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.state.list_jobs(), [])
        self.assertEqual(len(self.storage.list_tasks(self.pid)), 1)
        listing = self.client.get(f"/api/projects/{self.pid}/tasks").json()["tasks"]
        self.assertNotIn("image_prompt", listing[0])

    def test_unknown_or_duplicate_style_is_rejected_without_task(self):
        response = self.post(f"/api/projects/{self.pid}/tasks", data={
            "request_id": uuid4().hex, "operation": "generate", "prompt": "一只杯子", "style_id": "unknown"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.storage.list_tasks(self.pid), [])
        response = self.post(f"/api/projects/{self.pid}/tasks", files=[
            ("request_id", (None, uuid4().hex)), ("operation", (None, "generate")), ("prompt", (None, "一只杯子")),
            ("style_id", (None, "watercolor")), ("style_id", (None, "retro-film"))])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.state.list_jobs(), [])

    def test_local_execution_gets_saved_prompt_and_original_task_remains_intact(self):
        detail = self.task(prompt="白色产品，文字：你好", style_id="commercial-product", aspect_ratio="3:2")
        task = self.storage.get_task(self.pid, detail["task_id"])
        original = task.prompt
        self.state.submit(self.pid, task.task_id, request_id=uuid4().hex, confirmed=True)
        backend = RecordingBackend([self.fixture])
        with mock.patch("image_workbench.image_prompting.default_image_skill", side_effect=AssertionError("Saved task must not be recompiled")):
            LocalWorker(self.state, backend).serve(once=True)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.requests[0].task.prompt, detail["image_prompt"]["compiled_prompt"])
        self.assertEqual(self.storage.get_task(self.pid, task.task_id).prompt, original)
        self.assertEqual(self.state.task_view(self.pid, task.task_id)["status"], "awaiting_review")

    def test_legacy_task_gets_default_rules_at_execution(self):
        task = self.state.create_task(self.pid, request_id=uuid4().hex, operation="generate",
                                      prompt="旧任务的蓝色小鸟" + "。" * 11992, aspect_ratio="3:2")
        detail = self.state.task_view(self.pid, task.task_id)
        self.assertGreater(len(detail["prompt"]), 12000)  # Legacy ratio suffix is not user input.
        task_id = detail["task_id"]
        options_path = self.state._task_file("tasks", self.pid, task_id)
        options = self.state.task_options(self.pid, task_id)
        options.pop("image_prompt")
        self.storage.files.write_json(options_path, options)
        self.state.submit(self.pid, task_id, request_id=uuid4().hex, confirmed=True)
        backend = RecordingBackend([self.fixture])
        LocalWorker(self.state, backend).serve(once=True)
        prompt = backend.requests[0].task.prompt
        self.assertIn("image-generation-default", prompt)
        self.assertIn("旧任务的蓝色小鸟", prompt)
        self.assertIn("3:2", prompt)
        self.assertIn('"aspect_ratio": "3:2"', prompt)
        self.assertNotIn("image_prompt", self.state.task_options(self.pid, task_id))

    def test_web_input_limit_remains_at_the_existing_boundary(self):
        response = self.post(f"/api/projects/{self.pid}/tasks", data={
            "request_id": uuid4().hex, "operation": "generate", "prompt": "x" * 12001})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.storage.list_tasks(self.pid), [])
        accepted = self.task(prompt="x" * 12000)
        self.assertEqual(len(accepted["image_prompt"]["source_prompt"]), 12000)

    def test_edit_keeps_base_reference_order_and_lineage(self):
        base_task = self.task(prompt="原图")
        base = self.storage.register_output_image(self.pid, base_task["task_id"], self.fixture)
        response = self.post(f"/api/projects/{self.pid}/tasks", data={
            "request_id": uuid4().hex, "operation": "edit", "prompt": "只修改光线，文字原样保留", "style_id": "cinematic-realism",
            "base_artifact_id": base.artifact_id}, files={"files": ("reference.png", self.fixture.read_bytes(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        self.assertEqual(len(detail["image_prompt"]["spec"]["input_images"]), 2)
        self.state.submit(self.pid, detail["task_id"], request_id=uuid4().hex, confirmed=True)
        backend = RecordingBackend([self.fixture])
        LocalWorker(self.state, backend).serve(once=True)
        request = backend.requests[0]
        self.assertEqual(request.task.base_artifact_id, base.artifact_id)
        self.assertEqual(request.edit_image.read_bytes(), self.fixture.read_bytes())
        self.assertEqual(len(request.reference_images), 1)
        output = self.state.task_view(self.pid, detail["task_id"])["outputs"][0]
        self.assertEqual(output["source_artifact_id"], base.artifact_id)


class ImagePromptRemoteTests(unittest.TestCase):
    setUp = test_remote.RemoteTests.setUp
    new_client = test_remote.RemoteTests.new_client
    worker = test_remote.RemoteTests.worker
    submit = test_remote.RemoteTests.submit

    def test_remote_worker_receives_same_snapshot_and_does_not_repeat_generation(self):
        task, _ = self.submit(style_id="watercolor", inputs=[self.image])
        saved = self.state.task_options(task.project_id, task.task_id)["image_prompt"]
        worker = self.worker()
        self.assertEqual(worker.run_once(), "completed")
        self.assertEqual(worker.run_once(), "idle")
        self.assertEqual(len(self.backend.requests), 1)
        self.assertEqual(self.backend.requests[0].task.prompt, saved["compiled_prompt"])
        self.assertEqual(self.backend.requests[0].reference_images[0].read_bytes(), self.image.read_bytes())
        self.assertEqual(self.storage.get_task(task.project_id, task.task_id).prompt, task.prompt)
        self.assertNotIn("example_prompt", json.dumps(saved["spec"]))
