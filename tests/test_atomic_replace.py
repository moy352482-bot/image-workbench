import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from image_workbench import StorageConfig, StorageManager
from image_workbench import atomic_files


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


def windows_permission_error(code):
    error = PermissionError(errno.EACCES, "The target is temporarily in use")
    error.winerror = code
    return error


class AtomicReplaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "pending.json"
        self.destination = self.root / "saved.json"
        self.source.write_bytes(b'{"version": 2}')
        self.destination.write_bytes(b'{"version": 1}')
        self.clock = FakeClock()

    def test_transient_windows_sharing_errors_retry_without_losing_contents(self):
        replace = os.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                self.source.write_bytes(b'{"version": 2}')
                self.destination.write_bytes(b'{"version": 1}')
                attempts = []

                def temporarily_blocked(source, destination):
                    self.assertEqual(Path(source).read_bytes(), b'{"version": 2}')
                    self.assertEqual(Path(destination).read_bytes(), b'{"version": 1}')
                    attempts.append((source, destination))
                    if len(attempts) <= 2:
                        raise windows_permission_error(code)
                    return replace(source, destination)

                with mock.patch.object(atomic_files, "time", self.clock), mock.patch.object(
                    atomic_files.os, "replace", side_effect=temporarily_blocked
                ):
                    atomic_files.replace_file(self.source, self.destination)

                self.assertEqual(len(attempts), 3)
                self.assertFalse(self.source.exists())
                self.assertEqual(self.destination.read_bytes(), b'{"version": 2}')

    def test_persistent_windows_sharing_error_is_bounded_and_preserves_original(self):
        error = windows_permission_error(5)
        with mock.patch.object(atomic_files, "time", self.clock), mock.patch.object(
            atomic_files.os, "replace", side_effect=error
        ) as replace:
            with self.assertRaises(PermissionError) as raised:
                atomic_files.replace_file(self.source, self.destination)

        self.assertIs(raised.exception, error)
        self.assertGreater(replace.call_count, 1)
        self.assertLess(replace.call_count, 50)
        self.assertGreaterEqual(self.clock.elapsed, 1.0)
        self.assertLess(self.clock.elapsed, 1.2)
        self.assertEqual(self.destination.read_bytes(), b'{"version": 1}')
        self.assertEqual(self.source.read_bytes(), b'{"version": 2}')

    def test_permission_error_without_winerror_is_not_retried(self):
        error = PermissionError(errno.EACCES, "Permanent access denied")
        with mock.patch.object(atomic_files, "time", self.clock), mock.patch.object(
            atomic_files.os, "replace", side_effect=error
        ) as replace:
            with self.assertRaises(PermissionError) as raised:
                atomic_files.replace_file(self.source, self.destination)

        self.assertIs(raised.exception, error)
        replace.assert_called_once_with(self.source, self.destination)
        self.assertEqual(self.clock.elapsed, 0.0)
        self.assertEqual(self.destination.read_bytes(), b'{"version": 1}')
        self.assertEqual(self.source.read_bytes(), b'{"version": 2}')

    def test_other_errors_are_not_retried(self):
        for error in (
            windows_permission_error(87),
            OSError(errno.ENOSPC, "No space left on device"),
        ):
            with self.subTest(error=error):
                with mock.patch.object(atomic_files, "time", self.clock), mock.patch.object(
                    atomic_files.os, "replace", side_effect=error
                ) as replace:
                    with self.assertRaises(type(error)) as raised:
                        atomic_files.replace_file(self.source, self.destination)

                self.assertIs(raised.exception, error)
                replace.assert_called_once_with(self.source, self.destination)
                self.assertEqual(self.clock.elapsed, 0.0)
                self.assertEqual(self.destination.read_bytes(), b'{"version": 1}')
                self.assertEqual(self.source.read_bytes(), b'{"version": 2}')

    def test_storage_json_write_recovers_and_cleans_up_after_persistent_failure(self):
        storage = StorageManager(StorageConfig(data_root=self.root / "isolated-data"))
        path = storage.data_root / "record.json"
        old = {"schema_version": 1, "label": "原始内容"}
        new = {"schema_version": 1, "label": "重试后内容"}
        storage._write_json(path, old)
        replace = os.replace
        attempts = []

        def temporarily_blocked(source, destination):
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), new)
            self.assertEqual(json.loads(Path(destination).read_text(encoding="utf-8")), old)
            attempts.append((source, destination))
            if len(attempts) == 1:
                raise windows_permission_error(5)
            return replace(source, destination)

        with mock.patch.object(atomic_files, "time", self.clock), mock.patch.object(
            atomic_files.os, "replace", side_effect=temporarily_blocked
        ):
            storage._write_json(path, new)

        self.assertEqual(len(attempts), 2)
        self.assertEqual(storage._read_json(path), new)
        self.assertEqual(list(storage.data_root.glob(".tmp-*.json")), [])

        with mock.patch.object(atomic_files, "time", self.clock), mock.patch.object(
            atomic_files.os, "replace", side_effect=windows_permission_error(32)
        ):
            with self.assertRaises(PermissionError):
                storage._write_json(path, old)

        self.assertEqual(storage._read_json(path), new)
        self.assertEqual(list(storage.data_root.glob(".tmp-*.json")), [])


if __name__ == "__main__":
    unittest.main()
