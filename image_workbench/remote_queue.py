"""持久远程领取协议。断线不释放已领取任务，避免重复消耗生图额度。"""

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import tempfile
import time
from uuid import UUID, uuid4, uuid5

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .locking import FileLock
from .storage import _now, _validate_id
from .task_runner import CandidateRecord, ExecutionRecord
from .scheduling import ACTIVE_STATUSES
from .workbench_state import ConflictError, FAILURE_MESSAGES

ID = r"^[0-9a-f]{32}$"
HASH = r"^[0-9a-f]{64}$"


def atomic_bytes(storage, path, data):
    storage._check_path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            storage._cleanup(temporary)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(pattern=ID)
    backend: str = Field(pattern=r"^(codex|local)$")


class Complete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(succeeded|failed|uncertain)$")
    hashes: list[str] = Field(default_factory=list, max_length=32)
    failure_type: str | None = None


class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(pattern=ID)


class RemoteQueue:
    def __init__(self, state):
        self.state, self.storage = state, state.storage
        self.root = self.storage._path(".workbench", "remote")
        self.root.mkdir(exist_ok=True)
        self.workers = self.storage._check_path(self.root / "workers")
        self.workers.mkdir(exist_ok=True)

    def heartbeat(self, worker_id):
        info = {
            "schema_version": 1, "worker_id": _validate_id(worker_id), "time": time.time(),
        }
        self.storage._write_json(self.workers / (worker_id + ".json"), info)
        self.storage._write_json(self.root / "heartbeat.json", info)

    def _owned(self, job_id, worker_id, claim_id):
        for value in (job_id, worker_id, claim_id):
            _validate_id(value)
        job = next((job for job in self.state.list_jobs() if job["job_id"] == job_id), None)
        if not job:
            raise FileNotFoundError("作业不存在或已被显式重试替代")
        if job.get("worker_id") != worker_id or job.get("claim_id") != claim_id:
            raise ConflictError("作业不属于此执行器")
        return job

    def _manifest(self, job):
        task = self.storage.get_task(job["project_id"], job["task_id"])
        inputs = [item for item in self.storage.list_task_artifacts(task.project_id, task.task_id) if item.kind == "input"]
        def asset(item):
            return {key: getattr(item, key) for key in ("artifact_id", "sha256", "size_bytes", "format")}
        return {"schema_version": 1, "job_id": job["job_id"], "claim_id": job["claim_id"],
                "task": asdict(task), "backend": job["backend"], "action": job["action"],
                "inputs": [asset(item) for item in inputs],
                "base": asset(self.storage.get_artifact(task.base_artifact_id)) if task.base_artifact_id else None,
                "max_image_bytes": self.storage.config.max_image_bytes}

    def claim(self, worker_id, backend):
        with self.state.control_lock():
            if backend != self.state.backend:
                raise ConflictError("云端与本机后端模式不一致")
            jobs = self.state.list_jobs()
            active = next((j for j in jobs if j["status"] in ACTIVE_STATUSES
                           and j.get("worker_id") == worker_id), None)
            if active:
                self.heartbeat(worker_id)
                return self._manifest(active)
            job = self.state.scheduling_policy.choose(jobs, execution_mode=self.state.execution_mode)
            if not job:
                # 被其他执行器占满时，空轮询不能冒充正在执行任务的心跳。
                if not any(j["status"] in ACTIVE_STATUSES for j in jobs):
                    self.heartbeat(worker_id)
                return None
            if job["backend"] != backend or (job["action"] == "generate" and job.get("confirmed") is not True):
                raise ConflictError("作业未经明确提交或后端不匹配")
            self.state.require_job_credit(job)
            job.update(status="running", worker_id=worker_id, claim_id=uuid4().hex, started_at=_now())
            self.state.save_job(job)
            self.heartbeat(worker_id)
            return self._manifest(job)

    def beat(self, job_id, worker_id, claim_id):
        with self.state.control_lock():
            self._owned(job_id, worker_id, claim_id)
            self.heartbeat(worker_id)

    def start(self, job_id, worker_id, claim_id, attempt_id):
        with self.state.control_lock():
            job = self._owned(job_id, worker_id, claim_id)
            _validate_id(attempt_id)
            if job["status"] != "running" or job["action"] != "generate" or job.get("remote_result"):
                raise ConflictError("此作业不能启动生成")
            if job.get("attempt_id") and job["attempt_id"] != attempt_id:
                raise ConflictError("此作业已经开始执行，不能使用新的本机检查点重复生成")
            self.state.require_job_credit(job)
            job["attempt_id"] = attempt_id
            self.state.save_job(job)
            return {"ok": True}

    def asset(self, job_id, worker_id, claim_id, artifact_id):
        with self.state.control_lock():
            job = self._owned(job_id, worker_id, claim_id)
            manifest = self._manifest(job)
            allowed = [*manifest["inputs"], *([manifest["base"]] if manifest["base"] else [])]
            item = next((a for a in allowed if a["artifact_id"] == artifact_id), None)
            if not item:
                raise FileNotFoundError("图片不属于此作业")
            data = self.storage.get_artifact_path(artifact_id).read_bytes()
            if len(data) != item["size_bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("输入图片已改变")
            return data

    def upload(self, job_id, worker_id, claim_id, index, data):
        if not 0 <= index < 32 or not 0 < len(data) <= self.storage.config.max_image_bytes:
            raise ValueError("产物数量或大小超过限制")
        digest = hashlib.sha256(data).hexdigest()
        with self.state.control_lock():
            job = self._owned(job_id, worker_id, claim_id)
            directory = self.storage._check_path(self.root / job_id)
            directory.mkdir(exist_ok=True)
            path = self.storage._check_path(directory / f"{index:02d}.image")
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ConflictError("同一产物位置已有不同内容")
                return digest
            if job["status"] not in {"running", "uncertain"} or job.get("remote_result"):
                raise ConflictError("作业结果已经固定")
            atomic_bytes(self.storage, path, data)
            try:
                self.storage._inspect_image(path)
            except Exception:
                self.storage._cleanup(path)
                raise
            return digest

    def _archive(self, job, hashes):
        """只导入已经上传的产物；崩溃后使用固定图片 ID 继续登记，绝不启动模型。"""
        runner = self.state.runner
        pid, tid, eid = job["project_id"], job["task_id"], job["job_id"]
        with FileLock(self.storage._path(".execution.lock"), timeout=30):
            directory = runner._directory(pid, tid, eid)
            directory.mkdir(parents=True, exist_ok=True)
            products = self.storage._check_path(directory / "work" / "products")
            products.mkdir(parents=True, exist_ok=True)
            record_path = directory / "execution.json"
            record = runner.get_execution(pid, tid, eid) if record_path.exists() else None
            if record and record.status == "succeeded":
                return record
            candidates = []
            for index, digest in enumerate(hashes):
                source = self.storage._check_path(self.root / eid / f"{index:02d}.image")
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("上传图片与结果清单不一致")
                target = self.storage._check_path(products / f"{index:02d}.image")
                candidates.append(CandidateRecord(target.relative_to(self.storage.data_root).as_posix(),
                    uuid5(UUID(eid), str(index)).hex, digest, len(data)))
            if record and record.candidates != candidates:
                raise ConflictError("归档候选清单已经改变")
            record = record or ExecutionRecord(eid, pid, tid, "remote-" + job["backend"], "archiving", _now(), _now(),
                                                returncode=0, candidates=candidates)
            runner._save_checked(record)
            for index, candidate in enumerate(candidates):
                source = self.storage._check_path(self.root / eid / f"{index:02d}.image")
                atomic_bytes(self.storage, runner._candidate_path(record, candidate), source.read_bytes())
            runner._archive_guard(record)
            runner._save_checked(record)
            return record

    def complete(self, job_id, worker_id, claim_id, result):
        import re
        if result.status == "succeeded":
            if result.failure_type or any(not re.fullmatch(HASH, h) for h in result.hashes):
                raise ValueError("结果清单不合法")
        elif result.hashes or result.failure_type not in FAILURE_MESSAGES:
            raise ValueError("失败类型不合法")
        with self.state.control_lock():
            job = self._owned(job_id, worker_id, claim_id)
            payload = result.model_dump()
            previous = job.get("remote_result")
            if previous and previous != payload:
                # 唯一允许的变更：本机人工确认进程停止后解除不确定状态。
                if not (previous["status"] == "uncertain" and result.status == "failed" and result.failure_type == "interrupted"):
                    raise ConflictError("不能改变已提交结果")
            if job["status"] in {"completed", "failed"}:
                return {"status": job["status"]}
            if result.status == "succeeded" and job["action"] == "generate":
                if not result.hashes:
                    raise ValueError("成功结果不能为空")
                for index, digest in enumerate(result.hashes):
                    path = self.storage._check_path(self.root / job_id / f"{index:02d}.image")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise ValueError("缺少对应产物")
            job["remote_result"] = payload
            self.state.save_job(job)  # 先固定清单，响应丢失重传不能改变内容。
            if result.status == "succeeded":
                if job["action"] == "restore":
                    record = self.state.runner.retry_archive(job["project_id"], job["task_id"], job["restore_execution_id"])
                else:
                    record = self._archive(job, result.hashes)
                job.update(status="completed" if record.status == "succeeded" else "failed",
                           execution_id=record.execution_id, failure_type=record.failure_type)
            else:
                job.update(status=result.status, failure_type=result.failure_type)
            job["finished_at"] = _now() if job["status"] != "uncertain" else None
            self.state.save_job(job)
            return {"status": job["status"]}


def install_routes(app, state):
    queue = RemoteQueue(state)
    app.state.remote_queue = queue

    def identity(request):
        return (_validate_id(request.headers.get("x-worker-id", "")),
                _validate_id(request.headers.get("x-claim-id", "")))

    @app.post("/api/worker/claim")
    def claim(payload: Claim):
        return {"job": queue.claim(payload.worker_id, payload.backend)}

    @app.post("/api/worker/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, request: Request):
        queue.beat(job_id, *identity(request))
        return {"ok": True}

    @app.post("/api/worker/jobs/{job_id}/start")
    def start(job_id: str, payload: Start, request: Request):
        return queue.start(job_id, *identity(request), payload.attempt_id)

    @app.get("/api/worker/jobs/{job_id}/assets/{artifact_id}")
    def asset(job_id: str, artifact_id: str, request: Request):
        return Response(queue.asset(job_id, *identity(request), artifact_id), media_type="application/octet-stream")

    @app.put("/api/worker/jobs/{job_id}/outputs/{index}")
    async def upload(job_id: str, index: int, request: Request):
        digest = await run_in_threadpool(queue.upload, job_id, *identity(request), index, await request.body())
        return {"sha256": digest}

    @app.post("/api/worker/jobs/{job_id}/complete")
    def complete(job_id: str, payload: Complete, request: Request):
        return queue.complete(job_id, *identity(request), payload)
