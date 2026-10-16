"""Web 的作业、提交去重和人工审核附加记录；项目/任务/图片仍由 StorageManager 管理。"""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
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


class ConflictError(ValueError):
    pass


STATUS_LABELS = {"draft": "待开始", "queued": "等待执行", "generating": "生成中", "checking": "检查产物",
                 "awaiting_review": "等待审核", "approved": "已通过", "failed": "执行失败", "uncertain": "状态待确认"}
FAILURE_MESSAGES = {
    "prepare_failed": "输入或任务准备失败，请检查图片和提示词。",
    "codex_launch_failed": "无法启动本地 Codex，请检查本地安装和登录状态。",
    "codex_timeout": "本次 Codex 执行超时，日志已保留；不会自动重新生成。",
    "codex_execution_failed": "Codex 未完成图片生成或未提供可登记产物，请核查本地日志后手动重试。",
    "artifact_not_found": "未找到本次生成的图片文件，执行记录已保留。",
    "artifact_invalid": "返回图片未通过文件校验，未将其作为有效结果。",
    "register_failed": "图片登记失败，候选文件保留；可尝试仅恢复归档。",
    "state_update_failed": "执行记录写入失败，已保留可用证据，请先检查本地存储。",
    "interrupted": "执行状态需要确认；不会自动重放或再次消耗额度。",
    "unexpected_exception": "执行遇到异常，请查看本地日志；系统不会自动重试。",
}


def _seconds_between(start, end):
    if not start or not end:
        return None
    try:
        seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _execution_timing(status, latest, job):
    """列表与详情共用本次执行计时；任务创建与排队时间不算执行耗时。"""
    value, source = None, None
    if status in {"generating", "checking"}:
        start = job.get("started_at") if job else (latest.created_at if latest else None)
        value = _seconds_between(start, datetime.now(timezone.utc).isoformat())
        source = "running" if value is not None else None
    elif status not in {"draft", "queued", "uncertain"}:
        matching = latest and (not job or job.get("execution_id") == latest.execution_id)
        # 远端记录只计服务端归档，恢复归档记录则累计了先前尝试；两者采用本次作业时钟。
        use_record = matching and not latest.backend.startswith("remote-") and not (job and job.get("action") == "restore")
        if use_record and isinstance(latest.total_seconds, (int, float)) and math.isfinite(latest.total_seconds) and latest.total_seconds >= 0:
            value, source = latest.total_seconds, "execution_record"
        elif job:
            value = _seconds_between(job.get("started_at"), job.get("finished_at"))
            source = "job_clock" if value is not None else None
    return {"execution_seconds": value, "execution_timing_source": source, "elapsed_seconds": value}


class WorkbenchState:
    def __init__(self, storage: StorageManager, *, backend="codex", execution_mode="local"):
        if backend not in {"codex", "local"}:
            raise ValueError("未知后端")
        self.storage, self.runner, self.backend = storage, TaskRunner(storage), backend
        self.execution_mode = execution_mode
        self.scheduling_policy = SchedulingPolicy.from_environment()
        self.projects = ProjectManager(storage)
        self.identity = IdentityStore(storage) if storage._path(".identity", "accounts.sqlite3").exists() else None
        self.root = storage._path(".workbench")
        self.root.mkdir(exist_ok=True)
        for name in ("tasks", "jobs", "reviews", "uploads"):
            storage._path(".workbench", name).mkdir(exist_ok=True)

    def control_lock(self):
        return FileLock(self.storage._path(".workbench", "control.lock"), timeout=30)

    def worker_lock(self):
        return FileLock(self.storage._path(".workbench", "worker.lock"))

    def worker_running(self, worker_id=None):
        if self.execution_mode == "remote":
            root = self.storage._path(".workbench", "remote")
            legacy = self._read_optional(root / "heartbeat.json")
            def fresh(info):
                return bool(info and 0 <= time.time() - info["time"] <= 60)
            if worker_id:
                info = self._read_optional(root / "workers" / (_validate_id(worker_id) + ".json"))
                if info is not None:
                    return fresh(info)
                return fresh(legacy) and legacy.get("worker_id") == worker_id
            workers = root / "workers"
            if workers.is_dir() and any(fresh(self.storage._read_json(path)) for path in workers.glob("*.json")):
                return True
            # 旧版本只有单一心跳文件，升级后仍可读取。
            return fresh(legacy)
        try:
            with self.worker_lock():
                return False
        except LockBusyError:
            return True

    def _task_file(self, category, project_id, task_id, *, create=False):
        directory = self.storage._path(".workbench", category, _validate_id(project_id))
        if create:
            directory.mkdir(exist_ok=True)
        return self.storage._check_path(directory / (_validate_id(task_id) + ".json"))

    def _read_optional(self, path):
        try:
            return self.storage._read_json(path)
        except FileNotFoundError:
            return None

    def task_options(self, project_id, task_id):
        value = self._read_optional(self._task_file("tasks", project_id, task_id))
        if value and (value.get("project_id"), value.get("task_id")) != (project_id, task_id):
            raise ValueError("附加任务记录不匹配")
        return value

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
        self.storage._write_json(self._task_file("jobs", job["project_id"], job["task_id"], create=True), job)
        if self.identity:
            written = dict(job)
            self.identity.record_job(job)
            if job != written:
                # 保留先发布JSON再结算的故障恢复顺序，同时同步规范化的终态。
                self.storage._write_json(self._task_file("jobs", job["project_id"], job["task_id"]), job)

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
        for directory in self.storage._path(".workbench", "jobs").iterdir():
            self.storage._check_path(directory)
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
                    subproject_id=None, name=None, description=""):
        _validate_id(request_id)
        _text(prompt, "提示词")
        if len(prompt) > 12000:
            raise ValueError("提示词最多12000字符")
        if operation not in {"generate", "edit"} or aspect_ratio not in {"auto", "1:1", "3:2", "2:3"}:
            raise ValueError("无效模式或比例")
        if len(inputs) > 8:
            raise ValueError("最多上传8张参考图")
        main = self.projects.get_main_project(project_id)
        sub = self.projects.get_subproject(subproject_id) if subproject_id else self.projects.default_subproject(project_id)
        if sub.main_project_id != main.main_project_id:
            raise ValueError("子项目不属于指定主项目")
        project_id = self.projects.ensure_storage_namespace(main.main_project_id)
        if operation == "edit":
            base = self.storage._require_source(project_id, base_artifact_id)
            if base.kind != "output":
                raise ValueError("请选择已登记输出作为编辑基底")
        elif base_artifact_id:
            raise ValueError("生成模式不能指定编辑基底")
        hashes = []
        for path in inputs:
            path = Path(path)
            if path.stat().st_size > self.storage.config.max_image_bytes:
                raise ValueError("图片体积超过限制")
            self.storage._inspect_image(path)
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        fingerprint = hashlib.sha256(json.dumps([project_id, sub.subproject_id, name, description, operation, prompt, aspect_ratio, base_artifact_id, hashes], ensure_ascii=False).encode()).hexdigest()
        with self.control_lock():
            previous = self.task_options(project_id, request_id)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("同一保存请求的内容发生变化，请重新保存草稿")
                if previous["state"] != "ready":
                    raise ConflictError("上次保存未完成，未启动模型；请建立新的草稿")
                return self.storage.get_task(project_id, request_id)
            options = {"schema_version": 1, "project_id": project_id, "task_id": request_id,
                       "fingerprint": fingerprint, "aspect_ratio": aspect_ratio, "state": "preparing", "created_at": _now()}
            path = self._task_file("tasks", project_id, request_id, create=True)
            self.storage._write_json(path, options)
            try:
                effective_prompt = prompt if aspect_ratio == "auto" else prompt + f"\n画面宽高比 {aspect_ratio}。"
                self.projects.reserve_task(sub.subproject_id, request_id, name if name is not None else (prompt.strip().splitlines()[0][:120] or "未命名任务"), description)
                task = self.storage.create_task(project_id, operation, effective_prompt, base_artifact_id, task_id=request_id)
                for source in inputs:
                    self.storage.add_input_image(project_id, task.task_id, source)
                options["state"] = "ready"
                self.storage._write_json(path, options)
                self.projects.finish_task(request_id)
                return task
            except Exception:
                options["state"] = "failed"
                self.storage._write_json(path, options)
                raise

    def submit(self, project_id, task_id, *, request_id, confirmed=False, retry_of=None, restore_execution_id=None):
        _validate_id(request_id)
        if restore_execution_id is None and confirmed is not True:
            raise ValueError("请明确点击生成按钮后执行")
        with self.control_lock():
            self.storage.get_task(project_id, task_id)
            options = self.task_options(project_id, task_id)
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
        references = sum(item.kind == "input" for item in self.storage.list_task_artifacts(project_id, task_id))
        return CreditCostCalculator.calculate(task.operation, self.backend, 1, references + bool(task.base_artifact_id))

    def require_job_credit(self, job):
        if self.identity:
            self.identity.require_reservation(job["job_id"], job["project_id"], job["task_id"])

    def review(self, artifact):
        if artifact.kind != "output":
            return {"review_status": "pending", "reviewed_at": None}
        path = self.storage._path(".workbench", "reviews", artifact.artifact_id + ".json")
        value = self._read_optional(path)
        if value and (value.get("artifact_id") != artifact.artifact_id or value.get("sha256") != artifact.sha256):
            raise ValueError("审核记录与图片不匹配")
        return {"review_status": "approved" if value else "pending", "reviewed_at": value["reviewed_at"] if value else None}

    def approve(self, artifact_id):
        with self.control_lock():
            artifact = self.storage.get_artifact(artifact_id)
            if artifact.kind != "output":
                raise ValueError("只能审核输出图片")
            path = self.storage.get_artifact_path(artifact_id)
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
                raise ValueError("图片文件内容已改变，不能通过审核")
            if self.review(artifact)["review_status"] != "approved":
                self.storage._write_json(self.storage._path(".workbench", "reviews", artifact_id + ".json"), {
                    "schema_version": 1, "artifact_id": artifact_id, "sha256": artifact.sha256, "reviewed_at": _now(),
                })
            return self.artifact_dto(artifact)

    def artifact_dto(self, artifact, *, catalog=None):
        catalog = catalog if catalog is not None else self.projects.catalog_view()
        placement = catalog["task_placements"][artifact.task_id]
        sub = catalog["subprojects"][placement["subproject_id"]]
        values = {key: value for key, value in asdict(artifact).items() if key not in {"relative_path", "original_filename", "sha256"}}
        workspace_id = getattr(self.projects, "workspace_id", None)
        suffix = f"?workspace_id={workspace_id}" if workspace_id else ""
        return {**values, "main_project_id": sub["main_project_id"], "subproject_id": sub["subproject_id"],
                "url": f"/api/artifacts/{artifact.artifact_id}{suffix}",
                "download_url": f"/api/artifacts/{artifact.artifact_id}{suffix}{'&' if suffix else '?'}download=1", **self.review(artifact)}

    def task_view(self, project_id, task_id, *, detail=True, catalog=None):
        task = self.storage.get_task(project_id, task_id)
        catalog = catalog if catalog is not None else self.projects.catalog_view()
        placement = catalog["task_placements"][task_id]
        if placement["storage_project_id"] != project_id:
            raise ValueError("任务逻辑归属与物理定位不一致")
        sub = catalog["subprojects"][placement["subproject_id"]]
        main = catalog["main_projects"][sub["main_project_id"]]
        artifacts = self.storage.list_task_artifacts(project_id, task_id)
        inputs = [item for item in artifacts if item.kind == "input"]
        outputs = [item for item in artifacts if item.kind == "output"]
        records = self.runner.list_executions(project_id, task_id)
        latest = records[-1] if records else None
        job, options = self.get_job(project_id, task_id), self.task_options(project_id, task_id)
        status, failure = "awaiting_review" if outputs else "draft", None
        if latest:
            status = {"running": "generating", "archiving": "checking", "succeeded": "awaiting_review", "interrupted": "failed"}.get(latest.status, "failed")
            failure = latest.failure_type
        if job:
            if job["status"] == "queued":
                status, failure = "queued", None
            elif job["status"] == "uncertain":
                status, failure = "uncertain", "interrupted"
            elif job["status"] == "running":
                status = "checking" if latest and latest.status == "archiving" else "generating"
            elif job["status"] == "failed":
                status, failure = "failed", job.get("failure_type") or failure
        if options and options["state"] != "ready":
            status, failure = "failed", "prepare_failed"
        if status == "awaiting_review" and outputs and all(self.review(item)["review_status"] == "approved" for item in outputs):
            status = "approved"
        if status in {"generating", "checking"} and (not job or not self.worker_running(job.get("worker_id"))):
            status = "uncertain"  # CLI 历史活动记录也不能冒充网页 Worker 正在运行。
            failure = "interrupted"
        confirmed_interruption = status == "failed" and failure == "interrupted" and latest and latest.status == "interrupted"
        updated_at = max([task.created_at, placement["updated_at"],
                          *(item.created_at for item in artifacts),
                          *([latest.updated_at] if latest else []),
                          *([job.get("updated_at", job["created_at"])] if job else [])])
        result = {**asdict(task), "status": status, "status_label": "已中断" if confirmed_interruption else STATUS_LABELS[status],
                  "main_project_id": main["main_project_id"], "subproject_id": sub["subproject_id"],
                  "main_project_name": main["name"], "subproject_name": sub["name"],
                  "name": placement["name"], "description": placement["description"], "updated_at": updated_at,
                  "input_count": len(inputs), "output_count": len(outputs),
                  "execution_backend": latest.backend if latest else None,
                  "preview_artifact": self.artifact_dto(outputs[0], catalog=catalog) if outputs else None}
        result.update(_execution_timing(status, latest, job))
        if self.identity:
            result.update(self.identity.task_credit(task_id))
            # 下一次执行的报价与已有预约的冻结/结算金额分开；复用本次已校验的输入。
            result["estimated_credit"] = CreditCostCalculator.calculate(task.operation, self.backend, 1, len(inputs) + bool(task.base_artifact_id))
            result["workspace_id"] = self.identity.ownership(main["main_project_id"])["workspace_id"]
            if result["credit_status"] == "credit_insufficient":
                result.update(status="credit_insufficient", status_label="积分不足")
        if not detail:
            return result
        valid_options = not options or options["state"] == "ready"
        result.update(
            inputs=[self.artifact_dto(item, catalog=catalog) for item in inputs], outputs=[self.artifact_dto(item, catalog=catalog) for item in outputs],
            base_image=self.artifact_dto(self.storage.get_artifact(task.base_artifact_id), catalog=catalog) if task.base_artifact_id else None,
            aspect_ratio=options["aspect_ratio"] if options else "auto",
            current_job={key: job[key] for key in ("job_id", "status", "created_at", "started_at", "finished_at")} if job else None,
            last_execution_id=latest.execution_id if latest else None,
            can_generate=status == "draft" and valid_options,
            can_retry=status == "failed" and valid_options,
            can_restore=status == "failed" and bool(latest and latest.candidates),
            error_summary=("本次执行已中断，不会自动重试或提交新的生成请求。" if confirmed_interruption else
                           FAILURE_MESSAGES.get(failure, "执行失败，请检查本地日志。") if failure else None),
            failure_type=failure,
            timings={key: getattr(latest, key) for key in ("prepare_seconds", "backend_seconds", "codex_seconds", "validate_seconds", "register_seconds", "total_seconds")} if latest else None,
            executions=[{key: getattr(record, key) for key in ("execution_id", "status", "created_at", "failure_type", "total_seconds")} for record in reversed(records)],
        )
        return result

    def logical_task_view(self, task_id, *, detail=True, main_project_id=None):
        placement = self.projects.get_task_placement(task_id, main_project_id=main_project_id)
        return self.task_view(placement.storage_project_id, task_id, detail=detail)

    def project_views(self, main_project_id=None):
        """在同一逻辑快照中汇总，避免主/子分别维护计数或完整任务列表。"""
        catalog = self.projects.catalog_view()
        if main_project_id is not None:
            self.projects._required(catalog["main_projects"], main_project_id, "主项目")
        empty = lambda item: {**item, "task_count": 0, "image_count": 0, "input_count": 0,
                              "output_count": 0, "counts_by_status": {}}
        mains = {key: {**empty(item), "subproject_count": 0, "subprojects": []}
                 for key, item in catalog["main_projects"].items() if main_project_id is None or key == main_project_id}
        subs = {key: empty(item) for key, item in catalog["subprojects"].items() if item["main_project_id"] in mains}
        tasks = []
        for item in catalog["task_placements"].values():
            if item["subproject_id"] not in subs:
                continue
            try:
                task = self.task_view(item["storage_project_id"], item["task_id"], detail=False, catalog=catalog)
            except FileNotFoundError:
                if item["state"] == "pending":
                    continue
                raise
            tasks.append(task)
            for target in (subs[item["subproject_id"]], mains[task["main_project_id"]]):
                target["task_count"] += 1
                target["input_count"] += task["input_count"]
                target["output_count"] += task["output_count"]
                target["image_count"] += task["input_count"] + task["output_count"]
                target["updated_at"] = max(target["updated_at"], task["updated_at"])
                statuses = target["counts_by_status"]
                statuses[task["status"]] = statuses.get(task["status"], 0) + 1
        for sub in sorted(subs.values(), key=lambda item: (item["created_at"], item["subproject_id"])):
            main = mains[sub["main_project_id"]]
            main["subprojects"].append(sub)
            main["subproject_count"] += 1
            main["updated_at"] = max(main["updated_at"], sub["updated_at"])
        return mains, subs, sorted(tasks, key=lambda item: (item["updated_at"], item["task_id"]), reverse=True)
