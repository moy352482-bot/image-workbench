import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

from image_workbench import BackendResult, StorageConfig, StorageManager


TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_root = self.root / "cli-data"
        self.source = self.root / "local-input.png"
        Image.new("RGB", (14, 9), (40, 80, 120)).save(self.source, "PNG")

    def cli(self, *arguments, check=True):
        command = [sys.executable, "-X", "utf8", "-m", "image_workbench", "--data-root", str(self.data_root), *map(str, arguments)]
        result = subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"CLI returned {result.returncode}: {result.stderr}\n{result.stdout}")
        return result

    @staticmethod
    def find_id(payload, field):
        if isinstance(payload, dict):
            value = payload.get(field)
            if isinstance(value, str):
                return value
            for child in payload.values():
                found = CliTests.find_id(child, field)
                if found:
                    return found
        elif isinstance(payload, list):
            for child in payload:
                found = CliTests.find_id(child, field)
                if found:
                    return found
        return None

    def test_local_cli_project_task_run_status_outputs_flow(self):
        project_json = json.loads(self.cli("project-create", "--name", "命令行中文项目").stdout)
        project_id = self.find_id(project_json, "project_id")
        self.assertIsNotNone(project_id)

        task_json = json.loads(self.cli(
            "task-create", "--project", project_id, "--operation", "generate",
            "--prompt", "保留输入图并生成", "--input", self.source,
        ).stdout)
        task_id = self.find_id(task_json, "task_id")
        self.assertIsNotNone(task_id)

        run_json = json.loads(self.cli(
            "run", "--project", project_id, "--task", task_id,
            "--backend", "local", "--image", self.source,
        ).stdout)
        execution_id = self.find_id(run_json, "execution_id")
        self.assertIsNotNone(execution_id)
        self.assertIn("succeeded", json.dumps(run_json))

        status = json.loads(self.cli("status", "--project", project_id, "--task", task_id, "--execution", execution_id).stdout)
        self.assertIn("succeeded", json.dumps(status))
        outputs = json.loads(self.cli("outputs", "--project", project_id, "--task", task_id).stdout)
        self.assertIn("output", json.dumps(outputs))
        manager = StorageManager(StorageConfig(data_root=self.data_root))
        self.assertEqual(len(manager.list_projects()), 1)
        self.assertEqual(len([item for item in manager.list_task_artifacts(project_id, task_id) if item.kind == "output"]), 1)

    def test_codex_cli_requires_explicit_image_generation_authorization(self):
        project = StorageManager(StorageConfig(data_root=self.data_root)).create_project("Guard")
        task = StorageManager(StorageConfig(data_root=self.data_root)).create_task(project.project_id, "generate", "No implicit model call")
        result = self.cli("run", "--project", project.project_id, "--task", task.task_id, "--backend", "codex", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--allow-image-generation", result.stderr)
        self.assertEqual(TaskNoRecords(self.data_root, project.project_id, task.task_id), 0)

    def test_explicit_codex_flag_uses_backend_without_launching_real_cli(self):
        project = StorageManager(StorageConfig(data_root=self.data_root)).create_project("Explicit")
        task = StorageManager(StorageConfig(data_root=self.data_root)).create_task(project.project_id, "generate", "fake codex only")
        argv = [
            "image_workbench", "--data-root", str(self.data_root), "run", "--project", project.project_id,
            "--task", task.task_id, "--backend", "codex", "--allow-image-generation",
        ]
        calls = []

        def fake_generate(backend, request):
            calls.append(request)
            path = request.work_dir / "products" / "mocked.png"
            Image.new("RGB", (6, 5), "purple").save(path, "PNG")
            return BackendResult((path,))

        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             mock.patch("image_workbench.codex_backend.CodexImageBackend.generate", autospec=True, side_effect=fake_generate), \
             contextlib.redirect_stdout(stdout):
            try:
                runpy.run_module("image_workbench", run_name="__main__")
            except SystemExit as exc:
                self.assertEqual(exc.code, 0)
        self.assertEqual(len(calls), 1)
        result = json.loads(stdout.getvalue())
        self.assertIn("succeeded", json.dumps(result))


def TaskNoRecords(data_root, project_id, task_id):
    """Confirm a rejected CLI invocation did not initialize an execution."""
    runs = Path(data_root) / "projects" / project_id / "tasks" / task_id / "logs" / "runs"
    return len([path for path in runs.iterdir() if path.is_dir()]) if runs.exists() else 0


if __name__ == "__main__":
    unittest.main()
