"""管理员 Qwen 测试队列：隔离保存原图、请求、执行回执与成图，不参与积分。"""
import hashlib
import io
import json
import time
from uuid import uuid4

from PIL import Image, ImageOps

from .atomic_files import write_bytes as _write
from .locking import FileLock
from .storage import _now, _validate_id
from .domain_errors import ConflictError


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MODELS = {"fp8": "qwen-image-2.1-turbo-fp8-comfy.safetensors", "q5": "qwen-image-2.1-turbo-Q5_K_M.gguf"}
MODES = {"generate": 0, "edit": 1, "reference": 2}
ACTIVE = {"queued", "running", "uncertain"}


def image_info(data, *, normalize=False):
    if not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("图片大小需在 20 MB 以内")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                raise ValueError("仅支持静态 PNG、JPG、WEBP 图片")
            if min(source.size) < 32 or source.width * source.height > 16_000_000:
                raise ValueError("图片至少 32 像素，且总像素不能超过 1600 万")
            source.load()
            size = source.size
            if normalize:
                image = ImageOps.exif_transpose(source).convert("RGB")
                size = image.size
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                data = stream.getvalue()
            elif source.format != "PNG":
                raise ValueError("成图必须是 PNG")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片损坏或无法安全读取") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("解码后的图片超过 20 MB，请缩小后上传")
    return data, {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "width": size[0], "height": size[1]}


def validate_task(task):
    if task.get("mode") not in MODES or task.get("model") not in MODELS:
        raise ValueError("请选择受支持的模型与工作流")
    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError("请填写 1–4000 字的需求或编辑指令")
    if type(task.get("seed")) is not int or not 0 <= task["seed"] <= 4294967295:
        raise ValueError("随机种子需为 0–4294967295 的整数")
    if type(task.get("resolution")) is not int or task["resolution"] not in {1024, 1536}:
        raise ValueError("仅支持 1024 或 1536 尺寸")
    if task["mode"] != "generate" and task["resolution"] != 1024:
        raise ValueError("测试版图片编辑固定使用 1024 分辨率，画幅随原图")
    return {k: task[k] for k in ("mode", "model", "prompt", "seed", "resolution")}


class QwenLabStore:
    def __init__(self, storage, identity):
        self.storage, self.identity = storage, identity
        self.files = storage.files.scope(".qwen-lab")
        self.root = self.files.root
        self.root.mkdir(exist_ok=True)
        self.jobs = self.root / "jobs"
        self.workers = self.root / "workers"
        self.jobs.mkdir(exist_ok=True)
        self.workers.mkdir(exist_ok=True)

    def lock(self):
        return FileLock(self.root / "queue.lock", timeout=30)

    def _dir(self, jid):
        return self.files.check(self.jobs / _validate_id(jid))

    def _load(self, jid):
        return self.files.read_json(self._dir(jid) / "job.json")

    def _save(self, job):
        self.files.write_json(self._dir(job["job_id"]) / "job.json", job)

    def _all(self):
        return [self.files.read_json(self.files.check(p)) for p in self.jobs.glob("*/job.json")]

    def _owned(self, aid, wid, jid):
        self.identity.require_workspace(aid, wid)
        job = self._load(jid)
        if (job["account_id"], job["workspace_id"]) != (aid, wid):
            raise FileNotFoundError("测试任务不存在")
        return job

    def _public(self, job):
        keys = ("job_id", "status", "created_at", "started_at", "finished_at", "task", "inputs", "output", "error", "elapsed_seconds")
        result = {k: job.get(k) for k in keys}
        result["workspace_id"] = job["workspace_id"]
        result["heartbeat_at"] = job.get("heartbeat_at")
        return result

    def create(self, aid, wid, request_id, task, images):
        self.identity.require_workspace(aid, wid)
        _validate_id(request_id)
        task = validate_task(task)
        if len(images) != MODES[task["mode"]]:
            raise ValueError("文生图无需图片，单图编辑需一张原图，参考编辑需原图和参考图")
        normalized = [image_info(data, normalize=True) for data in images]
        inputs = [{"index": i + 1, **info} for i, (_, info) in enumerate(normalized)]
        fingerprint = hashlib.sha256(json.dumps({"task": task, "inputs": inputs}, sort_keys=True).encode()).hexdigest()
        with self.lock():
            path = self._dir(request_id)
            if (path / "job.json").exists():
                old = self._owned(aid, wid, request_id)
                if old["fingerprint"] != fingerprint:
                    raise ConflictError("此提交标识已经用于其他需求，请刷新后重新提交")
                return self._public(old)
            active = [j for j in self._all() if j["status"] in ACTIVE]
            if len(active) >= 6 or sum(j["account_id"] == aid for j in active) >= 3:
                raise ConflictError("测试队列已满，请等待当前任务完成或取消排队任务")
            path.mkdir(exist_ok=True)
            for i, (data, _) in enumerate(normalized):
                _write(path / f"input-{i+1}.png", data)
            job = {"schema_version": 1, "job_id": request_id, "account_id": aid, "workspace_id": wid,
                   "created_at": _now(), "status": "queued", "task": task, "inputs": inputs, "fingerprint": fingerprint}
            self._save(job)
            return self._public(job)

    def list(self, aid, wid):
        self.identity.require_workspace(aid, wid)
        with self.lock():
            jobs = [j for j in self._all() if (j["account_id"], j["workspace_id"]) == (aid, wid)]
        return [self._public(j) for j in sorted(jobs, key=lambda j: j["created_at"], reverse=True)[:50]]

    def get(self, aid, wid, jid):
        with self.lock():
            return self._public(self._owned(aid, wid, jid))

    def cancel(self, aid, wid, jid):
        with self.lock():
            job = self._owned(aid, wid, jid)
            if job["status"] == "cancelled":
                return self._public(job)
            if job["status"] != "queued":
                raise ConflictError("只可取消尚未领取的排队任务")
            job.update(status="cancelled", finished_at=_now())
            self._save(job)
            return self._public(job)

    def _image(self, job, name):
        allowed = {f"input-{x['index']}": x for x in job["inputs"]}
        if job.get("output"):
            allowed["output"] = job["output"]
        if name not in allowed:
            raise FileNotFoundError("图片不存在")
        data = self.files.check(self._dir(job["job_id"]) / (name + ".png")).read_bytes()
        if hashlib.sha256(data).hexdigest() != allowed[name]["sha256"]:
            raise ValueError("图片完整性校验失败")
        return data

    def image(self, aid, wid, jid, name):
        with self.lock():
            job = self._owned(aid, wid, jid)
            if name == "output" and job["status"] != "completed":
                raise FileNotFoundError("结果尚未完成")
            return self._image(job, name)

    def beat(self, worker_id, ready, message=""):
        self.files.write_json(self.workers / (_validate_id(worker_id) + ".json"),
                                 {"schema_version": 1, "time": time.time(), "ready": bool(ready), "message": message[:200]})

    def availability(self):
        workers = [self.files.read_json(self.files.check(p)) for p in self.workers.glob("*.json")]
        recent = [w for w in workers if time.time() - w["time"] < 45]
        return {"online": bool(recent), "ready": any(w["ready"] for w in recent),
                "message": next((w["message"] for w in recent if w["message"]), "")}

    def claim(self, worker_id, ready=True, message=""):
        _validate_id(worker_id)
        with self.lock():
            self.beat(worker_id, ready, message)
            jobs = self._all()
            own = next((j for j in jobs if j.get("worker_id") == worker_id and j["status"] in {"running", "uncertain"}), None)
            if own:
                return own
            if not ready or any(j["status"] in {"running", "uncertain"} for j in jobs):
                return None
            pending = sorted((j for j in jobs if j["status"] == "queued"), key=lambda j: j["created_at"])
            if not pending:
                return None
            job = pending[0]
            job.update(status="running", worker_id=worker_id, claim_id=uuid4().hex, heartbeat_at=time.time())
            self._save(job)
            return job

    def _worker_job(self, jid, worker_id, claim_id):
        job = self._load(jid)
        if (job.get("worker_id"), job.get("claim_id")) != (_validate_id(worker_id), _validate_id(claim_id)):
            raise ConflictError("测试任务不属于此执行器")
        return job

    def heartbeat(self, jid, worker_id, claim_id):
        with self.lock():
            job = self._worker_job(jid, worker_id, claim_id)
            job["heartbeat_at"] = time.time()
            self.beat(worker_id, True)
            self._save(job)
            return {"ok": True}

    def start(self, jid, worker_id, claim_id, attempt_id, prompt_id=None):
        _validate_id(attempt_id)
        with self.lock():
            job = self._worker_job(jid, worker_id, claim_id)
            if job["status"] not in {"running", "uncertain"} or job.get("attempt_id") not in {None, attempt_id}:
                raise ConflictError("任务已执行，不能再次提交模型")
            if prompt_id and job.get("prompt_id") not in {None, prompt_id}:
                raise ConflictError("模型执行回执发生变化")
            job.setdefault("started_at", _now())
            job.update(attempt_id=attempt_id, status="running", error=None)
            if prompt_id:
                job["prompt_id"] = prompt_id
            self._save(job)
            return {"ok": True}

    def worker_image(self, jid, worker_id, claim_id, index):
        with self.lock():
            return self._image(self._worker_job(jid, worker_id, claim_id), f"input-{index}")

    def upload(self, jid, worker_id, claim_id, data, sha256):
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("回传图片的 SHA-256 不匹配")
        data, info = image_info(data)
        with self.lock():
            job = self._worker_job(jid, worker_id, claim_id)
            if not job.get("attempt_id") or job["status"] not in {"running", "uncertain", "completed"}:
                raise ConflictError("此任务不能接收结果")
            if job.get("output") and job["output"]["sha256"] != sha256:
                raise ConflictError("此任务已有不同的成图，不能覆盖")
            _write(self._dir(jid) / "output.png", data)
            job["output"] = info
            self._save(job)
            return info

    def complete(self, jid, worker_id, claim_id, status, sha256=None, error=None, elapsed_seconds=None):
        target = {"succeeded": "completed", "failed": "failed", "uncertain": "uncertain"}.get(status)
        if not target:
            raise ValueError("执行结果无效")
        with self.lock():
            job = self._worker_job(jid, worker_id, claim_id)
            if job["status"] in {"completed", "failed"}:
                if job["status"] != target or (target == "completed" and job["output"]["sha256"] != sha256):
                    raise ConflictError("已完成任务不能改变结果")
                return self._public(job)
            if target == "completed":
                if not job.get("output") or job["output"]["sha256"] != sha256:
                    raise ConflictError("请先完整回传图片再确认完成")
                self._image(job, "output")
            elif job.get("output"):
                raise ConflictError("已有成图，请继续确认回传结果")
            job.update(status=target, error=error, elapsed_seconds=elapsed_seconds, heartbeat_at=time.time())
            if target != "uncertain":
                job["finished_at"] = _now()
            self._save(job)
            self.beat(worker_id, True)
            return self._public(job)
