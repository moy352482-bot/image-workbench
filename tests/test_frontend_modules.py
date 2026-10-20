from pathlib import Path
import shutil
import subprocess
import unittest


class FrontendModuleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for browser-component contracts")
    def test_component_behavior_contracts(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), "--test", "tests/frontend/workbench.test.js", "tests/frontend/storyboard.test.js"],
                                cwd=root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax checks")
    def test_all_shipped_javascript_parses(self):
        root = Path(__file__).resolve().parents[1]
        for path in sorted((root / "image_workbench/web_static").glob("*.js")):
            with self.subTest(file=path.name):
                result = subprocess.run([shutil.which("node"), "--check", str(path)],
                                        capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(result.returncode, 0, result.stderr)
