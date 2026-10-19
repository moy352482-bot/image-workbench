"""Connect H3 images, text versions and MP4 uploads to the existing account workspace."""
import hashlib
from pathlib import PureWindowsPath
from uuid import NAMESPACE_URL, uuid5

from fastapi import Request, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from .script_web import ScriptBody, ID
from .storage import _validate_id, _now
from .video_store import MAX_OUTPUT_BYTES
from .workbench_state import ConflictError


class ArtifactChoice(ScriptBody):
    artifact_id: str = Field(pattern=ID)


class ScriptChoice(ScriptBody):
    task_id: str = Field(pattern=ID)


class ImageRequest(ScriptBody):
    request_id: str = Field(pattern=ID)
    prompt: str = Field(min_length=1, max_length=12000)
    aspect_ratio: str = Field(default="auto", pattern=r"^(auto|1:1|3:2|2:3)$")
    reference_ids: list[str] = Field(default_factory=list, max_length=8)


def install_pipeline(application):
    app, store = application.auth, application.video_store

    def owner(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        return session["account_id"], request.scope["workspace_id"]

    def image_state(aid, wid):
        return application.scoped_app(aid, wid).state.workbench

    def binding(aid, wid, pid, *, create=False):
        project = store._project(aid, wid, pid)
        state = image_state(aid, wid)
        with store.lock():
            project = store._project(aid, wid, pid)
            link = project.get("pipeline")
            if not link and create:
                mid = uuid5(NAMESPACE_URL, "h3-pipeline:" + pid).hex
                main = state.create_main_project(project["name"], mid, "H3 图片、脚本与视频项目")
                sub = state.projects.default_subproject(main.main_project_id)
                link = {"main_project_id": main.main_project_id, "subproject_id": sub.subproject_id}
                project["pipeline"] = link
                store._write(store._project_path(pid), project)
            if not link:
                raise ConflictError("请先初始化当前项目的创作步骤")
            sub = state.projects.get_subproject(link["subproject_id"])
            if sub.main_project_id != link["main_project_id"]:
                raise ValueError("项目关联无效")
        return project, state, link

    def view(aid, wid, pid):
        project, state, link = binding(aid, wid, pid)
        _, _, records = state.project_views(link["main_project_id"])
        images = [state.logical_task_view(j["task_id"]) for j in records if j["subproject_id"] == link["subproject_id"]]
        scripts = application.script_service.list_tasks(aid, wid, link["subproject_id"])
        return {**link, "image_tasks": images, "script_tasks": scripts, "active_script_task_id": project.get("script_task_id")}

    def use_script(aid, wid, pid, tid):
        _, state, link = binding(aid, wid, pid)
        result = application.script_service.detail(aid, wid, tid)
        if state.projects.get_task_placement(tid).subproject_id != link["subproject_id"] or result["status"] != "completed":
            raise ValueError("请选择当前项目已经完成的脚本")
        with store.lock():
            project = store._project(aid, wid, pid)
            project.update(script=result["text"], script_task_id=tid, script_version_id=result["version_id"], updated_at=_now())
            store._write(store._project_path(pid), project)
        return store.project(aid, wid, pid)

    @app.post("/api/video/projects/{pid}/pipeline")
    def initialize(pid: str, body: ScriptBody, request: Request):
        aid, wid = owner(request)
        binding(aid, wid, pid, create=True)
        return view(aid, wid, pid)

    @app.get("/api/video/projects/{pid}/pipeline")
    def pipeline(pid: str, request: Request):
        return view(*owner(request), pid)

    @app.post("/api/video/projects/{pid}/images")
    def image_draft(pid: str, body: ImageRequest, request: Request):
        aid, wid = owner(request)
        _, state, link = binding(aid, wid, pid)
        if len(set(body.reference_ids)) != len(body.reference_ids):
            raise ValueError("参考图不能重复")
        inputs = [store.asset_file(aid, wid, pid, _validate_id(rid))[0] for rid in body.reference_ids]
        if sum(path.stat().st_size for path in inputs) > 40 * 1024 * 1024:
            raise ValueError("本次生图参考文件合计不能超过40 MiB")
        task = state.create_task(link["main_project_id"], request_id=body.request_id, operation="generate",
                                prompt=body.prompt, aspect_ratio=body.aspect_ratio, inputs=inputs,
                                subproject_id=link["subproject_id"])
        return state.logical_task_view(task.task_id)

    @app.post("/api/video/projects/{pid}/images/from-artifact")
    def import_image(pid: str, body: ArtifactChoice, request: Request):
        aid, wid = owner(request)
        _, state, link = binding(aid, wid, pid)
        artifact = state.storage.get_artifact(body.artifact_id)
        placement = state.projects.get_task_placement(artifact.task_id)
        if placement.subproject_id != link["subproject_id"] or artifact.kind != "output" or artifact.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("请选择当前项目的已生成图片")
        path = state.storage.get_artifact_path(artifact.artifact_id)
        if path.stat().st_size > store.storage.config.max_image_bytes:
            raise ValueError("图片产物超过大小限制")
        data = path.read_bytes()
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError("图片产物校验失败")
        return {"assets": store.add_assets(aid, wid, pid, [("生成图片-" + artifact.artifact_id[:8] + path.suffix, data)])}

    @app.post("/api/video/projects/{pid}/script/from-task")
    def select_script(pid: str, body: ScriptChoice, request: Request):
        return use_script(*owner(request), pid, body.task_id)

    @app.post("/api/video/projects/{pid}/script/upload")
    async def upload_script(pid: str, request: Request):
        aid, wid = owner(request)
        _, _, link = await run_in_threadpool(binding, aid, wid, pid)
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
            result = await run_in_threadpool(application.script_service.import_text, aid, wid, link["subproject_id"],
                form.get("request_id"), text, filename=filename[:255], generation_type=form.get("generation_type", "script"))
        await run_in_threadpool(use_script, aid, wid, pid, result["task_id"])
        return result

    @app.post("/api/video/projects/{pid}/videos/upload")
    async def upload_video(pid: str, request: Request):
        aid, wid = owner(request)
        await run_in_threadpool(store._project, aid, wid, pid)
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
