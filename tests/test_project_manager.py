"""两级项目目录合同测试；所有图片与项目数据均由临时夹具创建。"""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from image_workbench import StorageConfig, StorageManager
from image_workbench.project_manager import ProjectManager
from image_workbench.storage import MetadataError


class ProjectManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.manager = ProjectManager(self.storage)
        self.image = self.root / "fixture.png"
        Image.new("RGB", (13, 9), (25, 90, 140)).save(self.image, "PNG")

    def legacy_project(self, name="旧项目"):
        project = self.storage.create_project(name)
        task = self.storage.create_task(project.project_id, "generate", "旧任务提示词")
        artifact = self.storage.register_output_image(project.project_id, task.task_id, self.image)
        return project, task, artifact

    def apply_migration(self, *, backup_name="migration-backup"):
        plan = self.manager.migration_plan()
        return self.manager.migrate_legacy(dry_run=False,
                                           expected_fingerprint=plan["fingerprint"],
                                           backup_directory=self.root / backup_name)

    @staticmethod
    def tree_bytes(directory):
        return {path.relative_to(directory).as_posix(): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    @staticmethod
    def artifact_facts(storage, task):
        return [(item.artifact_id, item.relative_path, item.sha256,
                 storage.get_artifact_path(item.artifact_id).read_bytes())
                for item in storage.list_task_artifacts(task.project_id, task.task_id)]

    def test_multiple_main_and_subprojects_allow_same_names_without_cross_contamination(self):
        main_a = self.manager.create_main_project("同名主项目", "A")
        main_b = self.manager.create_main_project("同名主项目", "B")
        sub_a1 = self.manager.create_subproject(main_a.main_project_id, "同名子项目")
        sub_a2 = self.manager.create_subproject(main_a.main_project_id, "同名子项目")
        sub_b = self.manager.create_subproject(main_b.main_project_id, "同名子项目")

        task_a1 = self.manager.create_task(sub_a1.subproject_id, "generate", "A1", name="同名任务")
        task_a2 = self.manager.create_task(sub_a2.subproject_id, "generate", "A2", name="同名任务")
        task_b = self.manager.create_task(sub_b.subproject_id, "generate", "B", name="同名任务")

        self.assertEqual(len(self.manager.list_main_projects()), 2)
        self.assertEqual([len(self.manager.list_subprojects(item.main_project_id)) for item in (main_a, main_b)], [3, 2])
        self.assertEqual([item[0].prompt for item in self.manager.list_tasks(subproject_id=sub_a1.subproject_id)], ["A1"])
        self.assertEqual([item[0].prompt for item in self.manager.list_tasks(main_project_id=main_a.main_project_id)], ["A1", "A2"])
        self.assertEqual(self.manager.get_task_placement(task_b.task_id, main_project_id=main_b.main_project_id).subproject_id,
                         sub_b.subproject_id)
        with self.assertRaises(FileNotFoundError):
            self.manager.get_task_placement(task_b.task_id, main_project_id=main_a.main_project_id)
        with self.assertRaises(ValueError):
            self.manager.list_tasks(main_project_id=main_a.main_project_id, subproject_id=sub_b.subproject_id)
        self.assertEqual(len({task_a1.task_id, task_a2.task_id, task_b.task_id}), 3)

    def test_rename_and_move_only_change_catalog_and_keep_physical_records_and_artifacts_stable(self):
        legacy, task, artifact = self.legacy_project()
        self.apply_migration()
        main = self.manager.get_main_project(legacy.project_id)
        subs = self.manager.list_subprojects(main.main_project_id)
        target = self.manager.create_subproject(main.main_project_id, "整理后")
        placement_before = self.manager.get_task_placement(task.task_id)
        physical_before = self.tree_bytes(self.storage._path("projects"))
        artifacts_before = self.artifact_facts(self.storage, task)

        self.manager.update_main_project(main.main_project_id, name="改名主项目", description="新描述")
        self.manager.update_subproject(subs[0].subproject_id, name="旧任务箱")
        moved = self.manager.move_task(task.task_id, target.subproject_id,
                                       expected_subproject_id=subs[0].subproject_id)

        self.assertEqual(moved.task_id, placement_before.task_id)
        self.assertEqual(moved.storage_project_id, placement_before.storage_project_id)
        self.assertEqual(moved.subproject_id, target.subproject_id)
        self.assertEqual(self.storage.get_task(legacy.project_id, task.task_id), task)
        self.assertEqual(self.artifact_facts(self.storage, task), artifacts_before)
        self.assertEqual(artifacts_before[0][:3], (artifact.artifact_id, artifact.relative_path, artifact.sha256))
        self.assertEqual(self.tree_bytes(self.storage._path("projects")), physical_before)

    def test_cross_main_move_and_edit_base_reference_are_rejected(self):
        main_a = self.manager.create_main_project("A")
        main_b = self.manager.create_main_project("B")
        sub_a = self.manager.create_subproject(main_a.main_project_id, "A 子项目")
        sub_b = self.manager.create_subproject(main_b.main_project_id, "B 子项目")
        task = self.manager.create_task(sub_a.subproject_id, "generate", "A 图片")
        base = self.storage.register_output_image(task.project_id, task.task_id, self.image)

        with self.assertRaises(ValueError):
            self.manager.move_task(task.task_id, sub_b.subproject_id)
        with self.assertRaises(ValueError):
            self.manager.create_task(sub_b.subproject_id, "edit", "不能跨主项目引用", base.artifact_id)
        self.assertEqual(self.manager.get_task_placement(task.task_id).subproject_id, sub_a.subproject_id)

    def test_history_base_can_start_multiple_edit_branches_across_subprojects_of_same_main(self):
        main = self.manager.create_main_project("修图项目")
        sub_a = self.manager.create_subproject(main.main_project_id, "初稿")
        sub_b = self.manager.create_subproject(main.main_project_id, "精修")
        original = self.manager.create_task(sub_a.subproject_id, "generate", "生成原图")
        base = self.storage.register_output_image(original.project_id, original.task_id, self.image)
        first = self.manager.create_task(sub_b.subproject_id, "edit", "版本一", base.artifact_id)
        second = self.manager.create_task(sub_b.subproject_id, "edit", "从原图分支", base.artifact_id)
        self.assertEqual(first.base_artifact_id, base.artifact_id)
        self.assertEqual(second.base_artifact_id, base.artifact_id)
        self.assertNotEqual(first.task_id, second.task_id)

    def test_migration_is_dry_run_by_default_and_apply_requires_current_fingerprint_and_backup(self):
        project, task, _ = self.legacy_project()
        physical_before = self.tree_bytes(self.storage._path("projects"))
        plan = self.manager.migration_plan()
        dry_run = self.manager.migrate_legacy()
        self.assertEqual(dry_run["fingerprint"], plan["fingerprint"])
        self.assertFalse((self.storage._path(".project_management") / "catalog.json").exists())
        self.assertEqual(self.tree_bytes(self.storage._path("projects")), physical_before)

        backup = self.root / "backup"
        result = self.manager.migrate_legacy(dry_run=False, expected_fingerprint=plan["fingerprint"],
                                             backup_directory=backup)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["integrity_verified"])
        self.assertEqual(self.manager.get_task_placement(task.task_id).storage_project_id, project.project_id)
        self.assertTrue(any(path.is_file() for path in backup.rglob("*")))
        catalog_after = (self.storage._path(".project_management") / "catalog.json").read_bytes()
        second_plan = self.manager.migration_plan()
        second = self.manager.migrate_legacy(dry_run=False, expected_fingerprint=second_plan["fingerprint"],
                                             backup_directory=self.root / "backup-again")
        self.assertEqual(second["status"], "unchanged")
        self.assertTrue(second["integrity_verified"])
        self.assertEqual((self.storage._path(".project_management") / "catalog.json").read_bytes(), catalog_after)
        self.assertEqual(self.tree_bytes(self.storage._path("projects")), physical_before)

    def test_stale_migration_fingerprint_and_write_failure_leave_catalog_and_legacy_data_intact(self):
        project, _, _ = self.legacy_project()
        plan = self.manager.migration_plan()
        with self.assertRaises((ValueError, MetadataError)):
            self.manager.migrate_legacy(dry_run=False, expected_fingerprint="0" * 64,
                                        backup_directory=self.root / "stale-backup")
        self.assertFalse((self.storage._path(".project_management") / "catalog.json").exists())

        with patch.object(self.storage.files, "write_json", side_effect=OSError("injected catalog write failure")):
            with self.assertRaises(OSError):
                self.manager.migrate_legacy(dry_run=False, expected_fingerprint=plan["fingerprint"],
                                            backup_directory=self.root / "failed-backup")
        self.assertFalse((self.storage._path(".project_management") / "catalog.json").exists())
        self.assertEqual(self.storage.get_project(project.project_id).name, project.name)
        self.assertEqual(len(self.storage.list_tasks(project.project_id)), 1)

    def test_result_report_write_failure_rolls_back_catalog_without_changing_physical_files(self):
        project, task, _ = self.legacy_project()
        plan = self.manager.migration_plan()
        physical_before = self.tree_bytes(self.storage._path("projects"))
        with patch.object(ProjectManager, "_write_result", side_effect=OSError("injected result report write failure")):
            with self.assertRaises(OSError):
                self.manager.migrate_legacy(dry_run=False, expected_fingerprint=plan["fingerprint"],
                                            backup_directory=self.root / "report-write-failure-backup")
        catalog = self.storage._path(".project_management", "catalog.json")
        self.assertFalse(catalog.exists(), "报告提交失败后新建的逻辑目录必须回滚")
        self.assertEqual(self.tree_bytes(self.storage._path("projects")), physical_before)
        self.assertEqual(self.storage.get_task(project.project_id, task.task_id), task)

        inventory = self.manager._inventory()
        with patch.object(self.manager, "_inventory", side_effect=[inventory, inventory,
                                                                    MetadataError("injected post-write integrity failure")]):
            with self.assertRaises(MetadataError):
                self.manager.migrate_legacy(dry_run=False, expected_fingerprint=plan["fingerprint"],
                                            backup_directory=self.root / "post-write-failure-backup")
        self.assertFalse((self.storage._path(".project_management") / "catalog.json").exists())
        self.assertEqual(self.storage.get_project(project.project_id).name, project.name)
        self.assertEqual(len(self.storage.list_tasks(project.project_id)), 1)

    def test_migration_backup_contains_previous_catalog_and_plan_detects_changed_legacy_snapshot(self):
        project, _, _ = self.legacy_project()
        self.apply_migration()
        catalog = self.storage._path(".project_management", "catalog.json")
        self.manager.update_main_project(project.project_id, name="用户改过的逻辑名")
        previous = catalog.read_bytes()
        self.storage.create_project("稍后新增的旧项目")
        current_plan = self.manager.migration_plan()
        backup = self.root / "backup"
        result = self.manager.migrate_legacy(dry_run=False, expected_fingerprint=current_plan["fingerprint"],
                                             backup_directory=backup)
        backup_path = Path(result["backup_directory"])
        self.assertTrue(backup_path.is_relative_to(backup))
        self.assertEqual((backup_path / "catalog.before.json").read_bytes(), previous)
        self.assertTrue(any((backup_path / "metadata").rglob("*.json")))

    def test_corrupt_catalog_and_foreign_key_are_rejected_without_silent_rebuild(self):
        project, _, _ = self.legacy_project()
        self.apply_migration()
        catalog = self.storage._path(".project_management", "catalog.json")
        valid_bytes = catalog.read_bytes()
        catalog.write_text("{broken", encoding="utf-8")
        corrupt_bytes = catalog.read_bytes()
        with self.assertRaises(MetadataError):
            self.manager.list_main_projects()
        self.assertEqual(catalog.read_bytes(), corrupt_bytes)

        catalog.write_bytes(valid_bytes)
        valid = json.loads(catalog.read_text(encoding="utf-8"))
        valid["subprojects"].clear()
        catalog.write_text(json.dumps(valid), encoding="utf-8")
        broken_fk = catalog.read_bytes()
        with self.assertRaises(MetadataError):
            self.manager.list_main_projects()
        self.assertEqual(catalog.read_bytes(), broken_fk)
        self.assertEqual(self.storage.get_project(project.project_id).project_id, project.project_id)

    def test_copying_entire_data_root_preserves_project_mapping_and_pending_target(self):
        main = self.manager.create_main_project("复制验证")
        sub = self.manager.create_subproject(main.main_project_id, "恢复目标")
        pending_id = "a" * 32
        self.manager.reserve_task(sub.subproject_id, pending_id, "待恢复任务", "保存意图")
        physical_id = self.manager.ensure_storage_namespace(main.main_project_id)
        self.storage.create_task(physical_id, "generate", "已落盘", task_id=pending_id)

        copied = self.root / "copied-data"
        shutil.copytree(self.storage.data_root, copied)
        copied_storage = StorageManager(StorageConfig(data_root=copied))
        copied_manager = ProjectManager(copied_storage)
        copied_manager.finish_task(pending_id)
        self.assertEqual(copied_manager.get_task_placement(pending_id).subproject_id, sub.subproject_id)
        self.assertEqual(copied_manager.get_task_placement(pending_id).state, "ready")
        self.assertEqual(copied_storage.get_task(physical_id, pending_id).prompt, "已落盘")

    def test_task_metadata_update_does_not_rewrite_prompt(self):
        main = self.manager.create_main_project("名字")
        sub = self.manager.default_subproject(main.main_project_id)
        task = self.manager.create_task(sub.subproject_id, "generate", "不可变历史提示词", name="旧标题")
        placement = self.manager.update_task(task.task_id, name="新标题", description="任务说明")
        self.assertEqual(placement.name, "新标题")
        self.assertEqual(placement.description, "任务说明")
        self.assertEqual(self.storage.get_task(task.project_id, task.task_id).prompt, "不可变历史提示词")

    def test_summary_counts_tasks_inputs_and_outputs_at_subproject_and_main_scopes(self):
        main = self.manager.create_main_project("统计")
        sub_a = self.manager.create_subproject(main.main_project_id, "输入")
        sub_b = self.manager.create_subproject(main.main_project_id, "输出")
        task_a = self.manager.create_task(sub_a.subproject_id, "generate", "仅有输入")
        task_b = self.manager.create_task(sub_b.subproject_id, "generate", "已有输出")
        self.storage.add_input_image(task_a.project_id, task_a.task_id, self.image)
        self.storage.register_output_image(task_b.project_id, task_b.task_id, self.image)

        summary_a = self.manager.summary(subproject_id=sub_a.subproject_id)
        summary_b = self.manager.summary(subproject_id=sub_b.subproject_id)
        summary_main = self.manager.summary(main_project_id=main.main_project_id)
        self.assertEqual((summary_a["task_count"], summary_a["input_count"], summary_a["output_count"]), (1, 1, 0))
        self.assertEqual((summary_b["task_count"], summary_b["input_count"], summary_b["output_count"]), (1, 0, 1))
        self.assertEqual((summary_main["task_count"], summary_main["image_count"],
                          summary_main["input_count"], summary_main["output_count"]), (2, 2, 1, 1))
        self.assertTrue(summary_main["updated_at"])


if __name__ == "__main__":
    unittest.main()
