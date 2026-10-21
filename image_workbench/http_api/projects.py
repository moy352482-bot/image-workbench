"""Projects HTTP routes."""
from dataclasses import asdict
from ..storage import metadata_read_snapshot
from ..contracts.images import CreateProject, ProjectUpdate

def install_routes(app, state, storage):
    @app.get("/api/projects")
    def projects():
        return {"projects": [{**asdict(item), "project_id": item.main_project_id} for item in reversed(state.projects.list_main_projects())]}

    @app.post("/api/projects")
    def create_project(payload: CreateProject):
        return asdict(state.create_project(payload.name, payload.request_id))

    def main_overview(main_project_id):
        mains, _, _ = state.project_views(main_project_id)
        return mains[main_project_id]

    def sub_overview(subproject_id):
        sub = state.projects.get_subproject(subproject_id)
        _, subs, _ = state.project_views(sub.main_project_id)
        return subs[subproject_id]

    @app.get("/api/main-projects")
    def main_projects():
        with metadata_read_snapshot():
            mains, _, _ = state.project_views()
        return {"main_projects": sorted(mains.values(), key=lambda item: (item["updated_at"], item["main_project_id"]), reverse=True)}

    @app.post("/api/main-projects")
    def create_main(payload: CreateProject):
        main = state.create_main_project(payload.name, payload.request_id, payload.description)
        return main_overview(main.main_project_id)

    @app.get("/api/main-projects/{main_project_id}")
    def main_detail(main_project_id: str):
        mains, _, records = state.project_views(main_project_id)
        main = mains[main_project_id]
        return {"main_project": main, "subprojects": main["subprojects"], "recent_tasks": records[:8]}

    @app.patch("/api/main-projects/{main_project_id}")
    def update_main(main_project_id: str, payload: ProjectUpdate):
        with state.control_lock():
            state.projects.update_main_project(main_project_id, **payload.model_dump(exclude_none=True))
        return main_overview(main_project_id)

    @app.post("/api/main-projects/{main_project_id}/subprojects")
    def create_sub(main_project_id: str, payload: CreateProject):
        sub = state.create_subproject(main_project_id, payload.name, payload.request_id, payload.description)
        return sub_overview(sub.subproject_id)

    @app.patch("/api/subprojects/{subproject_id}")
    def update_sub(subproject_id: str, payload: ProjectUpdate):
        with state.control_lock():
            state.projects.update_subproject(subproject_id, **payload.model_dump(exclude_none=True))
        return sub_overview(subproject_id)
