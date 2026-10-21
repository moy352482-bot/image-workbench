"""Worker HTTP adapter; queue implementation has no web dependencies."""
from fastapi import Request, Response
from starlette.concurrency import run_in_threadpool
from ..storage import _validate_id
from ..contracts.image_queue import Claim, Start, Complete

def install_routes(app, queue):
    app.state.remote_queue = queue

    def identity(request):
        return (_validate_id(request.headers.get("x-worker-id", "")),
                _validate_id(request.headers.get("x-claim-id", "")))

    @app.post("/api/worker/claim")
    def claim(payload: Claim):
        return {"job": queue.claim(payload.worker_id, payload.backend)}

    @app.post("/api/worker/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, request: Request):
        queue.beat(job_id, *identity(request))
        return {"ok": True}

    @app.post("/api/worker/jobs/{job_id}/start")
    def start(job_id: str, payload: Start, request: Request):
        return queue.start(job_id, *identity(request), payload.attempt_id)

    @app.get("/api/worker/jobs/{job_id}/assets/{artifact_id}")
    def asset(job_id: str, artifact_id: str, request: Request):
        return Response(queue.asset(job_id, *identity(request), artifact_id), media_type="application/octet-stream")

    @app.put("/api/worker/jobs/{job_id}/outputs/{index}")
    async def upload(job_id: str, index: int, request: Request):
        digest = await run_in_threadpool(queue.upload, job_id, *identity(request), index, await request.body())
        return {"sha256": digest}

    @app.post("/api/worker/jobs/{job_id}/complete")
    def complete(job_id: str, payload: Complete, request: Request):
        return queue.complete(job_id, *identity(request), payload)
