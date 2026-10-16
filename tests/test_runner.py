import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from PIL import Image

from image_workbench import (
    BackendError, BackendResult, LocalFileBackend, StorageConfig, StorageManager,
    TaskRunner,
)


TEST_DIR = Path(__file__).resolve().parent


class RecordingBackend:
    name = "recording"

    def __init__(self, *, paths=(), error=None, returncode=0):
        self.paths = paths
        self.error = error
        self.returncode = returncode
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        paths = self.paths(request) if callable(self.paths) else self.paths
        return BackendResult(tuple(Path(path) for path in paths), self.returncode)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.runner = TaskRunner(self.storage)
        self.project = self.storage.create_project("运行器中文项目")
        self.task = self.storage.create_task(self.project.project_id, "generate", "生成森林边的小屋")

    def image(self, name="source.png", color=(23, 55, 99), size=(10, 8)):
        path = self.root / name
        Image.new("RGB", size, color).save(path, "PNG")
        return path

    def products(self, request, count=1):
        folder = request.work_dir / "products"
        folder.mkdir(parents=True, exist_ok=True)
        result = []
        for i in range(count):
            path = folder / f"candidate-{i}.png"
            Image.new("RGB", (8 + i, 6), (i * 40, 90, 120)).save(path, "PNG")
            result.append(path)
        return tuple(result)

    def run_record(self, backend):
        return self.runner.run(self.project.project_id, self.task.task_id, backend)

    def execution_dir(self, record):
        return self.root / "data" / "projects" / record.project_id / "tasks" / record.task_id / "logs" / "runs" / record.execution_id

    def test_local_backend_complete_generation_persists_logs_candidates_and_artifact(self):
        reference_path = self.image("reference.png")
        reference_bytes = reference_path.read_bytes()
        self.storage.add_input_image(self.project.project_id, self.task.task_id, reference_path)
        runner = TaskRunner(self.storage)
        record = runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([reference_path]))
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(len(record.output_artifact_ids), 1)
        self.assertEqual(len(record.candidates), 1)
        artifact = self.storage.get_artifact(record.output_artifact_ids[0])
        self.assertEqual(artifact.kind, "output")
        self.assertEqual(artifact.source_artifact_id, None)
        self.assertNotEqual(self.storage.get_artifact_path(artifact.artifact_id), reference_path)
        self.assertEqual(reference_path.read_bytes(), reference_bytes)
        run_dir = self.execution_dir(record)
        self.assertTrue((run_dir / "stdout.log").exists())
        self.assertTrue((run_dir / "stderr.log").exists())
        self.assertTrue((run_dir / "execution.json").exists())
        snapshots = list((run_dir / "work" / "references").iterdir())
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_bytes(), reference_bytes)
        self.assertNotEqual(snapshots[0], self.storage.get_artifact_path(self.storage.list_task_artifacts(self.project.project_id, self.task.task_id)[0].artifact_id))
        request = json.loads((run_dir / "work" / "request.json").read_text(encoding="utf-8"))
        self.assertIn(self.task.prompt, json.dumps(request, ensure_ascii=False))

    def test_edit_uses_base_snapshot_and_archived_version_source_without_mutating_old_output(self):
        old_source = self.image("old.png", (200, 30, 50))
        old = self.storage.register_output_image(self.project.project_id, self.task.task_id, old_source)
        old_bytes = self.storage.get_artifact_path(old.artifact_id).read_bytes()
        edit = self.storage.create_task(self.project.project_id, "edit", "改成傍晚", old.artifact_id)
        backend = RecordingBackend(paths=self.products)
        record = self.runner.run(self.project.project_id, edit.task_id, backend)
        self.assertEqual(record.status, "succeeded")
        request = backend.calls[0]
        self.assertIsNotNone(request.edit_image)
        self.assertEqual(request.edit_image.read_bytes(), old_bytes)
        self.assertNotEqual(request.edit_image, self.storage.get_artifact_path(old.artifact_id))
        new = self.storage.get_artifact(record.output_artifact_ids[0])
        self.assertEqual(new.source_artifact_id, old.artifact_id)
        self.assertEqual(self.storage.get_artifact_path(old.artifact_id).read_bytes(), old_bytes)

    def test_running_work_directories_are_unique_and_old_inputs_outputs_are_preserved(self):
        input_path = self.image("stable-input.png")
        original = input_path.read_bytes()
        input_artifact = self.storage.add_input_image(self.project.project_id, self.task.task_id, input_path)
        backend1 = RecordingBackend(paths=self.products)
        backend2 = RecordingBackend(paths=self.products)
        one = self.run_record(backend1)
        two = self.run_record(backend2)
        self.assertNotEqual(one.execution_id, two.execution_id)
        self.assertNotEqual(self.execution_dir(one), self.execution_dir(two))
        self.assertEqual(self.storage.get_artifact_path(input_artifact.artifact_id).read_bytes(), original)
        self.assertEqual(self.storage.get_artifact_path(one.output_artifact_ids[0]).exists(), True)
        self.assertEqual(self.storage.get_artifact_path(two.output_artifact_ids[0]).exists(), True)
        self.assertEqual(len(self.runner.list_executions(self.project.project_id, self.task.task_id)), 2)

    def test_backend_errors_are_persisted_with_classified_status_and_logs(self):
        cases = (
            (BackendError("exit failure", status="execution_failed", returncode=9), "execution_failed"),
            (BackendError("time limit", status="timed_out"), "timed_out"),
        )
        for error, expected in cases:
            with self.subTest(status=expected):
                record = self.run_record(RecordingBackend(error=error))
                self.assertEqual(record.status, expected)
                self.assertTrue(record.error)
                run_dir = self.execution_dir(record)
                self.assertTrue((run_dir / "stdout.log").exists())
                self.assertTrue((run_dir / "stderr.log").exists())
                self.assertEqual(record.output_artifact_ids, [])

        nonzero = self.run_record(RecordingBackend(paths=self.products, returncode=12))
        self.assertEqual(nonzero.status, "execution_failed")
        self.assertEqual(nonzero.returncode, 12)

    def test_missing_invalid_and_out_of_work_products_have_distinct_statuses(self):
        empty = self.run_record(RecordingBackend(paths=()))
        self.assertEqual(empty.status, "missing_artifact")

        def missing(request):
            return (request.work_dir / "products" / "gone.png",)

        missing_record = self.run_record(RecordingBackend(paths=missing))
        self.assertEqual(missing_record.status, "missing_artifact")

        def corrupt(request):
            path = request.work_dir / "products" / "bad.png"
            path.write_bytes(b"not a real image")
            return (path,)

        invalid = self.run_record(RecordingBackend(paths=corrupt))
        self.assertEqual(invalid.status, "invalid_artifact")

        outside = self.image("outside.png")
        escaped = self.run_record(RecordingBackend(paths=(outside,)))
        self.assertEqual(escaped.status, "invalid_artifact")

    def test_archive_failure_keeps_logs_candidates_and_retry_never_calls_backend(self):
        backend = RecordingBackend(paths=lambda request: self.products(request, 2))
        original_register = self.storage.register_output_image
        attempts = {"count": 0}

        def fail_second_once(*args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 2:
                raise OSError("injected archive failure")
            return original_register(*args, **kwargs)

        with mock.patch.object(self.storage, "register_output_image", side_effect=fail_second_once):
            failed = self.run_record(backend)
        self.assertEqual(failed.status, "archive_failed")
        self.assertEqual(len(failed.output_artifact_ids), 1)
        self.assertTrue((self.execution_dir(failed) / "stderr.log").exists())
        backend_calls = len(backend.calls)
        restored = self.runner.retry_archive(self.project.project_id, self.task.task_id, failed.execution_id)
        self.assertEqual(restored.status, "succeeded")
        self.assertEqual(len(backend.calls), backend_calls)
        self.assertEqual(len(restored.output_artifact_ids), 2)
        self.assertEqual(len({item.artifact_id for item in self.storage.list_task_artifacts(self.project.project_id, self.task.task_id) if item.kind == "output"}), 2)

    def test_retry_recovers_registration_succeeded_but_execution_record_write_failed(self):
        backend = RecordingBackend(paths=self.products)
        original_register = self.storage.register_output_image
        registered = {"done": False}

        def observe_register(*args, **kwargs):
            artifact = original_register(*args, **kwargs)
            registered["done"] = True
            return artifact

        original_save = self.runner._save
        failed_once = {"done": False}

        def fail_after_registration(record):
            if registered["done"] and not failed_once["done"]:
                failed_once["done"] = True
                raise OSError("execution JSON write failed after registration")
            return original_save(record)

        with mock.patch.object(self.storage, "register_output_image", side_effect=observe_register), \
             mock.patch.object(self.runner, "_save", side_effect=fail_after_registration):
            record = self.run_record(backend)
        self.assertEqual(record.status, "archive_failed")
        self.assertEqual(len(self.storage.list_task_artifacts(self.project.project_id, self.task.task_id)), 1)
        resumed = self.runner.retry_archive(self.project.project_id, self.task.task_id, record.execution_id)
        self.assertEqual(resumed.status, "succeeded")
        self.assertEqual(len(backend.calls), 1)
        outputs = [item for item in self.storage.list_task_artifacts(self.project.project_id, self.task.task_id) if item.kind == "output"]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].artifact_id, resumed.output_artifact_ids[0])

    def test_retry_rejects_candidate_content_changed_after_failed_archive(self):
        backend = RecordingBackend(paths=self.products)
        with mock.patch.object(self.storage, "register_output_image", side_effect=OSError("temporary registration failure")):
            record = self.run_record(backend)
        self.assertEqual(record.status, "archive_failed")
        candidate = self.root / "data" / record.candidates[0].relative_path
        candidate.write_bytes(self.image("changed-after-run.png", color=(255, 0, 0)).read_bytes())
        retried = self.runner.retry_archive(self.project.project_id, self.task.task_id, record.execution_id)
        self.assertEqual(retried.status, "invalid_artifact")
        self.assertEqual(retried.output_artifact_ids, [])

    def test_execution_records_and_archived_images_survive_data_root_move(self):
        record = self.run_record(RecordingBackend(paths=self.products))
        expected_image = self.storage.get_artifact_path(record.output_artifact_ids[0]).read_bytes()
        moved = self.root / "moved-data"
        shutil.move(self.root / "data", moved)
        relocated_storage = StorageManager(StorageConfig(data_root=moved))
        relocated_runner = TaskRunner(relocated_storage)
        loaded = relocated_runner.get_execution(self.project.project_id, self.task.task_id, record.execution_id)
        self.assertEqual(loaded, record)
        path = relocated_storage.get_artifact_path(record.output_artifact_ids[0])
        self.assertTrue(path.is_relative_to(moved))
        self.assertEqual(path.read_bytes(), expected_image)

    def test_mark_interrupted_persists_state_and_unknown_execution_is_missing(self):
        marked = []

        class InterruptDuringRun:
            name = "interrupt-test"

            def generate(inner_self, request):
                marked.append(self.runner.mark_interrupted(self.project.project_id, self.task.task_id, request.execution_id))
                raise KeyboardInterrupt

        interrupted = self.run_record(InterruptDuringRun())
        self.assertEqual(marked[0].status, "interrupted")
        self.assertEqual(interrupted.status, "interrupted")
        self.assertEqual(self.runner.get_execution(self.project.project_id, self.task.task_id), interrupted)
        with self.assertRaises(FileNotFoundError):
            self.runner.get_execution(self.project.project_id, self.task.task_id, "0" * 32)


if __name__ == "__main__":
    unittest.main()
