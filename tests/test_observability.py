from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

from image_workbench import (
    BackendError, BackendResult, CodexImageBackend, StorageConfig, StorageManager,
    TaskRunner,
)
from image_workbench.task_runner import StateUpdateError
from image_workbench.timing import utc_now


TEST_DIR = Path(__file__).resolve().parent


class Backend:
    name = "local-observability"

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        return self.behavior(request)


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.storage = StorageManager(StorageConfig(data_root=self.data))
        self.runner = TaskRunner(self.storage)
        self.project = self.storage.create_project("执行观测测试")
        self.task = self.storage.create_task(self.project.project_id, "generate", "生成测试图")

    def image(self, name="source.png", color=(24, 65, 102), size=(8, 7)):
        path = self.root / name
        Image.new("RGB", size, color).save(path, "PNG")
        return path

    def backend_image(self, request, *, name="candidate.png", color=(90, 110, 130)):
        path = request.work_dir / "products" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (9, 6), color).save(path, "PNG")
        return path

    def success_backend(self, *, name="candidate.png", timing_events=None):
        def generate(request):
            return BackendResult((self.backend_image(request, name=name),), timing_events=timing_events or [])
        return Backend(generate)

    def run_execution(self, backend=None, *, timeout_seconds=5):
        return self.runner.run(self.project.project_id, self.task.task_id, backend or self.success_backend(), timeout_seconds=timeout_seconds)

    def record_path(self, record):
        return self.runner.get_execution_directory(record.project_id, record.task_id, record.execution_id) / "execution.json"

    def test_success_timing_fields_are_nonnegative_complete_and_match_disk_record(self):
        record = self.run_execution()
        self.assertEqual(record.status, "succeeded")
        self.assertIsNone(record.failure_type)
        self.assertIsNone(record.codex_seconds)
        for field in ("prepare_seconds", "backend_seconds", "validate_seconds", "register_seconds", "total_seconds"):
            value = getattr(record, field)
            self.assertIsInstance(value, (int, float))
            self.assertGreaterEqual(value, 0)
            self.assertLess(value, 10)
        phases = {event["phase"] for event in record.timing_events}
        self.assertTrue({"prepare", "backend", "validate", "register", "total"} <= phases)
        self.assertNotIn("codex", phases)
        for event in record.timing_events:
            self.assertGreaterEqual(event["seconds"], 0)
            self.assertIsNotNone(event["ended_at"])
            self.assertTrue(event["started_at"].endswith("+00:00"))
            self.assertTrue(event["ended_at"].endswith("+00:00"))
        disk_record = json.loads(self.record_path(record).read_text(encoding="utf-8"))
        self.assertEqual({key: value for key, value in disk_record.items() if key != "schema_version"}, asdict(record))
        reread = self.runner.get_execution(record.project_id, record.task_id, record.execution_id)
        self.assertEqual(reread, record)

    def test_codex_phase_is_aggregated_from_backend_event_without_running_codex(self):
        start = utc_now()
        events = [{"phase": "codex", "started_at": start, "ended_at": utc_now(), "seconds": 0.125}]
        record = self.run_execution(self.success_backend(timing_events=events))
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.codex_seconds, 0.125)
        self.assertEqual([event["phase"] for event in record.timing_events].count("codex"), 1)

    def test_pre_observability_execution_json_loads_with_defaults(self):
        record = self.run_execution()
        path = self.record_path(record)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in ("failure_type", "timing_events", "prepare_seconds", "backend_seconds", "codex_seconds",
                    "validate_seconds", "register_seconds", "total_seconds"):
            payload.pop(key, None)
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = self.runner.get_execution(record.project_id, record.task_id, record.execution_id)
        self.assertEqual(loaded.failure_type, None)
        self.assertEqual(loaded.timing_events, [])
        self.assertEqual(loaded.prepare_seconds, 0.0)
        self.assertEqual(loaded.codex_seconds, None)

    def test_prepare_failure_unexpected_backend_failure_and_artifact_failure_types(self):
        source = self.image("registered-input.png")
        registered_bytes = source.read_bytes()
        input_record = self.storage.add_input_image(self.project.project_id, self.task.task_id, source)
        input_path = self.storage.get_artifact_path(input_record.artifact_id)
        Image.new("RGB", (8, 7), (200, 30, 10)).save(input_path, "PNG")
        should_not_run = self.success_backend()
        prepare = self.run_execution(should_not_run)
        self.assertEqual(prepare.status, "execution_failed")
        self.assertEqual(prepare.failure_type, "prepare_failed")
        self.assertEqual(should_not_run.calls, [])
        self.assertGreaterEqual(prepare.prepare_seconds, 0)
        self.assertEqual(prepare.backend_seconds, 0.0)
        input_path.write_bytes(registered_bytes)

        surprise = Backend(lambda _request: (_ for _ in ()).throw(RuntimeError("unexpected backend exception")))
        unexpected = self.run_execution(surprise)
        self.assertEqual(unexpected.status, "execution_failed")
        self.assertEqual(unexpected.failure_type, "unexpected_exception")
        self.assertIn("unexpected backend exception", unexpected.error)
        self.assertGreaterEqual(unexpected.backend_seconds, 0)

        missing = Backend(lambda request: BackendResult((request.work_dir / "products" / "missing.png",)))
        missing_record = self.run_execution(missing)
        self.assertEqual(missing_record.status, "missing_artifact")
        self.assertEqual(missing_record.failure_type, "artifact_not_found")

        def corrupt(request):
            path = request.work_dir / "products" / "corrupt.png"
            path.write_bytes(b"not a decodable image")
            return BackendResult((path,))

        invalid = self.run_execution(Backend(corrupt))
        self.assertEqual(invalid.status, "invalid_artifact")
        self.assertEqual(invalid.failure_type, "artifact_invalid")

    def test_register_failure_is_classified_and_has_stage_timing(self):
        backend = self.success_backend()
        with mock.patch.object(self.storage, "register_output_image", side_effect=OSError("disk full")):
            record = self.run_execution(backend)
        self.assertEqual(record.status, "archive_failed")
        self.assertEqual(record.failure_type, "register_failed")
        self.assertGreaterEqual(record.validate_seconds, 0)
        self.assertGreaterEqual(record.register_seconds, 0)
        self.assertTrue(record.candidates)
        self.assertEqual(record.output_artifact_ids, [])

    def test_state_write_failure_after_registration_recovers_without_duplicate_output(self):
        backend = self.success_backend()
        original_register = self.storage.register_output_image
        registered = {"done": False}

        def register_then_note(*args, **kwargs):
            artifact = original_register(*args, **kwargs)
            registered["done"] = True
            return artifact

        original_save = self.runner._save
        failed_once = {"done": False}

        def fail_one_execution_update(record):
            if registered["done"] and not failed_once["done"]:
                failed_once["done"] = True
                raise OSError("temporary execution.json write failure")
            return original_save(record)

        with mock.patch.object(self.storage, "register_output_image", side_effect=register_then_note), \
             mock.patch.object(self.runner, "_save", side_effect=fail_one_execution_update):
            failed = self.run_execution(backend)
        self.assertEqual(failed.status, "archive_failed")
        self.assertEqual(failed.failure_type, "state_update_failed")
        self.assertEqual(len(self.storage.list_task_artifacts(self.project.project_id, self.task.task_id)), 1)
        before_retry_total = failed.total_seconds
        resumed = self.runner.retry_archive(self.project.project_id, self.task.task_id, failed.execution_id)
        self.assertEqual(resumed.status, "succeeded")
        self.assertEqual(resumed.failure_type, None)
        self.assertGreater(resumed.total_seconds, before_retry_total)
        self.assertEqual(len(backend.calls), 1)
        outputs = [item for item in self.storage.list_task_artifacts(self.project.project_id, self.task.task_id) if item.kind == "output"]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].artifact_id, resumed.output_artifact_ids[0])

    def test_persistent_initial_state_write_failure_raises_record_for_batch_caller(self):
        backend = self.success_backend()
        with mock.patch.object(self.storage, "_write_json", side_effect=OSError("metadata disk unavailable")):
            with self.assertRaises(StateUpdateError) as raised:
                self.run_execution(backend)
        self.assertEqual(raised.exception.record.failure_type, "state_update_failed")
        self.assertEqual(raised.exception.record.status, "execution_failed")
        self.assertEqual(backend.calls, [])

    def test_codex_startup_failure_and_timeout_keep_specific_failure_classification(self):
        missing_executable = self.root / "missing-codex.exe"
        launch = self.run_execution(CodexImageBackend(executable=missing_executable, generated_root=self.root / "cache"))
        self.assertEqual(launch.status, "execution_failed")
        self.assertEqual(launch.failure_type, "codex_launch_failed")
        self.assertIsNone(launch.codex_seconds)

        executable = self.root / "codex.exe"
        executable.write_bytes(b"mocked CLI target")
        backend = CodexImageBackend(executable=executable, generated_root=self.root / "cache")

        class TimedOutProcess:
            pid = 987654321
            returncode = None
            stdin = None

            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired("fake codex", timeout)

            def poll(self):
                return None

        with mock.patch.object(backend, "_build_command", return_value=[str(executable), "exec"]), \
             mock.patch("image_workbench.codex_backend.subprocess.Popen", return_value=TimedOutProcess()) as popen, \
             mock.patch("image_workbench.codex_backend._stop_process", return_value="mock process stopped"):
            timed = self.run_execution(backend, timeout_seconds=0.01)
        self.assertEqual(timed.status, "timed_out")
        self.assertEqual(timed.failure_type, "codex_timeout")
        self.assertEqual(popen.call_count, 1)
        self.assertGreaterEqual(timed.codex_seconds, 0)
        self.assertIn("codex", {event["phase"] for event in timed.timing_events})


if __name__ == "__main__":
    unittest.main()
