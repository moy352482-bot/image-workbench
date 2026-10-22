"""Cloud text queue. Claims and exact model choices survive disconnects without replay."""
import hashlib
import json
import time
from uuid import uuid4


from .locking import FileLock
from .script_models import ScriptModelRegistry
from .storage import _validate_id, _now
from .domain_errors import ConflictError


from .contracts.script_queue import Capability, Claim, Start, Complete


class RemoteScriptRegistry(ScriptModelRegistry):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def models(self):
        entries = {}
        for path in self.queue.workers.glob("*.json"):
            worker = self.queue.storage.files.read_json(path)
            if not 0 <= time.time() - worker.get("time", 0) <= 120:
                continue
            for model in worker.get("models", []):
                entries.setdefault(model["model_id"], set()).update(model["efforts"])
        catalog = {"models": [{"slug": mid, "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra") if e in efforts]}
                              for mid, efforts in entries.items()]}
        values = ScriptModelRegistry(self.root, catalog=catalog).models()
        for value in values:
            value["availability_source"] = "remote_codex_worker"
            if not value["available"]:
                value["unavailable_reason"] = "脚本执行器离线或未提供此模型"
        return values


class ScriptRemoteQueue:
    def __init__(self, service):
        self.service, self.storage = service, service.storage
        self.workers = self.storage.files.path(".workbench", "script-workers")
        self.workers.mkdir(exist_ok=True)

    def _beat(self, wid, models=None):
        path = self.storage.files.check(self.workers / (_validate_id(wid) + ".json"))
        old = self.storage.files.read_json(path) if path.exists() else {}
        self.storage.files.write_json(path, {"schema_version": 1, "worker_id": wid, "time": time.time(), "models": models if models is not None else old.get("models", [])})

    def _manifest(self, job):
        keys = ("task_id", "claim_id", "worker_id", "status", "attempt_id", "model_id", "thinking_level", "generation_type", "generation_prompt")
        return {k: job.get(k) for k in keys}

    def claim(self, wid, models):
        _validate_id(wid)
        allowed = {m["model_id"] for m in self.service.registry.configs()[0]["models"]}
        if any(m["model_id"] not in allowed or not m["efforts"] or any(e not in {"low", "medium", "high", "xhigh", "max", "ultra"} for e in m["efforts"]) for m in models):
            raise ValueError("脚本执行器能力声明无效")
        with FileLock(self.storage.files.path(".workbench", "script-remote.lock"), timeout=30):
            self._beat(wid, models)
            jobs = [j for j in self.service.pending_jobs() if j.get("job_id")]
            for job in jobs:
                if not self.service._path(job["task_id"]).exists():
                    self.service._write(job)
            own = next((j for j in jobs if j.get("worker_id") == wid and j["status"] in {"running", "uncertain", "archiving"}), None)
            if own:
                return self._manifest(own)
            if any(j["status"] in {"running", "uncertain", "archiving"} for j in jobs):
                return None
            supported = {m["model_id"]: m["efforts"] for m in models}
            job = next((j for j in sorted(jobs, key=lambda j: (j["created_at"], j["task_id"])) if j["status"] == "queued"
                        and j["thinking_level"] in supported.get(j["model_id"], [])), None)
            if not job:
                return None
            with self.service._lock(job["task_id"]):
                job = self.service._load(job["task_id"])
                if job["status"] != "queued":
                    return None
                self.service.identity.require_reservation(job["job_id"], job["project_id"], job["task_id"])
                job.update(status="running", execution_mode="remote", worker_id=wid, claim_id=uuid4().hex,
                           started_at=_now(), heartbeat_at=time.time())
                self.service._write(job)
                return self._manifest(job)

    def _owned(self, tid, wid, cid):
        job = self.service._load(_validate_id(tid))
        if (job.get("worker_id"), job.get("claim_id")) != (_validate_id(wid), _validate_id(cid)):
            raise ConflictError("脚本作业不属于此执行器")
        return job

    def heartbeat(self, tid, wid, cid):
        with self.service._lock(tid):
            job = self._owned(tid, wid, cid)
            self._beat(wid)
            job["heartbeat_at"] = time.time()
            self.service._write(job)
            return {"ok": True}

    def start(self, tid, wid, cid, attempt_id):
        with self.service._lock(tid):
            job = self._owned(tid, wid, cid)
            if job["status"] != "running" or (job.get("attempt_id") and job["attempt_id"] != attempt_id):
                raise ConflictError("此任务不能重新执行")
            reservation = self.service.identity.require_reservation(job["job_id"], job["project_id"], tid)
            stored = json.loads(reservation["payload"])
            if stored.get("kind") != "script" or reservation["amount"] != job["price"] or any(stored.get(k) != job.get(k) for k in ("model_id", "thinking_level", "generation_prompt", "request_fingerprint")):
                raise ValueError("任务与冻结记录不一致")
            job["attempt_id"] = _validate_id(attempt_id)
            self.service._write(job)
            return {"ok": True}

    def complete(self, tid, wid, cid, body):
        with self.service._lock(tid):
            job = self._owned(tid, wid, cid)
            if not job.get("attempt_id") and body["status"] == "failed":
                job["attempt_id"] = _validate_id(body["attempt_id"])
            if job.get("attempt_id") != body["attempt_id"]:
                raise ConflictError("脚本执行标识不匹配")
            target = "completed" if body["status"] == "succeeded" else body["status"]
            if job["status"] in {"completed", "failed"}:
                if job["status"] != target or (target == "completed" and body.get("sha256") != self.service._read_artifact(job["output_artifact_id"])[1].sha256):
                    raise ConflictError("脚本任务已结算，不能改变结果")
                return self.service._public(self.service._reconcile(job))
            if target == "completed":
                data = (body.get("text") or "").encode("utf-8")
                if hashlib.sha256(data).hexdigest() != body.get("sha256"):
                    raise ValueError("脚本正文校验失败")
                self.service._publish_candidate(job, body["text"])
                job["status"] = "archiving"
                self.service._write(job)
                self.service._archive(job)
            else:
                if self.service._candidate(job).exists():
                    raise ConflictError("已收到正文，请恢复归档，不能改为失败")
                job.update(status=target, finished_at=_now(), error=body.get("error"))
                self.service._write(job)
            self._beat(wid)
            return self.service._public(job)
