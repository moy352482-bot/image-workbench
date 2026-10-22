import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image

from image_workbench import BackendError, LocalFileBackend, StorageConfig, StorageManager, TaskRunner
from image_workbench.performance import PerformanceTrace


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.project = self.storage.create_project("performance test")
        self.task = self.storage.create_task(self.project.project_id, "generate", "private prompt excluded from telemetry")
        self.source = self.root / "fixture.png"
        Image.new("RGB", (12, 8), "blue").save(self.source)
        self.runner = TaskRunner(self.storage)

    def read(self, record):
        return json.loads((self.storage.data_root / "logs" / "performance" /
                           f"{record.task_id}--{record.execution_id}.json").read_text(encoding="utf-8"))

    def test_success_has_creation_to_completion_and_does_not_mix_idle_into_runner(self):
        time.sleep(0.02)
        record = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([self.source]))
        data = self.read(record)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["output_count"], 1)
        self.assertEqual(data["timestamps"]["task_created_time"], self.task.created_at)
        seconds = data["durations_seconds"]
        self.assertGreater(seconds["queue_or_idle"], 0.015)
        self.assertAlmostEqual(seconds["total"], seconds["queue_or_idle"] + seconds["runner"], delta=0.01)
        self.assertIsNone(seconds["codex_execution"])
        self.assertIsNone(seconds["codex_launch"])
        self.assertGreater(seconds["artifact_check"], 0)
        self.assertGreater(seconds["register"], 0)
        self.assertNotIn(self.task.prompt, json.dumps(data))

    def test_failure_keeps_partial_durations_without_automatic_retry(self):
        class Failure:
            name = "codex"
            calls = 0

            def generate(self, request):
                self.calls += 1
                request.performance.mark("codex_launch_start")
                request.performance.mark("subprocess_spawn_end")
                request.performance.mark("codex_execution_end")
                raise BackendError("synthetic failure", failure_type="codex_execution_failed")

        backend = Failure()
        record = self.runner.run(self.project.project_id, self.task.task_id, backend)
        data = self.read(record)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["failure_type"], "codex_execution_failed")
        self.assertIsNotNone(data["durations_seconds"]["codex_combined"])
        self.assertIsNone(data["durations_seconds"]["codex_launch"])
        self.assertIsNone(data["durations_seconds"]["artifact_check"])

    def test_idempotent_reread_preserves_one_record(self):
        key = "a" * 32
        first = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([self.source]), idempotency_key=key)
        before = self.read(first)
        second = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([]), idempotency_key=key)
        self.assertEqual(self.read(second), before)
        self.assertEqual(len(list((self.storage.data_root / "logs" / "performance").glob("*.json"))), 1)

    def test_telemetry_write_failure_does_not_fail_successful_generation(self):
        original = self.storage.files.write_json

        def write(path, payload):
            if path.parent.name == "performance":
                raise OSError("synthetic telemetry write failure")
            return original(path, payload)

        with mock.patch.object(self.storage.files, "write_json", side_effect=write), self.assertLogs("image_workbench.performance", level="WARNING"):
            record = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([self.source]))
        self.assertEqual(record.status, "succeeded")
        self.assertTrue(self.storage.get_artifact_path(record.output_artifact_ids[0]).is_file())

    def test_live_boundary_splits_combined_and_redacts_payload(self):
        record = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([self.source]))
        trace = PerformanceTrace()
        trace.mark("runner_prepare_end")
        trace.mark("codex_launch_start")
        trace.mark("subprocess_spawn_end")
        time.sleep(0.005)
        trace.observe_cli_event({"type": "turn.started", "text": "secret", "arguments": "password"})
        time.sleep(0.005)
        trace.mark("codex_execution_end")
        data = trace.build(self.task, record)
        values = data["durations_seconds"]
        self.assertAlmostEqual(values["codex_combined"], values["codex_launch"] + values["codex_execution"], places=8)
        self.assertGreater(values["codex_launch"], 0)
        self.assertGreater(values["codex_execution"], 0)
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("password", json.dumps(data))
        self.assertIsNone(data["measurements"]["imagegen_tool_seconds"])

    def test_post_exit_turn_does_not_create_false_execution_split(self):
        record = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([self.source]))
        trace = PerformanceTrace()
        trace.mark("runner_prepare_end")
        trace.mark("codex_launch_start")
        trace.mark("codex_execution_end")
        trace.observe_cli_event({"type": "turn.started", "observation_phase": "post_exit"})
        data = trace.build(self.task, record)
        self.assertIsNone(data["durations_seconds"]["codex_execution"])
        self.assertEqual(data["measurements"]["codex_boundary"], "unavailable")

    def test_two_attempts_of_one_task_do_not_overwrite_performance(self):
        first = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([self.source]))
        before = self.read(first)
        second = self.runner.run(self.project.project_id, self.task.task_id, LocalFileBackend([self.source]))
        self.assertNotEqual(first.execution_id, second.execution_id)
        self.assertEqual(self.read(first), before)
        self.assertEqual(len(list((self.storage.data_root / "logs" / "performance").glob("*.json"))), 2)

    def test_prepare_failure_does_not_double_count_final_state_save(self):
        artifact = self.storage.add_input_image(self.project.project_id, self.task.task_id, self.source)
        self.storage.get_artifact_path(artifact.artifact_id).write_bytes(b"changed after registration")
        backend = mock.Mock(name="must_not_generate")
        backend.name = "codex"
        record = self.runner.run(self.project.project_id, self.task.task_id, backend)
        backend.generate.assert_not_called()
        data = self.read(record)
        self.assertEqual(data["failure_type"], "prepare_failed")
        values = data["durations_seconds"]
        self.assertAlmostEqual(values["runner"], values["prepare"] + values["register"] + values["other"], places=7)
        self.assertIsNone(values["codex_combined"])
        self.assertLessEqual(data["timestamps"]["prepare_end"], data["timestamps"]["register_start"])


if __name__ == "__main__":
    unittest.main()
