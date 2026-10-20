"""Image upload and project selection using the same artifact registration path."""
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..asset_library import AssetLibrary, MAX_UPLOAD_BYTES, MAX_UPLOAD_IMAGES


async def read_images(request, max_image_bytes):
    async with request.form(max_files=MAX_UPLOAD_IMAGES, max_fields=1) as form:
        if set(form) - {"files", "request_id"} or len(form.getlist("request_id")) > 1:
            raise ValueError("图片上传字段无效")
        if form.get("request_id"):
            from ..storage import _validate_id
            _validate_id(form["request_id"])
        files, total = [], 0
        for upload in form.getlist("files"):
            if not isinstance(upload, UploadFile):
                raise ValueError("请选择图片文件")
            data = await upload.read(max_image_bytes + 1)
            total += len(data)
            if total > MAX_UPLOAD_BYTES:
                raise ValueError("本次图片合计超过 40 MiB，请分批上传")
            files.append((upload.filename or "clipboard.png", data))
        return files


def install_routes(app, state, storage):
    library = AssetLibrary(state)

    @app.get("/api/subprojects/{subproject_id}/assets")
    def images(subproject_id: str):
        return {"assets": [library.view(item) for item in library.list_images(subproject_id)]}

    @app.post("/api/subprojects/{subproject_id}/assets")
    async def upload(subproject_id: str, request: Request):
        await run_in_threadpool(state.projects.get_subproject, subproject_id)
        files = await read_images(request, storage.config.max_image_bytes)
        return {"assets": await run_in_threadpool(library.upload, subproject_id, files)}
