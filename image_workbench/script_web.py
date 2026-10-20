"""脚本与剧本的账号 Web 入口；模型执行、版本和积分由 ScriptService 负责。"""

from pathlib import Path
import traceback
from urllib.parse import quote

from fastapi import BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from .storage import _now, _validate_id


from .contracts.scripts import ID, ScriptBody, ScriptRequest, ScriptSubmission, ScriptRevision


def install_scripts(app, get_service, storage, *, remote_mode=False):
    from .script_resource_web import install_script_resources
    install_script_resources(app, get_service)
    directory = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=directory / "web_templates")

    def owner(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        return session["account_id"], request.scope["workspace_id"]

    def log_error(exc):
        try:
            path = storage._path(".workbench", "web-errors.log")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n{_now()}\n{''.join(traceback.format_exception(exc))}\n")
        except OSError:
            pass

    def invoke(method, *args):
        try:
            return getattr(get_service(), method)(*args)
        except (ValueError, FileNotFoundError, HTTPException):
            raise
        except Exception as exc:
            log_error(exc)
            raise HTTPException(500, "操作未完成，详细原因已保存到本地日志。请刷新后检查状态。") from exc

    def execute(account_id, workspace_id, task_id):
        # Service persists failures and releases the reservation. An unexpected
        # failure still receives the same local diagnostic record as Web errors.
        try:
            get_service().run(account_id, workspace_id, task_id)
        except Exception as exc:
            log_error(exc)

    @app.get("/scripts")
    def page(request: Request):
        owner(request)
        revision = max((directory / "web_static" / name).stat().st_mtime_ns
                       for name in ("workbench.css", "accounts.css", "scripts.css", "scripts.js", "storyboard.css", "storyboard.js"))
        return templates.TemplateResponse(request=request, name="scripts.html", context={"asset_version": str(revision)},
                                          headers={"Cache-Control": "no-store"})

    @app.get("/api/scripts/config")
    def config(request: Request):
        return {**invoke("config", *owner(request)), "csrf_token": request.scope.get("account_csrf")}

    @app.post("/api/scripts/quote")
    def estimate(body: ScriptRequest, request: Request):
        return invoke("quote", *owner(request), body.model_dump(exclude_none=True))

    @app.get("/api/scripts/tasks")
    def tasks(request: Request, subproject_id: str | None = Query(default=None, pattern=ID)):
        return {"tasks": invoke("list_tasks", *owner(request), subproject_id)}

    @app.post("/api/scripts/tasks")
    def submit(body: ScriptSubmission, request: Request, background_tasks: BackgroundTasks):
        account_id, workspace_id = owner(request)
        result = invoke("submit", account_id, workspace_id, body.model_dump(exclude_none=True))
        if result.get("status") == "queued" and not remote_mode:
            background_tasks.add_task(execute, account_id, workspace_id, result["task_id"])
        return result

    @app.get("/api/scripts/tasks/{task_id}")
    def detail(task_id: str, request: Request):
        return invoke("detail", *owner(request), _validate_id(task_id))

    @app.get("/api/scripts/tasks/{task_id}/download")
    def download(task_id: str, request: Request):
        data, filename = invoke("download", *owner(request), _validate_id(task_id))
        return Response(data, media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=\"script.txt\"; filename*=UTF-8''{quote(filename, safe='')}",
            "Cache-Control": "no-store",
        })

    @app.post("/api/scripts/tasks/{task_id}/run")
    def resume(task_id: str, body: ScriptBody, request: Request, background_tasks: BackgroundTasks):
        account_id, workspace_id = owner(request)
        result = invoke("detail", account_id, workspace_id, _validate_id(task_id))
        if result.get("status") == "queued" and not remote_mode:
            background_tasks.add_task(execute, account_id, workspace_id, task_id)
        return result

    @app.post("/api/scripts/tasks/{task_id}/recover")
    def recover(task_id: str, body: ScriptBody, request: Request):
        return invoke("recover", *owner(request), _validate_id(task_id))

    @app.post("/api/scripts/tasks/{task_id}/revisions")
    def revise(task_id: str, body: ScriptRevision, request: Request):
        return invoke("save_revision", *owner(request), _validate_id(task_id), body.model_dump())
