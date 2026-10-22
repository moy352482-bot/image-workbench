"""Tasks HTTP routes."""
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from ..contracts.images import RunTask, RestoreTask, TaskUpdate, MoveTask
from ..domain_errors import ConflictError
from .uploads import save_task

def install_routes(app, state, storage):
    @app.get("/api/image-styles")
    def image_styles():
        return state.image_style_catalog()

    @app.get("/api/projects/{project_id}/tasks")
    def tasks(project_id: str):
        _, _, records = state.project_views(project_id)
        return {"tasks": records}

    @app.post("/api/projects/{project_id}/tasks")
    async def create_task(project_id: str, request: Request):
        sub = await run_in_threadpool(state.projects.default_subproject, project_id)
        return await save_task(state, storage, project_id, sub.subproject_id, request)

    @app.get("/api/projects/{project_id}/tasks/{task_id}")
    def task(project_id: str, task_id: str):
        return state.logical_task_view(task_id, main_project_id=project_id)

    @app.get("/api/projects/{project_id}/tasks/{task_id}/recovery")
    def recovery(project_id: str, task_id: str):
        return state.logical_task_view(task_id, main_project_id=project_id)["recovery"]

    @app.post("/api/projects/{project_id}/tasks/{task_id}/run")
    def run(project_id: str, task_id: str, payload: RunTask):
        placement = state.projects.get_task_placement(task_id, main_project_id=project_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id,
                           confirmed=payload.confirm_generation, retry_of=payload.retry_of)
        return {"job_id": job["job_id"], "status": job["status"]}

    @app.post("/api/projects/{project_id}/tasks/{task_id}/restore")
    def restore(project_id: str, task_id: str, payload: RestoreTask):
        placement = state.projects.get_task_placement(task_id, main_project_id=project_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id, restore_execution_id=payload.execution_id)
        return {"job_id": job["job_id"], "status": job["status"]}

    @app.get("/api/subprojects/{subproject_id}/tasks")
    def sub_tasks(subproject_id: str):
        sub = state.projects.get_subproject(subproject_id)
        mains, subs, records = state.project_views(sub.main_project_id)
        return {"subproject": subs[subproject_id], "main_project": mains[sub.main_project_id],
                "tasks": [item for item in records if item["subproject_id"] == subproject_id]}

    @app.post("/api/subprojects/{subproject_id}/tasks")
    async def create_sub_task(subproject_id: str, request: Request):
        sub = await run_in_threadpool(state.projects.get_subproject, subproject_id)
        return await save_task(state, storage, sub.main_project_id, subproject_id, request)

    @app.get("/api/tasks/{task_id}")
    def logical_task(task_id: str):
        return state.logical_task_view(task_id)

    @app.get("/api/tasks/{task_id}/recovery")
    def logical_recovery(task_id: str):
        return state.logical_task_view(task_id)["recovery"]

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, payload: TaskUpdate):
        with state.control_lock():
            state.projects.update_task(task_id, **payload.model_dump(exclude_none=True))
        return state.logical_task_view(task_id)

    @app.post("/api/tasks/{task_id}/move")
    def move_task(task_id: str, payload: MoveTask):
        with state.control_lock():
            placement = state.projects.get_task_placement(task_id)
            if (state.task_options(placement.storage_project_id, task_id) or {}).get("asset_library"):
                raise ConflictError("项目素材库不能移动；请在目标子项目添加素材")
            state.projects.move_task(task_id, payload.target_subproject_id, expected_subproject_id=payload.expected_subproject_id)
        return state.logical_task_view(task_id)

    @app.post("/api/tasks/{task_id}/run")
    def run_logical_task(task_id: str, payload: RunTask):
        placement = state.projects.get_task_placement(task_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id,
                           confirmed=payload.confirm_generation, retry_of=payload.retry_of)
        return {"job_id": job["job_id"], "status": job["status"]}

    @app.post("/api/tasks/{task_id}/restore")
    def restore_logical_task(task_id: str, payload: RestoreTask):
        placement = state.projects.get_task_placement(task_id)
        job = state.submit(placement.storage_project_id, task_id, request_id=payload.request_id, restore_execution_id=payload.execution_id)
        return {"job_id": job["job_id"], "status": job["status"]}
