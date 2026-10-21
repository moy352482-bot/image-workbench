import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

from image_workbench import MetadataError, StorageConfig, StorageManager
import image_workbench.storage as storage_module


TEST_DIR = Path(__file__).resolve().parent


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.data = self.work / "isolated-data"
        self.manager = StorageManager(StorageConfig(data_root=self.data))

    def image(self, name="source.png", fmt="PNG", size=(8, 6), color=(30, 80, 140)):
        path = self.work / name
        Image.new("RGB", size, color).save(path, format=fmt)
        return path

    def project_task(self, operation="generate", prompt="中文提示词", base=None):
        project = self.manager.create_project("中文项目：春日花园")
        task = self.manager.create_task(project.project_id, operation, prompt, base)
        return project, task

    def manifest(self, project_id, task_id):
        return self.data / "projects" / project_id / "tasks" / task_id / "artifacts.json"

    def test_config_resolution_precedence_and_limits(self):
        with mock.patch.dict(os.environ, {"IMAGE_WORKBENCH_DATA_ROOT": "env-store"}, clear=False):
            env_config = StorageConfig()
            explicit = StorageConfig(data_root="chosen-store")
        self.assertEqual(env_config.data_root, (Path.cwd() / "env-store").resolve())
        self.assertEqual(explicit.data_root, (Path.cwd() / "chosen-store").resolve())
        for bad in (0, -1, True, 1.5):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                StorageConfig(data_root=self.data, max_image_bytes=bad)
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                StorageConfig(data_root=self.data, max_image_pixels=bad)
        with mock.patch.dict(os.environ, {}, clear=True):
            default = StorageConfig(data_root=None)
        self.assertEqual(default.data_root, (Path(storage_module.__file__).resolve().parent.parent / "data").resolve())

    def test_unsupported_image_decoder_is_never_entered_for_disguised_upload(self):
        from PIL import TiffImagePlugin
        project, task = self.project_task()
        disguised = self.image("disguised.png", "TIFF")
        with mock.patch.object(TiffImagePlugin.TiffImageFile, "_open", side_effect=RuntimeError("unsupported decoder entered")) as decoder:
            with self.assertRaises(ValueError):
                self.manager.add_input_image(project.project_id, task.task_id, disguised)
            decoder.assert_not_called()
        self.assertEqual(self.manager.list_task_artifacts(project.project_id, task.task_id), [])

    def test_chinese_project_references_output_edit_versions_and_root_migration(self):
        project, task = self.project_task()
        self.assertEqual(self.manager.get_project(project.project_id).name, "中文项目：春日花园")
        inputs = []
        for name, fmt, suffix in (("参考甲", "PNG", ".png"), ("参考乙", "JPEG", ".jpg"), ("参考丙", "WEBP", ".webp")):
            source = self.image(name + ".bin", fmt)
            original = source.read_bytes()
            artifact = self.manager.add_input_image(project.project_id, task.task_id, source)
            self.assertEqual(artifact.format, fmt)
            self.assertTrue(artifact.relative_path.endswith(suffix))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(hashlib.sha256(self.manager.get_artifact_path(artifact.artifact_id).read_bytes()).hexdigest(), artifact.sha256)
            self.assertEqual(self.manager.get_artifact(artifact.artifact_id), artifact)
            inputs.append(artifact)
        same_again = self.manager.add_input_image(project.project_id, task.task_id, self.work / "参考甲.bin")
        self.assertNotEqual(same_again.artifact_id, inputs[0].artifact_id)
        self.assertEqual(same_again.sha256, inputs[0].sha256)
        self.assertEqual(self.manager.get_artifact_path(inputs[0].artifact_id).read_bytes(), self.manager.get_artifact_path(same_again.artifact_id).read_bytes())
        output_source = self.image("result.webp", "WEBP", (10, 9), (220, 40, 70))
        output_bytes = output_source.read_bytes()
        first = self.manager.register_output_image(project.project_id, task.task_id, output_source)
        self.assertEqual(first.kind, "output")
        self.assertIsNone(first.role)
        self.assertIsNone(first.source_artifact_id)
        self.assertEqual(output_source.read_bytes(), output_bytes)

        edit = self.manager.create_task(project.project_id, "edit", "把花园改成黄昏", first.artifact_id)
        edit_src = self.image("edit-output.png")
        second = self.manager.register_output_image(project.project_id, edit.task_id, edit_src)
        self.assertEqual(second.source_artifact_id, first.artifact_id)
        self.assertEqual(self.manager.get_artifact_path(first.artifact_id).read_bytes(), output_bytes)
        self.assertEqual(self.manager.get_task(project.project_id, edit.task_id).base_artifact_id, first.artifact_id)
        self.assertEqual({a.artifact_id for a in self.manager.list_task_artifacts(project.project_id, edit.task_id)}, {second.artifact_id})

        moved = self.work / "relocated-data"
        shutil.move(self.data, moved)
        restarted = StorageManager(StorageConfig(data_root=moved))
        self.assertEqual(restarted.get_project(project.project_id).name, project.name)
        self.assertEqual(restarted.get_task(project.project_id, edit.task_id), edit)
        self.assertEqual(restarted.get_artifact(first.artifact_id), first)
        relocated_path = restarted.get_artifact_path(first.artifact_id)
        self.assertEqual(relocated_path.read_bytes(), output_bytes)
        self.assertTrue(relocated_path.is_relative_to(moved))
        self.assertFalse(self.data.exists())
        self.assertEqual(len(restarted.list_projects()), 1)

    def test_invalid_ids_windows_ads_and_task_relationships(self):
        project, task = self.project_task()
        for value in ("../escape", "A" * 32, "0" * 31, "0" * 32 + ":stream", "C:\\temp\\x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.get_project(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.get_artifact(value)
        with self.assertRaises(ValueError):
            self.manager.create_task(project.project_id, "generate", "x", "0" * 32)
        with self.assertRaises(ValueError):
            self.manager.create_task(project.project_id, "other", "x")
        with self.assertRaises(FileNotFoundError):
            self.manager.create_task(project.project_id, "edit", "x", "0" * 32)
        other = self.manager.create_project("另一项目")
        inp = self.manager.add_input_image(project.project_id, task.task_id, self.image())
        with self.assertRaises(ValueError):
            self.manager.create_task(other.project_id, "edit", "跨项目", inp.artifact_id)
        other_task = self.manager.create_task(other.project_id, "generate", "外部项目")
        other_input = self.manager.add_input_image(other.project_id, other_task.task_id, self.image("other.png"))
        with self.assertRaises(ValueError):
            self.manager.register_output_image(project.project_id, task.task_id, self.image("out.png"), other_input.artifact_id)
        second_input = self.manager.add_input_image(project.project_id, task.task_id, self.image("second.png"))
        edit_task = self.manager.create_task(project.project_id, "edit", "锁定来源", inp.artifact_id)
        with self.assertRaises(ValueError):
            self.manager.register_output_image(project.project_id, edit_task.task_id, self.image("wrong-source.png"), second_input.artifact_id)
        with self.assertRaises(ValueError):
            self.manager.get_task(project.project_id, "../" + task.task_id)

    def test_content_validation_limits_and_source_immutability(self):
        project, task = self.project_task()
        fake = self.work / "renamed.jpg"
        Image.new("RGB", (4, 4), "red").save(fake, "PNG")
        before = fake.read_bytes()
        artifact = self.manager.add_input_image(project.project_id, task.task_id, fake)
        self.assertEqual(artifact.format, "PNG")
        self.assertTrue(artifact.relative_path.endswith(".png"))
        self.assertEqual(fake.read_bytes(), before)

        gif = self.image("animated.gif", "GIF")
        truncated = self.image("valid.png")
        truncated.write_bytes(truncated.read_bytes()[:-12])
        broken = self.work / "broken.png"
        broken.write_bytes(b"not an image")
        for source in (gif, truncated, broken):
            with self.subTest(source=source.name), self.assertRaises(ValueError):
                self.manager.add_input_image(project.project_id, task.task_id, source)

        large = self.image("large.png", "PNG", (9, 9))
        byte_limited = StorageManager(StorageConfig(self.work / "byte-limit", max_image_bytes=8))
        p1 = byte_limited.create_project("bytes")
        t1 = byte_limited.create_task(p1.project_id, "generate", "bytes")
        with self.assertRaises(ValueError):
            byte_limited.add_input_image(p1.project_id, t1.task_id, large)

        pixel_limited = StorageManager(StorageConfig(self.work / "pixel-limit", max_image_pixels=80))
        p2 = pixel_limited.create_project("pixels")
        t2 = pixel_limited.create_task(p2.project_id, "generate", "pixels")
        with self.assertRaises(ValueError):
            pixel_limited.add_input_image(p2.project_id, t2.task_id, large)

        frames = [Image.new("RGB", (5, 5), color) for color in ("red", "green", "blue", "white")]
        animated = self.work / "many-frames.webp"
        frames[0].save(animated, "WEBP", save_all=True, append_images=frames[1:], duration=40, loop=0)
        with Image.open(animated) as picture:
            self.assertEqual(picture.n_frames, 4)
        with self.assertRaises(ValueError):
            pixel_limited.add_input_image(p2.project_id, t2.task_id, animated)

    def test_metadata_tampering_and_corrupt_json_are_not_ignored(self):
        project, task = self.project_task()
        item = self.manager.add_input_image(project.project_id, task.task_id, self.image())
        self.assertEqual(self.manager.get_artifact(item.artifact_id), item)  # Warm the index before external edits.
        path = self.manifest(project.project_id, task.task_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        original_path = payload["artifacts"][0]["relative_path"]
        for tampered in ("../../outside.png", "C:/outside.png", original_path + ":stream", "projects/" + project.project_id + "/tasks/" + task.task_id + "/outputs/" + item.artifact_id + ".png"):
            with self.subTest(relative_path=tampered):
                payload["artifacts"][0]["relative_path"] = tampered
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(MetadataError):
                    self.manager.get_artifact(item.artifact_id)
        payload["artifacts"][0]["relative_path"] = original_path
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.manager.get_artifact_path(item.artifact_id).unlink()
        with self.assertRaises(FileNotFoundError):
            self.manager.get_artifact_path(item.artifact_id)
        # Restore the image so later corruption behavior tests metadata only.
        source = self.image("restored.png")
        shutil.copyfile(source, self.data / Path(item.relative_path))

        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(MetadataError):
            self.manager.list_task_artifacts(project.project_id, task.task_id)

    def test_artifact_index_reuses_stat_checked_cache_and_discovers_new_records(self):
        project, task = self.project_task()
        first = self.manager.add_input_image(project.project_id, task.task_id, self.image("indexed-first.png"))
        self.assertEqual(self.manager.get_artifact(first.artifact_id), first)

        read_json = self.manager._read_json
        with mock.patch.object(self.manager, "_read_json", wraps=read_json) as reads:
            self.assertEqual(self.manager.get_artifact(first.artifact_id), first)
            self.assertEqual(reads.call_count, 0)

        second_task = self.manager.create_task(project.project_id, "generate", "cache invalidation after new task")
        second = self.manager.add_input_image(project.project_id, second_task.task_id, self.image("indexed-second.png"))
        self.assertEqual(self.manager.get_artifact(second.artifact_id), second)

    def test_artifact_index_detects_external_instance_writes_and_duplicate_ids(self):
        project, task = self.project_task()
        first = self.manager.add_input_image(project.project_id, task.task_id, self.image("external-first.png"))
        self.assertEqual(self.manager.get_artifact(first.artifact_id), first)

        other = StorageManager(StorageConfig(data_root=self.data))
        external_project = other.create_project("cross-instance write")
        external_task = other.create_task(external_project.project_id, "generate", "external artifact")
        external = other.add_input_image(external_project.project_id, external_task.task_id, self.image("external-second.png"))
        self.assertEqual(self.manager.get_artifact(external.artifact_id), external)

        duplicate_task = self.manager.create_task(project.project_id, "generate", "duplicate ID record")
        manifest = self.manifest(project.project_id, duplicate_task.task_id)
        duplicate = json.loads(json.dumps(json.loads(self.manifest(project.project_id, task.task_id).read_text(encoding="utf-8"))["artifacts"][0]))
        duplicate["task_id"] = duplicate_task.task_id
        duplicate["relative_path"] = f"projects/{project.project_id}/tasks/{duplicate_task.task_id}/inputs/{first.artifact_id}.png"
        manifest.write_text(json.dumps({"schema_version": 1, "artifacts": [duplicate]}), encoding="utf-8")
        with self.assertRaises(MetadataError):
            self.manager.get_artifact(first.artifact_id)

    def test_missing_resources_and_invalid_source_paths(self):
        project, task = self.project_task()
        with self.assertRaises(FileNotFoundError):
            self.manager.get_project("0" * 32)
        with self.assertRaises(FileNotFoundError):
            self.manager.add_input_image(project.project_id, task.task_id, self.work / "missing.png")
        for value in ("", 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.add_input_image(project.project_id, task.task_id, value)

    def test_copy_and_final_write_failures_leave_no_valid_fake_record(self):
        project, task = self.project_task()
        source = self.image()
        original = source.read_bytes()
        manifest = self.manifest(project.project_id, task.task_id)
        before_manifest = manifest.read_bytes()
        def fail_source_copy(source_path, target_path):
            target_path.write_bytes(source_path.read_bytes()[:12])
            raise OSError("injected copy failure")

        with mock.patch.object(self.manager, "_copy_source", side_effect=fail_source_copy):
            with self.assertRaisesRegex(OSError, "injected copy failure"):
                self.manager.add_input_image(project.project_id, task.task_id, source)
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(self.manager.list_task_artifacts(project.project_id, task.task_id)), [])

        def fail_final_copy(reader, writer, length=0):
            writer.write(reader.read(12))
            raise OSError("injected final write failure")

        with mock.patch.object(storage_module.shutil, "copyfileobj", side_effect=fail_final_copy):
            with self.assertRaisesRegex(OSError, "injected final write failure"):
                self.manager.add_input_image(project.project_id, task.task_id, source)
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(list(self.manager.list_task_artifacts(project.project_id, task.task_id)), [])
        input_dir = self.data / "projects" / project.project_id / "tasks" / task.task_id / "inputs"
        self.assertEqual(list(input_dir.iterdir()), [])
        self.assertEqual(source.read_bytes(), original)

    def test_manifest_atomic_replace_failure_preserves_existing_and_source(self):
        project, task = self.project_task()
        source1 = self.image("one.png", color=(1, 2, 3))
        existing = self.manager.add_input_image(project.project_id, task.task_id, source1)
        existing_bytes = self.manager.get_artifact_path(existing.artifact_id).read_bytes()
        manifest = self.manifest(project.project_id, task.task_id)
        manifest_before = manifest.read_bytes()
        source2 = self.image("two.png", color=(90, 100, 110))
        source2_before = source2.read_bytes()
        other_task = self.manager.create_task(project.project_id, "generate", "other task")
        other_source = self.image("other-task.png", color=(3, 3, 3))
        other_artifact = self.manager.add_input_image(project.project_id, other_task.task_id, other_source)
        other_artifact_bytes = self.manager.get_artifact_path(other_artifact.artifact_id).read_bytes()
        real_replace = os.replace

        def fail_artifact_manifest(src, dst):
            if Path(dst).name == "artifacts.json":
                raise OSError("injected atomic replace failure")
            return real_replace(src, dst)

        with mock.patch.object(storage_module.os, "replace", side_effect=fail_artifact_manifest):
            with self.assertRaisesRegex(OSError, "injected atomic replace failure"):
                self.manager.add_input_image(project.project_id, task.task_id, source2)
        self.assertEqual(manifest.read_bytes(), manifest_before)
        self.assertEqual(self.manager.get_artifact_path(existing.artifact_id).read_bytes(), existing_bytes)
        self.assertEqual(self.manager.get_artifact_path(other_artifact.artifact_id).read_bytes(), other_artifact_bytes)
        self.assertEqual([a.artifact_id for a in self.manager.list_task_artifacts(project.project_id, task.task_id)], [existing.artifact_id])
        self.assertEqual(source2.read_bytes(), source2_before)
        input_dir = self.data / "projects" / project.project_id / "tasks" / task.task_id / "inputs"
        self.assertEqual(sorted(p.name for p in input_dir.iterdir()), [existing.artifact_id + ".png"])

    def test_project_task_failure_rolls_back_and_uuid_collision_does_not_overwrite(self):
        with mock.patch.object(self.manager, "_write_json", side_effect=OSError("project write failure")):
            with self.assertRaisesRegex(OSError, "project write failure"):
                self.manager.create_project("失败项目")
        self.assertEqual(self.manager.list_projects(), [])
        self.assertEqual(list((self.data / "projects").iterdir()), [])

        project = self.manager.create_project("任务回滚")
        with mock.patch.object(self.manager, "_write_json", side_effect=OSError("task write failure")):
            with self.assertRaisesRegex(OSError, "task write failure"):
                self.manager.create_task(project.project_id, "generate", "失败任务")
        self.assertEqual(self.manager.list_tasks(project.project_id), [])
        task = self.manager.create_task(project.project_id, "generate", "稳定任务")
        artifact = self.manager.add_input_image(project.project_id, task.task_id, self.image())
        existing_path = self.manager.get_artifact_path(artifact.artifact_id)
        existing_bytes = existing_path.read_bytes()
        with mock.patch.object(storage_module, "uuid4", return_value=mock.Mock(hex=artifact.artifact_id)):
            with self.assertRaises(FileExistsError):
                self.manager.add_input_image(project.project_id, task.task_id, self.image("collision.png"))
        self.assertEqual(existing_path.read_bytes(), existing_bytes)
        self.assertEqual([a.artifact_id for a in self.manager.list_task_artifacts(project.project_id, task.task_id)], [artifact.artifact_id])

    def test_managed_symlink_is_rejected_when_platform_allows_creation(self):
        project, task = self.project_task()
        artifact = self.manager.add_input_image(project.project_id, task.task_id, self.image("symlink-target.png"))
        self.manager.get_artifact(artifact.artifact_id)  # Warm the index before replacing a managed path.
        external = self.work / "outside"
        external.mkdir()
        task_dir = self.data / "projects" / project.project_id / "tasks" / task.task_id
        inputs = task_dir / "inputs"
        saved = task_dir / "inputs-saved"
        inputs.rename(saved)
        link_created = False
        try:
            try:
                inputs.symlink_to(external, target_is_directory=True)
                link_created = True
            except (OSError, NotImplementedError) as exc:
                try:
                    import _winapi
                    create_junction = getattr(_winapi, "CreateJunction", None)
                    if create_junction is None:
                        self.skipTest(f"当前平台/权限不能创建目录符号链接，且无 junction API: {exc}")
                    create_junction(str(external), str(inputs))
                    link_created = True
                except (ImportError, OSError, NotImplementedError) as junction_exc:
                    self.skipTest(f"当前平台/权限不能创建目录符号链接或 junction: {junction_exc}")
            with self.assertRaises(ValueError):
                self.manager.get_artifact_path(artifact.artifact_id)
            with self.assertRaises(ValueError):
                self.manager.add_input_image(project.project_id, task.task_id, self.image())
        finally:
            if inputs.is_symlink():
                inputs.unlink()
            elif inputs.exists():
                inputs.rmdir()
            if saved.exists() and not inputs.exists():
                saved.rename(inputs)

    def test_example_cli_runs_end_to_end_in_temporary_data_root(self):
        reference = self.image("cli-reference.webp", "WEBP")
        first = self.image("cli-v1.jpg", "JPEG", (12, 10))
        second = self.image("cli-v2.png", "PNG", (12, 10), (180, 30, 60))
        data_root = self.work / "cli-data"
        script = Path(__file__).resolve().parents[1] / "example.py"
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(script), "--reference", str(reference), "--output-v1", str(first),
             "--output-v2", str(second), "--data-root", str(data_root)],
            cwd=script.parent, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=True,
        )
        self.assertIn("项目:", result.stdout)
        cli_storage = StorageManager(StorageConfig(data_root=data_root))
        projects = cli_storage.list_projects()
        self.assertEqual(len(projects), 1)
        tasks = cli_storage.list_tasks(projects[0].project_id)
        self.assertEqual({task.operation for task in tasks}, {"generate", "edit"})
        outputs = [artifact for task in tasks for artifact in cli_storage.list_task_artifacts(projects[0].project_id, task.task_id)
                   if artifact.kind == "output"]
        self.assertEqual(len(outputs), 2)
        linked = [item for item in outputs if item.source_artifact_id is not None]
        self.assertEqual(len(linked), 1)
        self.assertIn(cli_storage.get_artifact(linked[0].source_artifact_id), outputs)


if __name__ == "__main__":
    unittest.main()
