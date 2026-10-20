"""Worker HTTP adapter; queue implementation has no web dependencies."""
from fastapi import Request
from ..storage import _validate_id
from ..contracts.script_queue import Claim, Start, Complete

def install_script_worker(app, queue):

    def worker(request):
        return (_validate_id(request.headers.get("x-worker-id", "")), _validate_id(request.headers.get("x-claim-id", "")))

    @app.post("/api/worker/scripts/claim")
    def claim(body: Claim):
        return {"job": queue.claim(body.worker_id, [m.model_dump() for m in body.models])}

    @app.post("/api/worker/scripts/tasks/{tid}/heartbeat")
    def heartbeat(tid: str, request: Request):
        return queue.heartbeat(tid, *worker(request))

    @app.post("/api/worker/scripts/tasks/{tid}/start")
    def start(tid: str, body: Start, request: Request):
        return queue.start(tid, *worker(request), body.attempt_id)

    @app.post("/api/worker/scripts/tasks/{tid}/complete")
    def complete(tid: str, body: Complete, request: Request):
        return queue.complete(tid, *worker(request), body.model_dump())
