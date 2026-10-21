"""Qwen 测试页及使用现有执行器认证的图片传递接口。"""
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.concurrency import run_in_threadpool

from .qwen_lab import MAX_IMAGE_BYTES, QwenLabStore
from .storage import _validate_id

ID = r"^[0-9a-f]{32}$"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Submission(Body):
    request_id: str = Field(pattern=ID)
    mode: Literal["generate", "edit", "reference"]
    model: Literal["fp8", "q5"] = "fp8"
    prompt: str = Field(min_length=1, max_length=4000)
    seed: StrictInt = Field(ge=0, le=4294967295)
    resolution: Literal[1024, 1536] = 1024


class Claim(Body):
    worker_id: str = Field(pattern=ID)
    ready: StrictBool = True
    message: str = Field(default="", max_length=200)


class Start(Body):
    attempt_id: str = Field(pattern=ID)
    prompt_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,200}$")


class Complete(Body):
    status: Literal["succeeded", "failed", "uncertain"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: str | None = Field(default=None, max_length=1000)
    elapsed_seconds: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)


def install_qwen(app, store):

    def owner(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        if not session["is_admin"]:
            raise HTTPException(403, "本地模型测试版暂仅管理员可用")
        return session["account_id"], request.scope["workspace_id"]

    def worker(request):
        return _validate_id(request.headers.get("x-worker-id", "")), _validate_id(request.headers.get("x-claim-id", ""))

    @app.get("/qwen-test")
    def page(request: Request):
        owner(request)
        return FileResponse(Path(__file__).parent / "web_templates/qwen.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/qwen/config")
    def config(request: Request):
        owner(request)
        return {"csrf_token": request.scope["account_csrf"], "worker": store.availability(), "cost": 0,
                "models": [{"id": "fp8", "name": "FP8 · 推荐"}, {"id": "q5", "name": "Q5_K_M · 对照"}]}

    @app.get("/api/qwen/jobs")
    def jobs(request: Request):
        return {"jobs": store.list(*owner(request))}

    @app.post("/api/qwen/jobs")
    async def submit(request: Request):
        aid, wid = owner(request)
        async with request.form(max_files=2, max_fields=1) as form:
            if set(form.keys()) - {"payload", "images"} or not isinstance(form.get("payload"), str):
                raise ValueError("提交格式无效")
            try:
                body = Submission.model_validate_json(form["payload"])
            except ValueError as exc:
                raise ValueError("请检查工作流、需求、尺寸和随机种子") from exc
            images = []
            for upload in form.getlist("images"):
                if not hasattr(upload, "read") or not hasattr(upload, "filename"):
                    raise ValueError("请上传图片文件")
                images.append(await upload.read(MAX_IMAGE_BYTES + 1))
            return await run_in_threadpool(store.create, aid, wid, body.request_id,
                                           body.model_dump(exclude={"request_id"}), images)

    @app.get("/api/qwen/jobs/{jid}")
    def job(jid: str, request: Request):
        return store.get(*owner(request), jid)

    @app.post("/api/qwen/jobs/{jid}/cancel")
    def cancel(jid: str, request: Request):
        return store.cancel(*owner(request), jid)

    @app.get("/api/qwen/jobs/{jid}/images/{name}")
    def image(jid: str, name: str, request: Request, download: bool = False):
        data = store.image(*owner(request), jid, name)
        headers = {"Cache-Control": "private, no-store"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="qwen-{_validate_id(jid)}-{name}.png"'
        return Response(data, media_type="image/png", headers=headers)

    @app.post("/api/worker/qwen/claim")
    def claim(body: Claim):
        return {"job": store.claim(body.worker_id, body.ready, body.message)}

    @app.post("/api/worker/qwen/heartbeat")
    def worker_heartbeat(body: Claim):
        with store.lock():
            store.beat(body.worker_id, body.ready, body.message)
        return {"ok": True}

    @app.post("/api/worker/qwen/jobs/{jid}/heartbeat")
    def heartbeat(jid: str, request: Request):
        return store.heartbeat(jid, *worker(request))

    @app.post("/api/worker/qwen/jobs/{jid}/start")
    def start(jid: str, body: Start, request: Request):
        return store.start(jid, *worker(request), **body.model_dump())

    @app.get("/api/worker/qwen/jobs/{jid}/inputs/{index}")
    def reference(jid: str, index: int, request: Request):
        return Response(store.worker_image(jid, *worker(request), index), media_type="image/png")

    @app.put("/api/worker/qwen/jobs/{jid}/output")
    async def upload(jid: str, request: Request):
        identity = worker(request)
        # Authenticate ownership before consuming or decoding result bytes.
        await run_in_threadpool(store._worker_job, jid, *identity)
        data = await request.body()
        return await run_in_threadpool(store.upload, jid, *identity, data, request.headers.get("x-content-sha256", ""))

    @app.post("/api/worker/qwen/jobs/{jid}/complete")
    def complete(jid: str, body: Complete, request: Request):
        return store.complete(jid, *worker(request), **body.model_dump())
