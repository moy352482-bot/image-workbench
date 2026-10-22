"""HTTP 使用的空间边界；图片仍由原 StorageManager 解析。"""
from dataclasses import replace
from contextlib import contextmanager
from contextvars import ContextVar
from .project_manager import ProjectManager
from .storage import _validate_id

_request_catalogs = ContextVar("workspace_catalogs", default=None)


@contextmanager
def workspace_request():
    token = _request_catalogs.set({})
    try:
        yield
    finally:
        _request_catalogs.reset(token)


class WorkspaceProjects:
    def __init__(self, storage, identity, account_id, workspace_id):
        identity.require_workspace(account_id, workspace_id)
        self.raw = ProjectManager(storage)
        self.identity, self.account_id, self.workspace_id = identity, account_id, workspace_id

    @property
    def migration_required(self):
        return self.raw.migration_required

    _required = staticmethod(ProjectManager._required)

    def require_main(self, mid):
        owner = self.identity.ownership(_validate_id(mid))
        if owner["workspace_id"] != self.workspace_id or owner["account_id"] != self.account_id or owner["deleted"]:
            raise FileNotFoundError("项目不存在")
        return owner

    def require_namespace(self, pid):
        _validate_id(pid)
        mid = next((mid for mid, value in self._catalog()["storage_namespaces"].items() if value == pid), None)
        if not mid:
            raise FileNotFoundError("项目不存在")
        self.require_main(mid)

    def _catalog(self):
        cache = _request_catalogs.get()
        key = str(self.raw.storage.data_root)
        if cache is None:
            return self.raw.catalog_view()
        if key not in cache:
            cache[key] = self.raw.catalog_view()
        return cache[key]

    def _invalidate(self):
        cache = _request_catalogs.get()
        if cache is not None:
            cache.pop(str(self.raw.storage.data_root), None)

    def catalog_view(self):
        value = dict(self._catalog())
        ids = self.identity.project_ids(self.workspace_id)
        value["main_projects"] = {key: {**item, "workspace_id": self.workspace_id} for key, item in value["main_projects"].items() if key in ids}
        for section in ("storage_namespaces", "default_subprojects"):
            value[section] = {key: item for key, item in value[section].items() if key in ids}
        value["subprojects"] = {key: item for key, item in value["subprojects"].items() if item["main_project_id"] in ids}
        value["task_placements"] = {key: item for key, item in value["task_placements"].items() if item["subproject_id"] in value["subprojects"]}
        return value

    def get_main_project(self, mid):
        self.require_main(mid)
        return replace(self.raw.get_main_project(mid), workspace_id=self.workspace_id)

    def list_main_projects(self):
        ids = self.identity.project_ids(self.workspace_id)
        return [replace(item, workspace_id=self.workspace_id) for item in self.raw.list_main_projects() if item.main_project_id in ids]

    def create_main_project(self, name, description="", *, main_project_id):
        if main_project_id in self.raw.catalog_view()["main_projects"]:
            # 不允许通过客户端自选 ID 认领尚未迁移或由 CLI 创建的旧项目。
            raise FileNotFoundError("项目 ID 不可用")
        self.identity.assign_project(main_project_id, self.workspace_id)
        try:
            return replace(self.raw.create_main_project(name, description, main_project_id=main_project_id), workspace_id=self.workspace_id)
        finally:
            self._invalidate()

    def get_subproject(self, sid):
        item = self.raw.get_subproject(sid)
        self.require_main(item.main_project_id)
        return item

    def get_task_placement(self, tid, *, main_project_id=None):
        item = self.raw.get_task_placement(tid, main_project_id=main_project_id)
        self.get_subproject(item.subproject_id)
        return item

    def move_task(self, tid, target_subproject_id, **kwargs):
        self.get_task_placement(tid)
        self.get_subproject(target_subproject_id)
        try:
            return self.raw.move_task(tid, target_subproject_id, **kwargs)
        finally:
            self._invalidate()

    def __getattr__(self, name):
        main_methods = {"update_main_project", "create_subproject", "list_subprojects", "default_subproject", "ensure_storage_namespace"}
        sub_methods = {"update_subproject", "reserve_task", "create_task"}
        task_methods = {"finish_task", "update_task"}
        if name not in main_methods | sub_methods | task_methods:
            raise AttributeError(name)
        def checked(identity, *args, **kwargs):
            if name in main_methods:
                self.require_main(identity)
            elif name in sub_methods:
                self.get_subproject(identity)
            else:
                self.get_task_placement(identity)
            try:
                return getattr(self.raw, name)(identity, *args, **kwargs)
            finally:
                if name not in {"list_subprojects", "default_subproject"}:
                    self._invalidate()
        return checked


class WorkspaceStorage:
    def __init__(self, storage, projects):
        self.raw, self.projects = storage, projects
        self.config, self.data_root = storage.config, storage.data_root
        # Trusted services use this file port for shared queue/catalog metadata.
        # Project/Artifact methods below remain the Workspace authorization boundary.
        self.files = storage.files

    def get_artifact(self, artifact_id):
        item = self.raw.get_artifact(artifact_id)
        self.projects.require_namespace(item.project_id)
        return item

    def get_artifact_path(self, artifact_id):
        self.get_artifact(artifact_id)
        return self.raw.get_artifact_path(artifact_id)

    def artifact_file(self, artifact):
        self.projects.require_namespace(artifact.project_id)
        return self.raw.artifact_file(artifact)

    def inspect_image(self, path):
        return self.raw.inspect_image(path)

    def copy_image_source(self, source, destination):
        return self.raw.copy_image_source(source, destination)

    def list_projects(self):
        allowed = set(self.projects.catalog_view()["storage_namespaces"].values())
        return [item for item in self.raw.list_projects() if item.project_id in allowed]

    def __getattr__(self, name):
        project_methods = {"get_project", "create_task", "get_task", "list_tasks", "list_task_artifacts", "add_input_image", "register_output_image", "register_output_text", "register_output_document", "require_image_source", "task_files", "_require_source", "_require_text_source", "_project_dir", "_task_dir"}
        if name in project_methods:
            def checked(project_id, *args, **kwargs):
                self.projects.require_namespace(project_id)
                return getattr(self.raw, name)(project_id, *args, **kwargs)
            return checked
        if name.startswith("_"):
            return getattr(self.raw, name)
        raise AttributeError(name)
