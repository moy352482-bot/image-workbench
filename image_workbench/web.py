"""只监听本机的 Web 适配层；真实执行始终交给持久队列和原 TaskRunner。"""

from dataclasses import asdict
import hashlib
from pathlib import Path
import re
import secrets
import traceback
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from .storage import MetadataError, StorageManager, _now, metadata_read_snapshot
from .workbench_state import ConflictError, WorkbenchState
from .project_manager import MigrationRequiredError, PlacementConflictError

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_AUTH_BYTES = 16 * 1024
ID_PATTERN = r"^[0-9a-f]{32}$"


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProject(Payload):
    request_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)


class ProjectUpdate(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    status: str | None = None


class TaskUpdate(Payload):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)


class MoveTask(Payload):
    target_subproject_id: str = Field(pattern=ID_PATTERN)
    expected_subproject_id: str | None = Field(default=None, pattern=ID_PATTERN)


class RunTask(Payload):
    request_id: str = Field(pattern=ID_PATTERN)
    confirm_generation: StrictBool = False
    retry_of: str | None = Field(default=None, pattern=ID_PATTERN)


class RestoreTask(Payload):
    request_id: str = Field(pattern=ID_PATTERN)
    execution_id: str = Field(pattern=ID_PATTERN)


class LocalRequestGuard:
    """拒绝非本机 Host、跨站写请求；读取完整请求前限制实际字节数。"""

    def __init__(self, app, *, csrf_token, remote_access=None, account_mode=False):
        self.app, self.csrf_token, self.remote_access = app, csrf_token, remote_access
        self.account_mode = account_mode

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def guarded_send(event):
            if event["type"] == "http.response.start":
                event["headers"] = [*event.get("headers", []),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
                if scope["path"].startswith("/api/"):
                    event["headers"].append((b"cache-control", b"no-store"))
            await send(event)

        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin1").lower()
        origin = headers.get(b"origin", b"").decode("latin1")
        remote = self.remote_access
        worker_request = bool(remote and scope["path"].startswith("/api/worker/"))
        if remote and (worker_request or not self.account_mode):
            expected = remote.worker_authorization if worker_request else remote.browser_authorization
            if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
                challenge = {} if worker_request else {"WWW-Authenticate": 'Basic realm="Image Workbench", charset="UTF-8"'}
                await JSONResponse({"detail": "请先登录。"}, status_code=401, headers=challenge)(scope, receive, guarded_send)
                return
        error, code = None, 403
        if remote and host != remote.host:
            error = "访问地址与配置的源站不一致。"
        elif not remote and not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", host):
            error = "仅允许通过本机 localhost 访问。"
        elif origin and origin != (remote.origin if remote else "http://" + host):
            error = "不接受跨站请求，请从本地工作台操作。"
        elif not worker_request and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(headers.get(b"x-workbench-csrf", b""), scope.get("account_csrf", self.csrf_token).encode()):
                error = "页面校验已失效，请刷新后重新操作。"
            elif headers.get(b"sec-fetch-site") == b"cross-site":
                error = "不接受跨站请求，请从本地工作台操作。"
        if error:
            await JSONResponse({"detail": error}, status_code=code)(scope, receive, guarded_send)
            return
        if self.account_mode and not worker_request and not scope.get("account_session") and not scope.get("account_public"):
            # AccountApplication can reject an anonymous request without consuming
            # attacker-controlled bodies. Public auth routes still have size limits.
            await self.app(scope, receive, guarded_send)
            return
        auth_request = scope["path"] in {"/api/auth/login", "/api/auth/register", "/api/auth/activate"}
        if scope["method"] in {"GET", "HEAD", "OPTIONS"} or (self.account_mode and scope.get("account_public") and not auth_request):
            # These routes never consume a request body. Do not allocate an upload
            # buffer for image downloads, static files or other read-only requests.
            await self.app(scope, receive, guarded_send)
            return
        admin_request = scope["path"].startswith("/api/admin/")
        if admin_request and self.account_mode and not scope.get("account_session", {}).get("is_admin"):
            await self.app(scope, receive, guarded_send)
            return
        limit = MAX_AUTH_BYTES if auth_request or admin_request else MAX_UPLOAD_BYTES + 1024 * 1024
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = limit + 1
        chunks, size = [], 0
        if declared <= limit:
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                chunk = event.get("body", b"")
                size += len(chunk)
                if size > limit:
                    break
                chunks.append(chunk)
                if not event.get("more_body", False):
                    break
        if declared > limit or size > limit:
            message = "管理或账号请求不能超过 16 KiB。" if auth_request or admin_request else "上传总大小不能超过40 MB。"
            await JSONResponse({"detail": message}, status_code=413)(scope, receive, guarded_send)
            return
        body = b"".join(chunks)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, guarded_send)


def create_workspace_app(storage: StorageManager | None = None, *, backend="codex", remote_access=None, state=None, guarded=True) -> FastAPI:
    storage = storage or StorageManager()
    state = state or WorkbenchState(storage, backend=backend, execution_mode="remote" if remote_access else "local")
    token = secrets.token_urlsafe(32)
    app = FastAPI(title="Image Workbench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workbench = state
    if guarded:
        app.add_middleware(LocalRequestGuard, csrf_token=token, remote_access=remote_access)
    if remote_access:
        from .remote_queue import install_routes
        install_routes(app, state)
    directory = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=directory / "web_templates")
    app.mount("/static", StaticFiles(directory=directory / "web_static"), name="static")

    def log_error(exc):
        try:
            with storage._path(".workbench", "web-errors.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n{_now()}\n{''.join(traceback.format_exception(exc))}\n")
        except OSError:
            pass

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "参数格式不正确，请检查任务信息后重试。"}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse({"detail": str(exc.detail) if exc.status_code in {400, 409, 413} else "资源不存在或请求无效。"}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        log_error(exc)
        return JSONResponse({"detail": "操作未完成，详细原因已保存到本地日志。请刷新后检查状态。"}, status_code=500)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        from .accounts import CreditInsufficient
        from .capacity import CapacityExceeded
        if isinstance(exc, CapacityExceeded):
            return JSONResponse({"detail": str(exc), "status": "capacity_exceeded"}, status_code=429)
        if isinstance(exc, CreditInsufficient):
            return JSONResponse({"detail": str(exc), "status": "credit_insufficient", "expected_credit": exc.expected, "remaining_credit": exc.remaining}, status_code=402)
        if isinstance(exc, MetadataError):
            log_error(exc)
        if isinstance(exc, (ConflictError, MigrationRequiredError, PlacementConflictError)):
            return JSONResponse({"detail": str(exc)}, status_code=409)
        message = "本地数据校验失败，请检查本地日志。" if isinstance(exc, MetadataError) else "输入校验失败：请检查图片内容、大小、模式、比例和版本来源。"
        return JSONResponse({"detail": message}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def missing_resource(request, exc):
        return JSONResponse({"detail": "项目、任务或图片不存在，请刷新后重试。"}, status_code=404)

    @app.get("/")
    def index(request: Request):
        # Refresh gets the current JS/CSS after a local upgrade, without requiring
        # users to know a browser's force-reload shortcut.
        revision = max((directory / "web_static" / name).stat().st_mtime_ns
                       for name in ("workbench.js", "workbench.css", "accounts.css", "image-viewer.js",
                                    "image-viewer.css", "image-comparison.js", "image-comparison.css"))
        return templates.TemplateResponse(request=request, name="index.html",
                                          context={"asset_version": str(revision), "remote_mode": state.execution_mode == "remote"},
                                          headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    def config(request: Request):
        return {"csrf_token": request.scope.get("account_csrf", token), "backend": backend, "worker_running": state.worker_running(),
                "execution_mode": state.execution_mode,
                "project_migration_required": state.projects.migration_required,
                "max_input_images": 8, "max_image_bytes": storage.config.max_image_bytes,
                "max_upload_bytes": MAX_UPLOAD_BYTES, "ratios": ["auto", "1:1", "3:2", "2:3"]}

    @app.get("/api/projects")
    def projects():
        return {"projects": [{**asdict(item), "project_id": item.main_project_id} for item in reversed(state.projects.list_main_projects())]}

    @app.post("/api/projects")
    def create_project(payload: CreateProject):
        return asdict(state.create_project(payload.name, payload.request_id))

    @app.get("/api/projects/{project_id}/tasks")
    def tasks(project_id: str):
        _, _, records = state.project_views(project_id)
        return {"tasks": records}

    @app.post("/api/projects/{project_id}/tasks")
    async def create_task(project_id: str, request: Request):
        sub = await run_in_threadpool(state.projects.default_subproject, project_id)
        return await save_task(project_id, sub.subproject_id, request)

    async def save_task(project_id: str, subproject_id: str, request: Request):
        async with request.form(max_files=8, max_fields=8, max_part_size=64 * 1024) as form:
            allowed = {"request_id", "operation", "prompt", "aspect_ratio", "base_artifact_id", "files", "name", "description"}
            if set(form) - allowed or any(len(form.getlist(key)) != 1 for key in set(form) - {"files"}):
                raise HTTPException(400, "任务字段不正确，不能指定本地文件路径。")
            for key in ("request_id", "operation", "prompt"):
                if not isinstance(form.get(key), str):
                    raise HTTPException(400, "请填写请求标识、模式和提示词。")
            files = form.getlist("files")
            if len(files) > 8 or any(not isinstance(item, UploadFile) for item in files):
                raise HTTPException(400, "最多上传8张参考图片。")
            for key in ("aspect_ratio", "base_artifact_id", "name", "description"):
                if key in form and not isinstance(form[key], str):
                    raise HTTPException(400, "比例或版本来源格式错误。")
            temporary = storage._path(".workbench", "uploads", uuid4().hex)
            temporary.mkdir()
            sources, total = [], 0
            try:
                for upload in files:
                    path = storage._check_path(temporary / (uuid4().hex + ".upload"))
                    sources.append(path)
                    size = 0
                    with path.open("xb") as stream:
                        while block := await upload.read(1024 * 1024):
                            size += len(block)
                            total += len(block)
                            if size > storage.config.max_image_bytes or total > MAX_UPLOAD_BYTES:
                                raise HTTPException(413, "单张图片最多20 MB，上传合计最多40 MB。")
                            stream.write(block)
                task = await run_in_threadpool(state.create_task, project_id,
                    request_id=form["request_id"], operation=form["operation"], prompt=form["prompt"],
                    aspect_ratio=form.get("aspect_ratio", "auto"), base_artifact_id=form.get("base_artifact_id") or None,
                    inputs=sources, subproject_id=subproject_id, name=form.get("name") or None, description=form.get("description", ""))
                return await run_in_threadpool(state.task_view, project_id, task.task_id)
            finally:
                for path in sources:
                    storage._cleanup(path)
                storage._cleanup(temporary, directory=True)

    @app.get("/api/projects/{project_id}/tasks/{task_id}")
    def task(project_id: str, task_id: str):
        return state.logical_task_view(task_id, main_project_id=project_id)

    @app.post("/api/projects/{project_id}/tasks/{task_id}/run")
    def run(project_id: str, task_id: str, payload: RunTask):
        placement = state.projects.get_task_placement(task_id, main_project_id=project_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id,
                           confirmed=payload.confirm_generation, retry_of=payload.retry_of)
        return {"job_id": job["job_id"], "status": job["status"]}

    @app.post("/api/projects/{project_id}/tasks/{task_id}/restore")
    def restore(project_id: str, task_id: str, payload: RestoreTask):
        placement = state.projects.get_task_placement(task_id, main_project_id=project_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id, restore_execution_id=payload.execution_id)
        return {"job_id": job["job_id"], "status": job["status"]}

    def main_overview(main_project_id):
        mains, _, _ = state.project_views(main_project_id)
        return mains[main_project_id]

    def sub_overview(subproject_id):
        sub = state.projects.get_subproject(subproject_id)
        _, subs, _ = state.project_views(sub.main_project_id)
        return subs[subproject_id]

    @app.get("/api/main-projects")
    def main_projects():
        with metadata_read_snapshot():
            mains, _, _ = state.project_views()
        return {"main_projects": sorted(mains.values(), key=lambda item: (item["updated_at"], item["main_project_id"]), reverse=True)}

    @app.post("/api/main-projects")
    def create_main(payload: CreateProject):
        main = state.create_main_project(payload.name, payload.request_id, payload.description)
        return main_overview(main.main_project_id)

    @app.get("/api/main-projects/{main_project_id}")
    def main_detail(main_project_id: str):
        mains, _, records = state.project_views(main_project_id)
        main = mains[main_project_id]
        return {"main_project": main, "subprojects": main["subprojects"], "recent_tasks": records[:8]}

    @app.patch("/api/main-projects/{main_project_id}")
    def update_main(main_project_id: str, payload: ProjectUpdate):
        with state.control_lock():
            state.projects.update_main_project(main_project_id, **payload.model_dump(exclude_none=True))
        return main_overview(main_project_id)

    @app.post("/api/main-projects/{main_project_id}/subprojects")
    def create_sub(main_project_id: str, payload: CreateProject):
        sub = state.create_subproject(main_project_id, payload.name, payload.request_id, payload.description)
        return sub_overview(sub.subproject_id)

    @app.patch("/api/subprojects/{subproject_id}")
    def update_sub(subproject_id: str, payload: ProjectUpdate):
        with state.control_lock():
            state.projects.update_subproject(subproject_id, **payload.model_dump(exclude_none=True))
        return sub_overview(subproject_id)

    @app.get("/api/subprojects/{subproject_id}/tasks")
    def sub_tasks(subproject_id: str):
        sub = state.projects.get_subproject(subproject_id)
        mains, subs, records = state.project_views(sub.main_project_id)
        return {"subproject": subs[subproject_id], "main_project": mains[sub.main_project_id],
                "tasks": [item for item in records if item["subproject_id"] == subproject_id]}

    @app.post("/api/subprojects/{subproject_id}/tasks")
    async def create_sub_task(subproject_id: str, request: Request):
        sub = await run_in_threadpool(state.projects.get_subproject, subproject_id)
        return await save_task(sub.main_project_id, subproject_id, request)

    @app.get("/api/tasks/{task_id}")
    def logical_task(task_id: str):
        return state.logical_task_view(task_id)

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, payload: TaskUpdate):
        with state.control_lock():
            state.projects.update_task(task_id, **payload.model_dump(exclude_none=True))
        return state.logical_task_view(task_id)

    @app.post("/api/tasks/{task_id}/move")
    def move_task(task_id: str, payload: MoveTask):
        with state.control_lock():
            state.projects.move_task(task_id, payload.target_subproject_id, expected_subproject_id=payload.expected_subproject_id)
        return state.logical_task_view(task_id)

    @app.post("/api/tasks/{task_id}/run")
    def run_logical_task(task_id: str, payload: RunTask):
        placement = state.projects.get_task_placement(task_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id,
                           confirmed=payload.confirm_generation, retry_of=payload.retry_of)
        return {"job_id": job["job_id"], "status": job["status"]}

    @app.post("/api/tasks/{task_id}/restore")
    def restore_logical_task(task_id: str, payload: RestoreTask):
        placement = state.projects.get_task_placement(task_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id, restore_execution_id=payload.execution_id)
        return {"job_id": job["job_id"], "status": job["status"]}

    @app.post("/api/artifacts/{artifact_id}/approve")
    def approve(artifact_id: str, payload: Payload):
        return state.approve(artifact_id)

    @app.get("/api/artifacts/{artifact_id}")
    def artifact(artifact_id: str, download: bool = False):
        item = storage.get_artifact(artifact_id)
        path = storage.get_artifact_path(artifact_id)
        if path.stat().st_size != item.size_bytes or item.size_bytes > storage.config.max_image_bytes:
            raise ValueError("图片体积与登记不符")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise ValueError("图片内容与登记不符")
        mime, extension = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "WEBP": ("image/webp", "webp")}[item.format]
        headers = {"Content-Disposition": f'{"attachment" if download else "inline"}; filename="image-{artifact_id}.{extension}"'}
        return Response(data, media_type=mime, headers=headers)

    return app


def create_app(storage: StorageManager | None = None, *, backend="codex", remote_access=None, registration_policy=None):
    from .accounts_web import AccountApplication
    return AccountApplication(storage or StorageManager(), backend=backend, remote_access=remote_access, registration_policy=registration_policy)
