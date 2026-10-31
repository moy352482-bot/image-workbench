"""Multipart input lifetime: validate, stage, submit and always clean up."""
from uuid import uuid4
import json
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from ..contracts.images import MAX_UPLOAD_BYTES

async def save_task(state, storage, project_id: str, subproject_id: str, request: Request):
    async with request.form(max_files=8, max_fields=13, max_part_size=64 * 1024) as form:
        allowed = {"request_id", "operation", "prompt", "aspect_ratio", "base_artifact_id", "files", "name", "description",
                   "reference_artifact_ids", "style_id", "original_prompt", "prompt_source"}
        if set(form) - allowed or any(len(form.getlist(key)) != 1 for key in set(form) - {"files"}):
            raise HTTPException(400, "任务字段不正确，不能指定本地文件路径。")
        for key in ("request_id", "operation", "prompt"):
            if not isinstance(form.get(key), str):
                raise HTTPException(400, "请填写请求标识、模式和提示词。")
        files = form.getlist("files")
        if len(files) > 8 or any(not isinstance(item, UploadFile) for item in files):
            raise HTTPException(400, "最多上传8张参考图片。")
        for key in ("aspect_ratio", "base_artifact_id", "name", "description", "style_id", "original_prompt", "prompt_source"):
            if key in form and not isinstance(form[key], str):
                raise HTTPException(400, "比例或版本来源格式错误。")
        try:
            refs = json.loads(form.get("reference_artifact_ids", "[]"))
            if not isinstance(refs, list) or len(refs) > 8 or any(not isinstance(item, str) for item in refs):
                raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(400, "参考素材列表无效，请重新选择图片")
        source_text = form.get("prompt_source")
        try:
            prompt_source = json.loads(source_text) if source_text else None
        except (TypeError, ValueError):
            raise HTTPException(400, "提示词来源格式错误。") from None
        temporary = storage.files.path(".workbench", "uploads", uuid4().hex)
        temporary.mkdir()
        sources, total = [], 0
        try:
            for upload in files:
                path = storage.files.check(temporary / (uuid4().hex + ".upload"))
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
                inputs=sources, reference_artifact_ids=refs, subproject_id=subproject_id,
                name=form.get("name") or None, description=form.get("description", ""),
                style_id=form.get("style_id") or None, original_prompt=form.get("original_prompt") or None, prompt_source=prompt_source)
            return await run_in_threadpool(state.task_view, project_id, task.task_id)
        finally:
            for path in sources:
                storage.files.cleanup(path)
            storage.files.cleanup(temporary, directory=True)
