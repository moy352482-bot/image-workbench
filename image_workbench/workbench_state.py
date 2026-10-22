"""Web 的作业、提交去重和人工审核附加记录；项目/任务/图片仍由 StorageManager 管理。"""

import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

from .locking import FileLock, LockBusyError
from .storage import StorageManager, _validate_id, _now, _text
from .task_runner import TaskRunner
from .models import Project
from .project_manager import ProjectManager
from .accounts import IdentityStore, CreditCostCalculator
from .scheduling import SchedulingPolicy


from .domain_errors import ConflictError


from .image_status import STATUS_LABELS, FAILURE_MESSAGES, _execution_timing, _seconds_between
from .image_queries import ImageQueries
from .image_references import task_input_artifacts
from .image_prompting import default_image_skill


class WorkbenchState:
    def __init__(self, storage: StorageManager, *, backend="codex", execution_mode="local"):
        if backend not in {"codex", "local"}:
            raise ValueError("未知后端")
        self.storage, self.runner, self.backend = storage, TaskRunner(storage), backend
        self.execution_mode = execution_mode
        self.scheduling_policy = SchedulingPolicy.from_environment()
        self.projects = ProjectManager(storage)
        self.identity = IdentityStore(storage) if storage.files.path(".identity", "accounts.sqlite3").exists() else None
        self.root = storage.files.path(".workbench")
        self.root.mkdir(exist_ok=True)
        for name in ("tasks", "jobs", "reviews", "uploads"):
            storage.files.path(".workbench", name).mkdir(exist_ok=True)

    def control_lock(self):
        return FileLock(self.storage.files.path(".workbench", "control.lock"), timeout=30)

    def worker_lock(self):
        return FileLock(self.storage.files.path(".workbench", "worker.lock"))

    def worker_running(self, worker_id=None):
        if self.execution_mode == "remote":
            root = self.storage.files.path(".workbench", "remote")
            legacy = self._read_optional(root / "heartbeat.json")
            def fresh(info):
                return bool(info and 0 <= time.time() - info["time"] <= 60)
            if worker_id:
                info = self._read_optional(root / "workers" / (_validate_id(worker_id) + ".json"))
                if info is not None:
                    return fresh(info)
                return fresh(legacy) and legacy.get("worker_id") == worker_id
            workers = root / "workers"
            if workers.is_dir() and any(fresh(self.storage.files.read_json(path)) for path in workers.glob("*.json")):
                return True
            # 旧版本只有单一心跳文件，升级后仍可读取。
            return fresh(legacy)
        try:
            with self.worker_lock():
                return False
        except LockBusyError:
            return True

    def _task_file(self, category, project_id, task_id, *, create=False):
        directory = self.storage.files.path(".workbench", category, _validate_id(project_id))
        if create:
            directory.mkdir(exist_ok=True)
        return self.storage.files.check(directory / (_validate_id(task_id) + ".json"))

    def _read_optional(self, path):
        try:
            return self.storage.files.read_json(path)
        except FileNotFoundError:
            return None

    def task_options(self, project_id, task_id):
        value = self._read_optional(self._task_file("tasks", project_id, task_id))
        if value and (value.get("project_id"), value.get("task_id")) != (project_id, task_id):
            raise ValueError("附加任务记录不匹配")
        return value

    def image_style_catalog(self):
        return default_image_skill().catalog()

    def get_job(self, project_id, task_id):
        value = self._read_optional(self._task_file("jobs", project_id, task_id))
        if value:
            if (value.get("project_id"), value.get("task_id")) != (project_id, task_id):
                raise ValueError("作业记录不匹配")
            _validate_id(value["job_id"])
            if value.get("status") not in {"queued", "running", "completed", "failed", "uncertain"}:
                raise ValueError("未知作业状态")
        return value

    def save_job(self, job):
        settled = self.identity.settled_job(job["job_id"]) if self.identity else None
        if settled:
            current = self.get_job(settled["project_id"], settled["task_id"])
            job.clear()
            job.update(settled)
            if current and current["job_id"] != job["job_id"]:
                return  # 旧终态通知不能覆盖后续重试。
        else:
            job["updated_at"] = _now()
        self.storage.files.write_json(self._task_file("jobs", job["project_id"], job["task_id"], create=True), job)
        if self.identity:
            written = dict(job)
            self.identity.record_job(job)
            if job != written:
                # 保留先发布JSON再结算的故障恢复顺序，同时同步规范化的终态。
                self.storage.files.write_json(self._task_file("jobs", job["project_id"], job["task_id"]), job)

    def list_jobs(self):
        jobs = []
        # SQLite 冻结与 outbox 同事务；JSON 发布中断后可恢复同一作业。
        if self.identity:
            for pending in self.identity.pending_jobs():
                if pending.get("kind", "image") != "image":
                    continue
                existing = self.get_job(pending["project_id"], pending["task_id"])
                if not existing or existing["job_id"] != pending["job_id"]:
                    self.save_job(pending)
                elif existing["status"] in {"completed", "failed"}:
                    self.identity.record_job(existing)
        for directory in self.storage.files.path(".workbench", "jobs").iterdir():
            self.storage.files.check(directory)
            if directory.is_dir():
                _validate_id(directory.name)
                for path in directory.glob("*.json"):
                    jobs.append(self.get_job(directory.name, path.stem))
        return sorted(jobs, key=lambda job: (job["created_at"], job["job_id"]))

    def create_project(self, name, request_id):
        main = self.create_main_project(name, request_id)
        pid = self.projects.ensure_storage_namespace(main.main_project_id)
        return Project(main.main_project_id, main.name, self.storage.get_project(pid).created_at)

    def create_main_project(self, name, request_id, description=""):
        _validate_id(request_id)
        _text(name, "项目名称")
        if len(name) > 120:
            raise ValueError("项目名称最多120字符")
        with self.control_lock():
            try:
                existing = self.projects.get_main_project(request_id)
            except FileNotFoundError:
                return self.projects.create_main_project(name, description, main_project_id=request_id)
            if existing.name != name or existing.description != description:
                raise ConflictError("该保存请求已使用，请刷新后重新创建")
            return existing

    def create_subproject(self, main_project_id, name, request_id, description=""):
        _validate_id(request_id)
        with self.control_lock():
            try:
                existing = self.projects.get_subproject(request_id)
            except FileNotFoundError:
                return self.projects.create_subproject(main_project_id, name, description, subproject_id=request_id)
            if (existing.main_project_id, existing.name, existing.description) != (main_project_id, name, description):
                raise ConflictError("该保存请求已使用，请刷新后重新创建")
            return existing

    def create_task(self, project_id, *, request_id, operation, prompt, aspect_ratio="auto", base_artifact_id=None, inputs=(),
                    subproject_id=None, name=None, description="", reference_artifact_ids=(), style_id=None):
        _validate_id(request_id)
        _text(prompt, "提示词")
        if len(prompt) > 12000:
            raise ValueError("提示词最多12000字符")
        if operation not in {"generate", "edit"} or aspect_ratio not in {"auto", "1:1", "3:2", "2:3"}:
            raise ValueError("无效模式或比例")
        if len(inputs) + len(reference_artifact_ids) > 8:
            raise ValueError("最多上传8张参考图")
        main = self.projects.get_main_project(project_id)
        sub = self.projects.get_subproject(subproject_id) if subproject_id else self.projects.default_subproject(project_id)
        if sub.main_project_id != main.main_project_id:
            raise ValueError("子项目不属于指定主项目")
        project_id = self.projects.ensure_storage_namespace(main.main_project_id)
        from .asset_library import AssetLibrary
        library = AssetLibrary(self)
        if len(set(reference_artifact_ids)) != len(reference_artifact_ids):
            raise ValueError("参考图不能重复")
        references = [library.require(sub.subproject_id, aid) for aid in reference_artifact_ids]
        if sum(item.size_bytes for item in references) + sum(Path(path).stat().st_size for path in inputs) > 40 * 1024 * 1024:
            raise ValueError("本次参考图片合计超过 40 MiB")
        if operation == "edit":
            library.require(sub.subproject_id, base_artifact_id)
            self.storage.require_image_source(project_id, base_artifact_id)
        elif base_artifact_id:
            raise ValueError("生成模式不能指定编辑基底")
        hashes = []
        for path in inputs:
            path = Path(path)
            if path.stat().st_size > self.storage.config.max_image_bytes:
                raise ValueError("图片体积超过限制")
            self.storage.inspect_image(path)
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        parts = [project_id, sub.subproject_id, name, description, operation, prompt, aspect_ratio, base_artifact_id, hashes]
        if reference_artifact_ids:
            parts.append(list(reference_artifact_ids))
        if style_id:
            parts.append({"style_id": style_id})
        fingerprint = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
        with self.control_lock():
            previous = self.task_options(project_id, request_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("同一保存请求的内容发生变化，请重新保存草稿")
                if previous["state"] != "ready":
                    raise ConflictError("上次保存未完成，未启动模型；请建立新的草稿")
                return self.storage.get_task(project_id, request_id)
            image_prompt = default_image_skill().compile(
                prompt, operation=operation, style_id=style_id, aspect_ratio=aspect_ratio,
                reference_count=len(inputs) + len(reference_artifact_ids),
            )
            options = {"schema_version": 1, "project_id": project_id, "task_id": request_id,
                       "fingerprint": fingerprint, "aspect_ratio": aspect_ratio, "state": "preparing", "created_at": _now(),
                       "reference_artifact_ids": list(reference_artifact_ids), "image_prompt": image_prompt}
            path = self._task_file("tasks", project_id, request_id, create=True)
            self.storage.files.write_json(path, options)
            try:
                effective_prompt = prompt if aspect_ratio == "auto" else prompt + f"\n画面宽高比 {aspect_ratio}。"
                self.projects.reserve_task(sub.subproject_id, request_id, name if name is not None else (prompt.strip().splitlines()[0][:120] or "未命名任务"), description)
                task = self.storage.create_task(project_id, operation, effective_prompt, base_artifact_id, task_id=request_id)
                for source in inputs:
                    self.storage.add_input_image(project_id, task.task_id, source)
                options["state"] = "ready"
                self.storage.files.write_json(path, options)
                self.projects.finish_task(request_id)
                return task
            except Exception:
                options["state"] = "failed"
                self.storage.files.write_json(path, options)
                raise

    def submit(self, project_id, task_id, *, request_id, confirmed=False, retry_of=None, restore_execution_id=None):
        _validate_id(request_id)
        if restore_execution_id is None and confirmed is not True:
            raise ValueError("请明确点击生成按钮后执行")
        with self.control_lock():
            if self.storage.get_task(project_id, task_id).operation not in {"generate", "edit"}:
                raise ValueError("文本任务请使用脚本生成服务")
            options = self.task_options(project_id, task_id)
            if options and options.get("asset_library"):
                raise ValueError("素材暂存任务不能生成，请将素材添加到新的创作任务")
            if options and options["state"] != "ready":
                raise ConflictError("任务输入尚未完整保存，不能执行")
            owner = None
            if self.identity:
                placement = self.projects.get_task_placement(task_id)
                sub = self.projects.get_subproject(placement.subproject_id)
                owner = self.identity.ownership(sub.main_project_id)
                if owner["deleted"]:
                    raise FileNotFoundError("项目已删除")
                previous_request = self.identity.request_job(owner["account_id"], request_id, project_id, task_id)
                if previous_request:
                    # 仅确认原请求。不可把旧 payload 写回覆盖后续重试的作业。
                    return previous_request
            job = self.get_job(project_id, task_id)
            if job and job["request_id"] == request_id:
                return job
            if job and job["status"] in {"queued", "running", "uncertain"}:
                return job
            previous = self.runner.get_execution(project_id, task_id)
            if previous is None and any(item.kind == "output" for item in self.storage.list_task_artifacts(project_id, task_id)):
                raise ConflictError("该任务已有登记图片，请从历史图片新建修改任务")
            if previous and previous.status in {"running", "archiving"}:
                raise ConflictError("已有执行状态待确认，不会启动第二次生成")
            if restore_execution_id:
                _validate_id(restore_execution_id)
                if not previous or previous.execution_id != restore_execution_id or not previous.candidates:
                    raise ConflictError("没有可恢复的最新候选产物")
                if previous.status == "succeeded":
                    raise ConflictError("产物已经完成归档")
            elif previous or job:
                expected = job["job_id"] if job else previous.execution_id
                if not retry_of:
                    if job and job["status"] == "completed":
                        return job
                    raise ConflictError("该任务已有执行记录，请查看结果或显式重试")
                if retry_of != expected or (previous and previous.status == "succeeded"):
                    raise ConflictError("任务状态已变化或已成功，请刷新后查看结果")
            job = {"schema_version": 1, "job_id": uuid4().hex, "request_id": request_id,
                   "project_id": project_id, "task_id": task_id, "backend": self.backend,
                   "action": "restore" if restore_execution_id else "generate", "confirmed": bool(confirmed),
                   "restore_execution_id": restore_execution_id,
                   "previous_execution_id": previous.execution_id if previous else None,
                   "execution_id": None, "status": "queued", "failure_type": None,
                   "created_at": _now(), "started_at": None, "finished_at": None}
            if self.identity:
                job = self.identity.reserve(job, owner, self.estimate_credit(project_id, task_id))
            self.save_job(job)
            return job

    def estimate_credit(self, project_id, task_id):
        task = self.storage.get_task(project_id, task_id)
        references = len(task_input_artifacts(self.storage, project_id, task_id))
        return CreditCostCalculator.calculate(task.operation, self.backend, 1, references + bool(task.base_artifact_id))

    def require_job_credit(self, job):
        if self.identity:
            self.identity.require_reservation(job["job_id"], job["project_id"], job["task_id"])

    def review(self, artifact):
        if artifact.kind != "output":
            return {"review_status": "pending", "reviewed_at": None}
        path = self.storage.files.path(".workbench", "reviews", artifact.artifact_id + ".json")
        value = self._read_optional(path)
        if value and (value.get("artifact_id") != artifact.artifact_id or value.get("sha256") != artifact.sha256):
            raise ValueError("审核记录与图片不匹配")
        return {"review_status": "approved" if value else "pending", "reviewed_at": value["reviewed_at"] if value else None}

    def approve(self, artifact_id):
        with self.control_lock():
            artifact = self.storage.get_artifact(artifact_id)
            if artifact.kind != "output" or artifact.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("只能审核输出图片")
            path = self.storage.get_artifact_path(artifact_id)
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
                raise ValueError("图片文件内容已改变，不能通过审核")
            if self.review(artifact)["review_status"] != "approved":
                self.storage.files.write_json(self.storage.files.path(".workbench", "reviews", artifact_id + ".json"), {
                    "schema_version": 1, "artifact_id": artifact_id, "sha256": artifact.sha256, "reviewed_at": _now(),
                })
            return self.artifact_dto(artifact)

    def artifact_dto(self, artifact, *, catalog=None):
        return ImageQueries(self).artifact_dto(artifact, catalog=catalog)

    def task_view(self, project_id, task_id, *, detail=True, catalog=None):
        return ImageQueries(self).task_view(project_id, task_id, detail=detail, catalog=catalog)

    def logical_task_view(self, task_id, *, detail=True, main_project_id=None):
        return ImageQueries(self).logical_task_view(task_id, detail=detail, main_project_id=main_project_id)

    def project_views(self, main_project_id=None):
        return ImageQueries(self).project_views(main_project_id)
