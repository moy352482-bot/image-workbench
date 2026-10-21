from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import unittest

from PIL import Image

from image_workbench import BackendResult, StorageConfig, StorageManager, TaskRunner
from image_workbench.locking import LockBusyError
from image_workbench.workbench_state import WorkbenchState
from image_workbench.workbench_state import ConflictError


TEST_DIR = Path(__file__).resolve().parent


class CountingBackend:
    name = "local"

    def __init__(self, *, delay=0):
        self.calls = 0
        self.delay = delay

    def generate(self, request):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        path = request.work_dir / "products" / "generated.png"
        Image.new("RGB", (11, 8), (110, 80, 50)).save(path, "PNG")
        return BackendResult((path,))


class WorkbenchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.storage = StorageManager(StorageConfig(data_root=self.data))
        self.state = WorkbenchState(self.storage, backend="local")
        self.asset = self.root / "input.png"
        Image.new("RGB", (14, 10), (20, 70, 120)).save(self.asset, "PNG")

    @staticmethod
    def request_id(char):
        return char * 32

    def new_project(self, name="Workbench project"):
        return self.state.create_project(name, self.request_id("a"))

    def new_task(self, project_id, *, request_id="b", operation="generate", prompt="make a picture", base=None, inputs=()):
        return self.state.create_task(project_id, request_id=self.request_id(request_id), operation=operation,
                                      prompt=prompt, base_artifact_id=base, inputs=inputs)

    def test_project_request_id_is_idempotent_and_conflicting_content_is_rejected(self):
        request_id = self.request_id("c")
        first = self.state.create_project("同一保存意图", request_id)
        replay = self.state.create_project("同一保存意图", request_id)
        self.assertEqual(first, replay)
        self.assertEqual(len(self.storage.list_projects()), 1)
        with self.assertRaises(ConflictError):
            self.state.create_project("改过的名字", request_id)
        self.assertEqual(self.storage.get_project(request_id), first)

    def test_task_request_id_survives_state_restart_and_conflicts_on_changed_content(self):
        project = self.new_project()
        request_id = self.request_id("d")
        first = self.state.create_task(project.project_id, request_id=request_id, operation="generate",
                                       prompt="生成同一张图", aspect_ratio="3:2", inputs=[self.asset])
        restarted_storage = StorageManager(StorageConfig(data_root=self.data))
        restarted_state = WorkbenchState(restarted_storage, backend="local")
        replay = restarted_state.create_task(project.project_id, request_id=request_id, operation="generate",
                                             prompt="生成同一张图", aspect_ratio="3:2", inputs=[self.asset])
        self.assertEqual(first, replay)
        self.assertEqual(len(restarted_storage.list_tasks(project.project_id)), 1)
        with self.assertRaises(ConflictError):
            restarted_state.create_task(project.project_id, request_id=request_id, operation="generate",
                                        prompt="内容被修改", aspect_ratio="3:2", inputs=[self.asset])

    def test_invalid_input_content_is_rejected_and_old_output_can_be_selected_as_branch_base(self):
        project = self.new_project()
        broken = self.root / "broken.png"
        broken.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            self.new_task(project.project_id, inputs=[broken])
        self.assertEqual(self.storage.list_tasks(project.project_id), [])

        generate = self.new_task(project.project_id, request_id="e")
        first_path = self.root / "first.png"
        second_path = self.root / "second.png"
        Image.new("RGB", (10, 10), "red").save(first_path, "PNG")
        Image.new("RGB", (10, 10), "blue").save(second_path, "PNG")
        older = self.storage.register_output_image(project.project_id, generate.task_id, first_path)
        newer = self.storage.register_output_image(project.project_id, generate.task_id, second_path)
        self.assertNotEqual(older.artifact_id, newer.artifact_id)
        edit = self.state.create_task(project.project_id, request_id=self.request_id("f"), operation="edit",
                                      prompt="以早期版本另开分支", base_artifact_id=older.artifact_id)
        self.assertEqual(edit.base_artifact_id, older.artifact_id)
        self.assertNotEqual(edit.base_artifact_id, newer.artifact_id)

    def test_concurrent_different_submit_ids_for_one_task_create_only_one_job(self):
        project = self.new_project()
        task = self.new_task(project.project_id)
        request_ids = [f"{index:032x}" for index in range(1, 13)]

        def submit(request_id):
            return self.state.submit(project.project_id, task.task_id, request_id=request_id, confirmed=True)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, request_ids))
        self.assertEqual(len({item["job_id"] for item in results}), 1)
        self.assertEqual(len(self.state.list_jobs()), 1)
        self.assertEqual(self.state.get_job(project.project_id, task.task_id)["status"], "queued")

    def test_task_runner_same_idempotency_key_runs_backend_once_and_rejects_stale_previous_id(self):
        project = self.new_project()
        task = self.new_task(project.project_id)
        runner_a, runner_b = TaskRunner(self.storage), TaskRunner(self.storage)
        backend = CountingBackend(delay=0.12)
        key = self.request_id("1")

        def run(runner):
            try:
                return runner.run(project.project_id, task.task_id, backend, idempotency_key=key,
                                  expected_previous_execution_id=None)
            except LockBusyError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(run, (runner_a, runner_b)))
        successful = [record for record in records if record is not None]
        self.assertEqual(len(successful), 1)
        replay = runner_a.run(project.project_id, task.task_id, backend, idempotency_key=key,
                              expected_previous_execution_id=None)
        self.assertEqual(replay, successful[0])
        self.assertEqual(replay.execution_id, key)
        self.assertEqual(backend.calls, 1)
        with self.assertRaises(ValueError):
            runner_a.run(project.project_id, task.task_id, backend, idempotency_key=self.request_id("2"),
                         expected_previous_execution_id=None)
        self.assertEqual(backend.calls, 1)

    def test_project_task_records_remain_isolated(self):
        project_a = self.new_project("A")
        project_b = self.state.create_project("B", self.request_id("9"))
        task_a = self.new_task(project_a.project_id, request_id="7", prompt="task A")
        task_b = self.state.create_task(project_b.project_id, request_id=self.request_id("8"), operation="generate", prompt="task B")
        state_after_restart = WorkbenchState(StorageManager(StorageConfig(data_root=self.data)), backend="local")
        self.assertEqual(state_after_restart.storage.get_task(project_a.project_id, task_a.task_id).prompt, "task A")
        self.assertEqual(state_after_restart.storage.get_task(project_b.project_id, task_b.task_id).prompt, "task B")
        self.assertNotEqual(task_a.task_id, task_b.task_id)
        self.assertEqual(state_after_restart.task_options(project_a.project_id, task_a.task_id)["task_id"], task_a.task_id)
        self.assertEqual(state_after_restart.task_options(project_b.project_id, task_b.task_id)["task_id"], task_b.task_id)


if __name__ == "__main__":
    unittest.main()
