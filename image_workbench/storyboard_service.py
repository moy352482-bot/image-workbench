"""Versioned storyboard planning, independent jobs and bridges to existing video tasks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from .accounts import CreditInsufficient
from .locking import FileLock, LockBusyError
from .script_models import CONFIG_ROOT, DemandRouter, ScriptModelRegistry, signature
from .storage import _now, _validate_id
from .storyboard_models import (EDIT_FIELDS, RATIOS, duration_capabilities, duration_conflict,
                                generation_plan, ids, integer, normalize_plan, normalize_shot,
                                shot_fingerprint, text)
from .storyboard_planner import StoryboardPlanner, build_prompt, load_skill
from .domain_errors import ConflictError
from .workspace_scope import WorkspaceProjects


OPTION_KEYS = {"mode", "selection_mode", "model_id", "thinking_level", "target_duration_ms", "aspect_ratio",
               "visual_style", "characters", "scenes", "requirements", "reference_artifact_ids",
               "video_model_id", "video_project_id"}


def stable(value):
    return uuid5(NAMESPACE_URL, "storyboard:" + value).hex


class StoryboardService:
    def __init__(self, storage, identity, script_source, video_store, *, registry=None, planner=None, pricing_path=None):
        self.storage, self.identity, self.script_source, self.video = storage, identity, script_source, video_store
        self.registry = registry or ScriptModelRegistry()
        self.planner = planner or StoryboardPlanner()
        self.pricing_path = Path(pricing_path or CONFIG_ROOT / "storyboard_pricing.json")
        self.root = storage.files.path(".workbench", "storyboards")
        self.tasks_root = storage.files.path(".workbench", "storyboard-tasks")
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks_root.mkdir(parents=True, exist_ok=True)

    def _control(self):
        return FileLock(self.storage.files.path(".workbench", "storyboard-control.lock"), timeout=30)

    def _task_path(self, tid):
        return self.storage.files.check(self.tasks_root / (_validate_id(tid) + ".json"))

    def _board_path(self, bid):
        return self.storage.files.check(self.root / (_validate_id(bid) + ".json"))

    def _task_lock(self, tid):
        return FileLock(self.storage.files.check(self.tasks_root / (_validate_id(tid) + ".lock")))

    def _pricing(self):
        value = json.loads(self.pricing_path.read_text(encoding="utf-8"))
        if not isinstance(value.get("models"), dict):
            raise ValueError("分镜价格配置无效")
        for levels in value["models"].values():
            if not isinstance(levels, dict) or any(v is not None and (type(v) is not int or v < 0) for v in levels.values()):
                raise ValueError("分镜价格需为非负整数或 null")
        return value

    def config(self, aid, wid):
        self.identity.require_workspace(aid, wid)
        pricing = self._pricing()
        models = deepcopy(self.registry.models())
        for model in models:
            for level in model["thinking_levels"]:
                level["price"] = pricing["models"].get(model["model_id"], {}).get(level["id"])
                level["pricing_status"] = "priced" if level["price"] is not None else "unpriced"
        return {"models": models, "mock_enabled": pricing.get("mock_enabled") is True,
                "real_enabled": pricing.get("real_enabled") is True,
                "free_test_enabled": pricing.get("free_test_enabled") is True,
                "video_models": self.video.config()["video_models"]}

    def normalize_options(self, raw):
        if not isinstance(raw, dict) or set(raw) - OPTION_KEYS:
            raise ValueError("分镜参数含未知字段")
        value = {"mode": "mock", "selection_mode": "recommended", "target_duration_ms": 60000,
                 "aspect_ratio": "16:9", "visual_style": "", "characters": "", "scenes": "", "requirements": "",
                 "reference_artifact_ids": [], "video_model_id": "h3", **deepcopy(raw)}
        if value["mode"] not in {"mock", "codex"} or value["selection_mode"] not in {"recommended", "manual"}:
            raise ValueError("分镜模式或模型选择方式无效")
        integer(value["target_duration_ms"], "目标时长", 1000)
        if value["aspect_ratio"] not in RATIOS or value["video_model_id"] != "h3":
            raise ValueError("分镜比例或视频模型无效")
        for field in ("visual_style", "characters", "scenes", "requirements"):
            value[field] = text(value[field], field)
        value["reference_artifact_ids"] = ids(value["reference_artifact_ids"], limit=9, field="参考资产")
        if value.get("video_project_id"):
            _validate_id(value["video_project_id"])
        for field in ("model_id", "thinking_level"):
            if field in value:
                value[field] = text(value[field], field, 100, required=True)
        return value

    def _source(self, aid, wid, body):
        return self.script_source(aid, wid, _validate_id(body.get("source_script_version")))

    def _check_video_project(self, aid, wid, options, subproject_id):
        if options.get("video_project_id"):
            project = self.video.pipeline_project(aid, wid, options["video_project_id"])
            if project.get("pipeline", {}).get("subproject_id") != subproject_id:
                raise ValueError("视频项目与脚本子项目不一致，请先初始化项目关联")

    def quote_options(self, aid, wid, raw, *, source=None, request=None, _skill=None):
        options = self.normalize_options(raw)
        cfg, pricing = self.config(aid, wid), self._pricing()
        if source:
            self._check_video_project(aid, wid, options, source["sub_project_id"])
            self._validate_refs(aid, wid, source["sub_project_id"], options["reference_artifact_ids"])
        elif request:
            self._check_video_project(aid, wid, options, request["subproject_id"])
            self._validate_refs(aid, wid, request["subproject_id"], options["reference_artifact_ids"])
        demand = {"input_request": source["text"] if source else (request or {}).get("input_request", ""),
                  "constraints": options["requirements"], "characters": options["characters"], "tone": options["visual_style"],
                  "duration": f"{options['target_duration_ms'] / 1000:g}秒", "generation_type": "screenplay"}
        # Only the Python classifier is reused. Never invoke the optional paid flash classifier.
        spec = DemandRouter(self.registry).classify(demand)
        choice = {"model_id": None, "thinking_level": None, "display_name": "未找到可用模型", "price": None}
        blocked = None
        if options["mode"] == "mock":
            choice = {"model_id": "mock-storyboard", "thinking_level": "rules", "display_name": "Mock 分镜（离线规则）", "price": 0}
            blocked = None if cfg["mock_enabled"] else "Mock 模式未启用"
            reason = "离线规则规划，不调用真实模型"
        else:
            models = cfg["models"]
            if options["selection_mode"] == "manual":
                model = next((m for m in models if m["model_id"] == options.get("model_id")), None)
                level = next((l for l in (model or {}).get("thinking_levels", []) if l["id"] == options.get("thinking_level")), None)
                if not model or not model["available"] or not level:
                    raise ValueError("分镜模型或思考强度当前不可用")
                choice = {"model_id": model["model_id"], "display_name": model["display_name"],
                          "thinking_level": level["id"], "price": level["price"]}
                reason = "使用用户独立选择的分镜模型"
            else:
                preferred = ([("gpt-6-astra", "high"), ("gpt-6.1-sol", "high"), ("gpt-6-luna", "high")]
                             if spec["complexity"] == "complex" else
                             [("gpt-6.1-sol", "medium"), ("gpt-6-luna", "medium"), ("gpt-6-luna", "high"), ("gpt-6-astra", "medium")])
                candidates = [(m, l) for mid, effort in preferred for m in models if m["model_id"] == mid and m["available"]
                              for l in m["thinking_levels"] if l["id"] == effort]
                candidate = next(((m, l) for m, l in candidates if l["price"] is not None), candidates[0] if candidates else None)
                if candidate:
                    model, level = candidate
                    choice = {"model_id": model["model_id"], "display_name": model["display_name"],
                              "thinking_level": level["id"], "price": level["price"]}
                reason = spec["reason"] + "；仅使用 Python 规则推荐"
            if not cfg["real_enabled"]:
                blocked = "真实分镜调用尚未批准开放，请使用 Mock"
            elif choice["price"] is None:
                blocked = "分镜模型/思考强度尚未独立定价，禁止真实调用"
            elif choice["price"] == 0 and not pricing.get("free_test_enabled"):
                blocked = "真实免费测试尚未启用"
        remaining = self.identity.wallet(aid)["remaining_credit"]
        skill = _skill if _skill is not None else load_skill()
        shortfall = max(0, (choice["price"] or 0) - remaining) if remaining is not None else 0
        signature_body = {"options": options, "choice": choice, "source_version": source["version_id"] if source else None,
                          "source_hash": source["sha256"] if source else signature(request or {}),
                          "skill_sha256": skill["sha256"], "pricing": pricing,
                          "video_models": cfg["video_models"]}
        return {**choice, "reason": reason, "demand_spec": spec, "shortfall": shortfall, "remaining_credit": remaining,
                "pricing_status": "priced" if choice["price"] is not None else "unpriced",
                "can_generate": not blocked and shortfall == 0, "blocked_reason": blocked,
                "quote_signature": signature(signature_body)}

    def quote(self, aid, wid, body):
        source = self._source(aid, wid, body)
        return self.quote_options(aid, wid, {k: v for k, v in body.items() if k in OPTION_KEYS}, source=source)

    def _binding(self, aid, wid, source, options):
        sid = source["sub_project_id"]
        self._check_video_project(aid, wid, options, sid)
        pid = options.get("video_project_id")
        if not pid:
            for project in self.video.projects(aid, wid):
                candidate = self.video.pipeline_project(aid, wid, project["project_id"])
                if candidate.get("pipeline", {}).get("subproject_id") == sid:
                    pid = project["project_id"]
                    break
        if not pid:
            pid = stable("video-project:" + aid + ":" + wid + ":" + sid)
            # Only create once; an existing project may contain a newer selected script.
            try:
                self.video.pipeline_project(aid, wid, pid)
            except FileNotFoundError:
                self.video.create_project(aid, wid, pid, "分镜创作 · " + sid[:8])
            self.video.ensure_pipeline_binding(aid, wid, pid,
                lambda _: {"main_project_id": source["main_project_id"], "subproject_id": sid})
        return pid

    def _task(self, aid, wid, tid):
        value = self.storage.files.read_json(self._task_path(tid))
        if value.get("kind") != "storyboard" or value.get("task_id") != tid:
            raise ValueError("分镜任务记录无效")
        if (value["account_id"], value["workspace_id"]) != (aid, wid):
            raise FileNotFoundError("分镜任务不存在")
        WorkspaceProjects(self.storage, self.identity, aid, wid).get_subproject(value["subproject_id"])
        return value

    def _write_task(self, job, *, settle=False):
        job["updated_at"] = _now()
        self.storage.files.write_json(self._task_path(job["task_id"]), job)
        if settle and job.get("job_id"):
            self.identity.record_job(job)
            self.storage.files.write_json(self._task_path(job["task_id"]), job)

    def _public_task(self, job):
        value = {k: deepcopy(v) for k, v in job.items() if k not in {"prompt", "source", "fingerprint", "quote", "candidate"}}
        if value.get("preview"):
            for shot in value["preview"]["shots"]:
                shot["duration_compatibility"] = generation_plan(shot["narrative_duration_ms"], shot["generation_duration_ms"],
                                                               duration_capabilities(self.video.config(), shot["video_model_id"]))
        return value

    def task(self, aid, wid, tid):
        job = self._task(aid, wid, tid)
        if job.get("job_id") and job["status"] in {"completed", "failed"}:
            settled = self.identity.settled_job(job["job_id"])
            if not settled:
                self._write_task(job, settle=True)
            else:
                job["credit_status"] = settled["credit_status"]
        if job.get("cancel_requested") and job["status"] == "failed":
            job.update(status="cancelled", finished_at=job.get("finished_at") or _now())
            self._write_task(job)
        return self._public_task(job)

    def submit(self, aid, wid, body, *, dependency=None, replan=None):
        source = self._source(aid, wid, body)
        options = self.normalize_options({k: v for k, v in body.items() if k in OPTION_KEYS})
        rid = _validate_id(body.get("request_id"))
        tid = stable("task:" + aid + ":" + rid)
        fingerprint = signature({"source": source["version_id"], "options": options, "replan": replan})
        with self._control():
            # Recover a successful SQLite reservation whose JSON publication was interrupted.
            if not self._task_path(tid).exists():
                pending = next((j for j in self.identity.pending_jobs() if j.get("kind") == "storyboard" and j.get("task_id") == tid), None)
                if pending:
                    self._write_task(pending)
            if self._task_path(tid).exists():
                old = self._task(aid, wid, tid)
                if old["fingerprint"] != fingerprint:
                    raise ConflictError("分镜请求标识已用于其他内容")
                return self._public_task(old)
            if body.get("confirmed") is not True:
                raise ValueError("请确认分镜模型、思考强度和预计积分")
            skill = load_skill()
            quote = self.quote_options(aid, wid, options, source=source, _skill=skill)
            if dependency:
                # Preserve the exact earlier approved model; do not silently re-route after TXT creation.
                approved = dependency["storyboard_quote"]
                if options["mode"] == "codex":
                    options.update(selection_mode="manual", model_id=approved["model_id"], thinking_level=approved["thinking_level"])
                    quote = self.quote_options(aid, wid, options, source=source, _skill=skill)
                if quote["price"] != approved["price"] or quote["model_id"] != approved["model_id"] or quote["thinking_level"] != approved["thinking_level"]:
                    raise ConflictError("分镜价格或模型已变化，脚本已保留，请重新确认分镜报价")
            elif body.get("quote_signature") != quote["quote_signature"]:
                raise ConflictError("分镜报价或参数已变化，请重新报价")
            if quote["shortfall"]:
                raise CreditInsufficient(quote["price"], quote["remaining_credit"])
            if not quote["can_generate"]:
                raise ValueError(quote["blocked_reason"] or "分镜暂不可执行")
            if replan:
                envelope = self._board(aid, wid, replan["storyboard_id"])
                if envelope["current_version"] != replan["expected_version"]:
                    raise ConflictError("正式分镜版本已变化，请重新规划")
            pid = self._binding(aid, wid, source, options)
            projects = WorkspaceProjects(self.storage, self.identity, aid, wid)
            task = projects.create_task(source["sub_project_id"], "storyboard", "分镜规划", source["artifact_id"], task_id=tid)
            capability = duration_capabilities(self.video.config(), options["video_model_id"])
            internal_options = {**options, "_video_capability": capability}
            job = {"schema_version": 1, "kind": "storyboard", "task_id": tid, "request_id": rid,
                   "account_id": aid, "workspace_id": wid, "project_id": task.project_id,
                   "main_project_id": source["main_project_id"], "subproject_id": source["sub_project_id"],
                   "video_project_id": pid, "storyboard_id": replan["storyboard_id"] if replan else stable("board:" + tid),
                   "source_script_artifact_id": source["artifact_id"], "source_script_version": source["version_id"],
                   "depends_on": {"script_task_id": source["task_id"], "artifact_id": source["artifact_id"], "version_id": source["version_id"]},
                   "source": source, "options": internal_options, "fingerprint": fingerprint, "quote": quote,
                   "model_id": quote["model_id"], "thinking_level": quote["thinking_level"],
                   "price": quote["price"], "status": "queued", "review_status": "planning",
                   "created_at": _now(), "finished_at": None, "error": None, "replan": replan,
                   "job_id": uuid4().hex if options["mode"] == "codex" else None,
                   "credit_status": "not_applicable" if options["mode"] == "mock" else "credit_frozen"}
            job["skill_reference"] = {k: v for k, v in skill.items() if k != "instructions"}
            job["prompt"] = build_prompt(source, internal_options, capability, replan.get("selected_shots") if replan else None, skill=skill)
            if job["job_id"]:
                job = self.identity.reserve(job, {"account_id": aid, "workspace_id": wid}, quote["price"])
            self._write_task(job)
        return self._public_task(job)

    def _work(self, job):
        path = self.storage.files.path("projects", job["project_id"], "tasks", job["task_id"], "logs", "storyboard")
        path.mkdir(exist_ok=True)
        return path

    def run(self, aid, wid, tid):
        try:
            with self._task_lock(tid):
                job = self._task(aid, wid, tid)
                if job["status"] in {"completed", "failed", "cancelled"}:
                    return self.task(aid, wid, tid)
                work = self._work(job)
                candidate_path = self.storage.files.check(work / "candidate.json")
                codex_receipt = self.storage.files.check(work / "codex" / "completed.json")
                recovering = job["status"] != "queued" and not candidate_path.exists() and codex_receipt.exists()
                if job["status"] != "queued" and not candidate_path.exists() and not recovering:
                    job.update(status="uncertain", error="原执行结果待确认；不会重复调用模型")
                    self._write_task(job)
                    return self._public_task(job)
                phase = "invoke"
                try:
                    if candidate_path.exists():
                        receipt = self.storage.files.read_json(candidate_path)
                        if receipt.get("fingerprint") != job["fingerprint"] or receipt.get("sha256") != signature(receipt.get("plan")):
                            raise ValueError("分镜候选与任务凭据不一致，禁止归档")
                        raw = normalize_plan(receipt["plan"], job["options"], tid)
                    else:
                        if job["job_id"]:
                            reservation = self.identity.require_reservation(job["job_id"], job["project_id"], tid)
                            held = json.loads(reservation["payload"])
                            if reservation["amount"] != job["price"] or any(held.get(k) != job.get(k) for k in ("model_id", "thinking_level", "prompt", "fingerprint")):
                                raise ValueError("分镜执行与已确认的积分预约不一致")
                            # Availability is checked at execution, but an existing reservation retains its quoted price.
                            if not recovering:
                                model = next((m for m in self.registry.models() if m["model_id"] == job["model_id"]), None)
                                if not model or not model["available"] or job["thinking_level"] not in [l["id"] for l in model["thinking_levels"]]:
                                    raise ValueError("分镜执行模型已不可用")
                                if not self._pricing().get("real_enabled"):
                                    raise ValueError("真实分镜调用已关闭")
                        if recovering:
                            raw = json.loads(self.planner.adapter.recover(prompt=job["prompt"], model_id=job["model_id"],
                                             thinking_level=job["thinking_level"], work_dir=work / "codex"))
                        else:
                            job.update(status="running", started_at=_now())
                            self._write_task(job, settle=True)
                            raw = self.planner.plan(job["source"], job["options"], job["quote"], work / "codex",
                                                    selected_shots=(job.get("replan") or {}).get("selected_shots"),
                                                    prompt=job["prompt"])
                        phase = "validate"
                        raw = normalize_plan(raw, job["options"], tid)
                        allowed_refs = set(job["options"]["reference_artifact_ids"])
                        for selected in (job.get("replan") or {}).get("selected_shots") or []:
                            allowed_refs.update(selected["reference_artifact_ids"])
                            allowed_refs.update(selected[k] for k in ("start_frame_artifact_id", "end_frame_artifact_id") if selected.get(k))
                        for shot in raw["shots"]:
                            self._validate_shot_refs(aid, wid, job["subproject_id"], shot)
                            references = set(shot["reference_artifact_ids"])
                            references.update(shot[k] for k in ("start_frame_artifact_id", "end_frame_artifact_id") if shot.get(k))
                            if not references.issubset(allowed_refs):
                                raise ValueError("规划引用了未提供给本次模型的资产，请先明确选择参考图")
                            compatibility = generation_plan(shot["narrative_duration_ms"], shot["generation_duration_ms"],
                                                            job["options"]["_video_capability"])
                            if not compatibility["supported"]:
                                raise ValueError("模型规划的素材时长不符合视频能力：" + compatibility["message"])
                        phase = "persist"
                        self.storage.files.write_json(candidate_path, {"schema_version": 1, "plan": raw, "fingerprint": job["fingerprint"], "sha256": signature(raw)})
                    with self._control():
                        if job.get("replan"):
                            phase = "validate"
                            job["preview"] = self._preview(aid, wid, job, raw)
                        else:
                            phase = "persist"
                            self._publish_initial(job, raw)
                        phase = "persist"
                        job.update(status="completed", review_status="awaiting_review", finished_at=_now(), error=None)
                        self._write_task(job, settle=True)
                except Exception as exc:
                    uncertain = getattr(exc, "uncertain", False) or phase == "persist"
                    job.update(status="uncertain" if uncertain else "failed", error=str(exc)[:2000], finished_at=_now())
                    self._write_task(job, settle=True)
                return self._public_task(job)
        except LockBusyError:
            return self.task(aid, wid, tid)

    def cancel(self, aid, wid, tid):
        with self._task_lock(tid):
            job = self._task(aid, wid, tid)
            if job["status"] == "cancelled" or job.get("cancel_requested"):
                return self.task(aid, wid, tid)
            if job["status"] != "queued":
                raise ConflictError("只能取消尚未开始的分镜任务")
            if job["job_id"]:
                job.update(status="failed", cancel_requested=True, error="用户取消排队分镜")
                self._write_task(job, settle=True)
            job.update(status="cancelled", finished_at=_now(), error="用户取消排队分镜")
            self._write_task(job)
            return self._public_task(job)

    def _board(self, aid, wid, bid):
        value = self.storage.files.read_json(self._board_path(bid))
        if value.get("storyboard_id") != bid or (value.get("account_id"), value.get("workspace_id")) != (aid, wid):
            raise FileNotFoundError("分镜不存在")
        WorkspaceProjects(self.storage, self.identity, aid, wid).get_subproject(value["subproject_id"])
        if value.get("pending"):
            self._finish_version(value)
        return value

    def _finish_version(self, envelope):
        pending = envelope.get("pending")
        if not pending:
            return
        record = pending["record"]
        projects = WorkspaceProjects(self.storage, self.identity, envelope["account_id"], envelope["workspace_id"])
        task = projects.create_task(envelope["subproject_id"], "storyboard", record["change_reason"] or "分镜版本",
                                    record.get("parent_artifact_id") or record["source_script_artifact_id"],
                                    task_id=pending["task_id"])
        data = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        self.storage.register_output_document(task.project_id, task.task_id, data, artifact_id=record["artifact_id"], format="JSON")
        envelope["versions"].append(record)
        envelope["current_version"] = record["version"]
        envelope["operations"][pending["request_id"]] = {"fingerprint": pending["fingerprint"], "version": record["version"]}
        envelope.pop("pending")
        self.storage.files.write_json(self._board_path(envelope["storyboard_id"]), envelope)

    def _save_version(self, envelope, record, rid, fingerprint):
        record = deepcopy(record)
        record["version"] = envelope["current_version"] + 1
        record["created_at"] = _now()
        record["artifact_id"] = stable("artifact:" + envelope["storyboard_id"] + ":" + rid)
        record["parent_artifact_id"] = envelope["versions"][-1]["artifact_id"] if envelope["versions"] else None
        envelope["pending"] = {"record": record, "request_id": rid, "fingerprint": fingerprint,
                               "task_id": stable("version-task:" + envelope["storyboard_id"] + ":" + rid)}
        self.storage.files.write_json(self._board_path(envelope["storyboard_id"]), envelope)
        self._finish_version(envelope)
        return envelope["versions"][-1]

    def _publish_initial(self, job, plan):
        bid = job["storyboard_id"]
        if self._board_path(bid).exists():
            envelope = self._board(job["account_id"], job["workspace_id"], bid)
            if envelope["versions"]:
                return
        else:
            envelope = {k: job[k] for k in ("account_id", "workspace_id", "project_id", "main_project_id", "subproject_id", "video_project_id", "storyboard_id")}
            envelope.update(schema_version=1, current_version=0, versions=[], operations={})
        record = {**plan, "schema_version": 1, "storyboard_id": bid, "source_script_artifact_id": job["source_script_artifact_id"],
                  "source_script_version": job["source_script_version"], "source_script_version_number": job["source"]["version"],
                  "target_duration_ms": job["options"]["target_duration_ms"], "status": "awaiting_review",
                  "retired_shots": [], "model_id": job["model_id"], "thinking_level": job["thinking_level"], "prompt": job["prompt"],
                  "planning_task_id": job["task_id"], "mode": job["options"]["mode"], "options": job["options"],
                  "change_reason": "首次分镜规划", "change_method": "planning", "changes": []}
        self._save_version(envelope, record, job["request_id"], job["fingerprint"])

    def list_storyboards(self, aid, wid, *, video_project_id=None, subproject_id=None):
        self.identity.require_workspace(aid, wid)
        if video_project_id:
            self.video.pipeline_project(aid, wid, video_project_id)
        if subproject_id:
            WorkspaceProjects(self.storage, self.identity, aid, wid).get_subproject(subproject_id)
        result = []
        with self._control():
            for path in self.root.glob("*.json"):
                if path.name.startswith(".tmp-"):
                    continue
                try:
                    board = self._board(aid, wid, path.stem)
                except FileNotFoundError:
                    continue
                if (video_project_id and board["video_project_id"] != video_project_id) or (subproject_id and board["subproject_id"] != subproject_id) or not board["versions"]:
                    continue
                latest = board["versions"][-1]
                result.append({k: latest[k] for k in ("storyboard_id", "version", "status", "target_duration_ms", "created_at", "source_script_version")})
        return sorted(result, key=lambda r: (r["created_at"], r["storyboard_id"]), reverse=True)

    def detail(self, aid, wid, bid, version=None):
        with self._control():
            board = self._board(aid, wid, bid)
            number = board["current_version"] if version is None else integer(version, "分镜版本", maximum=1000000)
            record = next((v for v in board["versions"] if v["version"] == number), None)
            if not record:
                raise FileNotFoundError("分镜版本不存在")
            record = deepcopy(record)
            for shot in record["shots"]:
                shot["duration_compatibility"] = generation_plan(shot["narrative_duration_ms"], shot["generation_duration_ms"],
                    duration_capabilities(self.video.config(), shot["video_model_id"]))
            return {**record, "video_project_id": board["video_project_id"], "subproject_id": board["subproject_id"],
                    "versions": [{k: v[k] for k in ("version", "status", "created_at", "change_reason")} for v in board["versions"]],
                    "videos": self._videos(aid, wid, board, record)}

    def _edit_changes(self, changes, source, shot_id, order, options, *, source_ids=None):
        if not isinstance(changes, dict) or set(changes) - EDIT_FIELDS:
            raise ValueError("镜头编辑包含不支持的字段")
        raw = {**source, **changes}
        raw.setdefault("title", "新镜头")
        raw.setdefault("visual_description", "待补充画面描述")
        raw.setdefault("duration_reason", "手动分配，待审核")
        raw.setdefault("narrative_duration_ms", 4000)
        raw.setdefault("generation_duration_ms", 4000)
        if source_ids is not None:
            raw["source_shot_ids"] = source_ids
        return normalize_shot(raw, shot_id=shot_id, order=order, options=options)

    def edit(self, aid, wid, bid, body):
        rid = _validate_id(body.get("request_id"))
        fingerprint = signature({k: v for k, v in body.items() if k != "request_id"})
        with self._control():
            envelope = self._board(aid, wid, bid)
            previous = envelope["operations"].get(rid)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("编辑请求标识已用于其他内容")
                return self._detail_unlocked(aid, wid, envelope, previous["version"])
            if body.get("expected_version") != envelope["current_version"]:
                raise ConflictError("分镜版本已变化，请刷新后再修改")
            record = deepcopy(envelope["versions"][-1])
            previous_target = record["target_duration_ms"]
            before = deepcopy(record["shots"])
            shots = record["shots"]
            op = body.get("operation")
            reason = text(body.get("reason") or "手动编辑", "修改原因", 2000)
            options = record["options"]
            selected = next((s for s in shots if s["shot_id"] == body.get("shot_id")), None)
            if op in {"update", "delete", "split"} and not selected:
                raise FileNotFoundError("镜头不存在")
            retired = []
            if op == "add":
                shots.append(self._edit_changes(body.get("changes", {}), {}, stable(rid + ":add"), len(shots) + 1, options))
            elif op == "update":
                shots[shots.index(selected)] = self._edit_changes(body.get("changes", {}), selected, selected["shot_id"], selected["order"], options)
            elif op == "delete":
                shots.remove(selected)
                retired = [selected]
            elif op == "split":
                parts = body.get("parts", [{}, {}])
                if not isinstance(parts, list) or not 2 <= len(parts) <= 20:
                    raise ValueError("拆分需要 2–20 个子镜头")
                position = shots.index(selected)
                children = []
                for i, part in enumerate(parts):
                    narrative = selected["narrative_duration_ms"] // len(parts) + (selected["narrative_duration_ms"] % len(parts) if i == len(parts) - 1 else 0)
                    default = {**selected, "narrative_duration_ms": max(1, narrative),
                               "title": selected["title"][:100] + f" · {i + 1}", "duration_reason": "由原镜头手动拆分，待审核"}
                    child = self._edit_changes(part, default, stable(rid + ":split:" + str(i)), position + i + 1, options,
                                               source_ids=[selected["shot_id"]])
                    compatible = generation_plan(child["narrative_duration_ms"], child["narrative_duration_ms"],
                                                 duration_capabilities(self.video.config(), child["video_model_id"]))
                    if "generation_duration_ms" not in part:
                        child["generation_duration_ms"] = compatible["generation_duration_ms"]
                    children.append(child)
                shots[position:position + 1] = children
                retired = [selected]
            elif op == "merge":
                merge_ids = ids(body.get("shot_ids", []))
                if len(merge_ids) < 2:
                    raise ValueError("请至少选择两个相邻镜头")
                sources = [s for s in shots if s["shot_id"] in merge_ids]
                positions = [shots.index(s) for s in sources]
                if len(sources) != len(merge_ids) or positions != list(range(min(positions), min(positions) + len(positions))):
                    raise ValueError("只能合并相邻镜头")
                default = deepcopy(sources[0])
                for key in ("visual_description", "action", "dialogue", "voiceover", "video_prompt", "source_excerpt"):
                    default[key] = "\n".join(s[key] for s in sources if s[key])
                default.update(title=" + ".join(s["title"] for s in sources)[:120],
                               narrative_duration_ms=sum(s["narrative_duration_ms"] for s in sources),
                               generation_duration_ms=sum(s["generation_duration_ms"] for s in sources),
                               reference_artifact_ids=list(dict.fromkeys(a for s in sources for a in s["reference_artifact_ids"])),
                               duration_reason="相邻镜头合并，默认保留叙事时长之和；请核对模型单次生成上限")
                merged = self._edit_changes(body.get("changes", {}), default, stable(rid + ":merge"), positions[0] + 1, options, source_ids=[s["shot_id"] for s in sources])
                shots[positions[0]:positions[-1] + 1] = [merged]
                retired = sources
            elif op == "reorder":
                order = ids(body.get("shot_ids", []))
                if set(order) != {s["shot_id"] for s in shots}:
                    raise ValueError("排序必须包含全部当前镜头且不能重复")
                by_id = {s["shot_id"]: s for s in shots}
                shots[:] = [by_id[sid] for sid in order]
            elif op not in {"save", "approve", "revision_required"}:
                raise ValueError("未知分镜操作")
            if len(shots) > 200:
                raise ValueError("分镜最多 200 个镜头")
            for i, shot in enumerate(shots):
                shot["order"] = i + 1
                self._validate_shot_refs(aid, wid, envelope["subproject_id"], shot)
            for old in retired:
                record["retired_shots"].append({**old, "status": "superseded" if op in {"split", "merge"} else "deleted",
                                                "replaced_in_version": envelope["current_version"] + 1})
            if "target_duration_ms" in body:
                record["target_duration_ms"] = integer(body["target_duration_ms"], "目标时长", 1000)
                record["options"]["target_duration_ms"] = record["target_duration_ms"]
            # Renaming, reference/format edits and reordering do not resolve a
            # planner's warning about dialogue, action or emotional timing.
            timing_fields = {"narrative_duration_ms", "visual_description", "action", "dialogue",
                             "voiceover", "camera_movement", "emotion", "video_prompt"}
            prior_timing = {s["shot_id"]: {k: s.get(k) for k in timing_fields} for s in before}
            current_timing = {s["shot_id"]: {k: s.get(k) for k in timing_fields} for s in shots}
            inherited_conflict = record["duration_conflict"] if (prior_timing == current_timing
                                  and record["target_duration_ms"] == previous_target) else None
            record["duration_conflict"] = duration_conflict(shots, record["target_duration_ms"], inherited_conflict)
            if op == "approve":
                if not shots or record["duration_conflict"]:
                    raise ConflictError("请先处理空分镜或总时长冲突，再审核通过")
                record["status"] = "approved"
            elif op == "revision_required":
                record["status"] = "revision_required"
            else:
                record["status"] = "draft"
            record.update(change_reason=reason, change_method=op, changes=[{"before": before, "after": deepcopy(shots),
                          "method": op, "reason": reason, "changed_at": _now(), "source_shot_ids": [s["shot_id"] for s in retired]}])
            self._save_version(envelope, record, rid, fingerprint)
            return self._detail_unlocked(aid, wid, envelope)

    def _detail_unlocked(self, aid, wid, envelope, version=None):
        # Callers already hold the non-reentrant cross-process storyboard lock.
        record = deepcopy(next(v for v in envelope["versions"] if v["version"] == (version or envelope["current_version"])))
        for shot in record["shots"]:
            shot["duration_compatibility"] = generation_plan(shot["narrative_duration_ms"], shot["generation_duration_ms"],
                                                            duration_capabilities(self.video.config(), shot["video_model_id"]))
        return {**record, "video_project_id": envelope["video_project_id"], "subproject_id": envelope["subproject_id"],
                "versions": [{k: v[k] for k in ("version", "status", "created_at", "change_reason")} for v in envelope["versions"]],
                "videos": self._videos(aid, wid, envelope, record)}

    def _replan_input(self, aid, wid, bid, body):
        with self._control():
            envelope = self._board(aid, wid, bid)
            if body.get("expected_version") != envelope["current_version"]:
                raise ConflictError("分镜版本已变化，请刷新后重新规划")
            current = envelope["versions"][-1]
            chosen = ids(body.get("shot_ids", []))
            selected = [deepcopy(s) for s in current["shots"] if s["shot_id"] in chosen]
            if len(selected) != len(chosen):
                raise ValueError("选中范围包含无效镜头")
            groups = []
            for shot in selected:
                if not groups or groups[-1][-1]["order"] + 1 != shot["order"]:
                    groups.append([])
                groups[-1].append(shot)
            reason = text(body.get("reason", ""), "重新规划原因", 2000, required=True)
            options = {k: v for k, v in current["options"].items() if k in OPTION_KEYS}
            options.update({k: v for k, v in body.items() if k in OPTION_KEYS})
            options["requirements"] = options.get("requirements", "") + "\n修改理由：" + reason
            if chosen:
                options["target_duration_ms"] = sum(s["narrative_duration_ms"] for s in selected)
            replan = {"storyboard_id": bid, "expected_version": current["version"], "shot_ids": chosen,
                      "selected_shots": selected, "selection_groups": [[s["shot_id"] for s in group] for group in groups],
                      "reason": reason}
            return {"source_script_version": current["source_script_version"], **options}, replan

    def replan_quote(self, aid, wid, bid, body):
        request, replan = self._replan_input(aid, wid, bid, body)
        quote = self.quote(aid, wid, request)
        quote["quote_signature"] = signature({"quote": quote["quote_signature"], "replan": replan})
        return quote

    def replan(self, aid, wid, bid, body):
        request, context = self._replan_input(aid, wid, bid, body)
        quote = self.quote(aid, wid, request)
        if body.get("quote_signature") != signature({"quote": quote["quote_signature"], "replan": context}):
            raise ConflictError("重新规划的范围、版本或报价已变化")
        return self.submit(aid, wid, {**request, "request_id": body.get("request_id"),
                           "quote_signature": quote["quote_signature"], "confirmed": body.get("confirmed")}, replan=context)

    def _preview(self, aid, wid, job, plan):
        envelope = self._board(aid, wid, job["storyboard_id"])
        base = next(v for v in envelope["versions"] if v["version"] == job["replan"]["expected_version"])
        preview = deepcopy(base)
        ids_to_replace = job["replan"]["shot_ids"] or [s["shot_id"] for s in base["shots"]]
        old = [s for s in base["shots"] if s["shot_id"] in ids_to_replace]
        proposed = deepcopy(plan["shots"])
        groups = job["replan"].get("selection_groups") or [ids_to_replace]
        by_group = {i: [] for i in range(len(groups))}
        for shot in proposed:
            sources = shot.get("source_shot_ids") or (groups[0] if len(groups) == 1 else [])
            group_index = next((i for i, group in enumerate(groups) if sources and set(sources).issubset(group)), None)
            if group_index is None:
                raise ValueError("非连续局部重规划必须为每个候选标注选中范围内的 source_shot_ids，不能跨越未选镜头")
            shot["source_shot_ids"] = list(sources)
            by_group[group_index].append(shot)
        if any(not values for values in by_group.values()):
            raise ValueError("局部规划遗漏了选中段落，不能删除该剧情")
        new_shots, inserted = [], set()
        for shot in base["shots"]:
            if shot["shot_id"] in ids_to_replace:
                index = next(i for i, group in enumerate(groups) if shot["shot_id"] in group)
                if index not in inserted:
                    new_shots.extend(by_group[index])
                    inserted.add(index)
            else:
                new_shots.append(deepcopy(shot))
        preview["shots"] = new_shots
        for i, shot in enumerate(new_shots):
            shot["order"] = i + 1
        preview["retired_shots"].extend({**deepcopy(s), "status": "superseded", "replaced_in_version": base["version"] + 1} for s in old)
        if not job["replan"]["shot_ids"]:
            preview["target_duration_ms"] = job["options"]["target_duration_ms"]
            preview["options"] = deepcopy(job["options"])
        conflict = plan.get("duration_conflict")
        if conflict:
            untouched = sum(s["narrative_duration_ms"] for s in base["shots"] if s["shot_id"] not in ids_to_replace)
            conflict = {**conflict, "reasonable_duration_ms": conflict["reasonable_duration_ms"] + untouched}
        preview.update(duration_conflict=duration_conflict(new_shots, preview["target_duration_ms"], conflict),
                       status="awaiting_review", model_id=job["model_id"], thinking_level=job["thinking_level"],
                       prompt=job["prompt"], planning_task_id=job["task_id"], change_reason=job["replan"]["reason"],
                       change_method="ai_replan", mode=job["options"]["mode"],
                       changes=[{"before": deepcopy(old), "after": proposed, "method": "ai_replan",
                                 "reason": job["replan"]["reason"], "changed_at": _now(), "source_shot_ids": ids_to_replace}])
        return preview

    def accept(self, aid, wid, bid, body):
        rid = _validate_id(body.get("request_id"))
        fingerprint = signature({"method": "accept", **body})
        with self._control():
            envelope = self._board(aid, wid, bid)
            previous = envelope["operations"].get(rid)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ConflictError("确认请求标识已用于其他候选")
                return self._detail_unlocked(aid, wid, envelope, previous["version"])
            task = self._task(aid, wid, _validate_id(body.get("task_id")))
            if task["storyboard_id"] != bid or task["status"] != "completed" or not task.get("preview") or not task.get("replan"):
                raise ValueError("该任务没有可接受的分镜候选")
            if body.get("expected_version") != envelope["current_version"] or task["replan"]["expected_version"] != envelope["current_version"]:
                raise ConflictError("正式分镜已变化，不能覆盖，请重新规划")
            self._save_version(envelope, task["preview"], rid, fingerprint)
            task["review_status"] = "approved"
            task["accepted_version"] = envelope["current_version"]
            self._write_task(task)
            return self._detail_unlocked(aid, wid, envelope)

    def _validate_refs(self, aid, wid, sid, references):
        projects = WorkspaceProjects(self.storage, self.identity, aid, wid)
        sub = projects.get_subproject(sid)
        for reference in ids(references, limit=9, field="引用资产"):
            try:
                artifact = self.storage.get_artifact(reference)
                placement = projects.get_task_placement(artifact.task_id)
                ref_sub = projects.get_subproject(placement.subproject_id)
                if ref_sub.main_project_id != sub.main_project_id or artifact.format not in {"PNG", "JPEG", "WEBP"}:
                    raise FileNotFoundError("资产不属于当前项目")
                path = self.storage.get_artifact_path(reference)
                if path.stat().st_size != artifact.size_bytes or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
                    raise ValueError("引用图片校验失败：" + reference)
            except FileNotFoundError as exc:
                raise ValueError("引用资产缺失或不可访问：" + reference) from exc

    def _validate_shot_refs(self, aid, wid, sid, shot):
        references = list(dict.fromkeys([*shot["reference_artifact_ids"], *[shot[k] for k in ("start_frame_artifact_id", "end_frame_artifact_id") if shot.get(k)]]))
        self._validate_refs(aid, wid, sid, references)

    def assets(self, aid, wid, bid):
        with self._control():
            envelope = self._board(aid, wid, bid)
            return self._assets(aid, wid, envelope)

    def _assets(self, aid, wid, envelope):
        projects = WorkspaceProjects(self.storage, self.identity, aid, wid)
        link = {"main_project_id": envelope["main_project_id"], "subproject_id": envelope["subproject_id"]}
        pid = envelope["video_project_id"]
        video_assets = self.video.assets(aid, wid, pid)
        mapping = {}
        # Existing reference uploads receive immutable Artifact identities on first storyboard use.
        for asset in video_assets:
            artifact_id = asset.get("artifact_id")
            if not artifact_id:
                tid = stable("asset-task:" + pid + ":" + asset["asset_id"])
                task = projects.create_task(link["subproject_id"], "asset", "已有视频参考图登记", task_id=tid)
                artifacts = self.storage.list_task_artifacts(task.project_id, tid)
                artifact = next(iter(artifacts), None)
                if artifact is None:
                    path, _ = self.video.asset_file(aid, wid, pid, asset["asset_id"])
                    artifact = self.storage.add_input_image(task.project_id, tid, path, role="reference")
                artifact_id = artifact.artifact_id
                self.video.associate_artifact(aid, wid, pid, asset["asset_id"], artifact_id)
            mapping[artifact_id] = asset["asset_id"]
        result = []
        for placement in projects.catalog_view()["task_placements"].values():
            if placement["storage_project_id"] != envelope["project_id"]:
                continue
            try:
                artifacts = self.storage.list_task_artifacts(envelope["project_id"], placement["task_id"])
            except FileNotFoundError:
                continue
            for artifact in artifacts:
                if artifact.format not in {"PNG", "JPEG", "WEBP"}:
                    continue
                try:
                    available = self.storage.get_artifact_path(artifact.artifact_id).exists()
                except FileNotFoundError:
                    available = False
                result.append({"artifact_id": artifact.artifact_id, "title": artifact.original_filename,
                               "video_asset_id": mapping.get(artifact.artifact_id), "available": available,
                               "url": f"/api/artifacts/{artifact.artifact_id}?workspace_id={wid}"})
        return result

    def video_draft(self, aid, wid, bid, shot_id, body):
        rid = _validate_id(body.get("request_id"))
        with self._control():
            envelope = self._board(aid, wid, bid)
            record = next((v for v in envelope["versions"] if v["version"] == body.get("expected_version")), None)
            if not record:
                raise ConflictError("分镜版本不存在")
            if record["version"] != envelope["current_version"]:
                raise ConflictError("请从最新分镜版本创建视频，历史媒体仍然保留")
            if record["status"] != "approved" and body.get("allow_draft_test") is not True:
                raise ConflictError("未审核分镜只允许明确确认的单镜头草稿测试")
            shot = next((s for s in record["shots"] if s["shot_id"] == shot_id), None)
            if not shot:
                raise FileNotFoundError("镜头不存在")
            compatibility = generation_plan(shot["narrative_duration_ms"], shot["generation_duration_ms"],
                                            duration_capabilities(self.video.config(), shot["video_model_id"]))
            if not compatibility["supported"]:
                raise ValueError(compatibility["message"])
            self._validate_shot_refs(aid, wid, envelope["subproject_id"], shot)
            self._assets(aid, wid, envelope)
            pid = envelope["video_project_id"]
            if shot.get("start_frame_artifact_id"):
                references = [shot["start_frame_artifact_id"]]
                if shot.get("end_frame_artifact_id"):
                    references.append(shot["end_frame_artifact_id"])
                if set(shot["reference_artifact_ids"]) - set(references):
                    raise ValueError("H3 首尾帧模式只接收首帧和尾帧；请移除额外参考图或改用多图参考模式，不能静默忽略资产")
                mode = "I2V"
            else:
                references = shot["reference_artifact_ids"]
                mode = "R2V" if references else "T2V"
            assets = self.video.assets(aid, wid, pid)
            refs = []
            for reference in references:
                match = next((a for a in assets if a.get("artifact_id") == reference), None)
                if not match:
                    artifact = self.storage.get_artifact(reference)
                    match = self.video.link_artifacts(aid, wid, pid, [{"artifact_id": reference, "filename": artifact.original_filename}])[0]
                    assets.append({**match, "artifact_id": reference})
                refs.append(match["asset_id"])
            binding = {"storyboard_id": bid, "storyboard_version": record["version"], "storyboard_shot_id": shot_id,
                       "storyboard_artifact_id": record["artifact_id"], "shot_fingerprint": shot_fingerprint(shot),
                       "storage_project_id": envelope["project_id"], "subproject_id": envelope["subproject_id"],
                       "source_script_artifact_id": record["source_script_artifact_id"], "source_script_version": record["source_script_version"],
                       "reference_artifact_ids": shot["reference_artifact_ids"],
                       "start_frame_artifact_id": shot.get("start_frame_artifact_id"), "end_frame_artifact_id": shot.get("end_frame_artifact_id"),
                       "narrative_duration_ms": shot["narrative_duration_ms"], "generation_duration_ms": shot["generation_duration_ms"],
                       "draft_test_confirmed": body.get("allow_draft_test") is True}
            return self.video.create_shot(aid, wid, pid, request_id=rid, name=shot["title"], prompt=shot["video_prompt"],
                mode=mode, duration=shot["generation_duration_ms"] // 1000, aspect_ratio=RATIOS[shot["aspect_ratio"]],
                reference_ids=refs, resolution=body.get("resolution", "720p"), storyboard_binding=binding)

    def _videos(self, aid, wid, envelope, record):
        shots = {s["shot_id"]: s for s in record["shots"]}
        project = self.video.project(aid, wid, envelope["video_project_id"])
        return [{**s, "outdated": s["storyboard_shot_id"] not in shots or
                 s.get("shot_fingerprint") != shot_fingerprint(shots[s["storyboard_shot_id"]])}
                for s in project["shots"] if s.get("storyboard_id") == envelope["storyboard_id"]]

    def record_dependency_failure(self, script_job, error):
        """Persist a failed stage when admission fails before it can reserve credit."""
        aid, wid = script_job["account_id"], script_job["workspace_id"]
        rid = stable("dependency:" + script_job["task_id"])
        tid = stable("task:" + aid + ":" + rid)
        with self._control():
            if not self._task_path(tid).exists():
                job = {"schema_version": 1, "kind": "storyboard", "task_id": tid, "request_id": rid,
                       "account_id": aid, "workspace_id": wid, "project_id": script_job["project_id"],
                       "subproject_id": script_job["subproject_id"], "storyboard_id": None,
                       "source_script_artifact_id": script_job["output_artifact_id"], "source_script_version": script_job["version_id"],
                       "depends_on": {"script_task_id": script_job["task_id"], "artifact_id": script_job["output_artifact_id"], "version_id": script_job["version_id"]},
                       "status": "failed", "error": str(error)[:2000], "created_at": _now(), "job_id": None,
                       "price": 0, "credit_status": "not_applicable"}
                self._write_task(job)
        return self.task(aid, wid, tid)

    def dependency_task(self, aid, wid, script_job):
        tid = stable("task:" + aid + ":" + stable("dependency:" + script_job["task_id"]))
        return self.task(aid, wid, tid) if self._task_path(tid).exists() else None

    def pending_jobs(self):
        return [job for path in self.tasks_root.glob("*.json") if not path.name.startswith(".tmp-")
                if (job := self.storage.files.read_json(path))["status"] in {"queued", "running", "uncertain"}]

    def resume_pending(self):
        """Restart recovery: execute only queued/never-started work, or archive a durable candidate.

        Running work without a candidate is marked uncertain; no inference replay.
        """
        for pending in self.identity.pending_jobs():
            if pending.get("kind") == "storyboard" and not self._task_path(pending["task_id"]).exists():
                self._write_task(pending)
        for path in self.tasks_root.glob("*.json"):
            if path.name.startswith(".tmp-"):
                continue
            job = self.storage.files.read_json(path)
            if job["status"] in {"completed", "failed"}:
                try:
                    self.task(job["account_id"], job["workspace_id"], job["task_id"])
                except (FileNotFoundError, ValueError):
                    continue
            if job["status"] in {"queued", "running", "uncertain"}:
                try:
                    self.run(job["account_id"], job["workspace_id"], job["task_id"])
                except (FileNotFoundError, ValueError):
                    # Revoked scope is not a license to run; leave the durable record for an operator.
                    continue
