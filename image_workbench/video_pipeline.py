"""Connect H3 images, text versions and MP4 uploads to the existing account workspace."""
from pathlib import PureWindowsPath
from typing import Literal

from fastapi import Request, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from .contracts.scripts import ScriptBody, ID
from .video_store import MAX_OUTPUT_BYTES


class ArtifactChoice(ScriptBody):
    artifact_id: str = Field(pattern=ID)


class ScriptChoice(ScriptBody):
    task_id: str = Field(pattern=ID)


class SubprojectChoice(ScriptBody):
    subproject_id: str = Field(pattern=ID)


class TrayChoice(ArtifactChoice):
    media_type: Literal["image", "video"]
    action: Literal["add", "remove"]
    shot_id: str | None = Field(default=None, pattern=ID)


class ClipChoice(ArtifactChoice):
    source_shot_id: str = Field(pattern=ID)


class ImageRequest(ScriptBody):
    request_id: str = Field(pattern=ID)
    prompt: str = Field(min_length=1, max_length=12000)
    aspect_ratio: str = Field(default="auto", pattern=r"^(auto|1:1|3:2|2:3)$")
    reference_ids: list[str] = Field(default_factory=list, max_length=8)


def install_pipeline(app, service, store):

    def owner(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        return session["account_id"], request.scope["workspace_id"]

    @app.post("/api/video/projects/from-subproject")
    def from_subproject(body: SubprojectChoice, request: Request):
        return service.from_subproject(*owner(request), body.subproject_id)

    @app.get("/api/video/projects/{pid}/tray")
    def tray(pid: str, request: Request):
        return service.tray(*owner(request), pid)

    @app.post("/api/video/projects/{pid}/tray")
    def change_tray(pid: str, body: TrayChoice, request: Request):
        return service.change_tray(*owner(request), pid, **body.model_dump())

    @app.post("/api/video/projects/{pid}/shots/{sid}/clip")
    def bind_clip(pid: str, sid: str, body: ClipChoice, request: Request):
        return store.bind_clip(*owner(request), pid, sid, **body.model_dump())

    @app.post("/api/video/projects/{pid}/pipeline")
    def initialize(pid: str, body: ScriptBody, request: Request):
        aid, wid = owner(request)
        service.binding(aid, wid, pid, create=True)
        return service.view(aid, wid, pid)

    @app.get("/api/video/projects/{pid}/pipeline")
    def pipeline(pid: str, request: Request):
        return service.view(*owner(request), pid)

    @app.post("/api/video/projects/{pid}/images")
    def image_draft(pid: str, body: ImageRequest, request: Request):
        return service.create_image(*owner(request), pid, **body.model_dump())

    @app.post("/api/video/projects/{pid}/images/from-artifact")
    def import_image(pid: str, body: ArtifactChoice, request: Request):
        return service.import_image(*owner(request), pid, body.artifact_id)

    @app.post("/api/video/projects/{pid}/script/from-task")
    def select_script(pid: str, body: ScriptChoice, request: Request):
        return service.use_script(*owner(request), pid, body.task_id)

    @app.post("/api/video/projects/{pid}/script/upload")
    async def upload_script(pid: str, request: Request):
        aid, wid = owner(request)
        await run_in_threadpool(service.binding, aid, wid, pid)
        async with request.form(max_files=1, max_fields=2) as form:
            if set(form) - {"file", "request_id", "generation_type"} or any(len(form.getlist(k)) != 1 for k in form):
                raise ValueError("脚本上传字段无效")
            upload = form.get("file")
            filename = PureWindowsPath(getattr(upload, "filename", "")).name
            if not filename.lower().endswith(".txt") or not hasattr(upload, "read"):
                raise ValueError("请上传 UTF-8 编码的 TXT 脚本")
            data = await upload.read(256 * 1024 + 1)
            if not 0 < len(data) <= 256 * 1024:
                raise ValueError("脚本文件为空或超过256 KiB")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeError as exc:
                raise ValueError("请将脚本另存为 UTF-8 编码的 TXT") from exc
            result = await run_in_threadpool(service.import_script, aid, wid, pid,
                form.get("request_id"), text, filename=filename[:255], generation_type=form.get("generation_type", "script"))
        return result

    @app.post("/api/video/projects/{pid}/videos/upload")
    async def upload_video(pid: str, request: Request):
        aid, wid = owner(request)
        await run_in_threadpool(store.pipeline_project, aid, wid, pid)
        async with request.form(max_files=1, max_fields=2) as form:
            if set(form) - {"file", "request_id", "name"} or any(len(form.getlist(k)) != 1 for k in form):
                raise ValueError("视频上传字段无效")
            upload = form.get("file")
            filename = PureWindowsPath(getattr(upload, "filename", "")).name
            if not filename.lower().endswith(".mp4") or not hasattr(upload, "read"):
                raise ValueError("请上传 MP4 文件")
            data = await upload.read(MAX_OUTPUT_BYTES + 1)
            return await run_in_threadpool(store.upload_video, aid, wid, pid, form.get("request_id"),
                                           form.get("name") or filename[:120], data)
