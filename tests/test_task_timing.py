"""执行时间展示契约；不调用图片后端，也不启动 Worker。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from image_workbench import StorageConfig, StorageManager
from image_workbench.task_runner import ExecutionRecord
from image_workbench.workbench_state import WorkbenchState


class TaskTimingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.state = WorkbenchState(StorageManager(StorageConfig(data_root=Path(temporary.name) / "data")), backend="local")
        self.project = self.state.create_project("计时测试", "a" * 32)
        self.task = self.state.create_task(self.project.project_id, request_id="b" * 32, operation="generate", prompt="不会执行")

    def record(self, **changes):
        values = dict(execution_id="c" * 32, project_id=self.project.project_id, task_id=self.task.task_id,
                      backend="codex", status="succeeded", created_at="2026-10-12T12:00:00+00:00",
                      updated_at="2026-10-12T12:02:00+00:00", total_seconds=12.75, backend_seconds=10.5)
        values.update(changes)
        return ExecutionRecord(**values)

    def job(self, **changes):
        values = dict(job_id="d" * 32, status="completed", action="generate", execution_id="c" * 32,
                      created_at="2026-10-11T00:00:00+00:00", started_at="2026-10-12T12:00:00+00:00",
                      finished_at="2026-10-12T12:02:00+00:00")
        values.update(changes)
        return values

    def view(self, records, job, *, detail=True, worker=False):
        with mock.patch.object(self.state.runner, "list_executions", return_value=records), \
                mock.patch.object(self.state, "get_job", return_value=job), \
                mock.patch.object(self.state, "worker_running", return_value=worker):
            return self.state.task_view(self.project.project_id, self.task.task_id, detail=detail)

    def test_completed_summary_and_detail_keep_measured_execution_duration(self):
        record, job = self.record(), self.job()
        for detail in (True, False):
            with self.subTest(detail=detail):
                result = self.view([record], job, detail=detail)
                self.assertEqual(result["status"], "awaiting_review")
                self.assertEqual(result["execution_seconds"], 12.75)
                self.assertEqual(result["elapsed_seconds"], 12.75)
                self.assertEqual(result["execution_timing_source"], "execution_record")
                if detail:
                    self.assertEqual(result["timings"]["backend_seconds"], 10.5)

    def test_failed_job_without_execution_record_uses_start_and_finish_not_submission(self):
        for detail in (True, False):
            with self.subTest(detail=detail):
                result = self.view([], self.job(status="failed", execution_id=None, failure_type="prepare_failed"), detail=detail)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["execution_seconds"], 120)
                self.assertEqual(result["execution_timing_source"], "job_clock")

    def test_remote_total_includes_execution_and_return_instead_of_archive_only(self):
        for detail in (True, False):
            with self.subTest(detail=detail):
                result = self.view([self.record(backend="remote-codex", total_seconds=0.2)], self.job(), detail=detail)
                self.assertEqual(result["execution_seconds"], 120)
                if detail:
                    self.assertEqual(result["timings"]["total_seconds"], 0.2)
                self.assertEqual(result["execution_timing_source"], "job_clock")

    def test_queued_retry_does_not_reuse_previous_execution_duration(self):
        result = self.view([self.record(status="execution_failed", total_seconds=99)],
                           self.job(status="queued", execution_id=None, started_at=None, finished_at=None))
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(result["execution_seconds"])
        self.assertIsNone(result["elapsed_seconds"])

    def test_running_retry_uses_current_start_and_uncertain_duration_is_unknown(self):
        start = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        record = self.record(status="execution_failed", total_seconds=99)
        job = self.job(status="running", execution_id=None, started_at=start, finished_at=None)
        running = self.view([record], job, worker=True)
        self.assertEqual(running["status"], "generating")
        self.assertGreaterEqual(running["execution_seconds"], 5)
        self.assertLess(running["execution_seconds"], 15)
        self.assertEqual(running["execution_timing_source"], "running")
        uncertain = self.view([record], job, worker=False)
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertIsNone(uncertain["execution_seconds"])

    def test_restore_reports_this_job_instead_of_cumulative_execution_record(self):
        result = self.view([self.record(total_seconds=250)], self.job(action="restore"))
        self.assertEqual(result["execution_seconds"], 120)
        self.assertEqual(result["execution_timing_source"], "job_clock")

    def test_absent_or_invalid_timestamps_do_not_fabricate_duration(self):
        for start, finish in ((None, None), ("bad", "2026-10-12T12:02:00+00:00"),
                              ("2026-10-12T12:03:00+00:00", "2026-10-12T12:02:00+00:00")):
            with self.subTest(start=start, finish=finish):
                result = self.view([], self.job(status="failed", execution_id=None, started_at=start, finished_at=finish))
                self.assertIsNone(result["execution_seconds"])


if __name__ == "__main__":
    unittest.main()
