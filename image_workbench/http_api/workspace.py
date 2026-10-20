"""Compose the scoped image workspace, without importing account routing."""
from pathlib import Path
import secrets
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from ..storage import StorageManager
from ..workbench_state import WorkbenchState
from .guard import LocalRequestGuard
from ..contracts.images import MAX_UPLOAD_BYTES
from .errors import install_error_handlers
from . import projects, tasks, artifacts

def create_workspace_app(storage: StorageManager | None = None, *, backend="codex", remote_access=None, state=None, guarded=True) -> FastAPI:
    storage = storage or StorageManager()
    state = state or WorkbenchState(storage, backend=backend, execution_mode="remote" if remote_access else "local")
    token = secrets.token_urlsafe(32)
    app = FastAPI(title="Image Workbench", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.workbench = state
    if guarded:
        app.add_middleware(LocalRequestGuard, csrf_token=token, remote_access=remote_access)
    if remote_access:
        from ..remote_queue import RemoteQueue
        from .image_workers import install_routes
        install_routes(app, RemoteQueue(state))
    directory = Path(__file__).resolve().parents[1]
    templates = Jinja2Templates(directory=directory / "web_templates")
    app.mount("/static", StaticFiles(directory=directory / "web_static"), name="static")
    install_error_handlers(app, storage)
    for routes in (projects, tasks, artifacts):
        routes.install_routes(app, state, storage)

    @app.get("/")
    def index(request: Request):
        # Refresh gets the current JS/CSS after a local upgrade, without requiring
        # users to know a browser's force-reload shortcut.
        revision = max((directory / "web_static" / name).stat().st_mtime_ns
                       for name in ("workbench.js", "workbench.css", "accounts.css", "image-viewer.js",
                                    "image-viewer.css", "image-comparison.js", "image-comparison.css",
                                    "workbench-format.js", "workbench-api.js", "workbench-preview.js", "workbench-accounts.js"))
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

    return app
