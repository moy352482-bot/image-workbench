"""独立于 HTTP 进程的本地顺序 Worker。作业只来源于显式提交，不自动重放失败作业。"""

from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

from .locking import LockBusyError
from .storage import StorageManager, _now
from .task_runner import StateUpdateError
from .domain_errors import ConflictError
from .workbench_state import WorkbenchState


class LocalWorker:
    def __init__(self, state: WorkbenchState, backend, *, timeout_seconds=600):
        self.state, self.backend, self.timeout = state, backend, timeout_seconds
        if backend.name != state.backend:
            raise ValueError("Worker 后端与队列配置不一致")

    def _record(self, job):
        execution_id = job.get("restore_execution_id") or job["job_id"]
        try:
            return self.state.runner.get_execution(job["project_id"], job["task_id"], execution_id)
        except FileNotFoundError:
            return None

    def reconcile(self):
        with self.state.control_lock():
            for job in self.state.list_jobs():
                if job["status"] not in {"running", "uncertain"}:
                    continue
                record = self._record(job)
                if record and record.status not in {"running", "archiving"}:
                    job.update(status="completed" if record.status == "succeeded" else "failed",
                               execution_id=record.execution_id, failure_type=record.failure_type, finished_at=_now())
                else:
                    job.update(status="uncertain", failure_type="interrupted")
                self.state.save_job(job)

    def run_next(self):
        with self.state.control_lock():
            jobs = self.state.list_jobs()
            if any(job["status"] in {"running", "uncertain"} for job in jobs):
                return False
            job = next((job for job in jobs if job["status"] == "queued"), None)
            if job is None:
                return False
            if job["backend"] != self.backend.name or (job["action"] == "generate" and job.get("confirmed") is not True):
                job.update(status="failed", failure_type="prepare_failed", finished_at=_now())
                self.state.save_job(job)
                return True
            self.state.require_job_credit(job)
            job.update(status="running", started_at=_now())
            self.state.save_job(job)  # 先持久化领取事实，再启动任何子进程。
        try:
            if job["action"] == "restore":
                record = self.state.runner.retry_archive(job["project_id"], job["task_id"], job["restore_execution_id"])
            else:
                record = self.state.runner.run(
                    job["project_id"], job["task_id"], self.backend, timeout_seconds=self.timeout,
                    idempotency_key=job["job_id"], expected_previous_execution_id=job["previous_execution_id"],
                )
            job.update(status="completed" if record.status == "succeeded" else
                       ("uncertain" if record.status in {"running", "archiving"} else "failed"),
                       execution_id=record.execution_id, failure_type=record.failure_type, finished_at=_now())
        except LockBusyError:
            # 尚未进入执行器；保持已授权的同一作业，而不是创建新的生图请求。
            job.update(status="queued", started_at=None)
        except StateUpdateError as exc:
            job.update(status="uncertain", failure_type="state_update_failed", execution_id=exc.record.execution_id,
                       execution_record_fallback=asdict(exc.record), finished_at=_now())
            self._log(traceback.format_exc())
        except BaseException:
            job.update(status="uncertain", failure_type="unexpected_exception", finished_at=_now())
            self._log(traceback.format_exc())
        with self.state.control_lock():
            self.state.save_job(job)
        return True

    def _log(self, message):
        with self.state.storage.files.path(".workbench", "worker-errors.log").open("a", encoding="utf-8") as stream:
            stream.write(f"\n{_now()}\n{message}\n")

    def serve(self, *, stop_event=None, once=False):
        with self.state.worker_lock():
            self.state.storage.files.write_json(self.state.storage.files.path(".workbench", "worker.json"), {
                "schema_version": 1, "pid": os.getpid(), "backend": self.backend.name, "started_at": _now(),
            })
            self.reconcile()
            while stop_event is None or not stop_event.is_set():
                self.run_next()
                if once:
                    break
                self.reconcile()
                if stop_event:
                    stop_event.wait(0.5)
                else:
                    time.sleep(0.5)


def confirm_stopped(state, project_id, task_id):
    """只能由明确确认子进程已停止的本地 CLI 操作者调用；本方法不终止进程。"""
    with state.control_lock():
        job = state.get_job(project_id, task_id)
        if not job or (job["status"] != "uncertain" and not (job.get("worker_id") and job["status"] == "running")):
            raise ConflictError("只有状态待确认的作业可以人工解除")
        execution_id = job.get("restore_execution_id") or job["job_id"]
        try:
            record = state.runner.get_execution(project_id, task_id, execution_id)
        except FileNotFoundError:
            record = None
        if record and record.status in {"running", "archiving"}:
            state.runner.mark_interrupted(project_id, task_id, record.execution_id)
        job.update(status="failed", failure_type="interrupted", finished_at=_now())
        state.save_job(job)
        return job


def ensure_worker(state, *, local_images=(), timeout_seconds=600):
    if state.worker_running():
        info = state.storage.files.read_json(state.storage.files.path(".workbench", "worker.json"))
        if info["backend"] != state.backend:
            raise ConflictError("该数据目录已有不同后端的 Worker；请使用独立测试数据目录")
        return
    command = [sys.executable, "-m", "image_workbench", "--data-root", str(state.storage.data_root),
               "worker", "--backend", state.backend, "--timeout", str(timeout_seconds)]
    for path in local_images:
        command.extend(["--image", str(Path(path).absolute())])
    options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    log_path = state.storage.files.path(".workbench", "worker-process.log")
    with log_path.open("ab") as stream:
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=stream, shell=False, **options)
    for _ in range(100):
        if state.worker_running():
            return
        if process.poll() is not None:
            raise RuntimeError("Worker 启动失败，详见本地 worker-process.log")
        time.sleep(0.05)
    raise RuntimeError("Worker 启动状态尚未确认，请检查本地日志")
