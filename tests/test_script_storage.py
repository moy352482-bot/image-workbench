"""文本产物的存储边界；仅使用本地临时目录，不调用模型。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from image_workbench import MetadataError, StorageConfig, StorageManager
from image_workbench.workspace_scope import WorkspaceStorage


class ScriptStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(self.root / "data"))
        self.project = self.storage.create_project("脚本项目")
        self.pid = self.project.project_id

    def task(self, operation="script", base=None):
        return self.storage.create_task(self.pid, operation, "用户要求", base)

    def text(self, content="第一场　室内　清晨\n小林推开窗，停顿后说：今天出发。\n", name=None):
        path = self.root / (name or (uuid4().hex + ".txt"))
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def image(self):
        path = self.root / "image.png"
        Image.new("RGB", (4, 3), "blue").save(path)
        return path

    def output(self, task=None, **kwargs):
        task = task or self.task()
        return self.storage.register_output_text(self.pid, task.task_id, self.text(), **kwargs)

    def test_utf8_output_uses_existing_manifest_index_and_directory(self):
        for operation in ("script", "screenplay"):
            with self.subTest(operation=operation):
                task = self.task(operation)
                source = self.text(name=operation + ".source")
                original = source.read_bytes()
                result = self.storage.register_output_text(self.pid, task.task_id, source)
                self.assertEqual((result.format, result.width, result.height, result.kind, result.role),
                                 ("TXT", 0, 0, "output", None))
                self.assertEqual(result.relative_path,
                                 f"projects/{self.pid}/tasks/{task.task_id}/outputs/{result.artifact_id}.txt")
                self.assertEqual(result.sha256, hashlib.sha256(original).hexdigest())
                self.assertEqual(result.size_bytes, len(original))
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(self.storage.get_artifact_path(result.artifact_id).read_bytes(), original)
                self.assertEqual(self.storage.get_artifact(result.artifact_id), result)
                restarted = StorageManager(self.storage.config)
                self.assertEqual(restarted.list_task_artifacts(self.pid, task.task_id), [result])

    def test_revision_preserves_parent_bytes_and_links_same_type(self):
        original = self.output()
        old_bytes = self.storage.get_artifact_path(original.artifact_id).read_bytes()
        revision = self.task(base=original.artifact_id)
        new = self.storage.register_output_text(self.pid, revision.task_id, self.text("第二稿\n人物先敲门再进屋。"))
        self.assertEqual(new.source_artifact_id, original.artifact_id)
        self.assertNotEqual(new.artifact_id, original.artifact_id)
        self.assertEqual(self.storage.get_artifact_path(original.artifact_id).read_bytes(), old_bytes)
        with self.assertRaisesRegex(ValueError, "类型必须一致"):
            self.task("screenplay", original.artifact_id)
        other_project = self.storage.create_project("其他项目")
        with self.assertRaisesRegex(ValueError, "同一项目"):
            self.storage.create_task(other_project.project_id, "script", "修订", original.artifact_id)
        other = self.output()
        with self.assertRaisesRegex(ValueError, "base_artifact_id"):
            self.output(revision, source_artifact_id=other.artifact_id)
        with self.assertRaisesRegex(ValueError, "base_artifact_id"):
            self.output(source_artifact_id=original.artifact_id)

    def test_image_and_text_operations_cannot_cross(self):
        text = self.output()
        picture_task = self.task("generate")
        picture = self.storage.register_output_image(self.pid, picture_task.task_id, self.image())
        with self.assertRaisesRegex(ValueError, "图片任务只能引用"):
            self.task("edit", text.artifact_id)
        with self.assertRaisesRegex(ValueError, "TXT"):
            self.task(base=picture.artifact_id)
        with self.assertRaises(ValueError):
            self.storage.register_output_image(self.pid, picture_task.task_id, self.image(), text.artifact_id)
        with self.assertRaises(ValueError):
            self.storage.register_output_text(self.pid, picture_task.task_id, self.text())
        script = self.task()
        with self.assertRaises(ValueError):
            self.storage.add_input_image(self.pid, script.task_id, self.image())
        with self.assertRaises(ValueError):
            self.storage.register_output_image(self.pid, script.task_id, self.image())
        with self.assertRaises(ValueError):
            self.storage.add_input_image(self.pid, picture_task.task_id, self.text())

    def test_invalid_text_never_commits(self):
        invalid = (b"\xff", "", " \r\n\t", "场景\x00正文", "场景\x1b[0m", "\ufeff \n",
                   '{"body": "正文"}', '["正文"]', "```text\n正文\n```", "~~~\n正文\n~~~",
                   "<html><body>正文</body></html>", "<think>模型推理</think>正文")
        task = self.task()
        for content in invalid:
            with self.subTest(content=repr(content)), self.assertRaises(ValueError):
                self.storage.register_output_text(self.pid, task.task_id, self.text(content))
        self.assertEqual(self.storage.list_task_artifacts(self.pid, task.task_id), [])
        self.assertEqual(list((self.storage._task_dir(self.pid, task.task_id) / "outputs").iterdir()), [])

    def test_text_limit_is_independent_from_image_limit(self):
        self.assertEqual(self.storage.config.max_text_bytes, 1024 * 1024)
        for invalid in (0, -1, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                StorageConfig(self.root / "invalid", max_text_bytes=invalid)
        task = self.task()
        small_images = StorageManager(StorageConfig(self.storage.data_root, max_image_bytes=2, max_text_bytes=64))
        output = small_images.register_output_text(self.pid, task.task_id, self.text("正文"))
        self.assertEqual(output.size_bytes, 6)
        small_text = StorageManager(StorageConfig(self.storage.data_root, max_text_bytes=5))
        with self.assertRaisesRegex(ValueError, "max_text_bytes"):
            small_text.register_output_text(self.pid, task.task_id, self.text("正文"))
        image_task = self.task("generate")
        self.assertEqual(small_text.register_output_image(self.pid, image_task.task_id, self.image()).format, "PNG")

    def test_explicit_artifact_ids_and_unregistered_files_are_never_overwritten(self):
        task = self.task()
        aid = uuid4().hex
        original = self.output(task, artifact_id=aid)
        with self.assertRaises(FileExistsError):
            self.output(task, artifact_id=aid)
        residual_id = uuid4().hex
        residual = self.storage._task_dir(self.pid, task.task_id) / "outputs" / (residual_id + ".txt")
        residual.write_bytes(b"preserve orphan")
        with self.assertRaises(FileExistsError):
            self.output(task, artifact_id=residual_id)
        self.assertEqual(residual.read_bytes(), b"preserve orphan")
        self.assertEqual(self.storage.list_task_artifacts(self.pid, task.task_id), [original])

    def test_failed_manifest_commit_preserves_source_and_prior_artifacts(self):
        task = self.task()
        original = self.output(task)
        source = self.text("新正文")
        before = source.read_bytes()
        with patch.object(self.storage.files, "write_json", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                self.storage.register_output_text(self.pid, task.task_id, source)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.storage.list_task_artifacts(self.pid, task.task_id), [original])
        self.assertEqual(len(list((self.storage._task_dir(self.pid, task.task_id) / "outputs").iterdir())), 1)

    def test_concurrent_managers_do_not_lose_artifacts(self):
        task = self.task()
        source = self.text()
        def save(_):
            manager = StorageManager(self.storage.config)
            return manager.register_output_text(self.pid, task.task_id, source)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(4)))
        self.assertEqual({item.artifact_id for item in self.storage.list_task_artifacts(self.pid, task.task_id)},
                         {item.artifact_id for item in results})

    def test_tampered_text_metadata_rejects_dimensions_and_image_task(self):
        task = self.task()
        self.output(task)
        path = self.storage._task_dir(self.pid, task.task_id) / "artifacts.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        for invalid in (1, False, None):
            value["artifacts"][0]["width"] = invalid
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.subTest(width=invalid), self.assertRaises(MetadataError):
                self.storage.list_task_artifacts(self.pid, task.task_id)
        value["artifacts"][0]["width"] = 0
        path.write_text(json.dumps(value), encoding="utf-8")
        task_path = self.storage._task_dir(self.pid, task.task_id) / "task.json"
        metadata = json.loads(task_path.read_text(encoding="utf-8"))
        metadata["operation"] = "generate"
        task_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(MetadataError, "任务类型"):
            self.storage.list_task_artifacts(self.pid, task.task_id)

    def test_scoped_storage_checks_namespace_before_text_registration(self):
        task = self.task()
        class Scope:
            def require_namespace(inner, project_id):
                if project_id != self.pid:
                    raise FileNotFoundError("项目不存在")
        scoped = WorkspaceStorage(self.storage, Scope())
        output = scoped.register_output_text(self.pid, task.task_id, self.text())
        self.assertEqual(scoped.get_artifact(output.artifact_id), output)
        with self.assertRaises(FileNotFoundError):
            scoped.register_output_text(uuid4().hex, task.task_id, self.text())


if __name__ == "__main__":
    unittest.main()
