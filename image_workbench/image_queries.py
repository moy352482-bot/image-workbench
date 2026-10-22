"""Read projections for lists/details; submissions stay in WorkbenchState."""
from dataclasses import asdict
from .accounts import CreditCostCalculator
from .image_status import STATUS_LABELS, FAILURE_MESSAGES, _execution_timing
from .image_references import task_input_artifacts


class ImageQueries:
    def __init__(self, state):
        self.state = state

    def artifact_dto(self, artifact, *, catalog=None):
        catalog = catalog if catalog is not None else self.state.projects.catalog_view()
        placement = catalog["task_placements"][artifact.task_id]
        sub = catalog["subprojects"][placement["subproject_id"]]
        values = {key: value for key, value in asdict(artifact).items() if key not in {"relative_path", "original_filename", "sha256"}}
        workspace_id = getattr(self.state.projects, "workspace_id", None)
        suffix = f"?workspace_id={workspace_id}" if workspace_id else ""
        return {**values, "main_project_id": sub["main_project_id"], "subproject_id": sub["subproject_id"],
                "url": f"/api/artifacts/{artifact.artifact_id}{suffix}",
                "download_url": f"/api/artifacts/{artifact.artifact_id}{suffix}{'&' if suffix else '?'}download=1", **self.state.review(artifact)}

    def task_view(self, project_id, task_id, *, detail=True, catalog=None):
        task = self.state.storage.get_task(project_id, task_id)
        if task.operation not in {"generate", "edit"}:
            raise ValueError("请在脚本与剧本页面查看该文本任务")
        catalog = catalog if catalog is not None else self.state.projects.catalog_view()
        placement = catalog["task_placements"][task_id]
        if placement["storage_project_id"] != project_id:
            raise ValueError("任务逻辑归属与物理定位不一致")
        sub = catalog["subprojects"][placement["subproject_id"]]
        main = catalog["main_projects"][sub["main_project_id"]]
        artifacts = self.state.storage.list_task_artifacts(project_id, task_id)
        inputs = task_input_artifacts(self.state.storage, project_id, task_id)
        outputs = [item for item in artifacts if item.kind == "output"]
        records = self.state.runner.list_executions(project_id, task_id)
        latest = records[-1] if records else None
        job, options = self.state.get_job(project_id, task_id), self.state.task_options(project_id, task_id)
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
        if status == "awaiting_review" and outputs and all(self.state.review(item)["review_status"] == "approved" for item in outputs):
            status = "approved"
        if status in {"generating", "checking"} and (not job or not self.state.worker_running(job.get("worker_id"))):
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
        if self.state.identity:
            result.update(self.state.identity.task_credit(task_id))
            # 下一次执行的报价与已有预约的冻结/结算金额分开；复用本次已校验的输入。
            result["estimated_credit"] = CreditCostCalculator.calculate(task.operation, self.state.backend, 1, len(inputs) + bool(task.base_artifact_id))
            result["workspace_id"] = self.state.identity.ownership(main["main_project_id"])["workspace_id"]
            if result["credit_status"] == "credit_insufficient":
                result.update(status="credit_insufficient", status_label="积分不足")
        if not detail:
            return result
        valid_options = not options or (options["state"] == "ready" and not options.get("asset_library"))
        result.update(
            image_prompt=(options or {}).get("image_prompt"),
            inputs=[self.artifact_dto(item, catalog=catalog) for item in inputs], outputs=[self.artifact_dto(item, catalog=catalog) for item in outputs],
            base_image=self.artifact_dto(self.state.storage.get_artifact(task.base_artifact_id), catalog=catalog) if task.base_artifact_id else None,
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
        placement = self.state.projects.get_task_placement(task_id, main_project_id=main_project_id)
        return self.task_view(placement.storage_project_id, task_id, detail=detail)

    def project_views(self, main_project_id=None):
        """在同一逻辑快照中汇总，避免主/子分别维护计数或完整任务列表。"""
        catalog = self.state.projects.catalog_view()
        if main_project_id is not None:
            self.state.projects._required(catalog["main_projects"], main_project_id, "主项目")
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
                if self.state.storage.get_task(item["storage_project_id"], item["task_id"]).operation not in {"generate", "edit"}:
                    continue
                if (self.state.task_options(item["storage_project_id"], item["task_id"]) or {}).get("asset_library"):
                    continue
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
