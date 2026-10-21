import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("performance_report", Path(__file__).resolve().parents[1] / "scripts" / "performance_report.py")
reporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)


def record(identifier="one", *, group="A_plain", status="success", total=100, **phases):
    durations = {phase: None for phase in reporter.PHASES}
    durations.update(total=total, **phases)
    return {"schema_version": 1, "task_id": "task-" + identifier, "execution_id": "execution-" + identifier,
            "project_id": "project", "operation": "generate", "backend": "codex", "prompt_type": group,
            "reference_count": int(group == "B_reference"), "output_count": int(status == "success"), "status": status,
            "execution_status": "succeeded" if status == "success" else "failed",
            "failure_type": None if status == "success" else "codex_execution_failed",
            "timestamps": {"task_created_time": "2026-10-13T01:00:00+00:00"}, "durations_seconds": durations,
            "measurements": {"codex_boundary": "unavailable", "imagegen_tool_seconds": None, "limitations": ["raw limitation"]},
            "phase_segments": [{"phase": "register", "seconds": 1}], "codex_events": []}


class PerformanceReportTests(unittest.TestCase):
    def test_null_phase_excluded_without_zero_imputation_and_paired_denominator(self):
        result = reporter.build_report([record("a", total=100, register=10), record("b", total=900)])
        metric = result["overall"]["success"]["durations_seconds"]["register"]
        self.assertEqual(metric["known_count"], 1)
        self.assertEqual(metric["missing_count"], 1)
        self.assertEqual(metric["mean"], 10)
        self.assertEqual(metric["weighted_share"], 0.1)
        self.assertEqual(metric["share_denominator_seconds"], 100)

    def test_weighted_share_is_ratio_of_sums_not_mean_of_ratios(self):
        result = reporter.build_report([record("a", total=100, register=50), record("b", total=900, register=90)])
        metric = result["overall"]["success"]["durations_seconds"]["register"]
        self.assertEqual(metric["weighted_share"], 0.14)
        self.assertEqual(metric["share_sample_count"], 2)

    def test_failure_not_mixed_with_success_and_groups_are_independent(self):
        result = reporter.build_report([record("a", total=10), record("b", group="B_reference", total=30),
                                        record("f", status="failed", total=1000)])
        self.assertEqual(result["overall"]["success"]["durations_seconds"]["total"]["mean"], 20)
        self.assertEqual(result["overall"]["failed"]["durations_seconds"]["total"]["mean"], 1000)
        self.assertEqual(result["groups"]["A_plain"]["success"]["count"], 1)
        self.assertEqual(result["groups"]["B_reference"]["success"]["durations_seconds"]["total"]["mean"], 30)
        self.assertEqual(result["groups"]["B_reference"]["failed"]["count"], 0)
        self.assertEqual(result["failures"][0]["execution_id"], "execution-f")

    def test_raw_data_is_preserved(self):
        source = record(register=2)
        before = copy.deepcopy(source)
        result = reporter.build_report([source], sample_metadata={"batch": "ten"})
        self.assertEqual(result["records"][0], before)
        self.assertEqual(source, before)
        self.assertEqual(result["sample_metadata"], {"batch": "ten"})

    def test_unknown_and_zero_are_distinct(self):
        result = reporter.build_report([record("a", total=0, register=0), record("b", total=None)])
        metrics = result["overall"]["success"]["durations_seconds"]
        self.assertEqual(metrics["register"]["known_count"], 1)
        self.assertEqual(metrics["register"]["mean"], 0)
        self.assertIsNone(metrics["register"]["weighted_share"])
        self.assertIsNone(metrics["codex_execution"]["mean"])
        self.assertEqual(metrics["codex_execution"]["missing_count"], 2)

    def test_negative_clock_duration_preserved_but_excluded(self):
        result = reporter.build_report([record(total=-10, register=4)])
        self.assertEqual(result["records"][0]["durations_seconds"]["total"], -10)
        self.assertEqual(result["overall"]["success"]["durations_seconds"]["total"]["invalid_count"], 1)
        self.assertIsNone(result["overall"]["success"]["durations_seconds"]["register"]["weighted_share"])
        self.assertEqual(result["warnings"][0]["phases"], ["total"])

    def test_thresholds_do_not_claim_image_model_timing(self):
        result = reporter.build_report([record(codex_execution=80)])
        text = " ".join(result["recommendations"]["short_term"])
        self.assertIn("Codex 执行整体（理解、调度、ImageGen、返回）", text)
        self.assertIn("不能据此断言图片模型", text)
        launch = reporter.build_report([record(codex_launch=35)])
        registration = reporter.build_report([record(register=21)])
        self.assertIn("超过 30%", " ".join(launch["recommendations"]["short_term"]))
        self.assertIn("超过 20%", " ".join(registration["recommendations"]["short_term"]))

    def test_exact_threshold_does_not_trigger(self):
        result = reporter.build_report([record(codex_execution=70, codex_launch=30, register=20)])
        self.assertIn("未触发", result["recommendations"]["short_term"][0])

    def test_combined_only_does_not_invent_execution_share(self):
        result = reporter.build_report([record(codex_combined=95)])
        text = " ".join(result["recommendations"]["short_term"])
        self.assertNotIn("由 Codex 执行整体", text)
        self.assertIn("边界未知", text)

    def test_low_register_share_keeps_storage_and_summary_lists_individual_tasks(self):
        result = reporter.build_report([record("a", codex_execution=90, register=0.1),
                                        record("b", group="B_reference", status="failed", total=5)])
        text = reporter.render_markdown(result)
        self.assertIn("保留现有存储实现", text)
        self.assertIn("当前证据不支持为提速减少复制", text)
        self.assertIn("每次任务明细", text)
        self.assertIn("| task-a | A | 成功 | 100.000 |", text)
        self.assertIn("| task-b | B | 失败 | 5.000 |", text)

    def test_manifest_only_loads_selected_file_and_emits_both_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected, unrelated = root / "selected.json", root / "historical.json"
            selected.write_text(json.dumps(record()), encoding="utf-8")
            unrelated.write_text(json.dumps(record("unselected", total=9000)), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"records": [str(selected)], "sample_metadata": {"batch": "controlled"}}), encoding="utf-8")
            self.assertEqual(reporter.main(["--manifest", str(manifest), "--output", str(root / "out")]), 0)
            result = json.loads((root / "out" / "performance_report.json").read_text(encoding="utf-8"))
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["overall"]["success"]["durations_seconds"]["total"]["mean"], 100)
            # Windows CI may expose TEMP through an 8.3 alias; reports use the
            # canonical path returned by load_manifest's strict resolution.
            self.assertEqual(result["source_files"][0]["path"], str(selected.resolve(strict=True)))
            self.assertTrue((root / "out" / "performance_summary.md").is_file())

    def test_empty_manifest_says_no_data(self):
        result = reporter.build_report([])
        self.assertIn("尚无数据", reporter.render_markdown(result))
        self.assertEqual(result["recommendations"]["short_term"], [])
        self.assertIsNone(result["overall"]["success"]["durations_seconds"]["total"]["mean"])

    def test_manifest_rejects_relative_paths_and_duplicate_executions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"records": ["record.json"]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                reporter.load_manifest(manifest)
            first, second = root / "first.json", root / "second.json"
            for path in (first, second):
                path.write_text(json.dumps(record()), encoding="utf-8")
            manifest.write_text(json.dumps({"records": [str(first), str(second)]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重复 task_id/execution_id"):
                reporter.load_manifest(manifest)

    def test_output_cannot_overwrite_original_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "performance_report.json"
            original = json.dumps(record())
            source.write_text(original, encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"records": [str(source)]}), encoding="utf-8")
            self.assertEqual(reporter.main(["--manifest", str(manifest), "--output", str(root)]), 2)
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_invalid_record_is_not_silently_omitted(self):
        for change in ({"schema_version": 2}, {"status": "unknown"}, {"prompt_type": "unknown"}):
            source = record()
            source.update(change)
            with self.assertRaises(ValueError):
                reporter.build_report([source])
        source = record(register=True)
        with self.assertRaisesRegex(ValueError, "有限数值或 null"):
            reporter.build_report([source])


if __name__ == "__main__":
    unittest.main()
