"""Project-scoped script production with shared credit reservations and immutable TXT versions."""
import hashlib
import json
import os
import traceback
import time
from uuid import uuid4, uuid5, NAMESPACE_URL

from .accounts import CreditInsufficient
from .locking import FileLock, LockBusyError
from .storage import _validate_id, _now
from .workspace_scope import WorkspaceProjects
from .workbench_state import ConflictError
from .script_models import ScriptModelRegistry, DemandRouter, signature
from .script_templates import ScriptTemplateLoader, build_prompt, validate_generated_text
from .script_adapter import CodexTextAdapter, ScriptGenerationError


class ScriptService:
    def __init__(self, storage, identity, *, registry=None, templates=None, adapter=None):
        self.storage, self.identity = storage, identity
        self.registry = registry or ScriptModelRegistry()
        self.templates = templates or ScriptTemplateLoader()
        self.adapter = adapter or CodexTextAdapter()
        self.router = DemandRouter(self.registry)
        self.root = storage._path(".workbench", "script-tasks")
        self.root.mkdir(parents=True, exist_ok=True)

    def _control(self):
        return FileLock(self.storage._path(".workbench", "control.lock"), timeout=30)

    def _path(self, tid):
        return self.storage._check_path(self.root / (_validate_id(tid) + ".json"))

    def _lock(self, tid):
        return FileLock(self.storage._check_path(self.root / (_validate_id(tid) + ".lock")))

    def _load(self, tid):
        value = self.storage._read_json(self._path(tid))
        if value.get("kind") != "script" or value.get("task_id") != tid:
            raise ValueError("脚本任务记录不匹配")
        return value

    def _write(self, job, *, settle=True):
        job["updated_at"] = _now()
        self.storage._write_json(self._path(job["task_id"]), job)
        if settle and job.get("job_id"):
            self.identity.record_job(job)
            self.storage._write_json(self._path(job["task_id"]), job)

    def _authorized(self, aid, wid, tid):
        projects = WorkspaceProjects(self.storage, self.identity, aid, wid)
        placement = projects.get_task_placement(_validate_id(tid))
        job = self._load(tid)
        if job["project_id"] != placement.storage_project_id or job["account_id"] != aid:
            raise FileNotFoundError("任务不存在")
        return job

    def _reconcile(self, job):
        """A durable terminal artifact decision may precede SQLite settlement."""
        if job.get("job_id") and job["status"] in {"completed", "failed"}:
            settled = self.identity.settled_job(job["job_id"])
            if not settled or settled["status"] != job["status"]:
                self._write(job)
            elif job.get("credit_status") != settled.get("credit_status"):
                job = settled
                self.storage._write_json(self._path(job["task_id"]), job)
        return job

    def _versions(self):
        path = self.storage._path(".workbench", "script-versions.json")
        return self.storage._read_json(path) if path.exists() else {"schema_version": 1, "versions": {}}

    def _parent(self, aid, wid, version_id, sid=None, generation_type=None):
        if not version_id:
            return None
        version = self._versions()["versions"].get(_validate_id(version_id))
        if not version:
            raise FileNotFoundError("版本不存在")
        parent = self._authorized(aid, wid, version["task_id"])
        if parent["status"] != "completed":
            raise ValueError("原版本尚未完成")
        if sid:
            sub = WorkspaceProjects(self.storage, self.identity, aid, wid).get_subproject(sid)
            if sub.main_project_id != parent["main_project_id"]:
                raise ValueError("修改版本必须保存在同一主项目")
        if generation_type and parent["generation_type"] != generation_type:
            raise ValueError("修改版本不能改变脚本或剧本类型")
        return version

    def _normalize(self, body):
        keys = ("subproject_id", "generation_type", "template_id", "input_request", "selection_mode", "model_id", "thinking_level",
                "duration", "aspect_ratio", "target_audience", "tone", "characters", "product_ip", "constraints", "parent_version_id", "change_reason")
        req = {key: body[key] for key in keys if body.get(key) not in (None, "")}
        req.setdefault("selection_mode", "recommended")
        if req.get("generation_type") not in {"script", "screenplay"} or req["selection_mode"] not in {"manual", "recommended"}:
            raise ValueError("内容类型或模型选择方式无效")
        if not req.get("template_id"):
            templates = self.templates.list_templates(req["generation_type"])
            if not templates:
                raise ValueError("当前内容类型暂无启用模板")
            req["template_id"] = templates[0]["template_id"]
        if not isinstance(req.get("input_request"), str) or not req["input_request"].strip() or len(req["input_request"]) > 20000:
            raise ValueError("请填写 1–20000 字的生成需求")
        for key, value in req.items():
            if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 20000:
                raise ValueError("生成参数格式无效或过长")
        _validate_id(req.get("subproject_id"))
        if req.get("parent_version_id") and not str(req.get("change_reason", "")).strip():
            raise ValueError("生成修改版本时请填写修改原因")
        return req

    def config(self, account_id, workspace_id):
        self.identity.require_workspace(account_id, workspace_id)
        return {"models": self.registry.models(), "templates": self.templates.list_templates(),
                "generation_types": [{"id": "script", "name": "脚本"}, {"id": "screenplay", "name": "剧本"}],
                "credits": self.identity.wallet(account_id)}

    def quote(self, account_id, workspace_id, body):
        req = self._normalize(body)
        projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
        projects.get_subproject(req["subproject_id"])
        self._parent(account_id, workspace_id, req.get("parent_version_id"), req["subproject_id"], req["generation_type"])
        template = self.templates.load(req.get("template_id"), req["generation_type"])
        choice, reason, spec = self.router.route(req, template)
        warnings = []
        if req["selection_mode"] == "manual" and self.router.classify(req)["complexity"] == "complex" and choice["thinking_level"] in {"none", "low", "medium"}:
            warnings.append("当前模型/思考强度可能无法充分满足该需求。请选择继续生成或返回修改。")
        remaining = self.identity.wallet(account_id)["remaining_credit"]
        shortfall = max(0, choice["price"] - remaining) if remaining is not None else 0
        refs = {"template": template["template_reference"], "examples": [{k: v for k, v in item.items() if k != "text"} for item in template["example_references"]]}
        quoted = {**choice, "reason": reason, "demand_spec": spec, "remaining_credit": remaining,
                  "shortfall": shortfall, "warnings": warnings, "can_generate": shortfall == 0,
                  "pricing_status": "priced"}
        quoted["quote_signature"] = signature({"request": req, "choice": choice, "references": refs})
        return quoted

    def _transaction_id(self, job_id, kind):
        with self.identity.connect() as db:
            row = db.execute("SELECT transaction_id FROM credit_transactions WHERE job_id=? AND type=? ORDER BY created_at LIMIT 1", (job_id, kind)).fetchone()
        return row[0] if row else None

    def submit(self, account_id, workspace_id, body):
        req = self._normalize(body)
        rid = _validate_id(body.get("request_id"))
        tid = uuid5(NAMESPACE_URL, "script:" + account_id + ":" + rid).hex
        fingerprint = signature(req)
        with self._control():
            with self.identity.connect() as db:
                prior = db.execute("SELECT task_id,payload FROM reservations WHERE account_id=? AND request_id=?", (account_id, rid)).fetchone()
            if prior and (prior["task_id"] != tid or json.loads(prior["payload"]).get("kind") != "script"):
                raise ConflictError("该请求标识已用于另一项任务")
            # Restore reserved outbox before deciding whether this is a new request.
            pending = next((j for j in self.identity.pending_jobs() if j.get("task_id") == tid and j.get("kind") == "script"), None)
            if not self._path(tid).exists() and pending:
                self.storage._write_json(self._path(tid), pending)
            if self._path(tid).exists():
                old = self._authorized(account_id, workspace_id, tid)
                if old["request_fingerprint"] != fingerprint:
                    raise ConflictError("该请求标识已用于不同内容，请重新提交")
                if old.get("job_id") and not old.get("credit_transaction_id"):
                    old["credit_transaction_id"] = self._transaction_id(old["job_id"], "freeze")
                    self._write(old)
                return self._public(self._reconcile(old))
            quote = self.quote(account_id, workspace_id, req)
            if quote["shortfall"]:
                raise CreditInsufficient(quote["price"], quote["remaining_credit"])
            if body.get("quote_signature") != quote["quote_signature"]:
                raise ConflictError("报价或生成参数已变化，请重新预览报价")
            if quote["warnings"] and body.get("warnings_confirmed") is not True:
                raise ConflictError("请先确认当前模型/思考强度的质量提示")
            projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
            sub = projects.get_subproject(req["subproject_id"])
            parent = self._parent(account_id, workspace_id, req.get("parent_version_id"), req["subproject_id"], req["generation_type"])
            template = self.templates.load(req["template_id"], req["generation_type"])
            parent_text = self._read_artifact(parent["artifact_id"])[0].decode("utf-8") if parent else None
            prompt = build_prompt(req, template, parent_text=parent_text)
            task = projects.create_task(req["subproject_id"], req["generation_type"], req["input_request"], parent["artifact_id"] if parent else None, task_id=tid)
            job = {**req, "schema_version": 1, "kind": "script", "task_id": tid, "project_id": task.project_id,
                   "main_project_id": sub.main_project_id, "account_id": account_id, "workspace_id": workspace_id,
                   "job_id": uuid4().hex, "request_id": rid, "request_fingerprint": fingerprint,
                   "model": quote["display_name"], "model_id": quote["model_id"], "thinking_level": quote["thinking_level"],
                   "price": quote["price"], "status": "queued", "created_at": task.created_at, "completed_at": None,
                   "output_artifact_id": uuid4().hex, "output_version": None, "version_id": uuid4().hex,
                   "document_id": parent["document_id"] if parent else uuid4().hex,
                   "template_reference": template["template_reference"],
                   "example_references": [{k: v for k, v in item.items() if k != "text"} for item in template["example_references"]],
                   "generation_prompt": prompt, "demand_spec": quote["demand_spec"], "route_reason": quote["reason"]}
            job = self.identity.reserve(job, {"account_id": account_id, "workspace_id": workspace_id}, quote["price"])
            if job.get("kind") != "script":
                raise ConflictError("该请求标识已用于其他类型任务")
            job["credit_transaction_id"] = self._transaction_id(job["job_id"], "freeze")
            self._write(job)
            return self._public(job)

    def _candidate(self, job):
        directory = self.storage._path("projects", job["project_id"], "tasks", job["task_id"], "logs", "script")
        directory.mkdir(parents=True, exist_ok=True)
        return self.storage._check_path(directory / "candidate.txt")

    def _publish_candidate(self, job, text):
        candidate = self._candidate(job)
        data = validate_generated_text(text).encode("utf-8")
        if candidate.exists():
            if self._candidate_bytes(job) != data:
                raise ConflictError("已有候选正文，不能覆盖原执行")
            return
        receipt = {"schema_version": 1, "task_id": job["task_id"], "request_fingerprint": job["request_fingerprint"],
                   "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.storage._write_json(candidate.with_suffix(".complete.json"), receipt)
        temporary = self.storage._check_path(candidate.parent / (".candidate-" + uuid4().hex + ".tmp"))
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if candidate.exists():
                raise ConflictError("已有候选正文，不能覆盖原执行")
            os.replace(temporary, candidate)
        finally:
            self.storage._cleanup(temporary)

    def _candidate_bytes(self, job):
        candidate = self._candidate(job)
        receipt = self.storage._read_json(candidate.with_suffix(".complete.json"))
        if candidate.stat().st_size > self.storage.config.max_text_bytes:
            raise ValueError("候选正文超过大小限制")
        data = candidate.read_bytes()
        if receipt != {"schema_version": 1, "task_id": job["task_id"], "request_fingerprint": job["request_fingerprint"],
                       "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}:
            raise ValueError("候选正文与完成凭据不匹配，不能归档或扣款")
        validate_generated_text(data.decode("utf-8"))
        return data

    def _archive(self, job):
        candidate = self._candidate(job)
        data = self._candidate_bytes(job)
        with self._control():
            try:
                artifact = self.storage.get_artifact(job["output_artifact_id"])
                self._read_artifact(artifact.artifact_id)
                task = self.storage.get_task(job["project_id"], job["task_id"])
                if (artifact.project_id, artifact.task_id, artifact.source_artifact_id, artifact.sha256, artifact.size_bytes) != (job["project_id"], job["task_id"], task.base_artifact_id, hashlib.sha256(data).hexdigest(), len(data)):
                    raise ValueError("已有产物与本次任务、父版本或正文不一致")
            except FileNotFoundError:
                artifact = self.storage.register_output_text(job["project_id"], job["task_id"], candidate, artifact_id=job["output_artifact_id"])
            value = self._versions()
            version = value["versions"].get(job["version_id"])
            if version and any(version.get(k) != v for k, v in {"document_id": job["document_id"], "task_id": job["task_id"], "artifact_id": artifact.artifact_id, "parent_version": job.get("parent_version_id")}.items()):
                raise ValueError("版本登记与当前任务不匹配")
            if not version:
                number = 1 + max((v["version"] for v in value["versions"].values() if v["document_id"] == job["document_id"]), default=0)
                version = {"version_id": job["version_id"], "document_id": job["document_id"], "version": number,
                           "parent_version": job.get("parent_version_id"), "change_reason": job.get("change_reason", "首次生成"),
                           "artifact_id": artifact.artifact_id, "task_id": job["task_id"], "model": job["model"],
                           "model_id": job["model_id"], "thinking_level": job["thinking_level"],
                           "prompt": job["generation_prompt"], "created_at": artifact.created_at,
                           "generation_type": job["generation_type"], "source": job.get("source", "codex")}
                value["versions"][job["version_id"]] = version
                self.storage._write_json(self.storage._path(".workbench", "script-versions.json"), value)
            job.update(output_version=version["version"], status="completed", completed_at=_now(), finished_at=_now(), error=None)
            self._write(job, settle=bool(job.get("job_id")))

    def run(self, account_id, workspace_id, task_id):
        try:
            with self._lock(task_id):
                job = self._authorized(account_id, workspace_id, task_id)
                if job["status"] != "queued":
                    return self._public(job)
                reservation = self.identity.require_reservation(job["job_id"], job["project_id"], task_id)
                stored = json.loads(reservation["payload"])
                if stored.get("kind") != "script" or reservation["amount"] != job["price"] or any(stored.get(k) != job.get(k) for k in ("model_id", "thinking_level", "generation_prompt", "request_fingerprint")):
                    raise ValueError("任务与积分冻结记录不一致，不能调用模型")
                job.update(status="running", started_at=_now())
                self._write(job)
                candidate = self._candidate(job)
                phase = "invoke"
                try:
                    # Keep the quoted exact model/effort; availability changes cause a refund, never substitution.
                    self.registry.resolve(job["model_id"], job["thinking_level"], job["generation_type"])
                    raw = self.adapter.generate(prompt=job["generation_prompt"], model_id=job["model_id"],
                                                thinking_level=job["thinking_level"], work_dir=self.storage._check_path(candidate.parent / "codex"))
                    phase = "validate"
                    text = validate_generated_text(raw)
                    phase = "persist"
                    self._publish_candidate(job, text)
                    job["status"] = "archiving"
                    self._write(job)
                    self._archive(job)
                except Exception as exc:
                    # A returned product/failed archive is recoverable without replaying inference.
                    uncertain = getattr(exc, "uncertain", False) or (phase == "persist" and (candidate.exists() or self.storage._check_path(candidate.parent / "codex" / "completed.json").exists()))
                    job.update(status="uncertain" if uncertain else "failed", finished_at=_now(), error=("正文已保留，归档状态待恢复；不会再次调用模型" if uncertain else str(exc)))
                    try:
                        self.storage._check_path(candidate.parent / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
                    except (OSError, ValueError):
                        pass  # Diagnostics must never prevent refund/terminal-state publication.
                    self._write(job)
                return self._public(job)
        except LockBusyError:
            return self._public(self._authorized(account_id, workspace_id, task_id))

    def recover(self, account_id, workspace_id, task_id):
        with self._lock(task_id):
            job = self._authorized(account_id, workspace_id, task_id)
            if job["status"] == "completed":
                return self._public(job)
            if job["status"] not in {"uncertain", "archiving", "running"}:
                raise ConflictError("暂无已完成正文可恢复，请核实原执行状态")
            if not self._candidate(job).is_file():
                work = self.storage._check_path(self._candidate(job).parent / "codex")
                self.storage._check_path(work / "completed.json")
                self.storage._check_path(work / "response.txt")
                if not hasattr(self.adapter, "recover") or not (work / "completed.json").is_file():
                    raise ConflictError("暂无已完成正文可恢复，请核实原执行状态")
                text = self.adapter.recover(prompt=job["generation_prompt"], model_id=job["model_id"], thinking_level=job["thinking_level"], work_dir=work)
                self._publish_candidate(job, text)
            self._archive(job)
            return self._public(job)

    def pending_jobs(self):
        # Include the SQLite outbox even when publication of the JSON job was interrupted.
        values = {j["task_id"]: j for j in self.identity.pending_jobs() if j.get("kind") == "script"}
        for path in self.root.glob("*.json"):
            job = self._load(path.stem)
            if job["status"] in {"queued", "running", "archiving", "uncertain"}:
                values[job["task_id"]] = job
        return list(values.values())

    def confirm_stopped(self, account_id, workspace_id, task_id, *, confirmed=False):
        """Local operator recovery, matching worker-confirm-stopped; never terminates a process."""
        if confirmed is not True:
            raise ValueError("请先核实原 Codex 执行已停止，再明确确认")
        with self._lock(task_id):
            job = self._authorized(account_id, workspace_id, task_id)
            if job["status"] == "failed":
                return self._public(self._reconcile(job))
            if job["status"] not in {"running", "archiving", "uncertain"} or not job.get("job_id"):
                raise ConflictError("仅可解除没有活动执行锁的待确认生成任务")
            if job["version_id"] in self._versions()["versions"]:
                raise ConflictError("该任务已登记版本，请恢复归档完成结算")
            job.update(status="failed", finished_at=_now(), error="已人工确认原执行停止，冻结积分已释放；不会自动重新生成")
            self._write(job)
            return self._public(job)

    @staticmethod
    def _public(job):
        return {k: v for k, v in job.items() if k not in {"generation_prompt", "request_fingerprint", "claim_id", "worker_id", "attempt_id"}}

    def list_tasks(self, account_id, workspace_id, subproject_id=None):
        projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
        if subproject_id:
            projects.get_subproject(subproject_id)
        result = []
        for path in self.root.glob("*.json"):
            try:
                job = self._authorized(account_id, workspace_id, path.stem)
            except FileNotFoundError:
                continue
            placement = projects.get_task_placement(job["task_id"])
            if subproject_id is None or placement.subproject_id == subproject_id:
                result.append({**self._public(job), "subproject_id": placement.subproject_id})
        return sorted(result, key=lambda j: (j["created_at"], j["task_id"]), reverse=True)

    def _read_artifact(self, artifact_id):
        artifact = self.storage.get_artifact(artifact_id)
        if artifact.format != "TXT":
            raise ValueError("该产物不是文本")
        path = self.storage.get_artifact_path(artifact_id)
        if path.stat().st_size > self.storage.config.max_text_bytes:
            raise ValueError("文本产物超过大小限制")
        content = path.read_bytes()
        if len(content) != artifact.size_bytes or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ValueError("文本产物校验失败")
        content.decode("utf-8")
        return content, artifact

    def detail(self, account_id, workspace_id, task_id):
        job = self._reconcile(self._authorized(account_id, workspace_id, task_id))
        remote_active = job.get("execution_mode") == "remote" and 0 <= time.time() - job.get("heartbeat_at", 0) <= 120
        if job["status"] in {"running", "archiving"} and not remote_active:
            try:
                with self._lock(task_id):
                    job = self._authorized(account_id, workspace_id, task_id)
                    if job["status"] in {"running", "archiving"}:
                        job.update(status="uncertain", error="执行进程已不在服务控制中，请核实原执行；不会自动重跑")
                        self._write(job, settle=bool(job.get("job_id")))
            except LockBusyError:
                pass
        text = self._read_artifact(job["output_artifact_id"])[0].decode("utf-8") if job["status"] == "completed" else ""
        versions = [{k: v for k, v in version.items() if k != "prompt"} for version in self._versions()["versions"].values() if version["document_id"] == job["document_id"]]
        return {**self._public(job), "text": text, "versions": sorted(versions, key=lambda v: v["version"]),
                "can_recover": job["status"] == "uncertain" and (self._candidate(job).is_file() or self.storage._check_path(self._candidate(job).parent / "codex" / "completed.json").is_file())}

    def download(self, account_id, workspace_id, task_id):
        job = self._authorized(account_id, workspace_id, task_id)
        if job["status"] != "completed":
            raise ConflictError("任务尚无已完成的 TXT")
        content, artifact = self._read_artifact(job["output_artifact_id"])
        return content, f"{job['generation_type']}_{job['document_id'][:8]}_v{job['output_version']}.txt"

    def save_revision(self, account_id, workspace_id, task_id, body):
        text = validate_generated_text(body.get("text", ""))
        reason = body.get("change_reason", "")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("请填写修改原因（不超过 2000 字）")
        rid = _validate_id(body.get("request_id"))
        tid = uuid5(NAMESPACE_URL, "script-edit:" + account_id + ":" + rid).hex
        fingerprint = signature({"parent_task": task_id, "text": text, "reason": reason})
        with self._lock(tid):
            with self._control():
                parent = self._authorized(account_id, workspace_id, task_id)
                if parent["status"] != "completed":
                    raise ValueError("只能修改已完成版本")
                if self._path(tid).exists():
                    job = self._authorized(account_id, workspace_id, tid)
                    if job["request_fingerprint"] != fingerprint:
                        raise ConflictError("该请求标识已用于不同修改")
                else:
                    projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
                    sid = projects.get_task_placement(task_id).subproject_id
                    task = projects.create_task(sid, parent["generation_type"], reason, parent["output_artifact_id"], task_id=tid)
                    job = {**parent, "task_id": tid, "request_id": rid, "request_fingerprint": fingerprint, "job_id": None,
                           "status": "archiving", "created_at": task.created_at, "completed_at": None, "subproject_id": sid,
                           "source": "manual_edit", "model": None, "model_id": None, "thinking_level": None,
                           "price": 0, "credit_cost": 0, "credit_status": "not_applicable", "credit_transaction_id": None,
                           "output_artifact_id": uuid4().hex, "version_id": uuid4().hex, "output_version": None,
                           "parent_version_id": parent["version_id"], "change_reason": reason, "generation_prompt": reason}
                    self._write(job, settle=False)
            if job["status"] != "completed":
                self._publish_candidate(job, text)
                self._archive(job)
            return self._public(job)

    def import_text(self, account_id, workspace_id, subproject_id, request_id, text, *, filename="script.txt", generation_type="script"):
        """Save an uploaded document through the same immutable Task/Artifact/Version path, free of charge."""
        text = validate_generated_text(text)
        if generation_type not in {"script", "screenplay"}:
            raise ValueError("内容类型无效")
        projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
        sub = projects.get_subproject(subproject_id)
        rid = _validate_id(request_id)
        tid = uuid5(NAMESPACE_URL, "script-upload:" + account_id + ":" + rid).hex
        fingerprint = signature({"sid": subproject_id, "text": text, "filename": filename, "type": generation_type})
        with self._lock(tid):
            with self._control():
                if self._path(tid).exists():
                    job = self._authorized(account_id, workspace_id, tid)
                    if job["request_fingerprint"] != fingerprint:
                        raise ConflictError("上传请求标识已用于不同内容")
                else:
                    task = projects.create_task(subproject_id, generation_type, "上传脚本：" + filename, task_id=tid)
                    job = {"schema_version": 1, "kind": "script", "task_id": tid, "project_id": task.project_id,
                           "main_project_id": sub.main_project_id, "subproject_id": subproject_id,
                           "account_id": account_id, "workspace_id": workspace_id, "request_id": rid,
                           "request_fingerprint": fingerprint, "job_id": None, "status": "archiving",
                           "generation_type": generation_type, "source": "upload", "model": None, "model_id": None,
                           "thinking_level": None, "price": 0, "credit_cost": 0, "credit_status": "not_applicable",
                           "credit_transaction_id": None, "created_at": task.created_at, "completed_at": None,
                           "input_request": "上传脚本：" + filename, "generation_prompt": "上传文件",
                           "output_artifact_id": uuid4().hex, "version_id": uuid4().hex, "document_id": uuid4().hex,
                           "output_version": None, "change_reason": "上传文件：" + filename}
                    self._write(job, settle=False)
            if job["status"] != "completed":
                self._publish_candidate(job, text)
                self._archive(job)
            return self._public(job)

    def storyboard_source(self, account_id, workspace_id, version_id):
        """Read-only, version-pinned input contract for a future independent Storyboard Skill."""
        version = self._parent(account_id, workspace_id, version_id)
        job = self._authorized(account_id, workspace_id, version["task_id"])
        content, artifact = self._read_artifact(version["artifact_id"])
        return {"schema_version": 1, "generation_type": job["generation_type"], "project_id": job["project_id"],
                "main_project_id": job["main_project_id"], "sub_project_id": WorkspaceProjects(self.storage, self.identity, account_id, workspace_id).get_task_placement(job["task_id"]).subproject_id,
                "task_id": job["task_id"], "artifact_id": artifact.artifact_id, "version_id": version_id,
                "version": version["version"], "sha256": artifact.sha256, "media_type": "text/plain; charset=utf-8",
                "text": content.decode("utf-8"), "parameters": {k: job[k] for k in ("duration", "aspect_ratio", "target_audience", "characters") if k in job}}
