"""Boundary and failure checks for service-scoped filesystem capabilities."""
import ctypes
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from image_workbench import StorageConfig, StorageManager
from image_workbench.managed_files import ManagedFiles, MetadataError
from tests.test_atomic_replace import FakeClock, windows_permission_error


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
        anchor = self.files.anchor

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

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 path aliases")
    def test_windows_short_alias_keeps_the_same_scoped_boundary(self):
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetShortPathNameW(str(self.root), buffer, len(buffer)):
            raise ctypes.WinError()
        short = Path(buffer.value)
        if short == self.root.resolve():
            self.skipTest("8.3 aliases are unavailable on this filesystem")
        files = ManagedFiles(short).scope("workspace-a")
        self.assertEqual(files.root, self.root.resolve() / "workspace-a")
        path = files.path("record.json")
        files.write_json(path, {"schema_version": 1, "value": "same directory"})
        self.assertEqual(self.files.read_json(self.files.path("record.json"))["value"], "same directory")
        storage = StorageManager(StorageConfig(data_root=short))
        self.assertEqual(storage._read_json(path)["value"], "same directory")
        with self.assertRaises(ValueError):
            files.read_json(short / "workspace-b" / "record.json")

    def test_read_retries_transient_windows_sharing_without_changing_record(self):
        path = self.files.path("record.json")
        record = {"schema_version": 1, "value": "complete record"}
        self.files.write_json(path, record)
        original = Path.open
        errors = [windows_permission_error(32)]
        if os.name == "nt":
            errors.append(PermissionError(errno.EACCES, "CRT read while file is in use"))
        for error in errors:
            attempts = []

            def blocked_then_open(target, *args, attempts=attempts, error=error, **kwargs):
                attempts.append(target)
                if len(attempts) < 3:
                    raise error
                return original(target, *args, **kwargs)

            with self.subTest(error=error), patch("image_workbench.managed_files.time", FakeClock()), patch.object(Path, "open", blocked_then_open):
                self.assertEqual(self.files.read_json(path), record)
            self.assertEqual(len(attempts), 3)
        self.assertEqual(list(self.files.root.iterdir()), [path])

    def test_read_permission_failure_is_bounded_and_other_errors_are_not_retried(self):
        path = self.files.path("record.json")
        clock = FakeClock()
        with patch("image_workbench.managed_files.time", clock), patch.object(Path, "open", side_effect=windows_permission_error(5)):
            with self.assertRaises(PermissionError):
                self.files.read_json(path)
        self.assertGreaterEqual(clock.elapsed, 1.0)
        self.assertLess(clock.elapsed, 1.2)
        for error in (windows_permission_error(87), FileNotFoundError("missing record")):
            with self.subTest(error=error), patch.object(Path, "open", side_effect=error) as opened:
                with self.assertRaises(type(error)):
                    self.files.read_json(path)
                self.assertEqual(opened.call_count, 1)
