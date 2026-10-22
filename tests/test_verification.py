"""All release entry points fail closed when the unified verifier fails."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from image_workbench import StorageConfig, StorageManager
from image_workbench.admin_operations import AdminOperations, source_fingerprint
from image_workbench.domain_errors import ConflictError
from scripts import deploy_release, verify


class VerificationTests(unittest.TestCase):
    def test_quality_or_architecture_failure_never_starts_tests(self):
        for failed_gate in ("check_quality.py", "check_architecture.py"):
            calls = []

            def run(command, *, calls=calls, failed_gate=failed_gate, **kwargs):
                gate = Path(command[-1]).name
                calls.append(gate)
                return SimpleNamespace(returncode=7 if gate == failed_gate else 0)

            with self.subTest(gate=failed_gate), patch.object(verify.subprocess, "run", side_effect=run):
                self.assertEqual(verify.main([]), 7)
                self.assertEqual(calls[-1], failed_gate)
                self.assertNotIn("-v", calls)

    def test_missing_node_fails_instead_of_skipping_frontend(self):
        with patch.object(verify.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
                patch.object(verify.shutil, "which", return_value=None):
            self.assertEqual(verify.main([]), 1)
            self.assertEqual(run.call_count, 2)

    def test_source_change_invalidates_successful_verification(self):
        with patch.object(verify, "source_fingerprint", side_effect=[{"code": "before"}, {"code": "after"}]), \
                patch.object(verify.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                patch.object(verify.shutil, "which", return_value="node"):
            self.assertEqual(verify.main([]), 2)

    def test_cli_and_admin_execute_failing_verifier_before_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scripts = root / "scripts"
            scripts.mkdir()
            # A real subprocess, with a controlled gate failure and no tests/models.
            (scripts / "verify.py").write_text("print('Ruff: new diagnostic')\nraise SystemExit(3)\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "未上传"):
                deploy_release.validate_local(root, root / "cli.log")
            self.assertIn("Ruff: new diagnostic", (root / "cli.log").read_text())
            operations = AdminOperations(StorageManager(StorageConfig(data_root=root / "data")), source_root=root)
            with self.assertRaises(ConflictError):
                operations.execute_checks({"id": "a" * 32, "source_fingerprint": source_fingerprint(root)})
            report = operations.reports()[0]
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["total"], 0)
            self.assertEqual(report["checks"][0]["status"], "failed")
