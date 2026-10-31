"""Artifacts HTTP routes."""
import hashlib
from fastapi import HTTPException
from fastapi.responses import Response
from ..contracts.images import Payload

def install_routes(app, state, storage):
    @app.post("/api/artifacts/{artifact_id}/approve")
    def approve(artifact_id: str, payload: Payload):
        return state.approve(artifact_id)

    @app.get("/api/artifacts/{artifact_id}")
    def artifact(artifact_id: str, download: bool = False):
        item = storage.get_artifact(artifact_id)
        path = storage.get_artifact_path(artifact_id)
        size_limit = (storage.config.max_text_bytes if item.format in {"TXT", "JSON"} else
                      40 * 1024 * 1024 if item.format == "MP4" else storage.config.max_image_bytes)
        if path.stat().st_size != item.size_bytes or item.size_bytes > size_limit:
            raise HTTPException(status_code=400, detail="文件校验未通过，请刷新后重试。")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise HTTPException(status_code=400, detail="文件校验未通过，请刷新后重试。")
        mime, extension = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "WEBP": ("image/webp", "webp"), "TXT": ("text/plain; charset=utf-8", "txt"),
                           "JSON": ("application/json", "json"), "MP4": ("video/mp4", "mp4")}[item.format]
        if item.format in {"TXT", "JSON"}:
            data.decode("utf-8")
        prefix = "text" if item.format == "TXT" else "image" if item.format in {"PNG", "JPEG", "WEBP"} else "artifact"
        headers = {"Content-Disposition": f'{"attachment" if download else "inline"}; filename="{prefix}-{artifact_id}.{extension}"'}
        return Response(data, media_type=mime, headers=headers)
