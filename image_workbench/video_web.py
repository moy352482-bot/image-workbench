"""Authenticated H3 video routes mounted by AccountApplication."""
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .storage import _validate_id
from .video_store import VideoStore


ID = r"^[0-9a-f]{32}$"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Project(Body):
    request_id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=120)
    script: str = Field(default="", max_length=100000)


class Shot(Body):
    request_id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=12000)
    mode: Literal["T2V", "I2V", "R2V"]
    duration: StrictInt = Field(ge=4, le=15)
    aspect_ratio: Literal["landscape", "portrait", "square"]
    reference_ids: list[str] = Field(default_factory=list, max_length=9)
    resolution: Literal["720p", "1080p"] = "720p"


class Run(Body):
    request_id: str = Field(pattern=ID)
    confirm_generation: Literal[True]
    expected_credit: StrictInt = Field(ge=1)
    retry_of: str | None = Field(default=None, pattern=ID)


class Claim(Body):
    worker_id: str = Field(pattern=ID)


class Start(Body):
    attempt_id: str = Field(pattern=ID)
    prompt_id: str | None = Field(default=None, min_length=1, max_length=200)


class Complete(Body):
    status: Literal["succeeded", "failed", "uncertain"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: str | None = Field(default=None, max_length=2000)


def install_video(application):
    app = application.auth
    store = getattr(application, "video_store", None) or VideoStore(application.storage, application.identity)
    application.video_store = store

    def owner(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        return session["account_id"], request.scope["workspace_id"]

    def worker(request):
        return (_validate_id(request.headers.get("x-worker-id", "")),
                _validate_id(request.headers.get("x-claim-id", "")))

    @app.get("/video")
    def page(request: Request):
        owner(request)
        return FileResponse(Path(__file__).parent / "web_templates" / "video.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/video/config")
    def config(request: Request):
        owner(request)
        return {**store.config(), "csrf_token": request.scope.get("account_csrf"),
                "is_admin": bool(request.scope["account_session"]["is_admin"])}

    @app.get("/api/video/projects")
    def projects(request: Request):
        return {"projects": store.projects(*owner(request))}

    @app.post("/api/video/projects")
    def create_project(body: Project, request: Request):
        return store.create_project(*owner(request), **body.model_dump())

    @app.get("/api/video/projects/{pid}")
    def project(pid: str, request: Request):
        return store.project(*owner(request), pid)

    @app.post("/api/video/projects/{pid}/assets")
    async def upload_assets(pid: str, request: Request):
        aid, wid = owner(request)
        # Check ownership before spending time decoding an upload.
        await run_in_threadpool(store._project, aid, wid, pid)
        async with request.form(max_files=9, max_fields=1) as form:
            if set(form.keys()) - {"files", "request_id"}:
                raise ValueError("只接受参考图文件")
            if form.get("request_id"):
                _validate_id(form.get("request_id"))
            files = []
            total = 0
            for upload in form.getlist("files"):
                if not isinstance(upload, UploadFile) and not (hasattr(upload, "read") and hasattr(upload, "filename")):
                    raise ValueError("参考图文件无效")
                data = await upload.read(store.storage.config.max_image_bytes + 1)
                total += len(data)
                if total > 40 * 1024 * 1024:
                    raise ValueError("本次参考图总大小超过40 MiB")
                files.append((upload.filename or "reference", data))
            result = await run_in_threadpool(store.add_assets, aid, wid, pid, files)
            return {"assets": result}

    @app.get("/api/video/projects/{pid}/assets/{asset_id}")
    def asset(pid: str, asset_id: str, request: Request):
        path, mime = store.asset_file(*owner(request), pid, asset_id)
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-store"})

    @app.post("/api/video/projects/{pid}/shots")
    def create_shot(pid: str, body: Shot, request: Request):
        return store.create_shot(*owner(request), pid, **body.model_dump())

    @app.get("/api/video/projects/{pid}/shots/{sid}")
    def shot(pid: str, sid: str, request: Request):
        return store.shot(*owner(request), pid, sid)

    @app.post("/api/video/projects/{pid}/shots/{sid}/run")
    def run(pid: str, sid: str, body: Run, request: Request):
        return store.submit(*owner(request), pid, sid, **body.model_dump())

    @app.get("/api/video/projects/{pid}/shots/{sid}/output")
    def output(pid: str, sid: str, request: Request, download: bool = False):
        path = store.output_file(*owner(request), pid, sid)
        return FileResponse(path, media_type="video/mp4", filename=f"video-{sid}.mp4",
                            content_disposition_type="attachment" if download else "inline",
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/worker/video/claim")
    def claim(body: Claim):
        return {"job": store.claim(body.worker_id)}

    @app.post("/api/worker/video/jobs/{job_id}/start")
    def start(job_id: str, body: Start, request: Request):
        return store.start(job_id, *worker(request), **body.model_dump())

    @app.post("/api/worker/video/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, request: Request):
        return store.heartbeat(job_id, *worker(request))

    @app.get("/api/worker/video/jobs/{job_id}/assets/{asset_id}")
    def worker_asset(job_id: str, asset_id: str, request: Request):
        return Response(store.worker_asset(job_id, *worker(request), asset_id), media_type="application/octet-stream")

    @app.put("/api/worker/video/jobs/{job_id}/output")
    async def worker_output(job_id: str, request: Request):
        return await run_in_threadpool(store.upload_output, job_id, *worker(request),
                                       await request.body(), request.headers.get("x-content-sha256", ""))

    @app.post("/api/worker/video/jobs/{job_id}/complete")
    def complete(job_id: str, body: Complete, request: Request):
        return store.complete(job_id, *worker(request), **body.model_dump())
