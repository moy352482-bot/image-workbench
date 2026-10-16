import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

from image_workbench import BackendResult, LocalFileBackend, StorageConfig, StorageManager
from image_workbench.task_runner import TaskRunner
from image_workbench.worker import LocalWorker
from image_workbench.workbench_state import WorkbenchState


TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parents[1]


class CountingLocalBackend(LocalFileBackend):
    name = "local"

    def __init__(self, sources):
        super().__init__(sources)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return super().generate(request)


class CandidateBackend:
    name = "local"

    def __init__(self, path):
        self.path = path
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        target = request.work_dir / "products" / "recover-me.png"
        target.write_bytes(self.path.read_bytes())
        return BackendResult((target,))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.storage = StorageManager(StorageConfig(data_root=self.data))
        self.state = WorkbenchState(self.storage, backend="local")
        self.fixture = self.root / "generated-fixture.png"
        Image.new("RGB", (18, 12), (33, 88, 144)).save(self.fixture, "PNG")
        self.missing = self.root / "missing.png"

    @staticmethod
    def rid(char):
        return char * 32

    def create_task(self, *, name="worker project", project_id_char="a", task_id_char="b", prompt="worker task"):
        project = self.state.create_project(name, self.rid(project_id_char))
        task = self.state.create_task(project.project_id, request_id=self.rid(task_id_char), operation="generate", prompt=prompt)
        return project, task

    def queue(self, project, task, *, char="c", **kwargs):
        return self.state.submit(project.project_id, task.task_id, request_id=self.rid(char), confirmed=True, **kwargs)

    def test_serve_once_consumes_job_and_success_defaults_to_pending_review_then_approval_persists(self):
        project, task = self.create_task()
        job = self.queue(project, task)
        backend = CountingLocalBackend([self.fixture])
        LocalWorker(self.state, backend).serve(once=True)

        completed = self.state.get_job(project.project_id, task.task_id)
        self.assertEqual(completed["job_id"], job["job_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(backend.calls, 1)
        execution = self.state.runner.get_execution(project.project_id, task.task_id, completed["execution_id"])
        self.assertEqual(execution.status, "succeeded")
        self.assertEqual(len(execution.output_artifact_ids), 1)
        view = self.state.task_view(project.project_id, task.task_id)
        self.assertEqual(view["status"], "awaiting_review")
        output = view["outputs"][0]
        self.assertEqual(output["review_status"], "pending")

        dto = self.state.approve(output["artifact_id"])
        self.assertEqual(dto["review_status"], "approved")
        reloaded_storage = StorageManager(StorageConfig(data_root=self.data))
        reloaded_state = WorkbenchState(reloaded_storage, backend="local")
        self.assertEqual(reloaded_state.task_view(project.project_id, task.task_id)["status"], "approved")
        self.assertEqual(reloaded_state.artifact_dto(reloaded_storage.get_artifact(output["artifact_id"]))["review_status"], "approved")

    def test_run_next_can_be_used_without_serve(self):
        project, task = self.create_task()
        self.queue(project, task)
        backend = CountingLocalBackend([self.fixture])
        worker = LocalWorker(self.state, backend)
        self.assertTrue(worker.run_next())
        self.assertFalse(worker.run_next())
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.state.get_job(project.project_id, task.task_id)["status"], "completed")

    def test_failed_job_needs_explicit_current_retry_of_and_rejects_old_retry_id(self):
        project, task = self.create_task()
        first_job = self.queue(project, task, char="1")
        backend = CountingLocalBackend([self.missing])
        worker = LocalWorker(self.state, backend)
        self.assertTrue(worker.run_next())
        failed = self.state.get_job(project.project_id, task.task_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failure_type"], "artifact_not_found")
        with self.assertRaises(ValueError):
            self.state.submit(project.project_id, task.task_id, request_id=self.rid("2"), confirmed=True)
        with self.assertRaises(ValueError):
            self.state.submit(project.project_id, task.task_id, request_id=self.rid("2"), confirmed=True,
                              retry_of=self.rid("9"))

        retry = self.state.submit(project.project_id, task.task_id, request_id=self.rid("2"), confirmed=True,
                                  retry_of=first_job["job_id"])
        self.assertNotEqual(retry["job_id"], first_job["job_id"])
        self.assertTrue(worker.run_next())
        second_failure = self.state.get_job(project.project_id, task.task_id)
        self.assertEqual(second_failure["status"], "failed")
        with self.assertRaises(ValueError):
            self.state.submit(project.project_id, task.task_id, request_id=self.rid("3"), confirmed=True,
                              retry_of=first_job["job_id"])
        self.assertEqual(backend.calls, 2)

    def test_startup_marks_running_without_terminal_execution_uncertain_and_blocks_queue(self):
        project, active_task = self.create_task(name="uncertain project", project_id_char="4", task_id_char="5")
        queued_task = self.state.create_task(project.project_id, request_id=self.rid("6"), operation="generate", prompt="queued behind uncertain")
        active = {
            "schema_version": 1, "job_id": self.rid("7"), "request_id": self.rid("8"),
            "project_id": project.project_id, "task_id": active_task.task_id, "backend": "local",
            "action": "generate", "confirmed": True, "restore_execution_id": None,
            "previous_execution_id": None, "execution_id": None, "status": "running",
            "failure_type": None, "created_at": "2026-10-12T00:00:00+00:00",
            "started_at": "2026-10-12T00:00:01+00:00", "finished_at": None,
        }
        self.state.save_job(active)
        queued = self.queue(project, queued_task, char="9")
        backend = CountingLocalBackend([self.fixture])
        LocalWorker(self.state, backend).serve(once=True)

        self.assertEqual(self.state.get_job(project.project_id, active_task.task_id)["status"], "uncertain")
        self.assertEqual(self.state.get_job(project.project_id, queued_task.task_id)["job_id"], queued["job_id"])
        self.assertEqual(self.state.get_job(project.project_id, queued_task.task_id)["status"], "queued")
        self.assertEqual(backend.calls, 0)

    def test_archive_restore_consumes_candidate_without_calling_local_backend(self):
        project, task = self.create_task(name="restore project", project_id_char="e", task_id_char="f")
        candidate_backend = CandidateBackend(self.fixture)
        with mock.patch.object(self.storage, "register_output_image", side_effect=OSError("archive temporarily unavailable")):
            failed_record = TaskRunner(self.storage).run(project.project_id, task.task_id, candidate_backend)
        self.assertEqual(failed_record.status, "archive_failed")
        self.assertTrue(failed_record.candidates)

        job = self.state.submit(project.project_id, task.task_id, request_id=self.rid("a"),
                                restore_execution_id=failed_record.execution_id)
        self.assertEqual(job["action"], "restore")
        local_backend = CountingLocalBackend([self.fixture])
        self.assertTrue(LocalWorker(self.state, local_backend).run_next())
        completed = self.state.get_job(project.project_id, task.task_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["execution_id"], failed_record.execution_id)
        self.assertEqual(candidate_backend.calls, 1)
        self.assertEqual(local_backend.calls, 0)
        outputs = [item for item in self.storage.list_task_artifacts(project.project_id, task.task_id) if item.kind == "output"]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].artifact_id, failed_record.candidates[0].artifact_id)

    def test_file_lock_blocks_other_process_and_same_directory_worker_reentry(self):
        project, task = self.create_task(name="lock project", project_id_char="b", task_id_char="c")
        lock_path = self.storage._path(".workbench", "worker.lock")
        code = (
            "import sys; from pathlib import Path; from image_workbench.locking import FileLock, LockBusyError; "
            "p=Path(sys.argv[1]); "
            "\ntry:\n with FileLock(p, timeout=0.1): raise SystemExit(3)\n"
            "except LockBusyError: raise SystemExit(0)"
        )
        with self.state.worker_lock():
            child = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(lock_path)], cwd=REPO_ROOT,
                                   capture_output=True, text=True, timeout=10, shell=False)
        self.assertEqual(child.returncode, 0, child.stderr)

        queued = self.queue(project, task, char="d")
        backend = CountingLocalBackend([self.fixture])
        worker = LocalWorker(self.state, backend)
        self.assertTrue(worker.run_next())
        self.assertEqual(self.state.get_job(project.project_id, task.task_id)["job_id"], queued["job_id"])
        self.assertEqual(self.state.get_job(project.project_id, task.task_id)["status"], "completed")
        self.assertEqual(backend.calls, 1)


if __name__ == "__main__":
    unittest.main()
