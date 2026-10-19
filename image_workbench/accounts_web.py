"""账号入口；每个 HTTP 数据请求都使用经过验证的工作空间视图。"""
from dataclasses import asdict
from http.cookies import SimpleCookie
from pathlib import Path
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .accounts import AuthRateLimited, IdentityStore
from .registration import RegistrationDenied
from .project_manager import ProjectManager
from .workbench_state import WorkbenchState, ConflictError
from .workspace_scope import WorkspaceProjects, WorkspaceStorage, workspace_request
from .web import LocalRequestGuard, create_workspace_app
from .storage import _now


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Body):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=1, max_length=256)


class Registration(Credentials):
    email: str | None = Field(default=None, max_length=254)
    invitation_code: str | None = Field(default=None, max_length=128)


class Activation(Body):
    activation_token: str = Field(min_length=32, max_length=128)
    password: str = Field(min_length=10, max_length=256)


class WorkspaceName(Body):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceChoice(Body):
    workspace_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class AccountApplication:
    def __init__(self, storage, *, backend, remote_access=None, registration_policy=None):
        self.started_at = _now()
        self.storage, self.backend, self.remote_access = storage, backend, remote_access
        self.identity = IdentityStore(storage, registration_policy=registration_policy)
        catalog = ProjectManager(storage).catalog_view()
        # 创建空部署安全预留管理员；已有数据必须先执行可检查的迁移命令。
        with self.identity.connect() as db:
            initialized = db.execute("SELECT 1 FROM accounts WHERE username='moy' AND is_admin=1").fetchone()
        if not initialized:
            if catalog["main_projects"]:
                raise ValueError("已有项目需要先运行 accounts-migrate --dry-run，再应用账号归属迁移")
        self.identity.bootstrap([])
        self.state = SimpleNamespace(workbench=WorkbenchState(storage, backend=backend, execution_mode="remote" if remote_access else "local"), identity=self.identity)
        self.public_csrf = secrets.token_urlsafe(32)
        self.apps = {}
        self.auth = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.worker_app = create_workspace_app(storage, backend=backend, remote_access=remote_access, state=self.state.workbench, guarded=False)
        self.guard = LocalRequestGuard(self.dispatch, csrf_token=self.public_csrf, remote_access=remote_access, account_mode=True)
        self.install_auth()
        from .admin_web import install_admin
        install_admin(self)
        from .video_web import install_video
        install_video(self)
        from .script_web import install_scripts
        install_scripts(self)
        if remote_access:
            from .script_remote import install_script_worker
            install_script_worker(self)
        from .video_pipeline import install_pipeline
        install_pipeline(self)

    def scoped_app(self, account_id, workspace_id):
        key = account_id, workspace_id
        if key not in self.apps:
            projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
            storage = WorkspaceStorage(self.storage, projects)
            state = WorkbenchState(storage, backend=self.backend, execution_mode="remote" if self.remote_access else "local")
            state.projects = projects
            self.apps[key] = create_workspace_app(storage, backend=self.backend, state=state, guarded=False)
        return self.apps[key]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.auth(scope, receive, send)
            return
        headers = dict(scope["headers"])
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get(b"cookie", b"").decode("latin1"))
            token = cookies["iw_session"].value if "iw_session" in cookies else ""
        except Exception:
            token = ""
        session = await run_in_threadpool(self.identity.session, token)
        scope["account_session"] = session
        scope["account_token"] = token
        scope["account_public"] = scope["path"] in {"/login", "/api/auth/config", "/api/auth/register", "/api/auth/login", "/api/auth/activate"} or scope["path"].startswith("/static/")
        if session:
            scope["account_csrf"] = session["csrf"]
        await self.guard(scope, receive, send)

    async def dispatch(self, scope, receive, send):
        path = scope["path"]
        if path.startswith(("/api/worker/video/", "/api/worker/scripts/")):
            if not self.remote_access:
                await JSONResponse({"detail": "资源不存在"}, status_code=404)(scope, receive, send)
            else:
                await self.auth(scope, receive, send)
            return
        if path.startswith("/api/worker/"):
            if not self.remote_access:
                await JSONResponse({"detail": "资源不存在"}, status_code=404)(scope, receive, send)
            else:
                await self.worker_app(scope, receive, send)
            return
        if scope.get("account_public"):
            await (self.worker_app if path.startswith("/static/") else self.auth)(scope, receive, send)
            return
        session = scope.get("account_session")
        if not session:
            response = RedirectResponse("/login", status_code=303) if path in {"/", "/admin", "/video", "/scripts"} else JSONResponse({"detail": "请先登录"}, status_code=401)
            await response(scope, receive, send)
            return
        if path == "/admin" or path.startswith("/api/admin/"):
            if not session["is_admin"]:
                await JSONResponse({"detail": "仅管理员可访问"}, status_code=403)(scope, receive, send)
            else:
                await self.auth(scope, receive, send)
            return
        headers = dict(scope["headers"])
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="ignore"))
        wid = headers.get(b"x-workspace-id", b"").decode() or query.get("workspace_id", [session["workspace_id"]])[0]
        try:
            await run_in_threadpool(self.identity.require_workspace, session["account_id"], wid)
        except (ValueError, FileNotFoundError):
            await JSONResponse({"detail": "工作空间不存在"}, status_code=404)(scope, receive, send)
            return
        scope["workspace_id"] = wid
        if path in {"/video", "/scripts"} or path.startswith(("/api/video/", "/api/scripts/", "/api/auth/", "/api/workspaces", "/api/credits", "/api/project-actions")):
            await self.auth(scope, receive, send)
        else:
            with workspace_request():
                await self.scoped_app(session["account_id"], wid)(scope, receive, send)

    def install_auth(self):
        app, identity = self.auth, self.identity

        @app.exception_handler(ValueError)
        async def invalid(request, exc):
            from .accounts import CreditInsufficient
            from .capacity import CapacityExceeded
            if isinstance(exc, CreditInsufficient):
                return JSONResponse({"detail": str(exc), "status": "credit_insufficient", "expected_credit": exc.expected, "remaining_credit": exc.remaining}, status_code=402)
            if isinstance(exc, CapacityExceeded):
                return JSONResponse({"detail": str(exc), "status": "capacity_exceeded"}, status_code=429)
            if isinstance(exc, AuthRateLimited):
                return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": str(exc.retry_after)})
            if isinstance(exc, RegistrationDenied):
                return JSONResponse({"detail": str(exc)}, status_code=403)
            return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, ConflictError) else 400)

        @app.exception_handler(FileNotFoundError)
        async def missing(request, exc):
            return JSONResponse({"detail": "资源不存在"}, status_code=404)

        @app.exception_handler(RequestValidationError)
        async def validation(request, exc):
            # 只返回字段说明，禁止把密码、启用凭据或请求内容回显给客户端。
            labels = {"username": "用户名需为 3–40 个字符", "password": "密码需为 1–256 个字符",
                      "activation_token": "请填写完整的本机启用凭据（32–128 个字符）",
                      "invitation_code": "邀请码不能超过 128 个字符",
                      "email": "邮箱不能超过 254 个字符", "name": "工作空间名称需为 1–120 个字符",
                      "workspace_id": "请选择有效的工作空间"}
            if request.url.path == "/api/auth/activate":
                labels["password"] = "新密码需为 10–256 个字符"
            messages = []
            for error in exc.errors():
                field = error["loc"][-1] if error["loc"] else ""
                message = labels.get(field, "提交了不支持的字段" if error["type"] == "extra_forbidden" else "请求格式无效，请刷新后重试")
                if message not in messages:
                    messages.append(message)
            return JSONResponse({"detail": "；".join(messages)}, status_code=422)

        @app.get("/login")
        def login_page():
            return FileResponse(Path(__file__).parent / "web_templates" / "login.html", headers={"Cache-Control": "no-store"})

        @app.get("/api/auth/config")
        def auth_config(request: Request):
            return {"csrf_token": request.scope.get("account_csrf", self.public_csrf),
                    "signed_in": bool(request.scope.get("account_session")), "backend": self.backend,
                    "registration": identity.registration_policy.public()}

        @app.post("/api/auth/register")
        def register(body: Registration):
            return identity.register(body.username, body.password, body.email, body.invitation_code)

        @app.post("/api/auth/activate")
        def activate(body: Activation):
            return identity.activate(body.activation_token, body.password)

        @app.post("/api/auth/login")
        def login(body: Credentials, request: Request):
            token = identity.login(body.username, body.password, request.headers.get("user-agent", ""))
            response = JSONResponse({"ok": True})
            response.set_cookie("iw_session", token, max_age=7*86400, httponly=True, secure=bool(self.remote_access and self.remote_access.origin.startswith("https:")), samesite="strict", path="/")
            return response

        @app.post("/api/auth/logout")
        def logout(request: Request):
            identity.logout(request.scope["account_token"])
            response = JSONResponse({"ok": True})
            response.delete_cookie("iw_session", path="/")
            return response

        @app.get("/api/auth/me")
        def me(request: Request):
            session = request.scope["account_session"]
            return {"account_id": session["account_id"], "username": session["username"], "is_admin": bool(session["is_admin"]),
                    "workspace_id": request.scope["workspace_id"], "workspaces": identity.workspaces(session["account_id"]), "credits": identity.wallet(session["account_id"])}

        @app.get("/api/workspaces")
        def workspaces(request: Request):
            return {"workspaces": identity.workspaces(request.scope["account_session"]["account_id"])}

        @app.post("/api/workspaces")
        def new_workspace(body: WorkspaceName, request: Request):
            value = identity.create_workspace(request.scope["account_session"]["account_id"], body.name)
            value.pop("storage_root", None)
            return value

        @app.post("/api/workspaces/select")
        def select_workspace(body: WorkspaceChoice, request: Request):
            identity.select_workspace(request.scope["account_token"], body.workspace_id)
            return {"workspace_id": body.workspace_id}

        @app.get("/api/credits")
        def credits(request: Request):
            aid = request.scope["account_session"]["account_id"]
            return {"credits": identity.wallet(aid), "transactions": identity.ledger(aid)}

        def change_project(request, mid, *, target=None, deleted=None):
            aid, wid = request.scope["account_session"]["account_id"], request.scope["workspace_id"]
            with self.state.workbench.control_lock():
                owner = identity.ownership(mid)
                if owner["account_id"] != aid or owner["workspace_id"] != wid:
                    raise FileNotFoundError("项目不存在")
                catalog = self.state.workbench.projects.catalog_view()
                pid = catalog["storage_namespaces"][mid]
                if any(job["project_id"] == pid and job["status"] in {"queued", "running", "uncertain"} for job in self.state.workbench.list_jobs()):
                    raise ConflictError("项目仍有排队、执行中或待确认任务，请结束任务后再操作")
                if any((job.get("project_id") in {pid, mid} or job.get("main_project_id") == mid)
                       and job.get("status") in {"queued", "running", "generating", "checking", "archiving", "uncertain"}
                       for job in self.script_service.pending_jobs()):
                    raise ConflictError("项目仍有脚本或剧本生成任务，请结束任务后再操作")
                identity.change_project(aid, wid, mid, target_workspace_id=target, deleted=deleted)
            return {"ok": True, "main_project_id": mid, "workspace_id": target or wid}

        @app.post("/api/project-actions/{main_id}/move")
        def move(main_id: str, body: WorkspaceChoice, request: Request):
            return change_project(request, main_id, target=body.workspace_id)

        @app.delete("/api/project-actions/{main_id}")
        def delete(main_id: str, request: Request):
            return change_project(request, main_id, deleted=True)

        @app.post("/api/project-actions/{main_id}/restore")
        def restore(main_id: str, request: Request):
            return change_project(request, main_id, deleted=False)

        @app.get("/api/project-actions/deleted")
        def deleted(request: Request):
            wid = request.scope["workspace_id"]
            ids = identity.project_ids(wid, deleted=True)
            return {"projects": [{**asdict(item), "workspace_id": wid} for item in self.state.workbench.projects.list_main_projects() if item.main_project_id in ids]}
