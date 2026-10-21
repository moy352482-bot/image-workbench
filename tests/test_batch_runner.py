import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image

from image_workbench import LocalFileBackend, StorageConfig, StorageManager
from image_workbench.batch_runner import BatchRunner, summarize
from image_workbench.locking import FileLock, LockBusyError
from image_workbench.task_runner import StateUpdateError


TEST_DIR = Path(__file__).resolve().parent


class CountingLocalBackend(LocalFileBackend):
    def __init__(self, source_paths):
        super().__init__(source_paths)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return super().generate(request)


class BatchRunnerTests(unittest.TestCase):
    def test_valid_timing_summary_excludes_failed_and_negative_cases(self):
        rows = []
        for index, (seconds, success, expected) in enumerate([(2, True, "success"), (4, True, "success"), (9, True, "success"), (200, False, "success"), (500, False, "expected_failure")]):
            rows.append({"case_id": str(index), "group": "test", "total_seconds": seconds, "codex_seconds": None,
                         "success": success, "expected_mode": expected, "expectation_met": success or expected == "expected_failure",
                         "failure_type": None if success else "prepare_failed"})
        result = summarize(rows)
        self.assertEqual(result["successful_normal_mean_seconds"], 5)
        self.assertEqual(result["successful_normal_median_seconds"], 4)
        self.assertEqual(result["successful_normal_max_seconds"], 9)
        self.assertIsNone(summarize([])["successful_normal_median_seconds"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.reports = self.root / "reports"
        self.storage = StorageManager(StorageConfig(data_root=self.data))
        self.batch = BatchRunner(self.storage)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.fixture = self.assets / "output-source.png"
        Image.new("RGB", (20, 16), (73, 101, 139)).save(self.fixture, "PNG")

    @staticmethod
    def case(case_id, *, project="批测项目", group="pure_generation", operation="generate", prompt="生成测试图",
             input_images=None, base_case=None, expected_mode="success", expected_failure_type=None):
        item = {
            "case_id": case_id, "project_name": project, "group": group,
            "operation": operation, "prompt": prompt, "input_images": input_images or [],
            "base_case": base_case, "expected_mode": expected_mode,
            "tags": ["test_case"],
        }
        if expected_failure_type is not None:
            item["expected_failure_type"] = expected_failure_type
        return item

    def write_manifest(self, cases, *, name="test-suite", path=None):
        manifest_path = path or self.root / "suite.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({"schema_version": 1, "suite_name": name, "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path

    def success_cases(self):
        corrupt = self.assets / "invalid.txt"
        corrupt.write_text("not an image", encoding="utf-8")
        return [
            self.case("plain_generate"),
            self.case("bad_reference", project="负例项目", group="boundary",
                      input_images=[{"path": "assets/invalid.txt", "role": "subject"}],
                      expected_mode="expected_failure", expected_failure_type="prepare_failed"),
            self.case("reference_generate", project="参考项目", group="reference_generation",
                      input_images=[{"path": "assets/output-source.png", "role": "subject"}]),
            self.case("edit_from_reference", project="参考项目", group="edit", operation="edit",
                      prompt="延续上个版本并调整光线", base_case="reference_generate"),
        ]

    def test_local_batch_success_reference_edit_prepare_negative_and_reports(self):
        manifest = self.write_manifest(self.success_cases())
        backend = CountingLocalBackend([self.fixture])
        result = self.batch.run(manifest, backend, reports_root=self.reports)

        self.assertTrue(result["acceptance_passed"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["total"], 4)
        self.assertEqual(result["summary"]["normal_total"], 3)
        self.assertEqual(result["summary"]["normal_succeeded"], 3)
        self.assertEqual(result["summary"]["expectation_met"], 4)
        self.assertEqual(backend.calls, 3)  # 明确的非法参考图在准备阶段被拒绝。

        report_dir = Path(result["report_directory"])
        report = json.loads((report_dir / "batch_results.json").read_text(encoding="utf-8"))
        self.assertEqual(report["summary"], result["summary"])
        rows = report["results"]
        self.assertEqual(len(rows), 4)
        failures = {row["case_id"]: row for row in rows if not row["success"]}
        self.assertEqual(failures["bad_reference"]["failure_type"], "prepare_failed")
        self.assertIsNotNone(failures["bad_reference"]["task_id"])
        self.assertIsNone(failures["bad_reference"]["execution_id"])
        self.assertTrue(any(Path(path).name == "request.json" and Path(path).parent.name == "work" for path in rows[0]["log_paths"]))

        reference_row = next(row for row in rows if row["case_id"] == "reference_generate")
        edit_row = next(row for row in rows if row["case_id"] == "edit_from_reference")
        self.assertEqual(edit_row["base_artifact_id"], reference_row["output_summary"][0]["artifact_id"])
        self.assertEqual(edit_row["output_summary"][0]["source_artifact_id"], edit_row["base_artifact_id"])
        self.assertTrue(edit_row["checks"]["snapshots"])

        with (report_dir / "batch_results.csv").open(encoding="utf-8-sig", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
            csv_fields = set(csv_rows[0])
        self.assertEqual(len(csv_rows), 4)
        self.assertTrue({"batch_id", "case_id", "attempt_id", "execution_id", "expected_mode", "expectation_met", "failure_type", "total_seconds", "codex_seconds", "log_directory"} <= csv_fields)
        self.assertEqual(sum(row["expectation_met"] == "True" for row in csv_rows), 4)
        summary = (report_dir / "batch_summary.md").read_text(encoding="utf-8")
        self.assertIn("正常任务成功 3/3", summary)
        self.assertIn("符合预期 4/4", summary)

    def test_two_batches_get_independent_ids_and_keep_old_output_hash(self):
        manifest = self.write_manifest([self.case("repeat_case")], name="repeat-suite")
        backend = CountingLocalBackend([self.fixture])
        first = self.batch.run(manifest, backend, reports_root=self.reports)
        first_row = json.loads((Path(first["report_directory"]) / "batch_results.json").read_text(encoding="utf-8"))["results"][0]
        first_artifact = first_row["output_summary"][0]
        first_path = Path(first_artifact["path"])
        first_bytes = first_path.read_bytes()
        first_hash = hashlib.sha256(first_bytes).hexdigest()

        second = self.batch.run(manifest, backend, reports_root=self.reports)
        second_row = json.loads((Path(second["report_directory"]) / "batch_results.json").read_text(encoding="utf-8"))["results"][0]
        second_artifact = second_row["output_summary"][0]
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertNotEqual(first_row["attempt_id"], second_row["attempt_id"])
        self.assertNotEqual(first_row["task_id"], second_row["task_id"])
        self.assertNotEqual(first_artifact["artifact_id"], second_artifact["artifact_id"])
        self.assertEqual(hashlib.sha256(first_path.read_bytes()).hexdigest(), first_hash)
        self.assertTrue(Path(second_artifact["path"]).exists())

    def test_resume_skips_successful_cases_and_stop_when_is_persisted(self):
        manifest = self.write_manifest([self.case("resume_first"), self.case("resume_second")], name="resume-suite")
        backend = CountingLocalBackend([self.fixture])
        first = self.batch.run(manifest, backend, reports_root=self.reports,
                               stop_when=lambda row: row["case_id"] == "resume_first")
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(backend.calls, 1)

        resumed = self.batch.run(manifest, backend, resume_directory=first["report_directory"])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(backend.calls, 2)
        rows = json.loads((Path(resumed["report_directory"]) / "batch_results.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual([row["case_id"] for row in rows], ["resume_first", "resume_second"])

    def test_resume_after_result_report_write_failure_reuses_terminal_execution(self):
        manifest = self.write_manifest([self.case("report_crash")], name="report-crash-suite")
        backend = CountingLocalBackend([self.fixture])
        import image_workbench.batch_runner as batch_module
        original = batch_module.write_reports
        calls = 0

        def fail_after_case(directory, metadata, rows):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated report write failure after execution result persisted")
            return original(directory, metadata, rows)

        with mock.patch.object(batch_module, "write_reports", side_effect=fail_after_case):
            with self.assertRaisesRegex(OSError, "simulated report write failure"):
                self.batch.run(manifest, backend, reports_root=self.reports)
        report_dir = next(self.reports.iterdir())
        self.assertEqual(backend.calls, 1)
        journal = json.loads((report_dir / "case_journal.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["cases"]["report_crash"]["state"], "executing")

        resumed = self.batch.run(manifest, backend, resume_directory=report_dir)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(backend.calls, 1)

    def test_resume_reference_and_edit_after_lost_batch_report_reuses_input_snapshot(self):
        manifest = self.write_manifest([
            self.case("reference_base", project="resume edit project", group="reference_generation",
                      input_images=[{"path": "assets/output-source.png", "role": "subject"}]),
            self.case("edit_base", project="resume edit project", group="edit", operation="edit", base_case="reference_base"),
        ], name="resume-reference-edit-suite")
        backend = CountingLocalBackend([self.fixture])
        import image_workbench.batch_runner as batch_module
        original = batch_module.write_reports
        calls = 0

        def fail_after_reference(directory, metadata, rows):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated report loss after reference execution")
            return original(directory, metadata, rows)

        with mock.patch.object(batch_module, "write_reports", side_effect=fail_after_reference):
            with self.assertRaisesRegex(OSError, "simulated report loss"):
                self.batch.run(manifest, backend, reports_root=self.reports)
        report_dir = next(self.reports.iterdir())
        journal = json.loads((report_dir / "case_journal.json").read_text(encoding="utf-8"))
        stable_task_id = journal["cases"]["reference_base"]["task_id"]
        self.assertEqual(backend.calls, 1)

        resumed = self.batch.run(manifest, backend, resume_directory=report_dir)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(backend.calls, 2)
        rows = json.loads((report_dir / "batch_results.json").read_text(encoding="utf-8"))["results"]
        reference = next(row for row in rows if row["case_id"] == "reference_base")
        edit = next(row for row in rows if row["case_id"] == "edit_base")
        self.assertEqual(reference["task_id"], stable_task_id)
        self.assertEqual(reference["input_count"], 1)
        self.assertEqual(edit["base_artifact_id"], reference["output_summary"][0]["artifact_id"])

    def test_preparing_recovery_reuses_project_and_task_ids_after_crash(self):
        manifest = self.write_manifest([self.case("prepare_recovery")], name="prepare-recovery-suite")
        backend = CountingLocalBackend([self.fixture])
        with mock.patch.object(self.storage, "create_project", side_effect=SystemExit("crash before project create")):
            with self.assertRaisesRegex(SystemExit, "crash before project create"):
                self.batch.run(manifest, backend, reports_root=self.reports)
        report_dir = next(self.reports.iterdir())

        with mock.patch.object(self.storage, "create_task", side_effect=SystemExit("crash before task create")):
            with self.assertRaisesRegex(SystemExit, "crash before task create"):
                self.batch.run(manifest, backend, resume_directory=report_dir)
        journal_path = report_dir / "case_journal.json"
        entry = json.loads(journal_path.read_text(encoding="utf-8"))["cases"]["prepare_recovery"]
        stable_project_id, stable_task_id = entry["project_id"], entry["task_id"]
        self.assertEqual(entry["state"], "preparing")

        resumed = self.batch.run(manifest, backend, resume_directory=report_dir)
        final_entry = json.loads(journal_path.read_text(encoding="utf-8"))["cases"]["prepare_recovery"]
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual((final_entry["project_id"], final_entry["task_id"]), (stable_project_id, stable_task_id))
        self.assertEqual(backend.calls, 1)

    def test_resume_directory_lock_rejects_second_runner_before_backend(self):
        manifest = self.write_manifest([self.case("locked_case")], name="locked-suite")
        backend = CountingLocalBackend([self.fixture])
        first = self.batch.run(manifest, backend, reports_root=self.reports, stop_when=lambda row: True)
        self.assertEqual(backend.calls, 1)
        report_dir = Path(first["report_directory"])
        with FileLock(report_dir / ".batch.lock"):
            with self.assertRaises(LockBusyError):
                self.batch.run(manifest, backend, resume_directory=report_dir)
        self.assertEqual(backend.calls, 1)

    def test_resume_with_active_execution_stops_without_backend_call(self):
        manifest = self.write_manifest([self.case("active_case")], name="active-suite")
        backend = CountingLocalBackend([self.fixture])
        first = self.batch.run(manifest, backend, reports_root=self.reports)
        report_dir = Path(first["report_directory"])
        journal_path = report_dir / "case_journal.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        entry = journal["cases"]["active_case"]
        execution = self.batch.runner.get_execution(entry["project_id"], entry["task_id"], entry["execution_id"])
        execution.status = "running"
        execution.error = None
        execution.failure_type = None
        self.storage._write_json(self.batch.runner._record_directory(execution) / "execution.json",
                                 {"schema_version": 1, **asdict(execution)})
        entry["state"] = "executing"
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")

        resumed = self.batch.run(manifest, backend, resume_directory=report_dir)
        self.assertEqual(resumed["status"], "needs_attention")
        self.assertEqual(backend.calls, 1)
        row = json.loads((report_dir / "batch_results.json").read_text(encoding="utf-8"))["results"][0]
        self.assertEqual(row["failure_type"], "resume_uncertain")
        self.assertIn("避免重复调用", row["error"])
        self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["cases"]["active_case"]["state"], "uncertain")

        execution.status = "succeeded"
        execution.error = None
        execution.failure_type = None
        self.storage._write_json(self.batch.runner._record_directory(execution) / "execution.json",
                                 {"schema_version": 1, **asdict(execution)})
        rebuilt = self.batch.run(manifest, backend, resume_directory=report_dir)
        self.assertEqual(rebuilt["status"], "completed")
        self.assertEqual(backend.calls, 1)
        final_rows = json.loads((report_dir / "batch_results.json").read_text(encoding="utf-8"))["results"]
        self.assertTrue(final_rows[0]["success"])

    def test_resume_rejects_model_mismatch_before_execution(self):
        manifest = self.write_manifest([self.case("model_bound")], name="model-bound-suite")
        backend = CountingLocalBackend([self.fixture])
        backend.model = "gpt-6-luna"
        first = self.batch.run(manifest, backend, reports_root=self.reports,
                               stop_when=lambda row: True)
        self.assertEqual(backend.calls, 1)
        backend.model = "different-model"
        with self.assertRaisesRegex(ValueError, "backend/model/data_root"):
            self.batch.run(manifest, backend, resume_directory=first["report_directory"])
        self.assertEqual(backend.calls, 1)

    def test_duplicate_ids_forward_bases_and_traversal_paths_reject_before_any_task(self):
        duplicate_cases = [self.case("same_id"), self.case("same_id")]
        forward_cases = [
            self.case("edit_first", project="same", group="edit", operation="edit", base_case="later"),
            self.case("later", project="same"),
        ]
        traversal_cases = [self.case("bad_path", input_images=[{"path": "../escape.png", "role": "subject"}])]
        for cases, label in ((duplicate_cases, "duplicate"), (forward_cases, "forward base"), (traversal_cases, "path traversal")):
            with self.subTest(case=label):
                isolated = self.root / label.replace(" ", "-")
                manifest = self.write_manifest(cases, path=isolated / "suite.json")
                backend = CountingLocalBackend([self.fixture])
                with self.assertRaises(ValueError):
                    self.batch.run(manifest, backend, reports_root=isolated / "reports")
                self.assertEqual(backend.calls, 0)
                self.assertEqual(self.storage.list_projects(), [])
                self.assertFalse((isolated / "reports").exists())

    def test_state_update_error_keeps_fallback_record_and_registered_output_evidence(self):
        manifest = self.write_manifest([self.case("state_failure")], name="state-suite")
        backend = CountingLocalBackend([self.fixture])
        actual_run = self.batch.runner.run

        def run_then_surface_state_error(*args, **kwargs):
            record = actual_run(*args, **kwargs)
            raise StateUpdateError(record, OSError("simulated state persistence failure"))

        with mock.patch.object(self.batch.runner, "run", side_effect=run_then_surface_state_error):
            result = self.batch.run(manifest, backend, reports_root=self.reports)
        report = json.loads((Path(result["report_directory"]) / "batch_results.json").read_text(encoding="utf-8"))
        row = report["results"][0]
        self.assertFalse(result["acceptance_passed"])
        self.assertEqual(row["failure_type"], "state_update_failed")
        self.assertIn("execution_record_fallback", row)
        fallback = row["execution_record_fallback"]
        self.assertEqual(fallback["execution_id"], row["execution_id"])
        self.assertEqual(fallback["status"], "succeeded")
        self.assertEqual(len(fallback["output_artifact_ids"]), 1)
        artifact = self.storage.get_artifact(fallback["output_artifact_ids"][0])
        self.assertEqual(self.storage.get_artifact_path(artifact.artifact_id).read_bytes(), self.fixture.read_bytes())

    def test_failed_base_marks_edit_dependency_failed_without_calling_backend_again(self):
        missing = self.assets / "not-created.png"
        manifest = self.write_manifest([
            self.case("base_missing", project="edit project"),
            self.case("dependent_edit", project="edit project", group="edit", operation="edit", base_case="base_missing"),
        ], name="dependency-suite")
        backend = CountingLocalBackend([missing])
        result = self.batch.run(manifest, backend, reports_root=self.reports)
        rows = json.loads((Path(result["report_directory"]) / "batch_results.json").read_text(encoding="utf-8"))["results"]
        self.assertEqual([row["case_id"] for row in rows], ["base_missing", "dependent_edit"])
        self.assertEqual(rows[0]["failure_type"], "artifact_not_found")
        self.assertEqual(rows[1]["failure_type"], "dependency_failed")
        self.assertIsNone(rows[1]["task_id"])
        self.assertEqual(backend.calls, 1)


if __name__ == "__main__":
    unittest.main()
