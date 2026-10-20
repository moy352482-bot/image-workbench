import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_architecture import analyze, cycles, imports


class ArchitectureTests(unittest.TestCase):
    def test_repository_dependency_contracts(self):
        result = analyze()
        self.assertEqual(result["errors"], [], "\n".join(result["errors"]))

    def test_conditional_relative_and_absolute_imports_are_checked(self):
        found = list(imports("if True:\n    from ..scripts import run\nimport app.http\nfrom app import web\n", "app.http.routes"))
        self.assertIn((2, "app.scripts", "run"), found)
        self.assertIn((3, "app.http", None), found)
        self.assertIn((4, "app", "web"), found)

    def test_cycle_reports_dependency_path(self):
        self.assertEqual(cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": set()}), [["a", "b", "c", "a"]])

    def test_forbidden_import_and_unregistered_module_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / "app"
            package.mkdir()
            (root / "architecture.json").write_text(json.dumps({
                "root_package": "app", "groups": {
                    "core": {"modules": ["core"], "may_import": []},
                    "web": {"modules": ["web"], "may_import": ["core"]}
                }, "http_framework_groups": ["web"]
            }), encoding="utf-8")
            (package / "core.py").write_text("from . import web\nfrom fastapi import Request\n", encoding="utf-8")
            (package / "web.py").write_text("", encoding="utf-8")
            (package / "unregistered.py").write_text("", encoding="utf-8")
            errors = "\n".join(analyze(root)["errors"])
            self.assertIn("core -> web is forbidden", errors)
            self.assertIn("imports HTTP framework", errors)
            self.assertIn("Register the module", errors)
