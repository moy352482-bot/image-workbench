"""已认证管理员界面。权限在分派与每个接口依赖中同时检查。"""
from dataclasses import asdict
import hashlib
from pathlib import Path
import threading

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .accounts import CreditCostCalculator
from .admin_operations import AdminOperations, source_fingerprint


class EmptyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeploymentPreview(EmptyAction):
    release_id: str = Field(pattern=r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


class DeploymentConfirmation(EmptyAction):
    token: str = Field(min_length=32, max_length=128)
    confirm: StrictBool


class Acknowledgment(EmptyAction):
    confirm: StrictBool


def require_admin(request: Request):
    session = request.scope.get("account_session")
    if not session:
        raise HTTPException(401, "请先登录")
    if not session.get("is_admin"):
        raise HTTPException(403, "仅管理员可访问")
    return session


def install_admin(application):
    app = application.auth
    directory = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=directory / "web_templates")
    creation_lock = threading.Lock()

    def service():
        with creation_lock:
            if not hasattr(application, "admin_operations"):
                application.admin_operations = AdminOperations(application.storage)
        return application.admin_operations

    @app.get("/admin", dependencies=[Depends(require_admin)])
    def page(request: Request):
        revision = max((directory / "web_static" / name).stat().st_mtime_ns for name in ("admin.js", "admin.css"))
        return templates.TemplateResponse(request=request, name="admin.html", context={"asset_version": str(revision)}, headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/overview")
    def overview(session=Depends(require_admin)):
        operations = service()
        identity, state = application.identity, application.state.workbench
        with identity.connect() as db:
            counts = {label: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for label, table in
                      (("accounts", "accounts"), ("workspaces", "workspaces"), ("projects", "project_ownership"), ("reservations", "reservations"))}
            counts["reservations"] = db.execute("SELECT COUNT(*) FROM reservations WHERE status IN ('credit_frozen','running')").fetchone()[0]
        jobs = {}
        with state.control_lock():
            all_jobs = state.list_jobs()
            scheduling = state.scheduling_policy.snapshot(all_jobs, execution_mode=state.execution_mode)
            for job in all_jobs:
                jobs[job["status"]] = jobs.get(job["status"], 0) + 1
        current = source_fingerprint(operations.source_root)
        reports = operations.reports()
        history = operations.operations()
        release = operations.source_root / "RELEASE.json"
        release_id = "dev-" + current[:12]
        if release.is_file():
            import json
            try:
                release_id = json.loads(release.read_text(encoding="utf-8"))["release_id"]
            except (OSError, ValueError, KeyError):
                release_id = "版本清单不可读"
        ordinary, reference = CreditCostCalculator.policies[application.backend]
        return {"username": session["username"],
                "version": {"id": release_id, "source_fingerprint": current,
                            "loaded_at": application.started_at, "mode": "release" if release.is_file() else "development"},
                "service": {"backend": application.backend, "execution_mode": state.execution_mode, "worker_running": state.worker_running(), "job_counts": jobs},
                "counts": counts, "policy": {"registration": identity.registration_policy.public(), "capacity": asdict(identity.capacity_policy),
                                                "scheduling": scheduling,
                                                "credits": {"initial_credit": 200, "generate": ordinary, "reference_or_edit": reference}},
                "deployment": operations.deployment_status(), "latest_report": reports[0] if reports else None,
                "active_operation": next((op for op in history if op["status"] in {"queued", "running", "needs_attention"}), None)}

    @app.get("/api/admin/reports", dependencies=[Depends(require_admin)])
    def reports():
        return {"reports": service().reports()}

    @app.get("/api/admin/operations", dependencies=[Depends(require_admin)])
    def operations():
        return {"operations": service().operations()}

    @app.get("/api/admin/operations/{operation_id}", dependencies=[Depends(require_admin)])
    def operation(operation_id: str):
        return service().public_operation(service().read("operations", operation_id))

    @app.post("/api/admin/operations/{operation_id}/acknowledge")
    def acknowledge(operation_id: str, body: Acknowledgment, session=Depends(require_admin)):
        if not body.confirm:
            raise HTTPException(400, "请先核实后台进程及目标环境")
        return service().acknowledge(operation_id, session["account_id"])

    @app.post("/api/admin/checks", status_code=202)
    def checks(body: EmptyAction, session=Depends(require_admin)):
        return service().start("checks", session["account_id"])

    @app.post("/api/admin/releases/prepare", status_code=202)
    def prepare(body: EmptyAction, session=Depends(require_admin)):
        return service().start("prepare", session["account_id"])

    @app.post("/api/admin/deployments/preview")
    def preview(body: DeploymentPreview, request: Request, session=Depends(require_admin)):
        session_key = hashlib.sha256(request.scope["account_token"].encode()).hexdigest()
        return service().preview(body.release_id, session["account_id"], session_key)

    @app.post("/api/admin/deployments", status_code=202)
    def deploy(body: DeploymentConfirmation, request: Request, session=Depends(require_admin)):
        if not body.confirm:
            raise HTTPException(400, "请先确认部署目标与版本")
        session_key = hashlib.sha256(request.scope["account_token"].encode()).hexdigest()
        return service().start("deploy", session["account_id"], session_key=session_key, token=body.token)
