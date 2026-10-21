"""只在新副本演练迁移及分类，不调用任何图片后端，也不修改源数据。"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_workbench import ProjectManager, StorageConfig, StorageManager


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if destination.exists() or destination.is_relative_to(source):
        parser.error("目标必须是源目录外尚不存在的新目录")
    original = ProjectManager(StorageManager(StorageConfig(source)))
    source_plan = original.migration_plan()
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("*.lock"))
    manager = ProjectManager(StorageManager(StorageConfig(destination)))
    plan = manager.migration_plan()
    if source_plan["fingerprint"] != plan["fingerprint"]:
        raise RuntimeError("复制期间源数据变化；副本未迁移，请重新准备")
    before = manager._inventory()
    applied = manager.migrate_legacy(dry_run=False, expected_fingerprint=plan["fingerprint"],
                                    backup_directory=destination.parent / "copy-backups")
    unchanged = manager.migrate_legacy(dry_run=False, expected_fingerprint=manager.migration_plan()["fingerprint"],
                                      backup_directory=destination.parent / "copy-backups")
    # 选择有历史输出的项目，在副本验证重命名、创建和归属调整。
    task_record = next(item for item in before["tasks"] if item["base_artifact_id"] is not None)
    tid, mid = task_record["task_id"], task_record["project_id"]
    placement_before = manager.get_task_placement(tid)
    manager.update_main_project(mid, name="迁移副本验收 · 已重命名")
    manager.update_subproject(placement_before.subproject_id, name="原任务分类 · 已重命名")
    target = manager.create_subproject(mid, "任务移动验收")
    manager.move_task(tid, target.subproject_id, expected_subproject_id=placement_before.subproject_id)
    after = manager._inventory()
    if before != after:
        raise RuntimeError("分类操作改变了物理资源或元数据")
    if original.migration_plan()["fingerprint"] != source_plan["fingerprint"]:
        raise RuntimeError("源数据已产生新变化；副本演练有效，但真实 dry-run 应刷新")
    report = {"source_data_root": str(source), "test_data_root": str(destination),
              "source_fingerprint": source_plan["fingerprint"], "migration": applied,
              "repeat_migration_status": unchanged["status"], "inventory_equal_before_after": True,
              "verified_image_sha256_count": len(before["artifacts"]),
              "verified_original_json_count": len(before["metadata_hashes"]),
              "placement_before": asdict(placement_before), "placement_after": asdict(manager.get_task_placement(tid)),
              "source_catalog_created": (source / ".project_management/catalog.json").exists(),
              "image_files_moved": 0, "image_files_changed": 0, "generation_calls": 0}
    path = destination.parent / "rehearsal_result.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(path), "counts": before["counts"],
                      "integrity_verified": True, "original_json_count": len(before["metadata_hashes"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
