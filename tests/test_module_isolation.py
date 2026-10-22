import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from image_workbench.admin_operations import source_fingerprint


class ModuleIsolationTests(unittest.TestCase):
    def test_public_exports_resolve_to_the_original_classes(self):
        import image_workbench
        for symbol, module in image_workbench._EXPORTS.items():
            with self.subTest(symbol=symbol):
                self.assertIs(getattr(image_workbench, symbol), getattr(importlib.import_module("image_workbench." + module), symbol))
                self.assertIn(symbol, dir(image_workbench))
        with self.assertRaises(AttributeError):
            getattr(image_workbench, "not_a_feature")

    def test_independent_features_do_not_import_http_or_unrelated_workers(self):
        cases = {
            "config": ["fastapi", "image_workbench.codex_backend", "image_workbench.accounts"],
            "script_service": ["fastapi", "image_workbench.workbench_state", "image_workbench.codex_backend", "image_workbench.h3_worker"],
            "qwen_lab": ["fastapi", "image_workbench.workbench_state", "image_workbench.h3_worker"],
            "remote_queue": ["fastapi", "image_workbench.web"],
            "script_remote": ["fastapi", "image_workbench.script_web"],
        }
        root = Path(__file__).resolve().parents[1]
        for module, forbidden in cases.items():
            with self.subTest(module=module):
                code = f"import importlib,sys,json; importlib.import_module('image_workbench.{module}'); print(json.dumps([m for m in {forbidden!r} if m in sys.modules]))"
                result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=root, capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [], result.stdout)

    def test_admin_report_is_invalidated_when_architecture_or_contract_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("architecture.json", "tests/contracts/http_routes.json", "tests/frontend/workbench.test.js",
                         "ruff.toml", "requirements-dev.txt", ".github/workflows/verify.yml",
                         "config/script_pricing.json", "skills/storyboard-planning/SKILL.md"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                before = source_fingerprint(root)
                path.write_text("first", encoding="utf-8")
                after_create = source_fingerprint(root)
                path.write_text("changed", encoding="utf-8")
                self.assertNotEqual(before, after_create)
                self.assertNotEqual(after_create, source_fingerprint(root))
