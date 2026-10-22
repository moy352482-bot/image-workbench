"""Boundary and failure checks for service-scoped filesystem capabilities."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from image_workbench import StorageConfig, StorageManager
from image_workbench.managed_files import ManagedFiles, MetadataError


class ManagedFilesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.files = ManagedFiles(self.root).scope("workspace-a")
        self.files.root.mkdir()

    def test_scope_cannot_read_write_or_cleanup_sibling_or_parent(self):
        sibling = self.root / "workspace-b"
        sibling.mkdir()
        outside = sibling / "record.json"
        original = '{"schema_version": 1, "owner": "b"}'
        outside.write_text(original, encoding="utf-8")
        for path in (outside, self.files.root / ".." / "workspace-b" / "record.json"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.files.read_json(path)
                with self.assertRaises(ValueError):
                    self.files.write_json(path, {"schema_version": 1})
                self.files.cleanup(path)
                self.assertEqual(outside.read_text(encoding="utf-8"), original)
        with self.assertRaises(ValueError):
            self.files.scope("..")

    def test_atomic_write_failure_keeps_prior_record_and_removes_scratch(self):
        path = self.files.path("record.json")
        old = {"schema_version": 1, "text": "原始记录"}
        self.files.write_json(path, old)
        with patch("image_workbench.managed_files.replace_file", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.files.write_json(path, {"schema_version": 1, "text": "new"})
        self.assertEqual(self.files.read_json(path), old)
        self.assertEqual(list(self.files.root.iterdir()), [path])

    def test_invalid_metadata_is_not_hidden_and_legacy_api_uses_same_policy(self):
        storage = StorageManager(StorageConfig(data_root=self.root))
        path = self.files.path("record.json")
        storage._write_json(path, {"schema_version": 1, "value": 3})
        self.assertEqual(self.files.read_json(path)["value"], 3)
        self.files.write_json(path, {"schema_version": 1, "value": 4})
        self.assertEqual(storage._read_json(path)["value"], 4)
        path.write_text('{"schema_version":', encoding="utf-8")
        with self.assertRaises(MetadataError):
            self.files.read_json(path)

    def test_scope_rechecks_parent_reparse_points_after_creation(self):
        original = Path.lstat
        anchor = self.root

        def reparse(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == anchor:
                class Reparse:
                    st_mode = info.st_mode
                    st_file_attributes = 0x400
                return Reparse()
            return info

        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(ValueError, "重解析点"):
                self.files.path("record.json")
