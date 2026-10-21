"""projects-migrate CLI 的隔离端到端测试；不调用任何图片生成后端。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

from image_workbench import StorageConfig, StorageManager


TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent


class ProjectMigrationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_root = self.root / "data"
        self.source = self.root / "legacy-output.png"
        Image.new("RGB", (10, 7), (30, 80, 150)).save(self.source, "PNG")
        self.storage = StorageManager(StorageConfig(data_root=self.data_root))
        self.project = self.storage.create_project("CLI 迁移旧项目")
        self.task = self.storage.create_task(self.project.project_id, "generate", "保留的旧任务")
        self.artifact = self.storage.register_output_image(self.project.project_id, self.task.task_id, self.source)

    def cli(self, *arguments, check=True):
        command = [sys.executable, "-X", "utf8", "-m", "image_workbench", "--data-root",
                   str(self.data_root), "projects-migrate", *map(str, arguments)]
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True,
                                encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                check=False, timeout=30)
        if check and result.returncode != 0:
            self.fail(f"CLI returned {result.returncode}: {result.stderr}\n{result.stdout}")
        return result

    def physical_bytes(self):
        return {path.relative_to(self.data_root / "projects").as_posix(): path.read_bytes()
                for path in (self.data_root / "projects").rglob("*") if path.is_file()}

    def catalog_path(self):
        return self.data_root / ".project_management" / "catalog.json"

    def test_preview_backup_apply_and_idempotent_apply(self):
        physical_before = self.physical_bytes()

        preview_result = self.cli()
        self.assertEqual(preview_result.returncode, 0)
        preview = json.loads(preview_result.stdout)
        self.assertEqual(preview["status"], "dry_run")
        fingerprint = preview["fingerprint"]
        self.assertTrue(fingerprint)
        self.assertFalse(self.catalog_path().exists(), "默认命令只预览，不能创建catalog")
        self.assertEqual(self.physical_bytes(), physical_before)

        backup_root = self.root / "external-backups"
        backup_result = self.cli("--backup-only", "--backup-directory", backup_root)
        backup = json.loads(backup_result.stdout)
        self.assertEqual(backup["status"], "backup_prepared")
        backup_directory = Path(backup["backup_directory"])
        self.assertTrue(backup_directory.is_relative_to(backup_root))
        self.assertTrue((backup_directory / "backup.json").is_file())
        backed_up_metadata = list((backup_directory / "metadata").rglob("*.json"))
        self.assertTrue(backed_up_metadata)
        self.assertTrue(any(path.name == "artifacts.json" for path in backed_up_metadata))
        self.assertFalse(self.catalog_path().exists(), "仅备份不应用迁移")
        self.assertEqual(self.physical_bytes(), physical_before)

        missing_fingerprint = self.cli("--apply", "--backup-directory", self.root / "missing-fp-backup", check=False)
        self.assertEqual(missing_fingerprint.returncode, 2)
        self.assertIn("--expected-fingerprint", missing_fingerprint.stderr)
        self.assertFalse(self.catalog_path().exists())
        self.assertEqual(self.physical_bytes(), physical_before)

        apply_result = self.cli("--apply", "--expected-fingerprint", fingerprint,
                                 "--backup-directory", self.root / "apply-backups")
        applied = json.loads(apply_result.stdout)
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(applied["integrity_verified"])
        self.assertTrue(self.catalog_path().is_file())
        self.assertEqual(self.physical_bytes(), physical_before)
        self.assertEqual(self.storage.get_task(self.project.project_id, self.task.task_id), self.task)
        migrated_artifact = self.storage.get_artifact(self.artifact.artifact_id)
        self.assertEqual((migrated_artifact.relative_path, migrated_artifact.sha256),
                         (self.artifact.relative_path, self.artifact.sha256))

        fresh_preview = json.loads(self.cli().stdout)
        unchanged_result = self.cli("--apply", "--expected-fingerprint", fresh_preview["fingerprint"],
                                    "--backup-directory", self.root / "repeat-backups")
        unchanged = json.loads(unchanged_result.stdout)
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertTrue(unchanged["integrity_verified"])
        self.assertEqual(self.physical_bytes(), physical_before)


if __name__ == "__main__":
    unittest.main()
