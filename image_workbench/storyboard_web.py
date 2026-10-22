"""Authenticated storyboard HTTP adapter; execution stays in the independent service."""
from contextlib import asynccontextmanager
import asyncio
from fastapi import BackgroundTasks, HTTPException, Query, Request

from .contracts.storyboards import (ID, Accept, Body, Edit, PlanRequest, Replan, ReplanSubmission,
                                    Submission, VideoDraft)
from .storage import _validate_id


def install_storyboards(app, service, *, resume_pending=None):
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            recovery = asyncio.create_task(asyncio.to_thread(resume_pending or service.resume_pending))
            try:
                yield
            finally:
                await recovery
    app.router.lifespan_context = lifespan

    def owner(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        return session["account_id"], request.scope["workspace_id"]

    @app.get("/api/storyboards/config")
    def config(request: Request):
        return service.config(*owner(request))

    @app.post("/api/storyboards/quote")
    def quote(body: PlanRequest, request: Request):
        return service.quote(*owner(request), body.model_dump(exclude_none=True))

    @app.post("/api/storyboards/tasks")
    def submit(body: Submission, request: Request, background_tasks: BackgroundTasks):
        aid, wid = owner(request)
        task = service.submit(aid, wid, body.model_dump(exclude_none=True))
        if task["status"] == "queued":
            background_tasks.add_task(service.run, aid, wid, task["task_id"])
        return task

    @app.get("/api/storyboards/tasks/{tid}")
    def task(tid: str, request: Request):
        return service.task(*owner(request), _validate_id(tid))

    @app.post("/api/storyboards/tasks/{tid}/run")
    def run(tid: str, body: Body, request: Request, background_tasks: BackgroundTasks):
        aid, wid = owner(request)
        task = service.task(aid, wid, _validate_id(tid))
        if task["status"] in {"queued", "uncertain", "running"}:
            background_tasks.add_task(service.run, aid, wid, tid)
        return task

    @app.post("/api/storyboards/tasks/{tid}/cancel")
    def cancel(tid: str, body: Body, request: Request):
        return service.cancel(*owner(request), _validate_id(tid))

    @app.get("/api/storyboards")
    def storyboards(request: Request, video_project_id: str | None = Query(default=None, pattern=ID),
                    subproject_id: str | None = Query(default=None, pattern=ID)):
        return {"storyboards": service.list_storyboards(*owner(request), video_project_id=video_project_id, subproject_id=subproject_id)}

    @app.get("/api/storyboards/{bid}")
    def detail(bid: str, request: Request, version: int | None = Query(default=None, ge=1)):
        return service.detail(*owner(request), _validate_id(bid), version)

    @app.get("/api/storyboards/{bid}/assets")
    def assets(bid: str, request: Request):
        return {"assets": service.assets(*owner(request), _validate_id(bid))}

    @app.post("/api/storyboards/{bid}/edit")
    def edit(bid: str, body: Edit, request: Request):
        return service.edit(*owner(request), _validate_id(bid), body.model_dump(exclude_none=True))

    @app.post("/api/storyboards/{bid}/replan/quote")
    def replan_quote(bid: str, body: Replan, request: Request):
        return service.replan_quote(*owner(request), _validate_id(bid), body.model_dump(exclude_none=True))

    @app.post("/api/storyboards/{bid}/replan")
    def replan(bid: str, body: ReplanSubmission, request: Request, background_tasks: BackgroundTasks):
        aid, wid = owner(request)
        task = service.replan(aid, wid, _validate_id(bid), body.model_dump(exclude_none=True))
        if task["status"] == "queued":
            background_tasks.add_task(service.run, aid, wid, task["task_id"])
        return task

    @app.post("/api/storyboards/{bid}/accept")
    def accept(bid: str, body: Accept, request: Request):
        return service.accept(*owner(request), _validate_id(bid), body.model_dump())

    @app.post("/api/storyboards/{bid}/shots/{shot_id}/video-draft")
    def video_draft(bid: str, shot_id: str, body: VideoDraft, request: Request):
        return service.video_draft(*owner(request), _validate_id(bid), _validate_id(shot_id), body.model_dump())
