import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from PIL import Image

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import IdentityStore
from image_workbench.project_manager import ProjectManager


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backup_workbench", ROOT / "scripts" / "backup_workbench.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iw-backup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # Cleanup only this test-owned tree, using the same OS namespace needed
        # to remove genuinely long fixture paths on Windows.
        if os.name == "nt":
            self.temp.name = "\\\\?\\" + str(self.root)
        self.source, self.bundle, self.restored = (self.root / name for name in ("source", "bundle", "restored"))
        self.source.mkdir()
        (self.source / "empty").mkdir()
        (self.source / "metadata.json").write_text('{"fixture":true}', encoding="utf-8")

    def create(self):
        return backup.backup(self.source, self.bundle, writers_stopped=True)

    def read_manifest(self):
        return json.loads((self.bundle / backup.MANIFEST).read_text(encoding="utf-8"))

    def write_manifest(self, manifest):
        (self.bundle / backup.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    def test_roundtrip_preserves_accounts_ownership_wallet_ledger_ids_and_image(self):
        storage = StorageManager(StorageConfig(data_root=self.source))
        projects = ProjectManager(storage)
        identity = IdentityStore(storage)
        alice = identity.register("backup_alice", "fixture-password-123")
        bob = identity.register("backup_bobby", "fixture-password-456")
        token = identity.login("backup_alice", "fixture-password-123")
        wid = identity.workspaces(alice["account_id"])[0]["workspace_id"]
        project = projects.create_main_project("隔离恢复项目")
        identity.assign_project(project.main_project_id, wid)
        task = projects.create_task(projects.default_subproject(project.main_project_id).subproject_id, "generate", "synthetic backup fixture")
        image = self.root / "fixture.png"
        Image.new("RGB", (16, 12), "green").save(image)
        artifact = storage.register_output_image(task.project_id, task.task_id, image)
        job = {"job_id": uuid4().hex, "request_id": uuid4().hex, "task_id": task.task_id,
               "project_id": task.project_id, "status": "queued"}
        identity.reserve(job, {"account_id": alice["account_id"], "workspace_id": wid}, 40)
        job["status"] = "completed"
        identity.record_job(job)
        wallet, ledger = identity.wallet(alice["account_id"]), identity.ledger(alice["account_id"])
        before = backup._inventory(self.source)[-1]
        summary = self.create()
        self.assertEqual(summary["sqlite_databases"], 1)
        self.assertEqual(backup.verify(self.bundle), summary)
        self.assertEqual(backup.restore(self.bundle, self.restored), summary)
        restored_storage = StorageManager(StorageConfig(data_root=self.restored))
        restored_identity = IdentityStore(restored_storage)
        self.assertEqual(restored_identity.wallet(alice["account_id"]), wallet)
        self.assertEqual(restored_identity.ledger(alice["account_id"]), ledger)
        self.assertEqual(restored_identity.ownership(project.main_project_id)["account_id"], alice["account_id"])
        self.assertEqual(restored_identity.session(token)["account_id"], alice["account_id"])
        self.assertTrue(restored_identity.login("backup_bobby", "fixture-password-456"))
        self.assertEqual(restored_identity.wallet(bob["account_id"])["remaining_credit"], 200)
        self.assertEqual(restored_storage.get_artifact(artifact.artifact_id), artifact)
        self.assertEqual(restored_storage.get_artifact_path(artifact.artifact_id).read_bytes(), image.read_bytes())
        self.assertEqual(ProjectManager(restored_storage).get_main_project(project.main_project_id), project)
        self.assertEqual(backup._inventory(self.source)[-1], before)
        self.assertTrue((self.restored / "empty").is_dir())

    def test_sqlite_snapshot_includes_committed_wal_and_excludes_uncommitted_transaction(self):
        path = self.source / "records.sqlite3"
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE records(value TEXT)")
        db.execute("INSERT INTO records VALUES ('committed')")
        db.commit()
        self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
        db.execute("INSERT INTO records VALUES ('not committed')")
        self.create()
        with contextlib.closing(sqlite3.connect(self.bundle / "data" / path.name)) as copied:
            self.assertEqual(copied.execute("SELECT * FROM records").fetchall(), [("committed",)])
        self.assertFalse(any(name.endswith(("-wal", "-shm", "-journal")) for name in self.read_manifest()["files"]))
        db.rollback()

    def test_stop_writers_acknowledgement_required_before_any_output(self):
        with self.assertRaisesRegex(ValueError, "writers-stopped"):
            backup.backup(self.source, self.bundle)
        self.assertFalse(self.bundle.exists())

    def test_existing_backup_or_restore_target_never_overwritten(self):
        self.create()
        manifest_before = (self.bundle / backup.MANIFEST).read_bytes()
        with self.assertRaises(FileExistsError):
            self.create()
        self.restored.mkdir()
        sentinel = self.restored / "existing"
        sentinel.write_text("keep")
        with self.assertRaises(FileExistsError):
            backup.restore(self.bundle, self.restored)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual((self.bundle / backup.MANIFEST).read_bytes(), manifest_before)

    def test_nested_or_ancestor_destination_is_rejected(self):
        for target in (self.source, self.source / "nested", self.root):
            with self.subTest(target=target), self.assertRaises(ValueError):
                backup.backup(self.source, target, writers_stopped=True)
        self.create()
        with self.assertRaises(ValueError):
            backup.restore(self.bundle, self.bundle / "nested")

    def test_source_change_during_copy_leaves_incomplete_unusable_backup(self):
        copy = backup._copy_checked
        def mutate(*args):
            copy(*args)
            (self.source / "metadata.json").write_text("changed after copying")
        with mock.patch.object(backup, "_copy_checked", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "源数据"):
                self.create()
        self.assertTrue((self.bundle / backup.INCOMPLETE).is_file())
        with self.assertRaises(ValueError):
            backup.restore(self.bundle, self.restored)
        self.assertFalse(self.restored.exists())

    def test_tamper_missing_and_extra_files_block_restore_before_target_creation(self):
        self.create()
        target = self.bundle / "data" / "metadata.json"
        original = target.read_bytes()
        for kind in ("tamper", "missing", "extra"):
            with self.subTest(kind=kind):
                target.write_bytes(original)
                extra = self.bundle / "data" / "unexpected"
                if kind == "tamper":
                    target.write_bytes(b"wrong")
                elif kind == "missing":
                    target.unlink()
                else:
                    extra.write_bytes(b"extra")
                with self.assertRaises(ValueError):
                    backup.restore(self.bundle, self.restored)
                self.assertFalse(self.restored.exists())
                extra.unlink(missing_ok=True)

    def test_manifest_traversal_ads_and_case_collision_block_restore(self):
        self.create()
        original = self.read_manifest()
        for name in ("../escape", "/absolute", "data:stream", "a\\b", "a//b", "NUL", "metadata.json.", "METADATA.JSON"):
            with self.subTest(name=name):
                manifest = json.loads(json.dumps(original))
                manifest["files"][name] = manifest["files"]["metadata.json"]
                self.write_manifest(manifest)
                with self.assertRaises(ValueError):
                    backup.restore(self.bundle, self.restored)
                self.assertFalse(self.restored.exists())

    def test_damaged_identity_and_sqlite_foreign_key_violations_fail_backup(self):
        identity = self.source / ".identity"
        identity.mkdir()
        path = identity / "accounts.sqlite3"
        path.write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.bundle.exists())
        path.unlink()
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript("CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(id INTEGER REFERENCES parent); INSERT INTO child VALUES (42);")
        with self.assertRaisesRegex(ValueError, "外键"):
            self.create()
        self.assertTrue((self.bundle / backup.INCOMPLETE).exists())

    def test_orphan_sqlite_sidecar_is_rejected(self):
        (self.source / "missing.sqlite3-wal").write_bytes(b"wal")
        with self.assertRaisesRegex(ValueError, "日志"):
            self.create()
        self.assertFalse(self.bundle.exists())

    def test_unreadable_directory_is_not_silently_omitted(self):
        blocked = self.source / "unreadable"
        blocked.mkdir()
        (blocked / "important").write_bytes(b"keep")
        scandir = os.scandir
        def deny(path):
            if backup._absolute_path(path) == backup._absolute_path(blocked):
                raise PermissionError("fixture inaccessible subtree")
            return scandir(path)
        with mock.patch.object(os, "scandir", side_effect=deny):
            with self.assertRaises(PermissionError):
                self.create()
        self.assertFalse(self.bundle.exists())

    @staticmethod
    def fixture_path(path):
        # Fixtures use an independent explicit prefix. Public backup/verify/
        # restore calls below receive ordinary user-facing paths.
        return Path("\\\\?\\" + str(path)) if os.name == "nt" else path

    def test_263_character_output_file_backup_verify_restore(self):
        source = self.root / "s"
        (source / "files").mkdir(parents=True)
        bundle = self.root / ("b" * 56)
        parent = bundle / "data" / "files"
        name = "x" * (263 - len(str(parent)) - 1 - 4) + ".bin"
        original = source / "files" / name
        original.write_bytes(b"synthetic 263-character path fixture")
        self.assertEqual(len(str(original)), 203)
        self.assertEqual(len(str(parent / name)), 263)
        summary = backup.backup(source, bundle, writers_stopped=True)
        self.assertEqual(backup.verify(bundle), summary)
        restored = self.root / ("r" * 70)
        self.assertEqual(backup.restore(bundle, restored), summary)
        self.assertEqual(self.fixture_path(restored / "files" / name).read_bytes(), original.read_bytes())
        with self.assertRaises(FileExistsError):
            backup.restore(bundle, restored)
        manifest = json.loads((bundle / backup.MANIFEST).read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["files"]), {"files/" + name})

    def test_long_source_target_roots_directories_and_sqlite_wal_roundtrip(self):
        parent = self.root / ("p" * 120) / ("q" * 120)
        source, bundle, restored = (parent / name for name in ("source", "bundle", "restored"))
        self.assertGreater(len(str(parent)), 260)
        native_source = self.fixture_path(source)
        native_source.mkdir(parents=True)
        nested = Path("nested") / ("d" * 120) / ("e" * 80)
        (native_source / nested / "empty").mkdir(parents=True)
        text_name = nested / "spaces #percent% 中文.txt"
        (native_source / text_name).write_bytes(b"synthetic long directories")
        (native_source / ".identity").mkdir()
        database = native_source / ".identity" / "accounts.sqlite3"
        db = sqlite3.connect(database)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.executescript("CREATE TABLE fixture(value TEXT); INSERT INTO fixture VALUES ('committed WAL');")
        self.assertGreater(Path(str(database) + "-wal").stat().st_size, 0)
        before = backup._inventory(source)[-1]
        summary = backup.backup(source, bundle, writers_stopped=True)
        self.assertEqual(summary["sqlite_databases"], 1)
        self.assertEqual(backup.verify(bundle), summary)
        self.assertEqual(backup.restore(bundle, restored), summary)
        self.assertEqual((self.fixture_path(restored) / text_name).read_bytes(), b"synthetic long directories")
        self.assertTrue((self.fixture_path(restored) / nested / "empty").is_dir())
        with contextlib.closing(sqlite3.connect(backup._sqlite_uri(restored / ".identity" / "accounts.sqlite3"), uri=True)) as copied:
            self.assertEqual(copied.execute("SELECT * FROM fixture").fetchall(), [("committed WAL",)])
            with self.assertRaises(sqlite3.OperationalError):
                copied.execute("INSERT INTO fixture VALUES ('must remain read-only')")
        self.assertEqual(backup._inventory(source)[-1], before)

    def test_sqlite_uri_escapes_filename_and_never_creates_missing_source(self):
        path = self.source / "#literal% escaped 中文.sqlite3"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.executescript("CREATE TABLE fixture(value TEXT); INSERT INTO fixture VALUES ('safe URI');")
        with contextlib.closing(sqlite3.connect(backup._sqlite_uri(path), uri=True)) as db:
            self.assertEqual(db.execute("SELECT * FROM fixture").fetchall(), [("safe URI",)])
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM fixture")
        missing = self.source / "#missing% no-create.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            sqlite3.connect(backup._sqlite_uri(missing), uri=True)
        self.assertFalse(missing.exists())

    @unittest.skipUnless(os.name == "nt", "Windows 路径命名空间专用检查")
    def test_windows_extended_paths_normalize_without_network_or_device_access(self):
        # Pure normalization: no UNC server is contacted by this test.
        self.assertEqual(backup._absolute_path(r"C:\safe\more\..\target"), Path(r"\\?\C:\safe\target"))
        self.assertEqual(backup._absolute_path(r"\\?\C:\safe\more\..\target"), Path(r"\\?\C:\safe\target"))
        self.assertEqual(backup._absolute_path(r"\\server\share\folder"), Path(r"\\?\UNC\server\share\folder"))
        self.assertEqual(backup._absolute_path(r"\\?\UNC\server\share\folder"), Path(r"\\?\UNC\server\share\folder"))
        for path in (r"\\.\PhysicalDrive0", r"//./PhysicalDrive0", r"\??\C:\target", r"\\?\GLOBALROOT\Device", r"\\?\Volume{fixture}\target", r"\\?\C:relative", r"\\?\UNC\server", r"C:\ambiguous.\target", r"ambiguous.\target", r"C:\file:stream"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                backup._absolute_path(path)

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 路径别名专用检查")
    def test_windows_short_name_alias_cannot_overlap_backup_or_restore_either_direction(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetShortPathNameW.restype = wintypes.DWORD

        def short_path(path):
            size = kernel.GetShortPathNameW(str(path), None, 0)
            if not size:
                raise ctypes.WinError(ctypes.get_last_error())
            output = ctypes.create_unicode_buffer(size)
            if not kernel.GetShortPathNameW(str(path), output, size):
                raise ctypes.WinError(ctypes.get_last_error())
            alias = Path(output.value)
            if alias == path:
                self.skipTest("当前临时卷没有为此目录生成 8.3 别名")
            self.assertTrue(os.path.samefile(alias, path))
            return alias

        alias = short_path(self.source)
        self.assertFalse(backup._absolute_path(alias).is_relative_to(backup._absolute_path(self.source)))
        before = backup._inventory(self.source)[-1]
        # Target physically within source, while lexical spellings disagree.
        with self.assertRaises(ValueError):
            backup.backup(self.source, alias / "not-created", writers_stopped=True)
        self.assertFalse((self.source / "not-created").exists())
        # Source physically within existing target, spelled with short names.
        with self.assertRaises(ValueError):
            backup.backup(alias, self.root, writers_stopped=True)
        self.assertEqual(backup._inventory(self.source)[-1], before)

        self.create()
        bundle_alias = short_path(self.bundle)
        with self.assertRaises(ValueError):
            backup.restore(self.bundle, bundle_alias / "not-restored")
        self.assertFalse((self.bundle / "not-restored").exists())
        with self.assertRaises(ValueError):
            backup.restore(bundle_alias, self.root)
        self.assertEqual(backup.verify(self.bundle)["files"], 1)

    def test_symbolic_link_or_windows_junction_cannot_import_external_files(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("private")
        link = self.source / "external"
        if os.name == "nt":
            # Junction creation requires no special symlink privilege.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.addCleanup(lambda: link.rmdir() if os.name == "nt" else link.unlink())
        with self.assertRaisesRegex(ValueError, "重解析点"):
            self.create()
        self.assertFalse(self.bundle.exists())
        self.assertEqual((outside / "secret").read_text(), "private")

    def test_restore_disk_failure_preserves_incomplete_marker_and_source(self):
        self.create()
        with mock.patch.object(backup, "_copy_checked", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                backup.restore(self.bundle, self.restored)
        self.assertTrue((self.restored / backup.INCOMPLETE).is_file())
        self.assertEqual(backup.verify(self.bundle)["files"], 1)
        with self.assertRaises(ValueError):
            backup.backup(self.restored, self.root / "bad-copy", writers_stopped=True)

    def test_private_container_creation_failure_aborts_before_copy(self):
        with mock.patch.object(backup, "_private_mkdir", side_effect=PermissionError("ACL failure")):
            with self.assertRaises(PermissionError):
                self.create()
        self.assertFalse(self.bundle.exists())

    def test_new_container_is_private_without_changing_parent_permissions(self):
        if os.name == "nt":
            script = "$ErrorActionPreference='Stop'; $acl = Get-Acl -LiteralPath $env:IW_BACKUP_TEST_PATH; [pscustomobject]@{protected=$acl.AreAccessRulesProtected;sddl=$acl.Sddl;owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;rules=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]) | ForEach-Object {$_.IdentityReference.Value})} | ConvertTo-Json -Compress"
            def acl(path):
                # PowerShell 7's module path cannot be inherited by Windows
                # PowerShell 5; let the latter construct its own module path.
                environment = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
                result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    env={**environment, "IW_BACKUP_TEST_PATH": str(path)}, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            before = acl(self.root)["sddl"]
            self.create()
            rule = acl(self.bundle)
            self.assertTrue(rule["protected"])
            self.assertEqual(len(rule["rules"]), 3)
            self.assertIn("S-1-5-18", rule["rules"])
            self.assertIn("S-1-5-32-544", rule["rules"])
            self.assertIn(rule["owner"], rule["rules"])
            self.assertNotIn("S-1-1-0", rule["rules"])
            child = acl(self.bundle / "data" / "metadata.json")
            self.assertEqual(child["owner"], rule["owner"])
            # Python may express mode 0600 with the OWNER RIGHTS well-known SID.
            self.assertTrue(set(child["rules"]) <= set(rule["rules"]) | {"S-1-3-4"})
            self.assertEqual(acl(self.root)["sddl"], before)
        else:
            before = self.root.stat().st_mode
            self.create()
            self.assertEqual(stat.S_IMODE(self.bundle.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.bundle / "data" / "metadata.json").stat().st_mode), 0o600)
            self.assertEqual(self.root.stat().st_mode, before)

    def test_cli_never_prints_private_data_and_never_collects_sibling_credentials(self):
        marker = "secret-token-must-not-be-logged"
        (self.source / "activation.txt").write_text(marker)
        (self.root / "auth.json").write_text(marker)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(backup.main(["backup", "--data-root", str(self.source), "--destination", str(self.bundle), "--writers-stopped"]), 0)
            self.assertEqual(backup.main(["restore", "--backup", str(self.bundle), "--destination", str(self.source)]), 1)
        self.assertNotIn(marker, output.getvalue() + errors.getvalue())
        self.assertNotIn(str(self.root), output.getvalue() + errors.getvalue())
        self.assertNotIn("auth.json", self.read_manifest()["files"])
        self.assertTrue(json.loads(output.getvalue())["contains_private_data"])


if __name__ == "__main__":
    unittest.main()
