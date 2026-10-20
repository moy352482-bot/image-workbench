"""Image/text/video orchestration independent of HTTP and concrete file layout."""
import hashlib
from typing import Callable, Protocol
from uuid import NAMESPACE_URL, uuid5

from .domain_errors import ConflictError
from .storage import _validate_id


class PipelineVideoPort(Protocol):
    """The video module owns persistence, locking and ownership validation."""
    @property
    def max_image_bytes(self) -> int: ...

    def pipeline_project(self, account_id, workspace_id, project_id): ...
    def ensure_pipeline_binding(self, account_id, workspace_id, project_id, create_link): ...
    def set_pipeline_script(self, account_id, workspace_id, project_id, *, text, task_id, version_id): ...
    def asset_file(self, account_id, workspace_id, project_id, asset_id): ...
    def add_assets(self, account_id, workspace_id, project_id, files): ...
    def associate_artifact(self, account_id, workspace_id, project_id, asset_id, artifact_id): ...


class PipelineService:
    def __init__(self, videos: PipelineVideoPort, scripts: Callable, image_state: Callable):
        self.videos, self.scripts, self.image_state = videos, scripts, image_state

    def binding(self, aid, wid, pid, *, create=False):
        project = self.videos.pipeline_project(aid, wid, pid)
        state = self.image_state(aid, wid)

        def create_link(project):
            mid = uuid5(NAMESPACE_URL, "h3-pipeline:" + pid).hex
            main = state.create_main_project(project["name"], mid, "H3 图片、脚本与视频项目")
            sub = state.projects.default_subproject(main.main_project_id)
            return {"main_project_id": main.main_project_id, "subproject_id": sub.subproject_id}

        if create:
            project = self.videos.ensure_pipeline_binding(aid, wid, pid, create_link)
        link = project.get("pipeline")
        if not link:
            raise ConflictError("请先初始化当前项目的创作步骤")
        sub = state.projects.get_subproject(link["subproject_id"])
        if sub.main_project_id != link["main_project_id"]:
            raise ValueError("项目关联无效")
        return project, state, link

    def view(self, aid, wid, pid):
        project, state, link = self.binding(aid, wid, pid)
        _, _, records = state.project_views(link["main_project_id"])
        images = [state.logical_task_view(j["task_id"]) for j in records if j["subproject_id"] == link["subproject_id"]]
        scripts = self.scripts().list_tasks(aid, wid, link["subproject_id"])
        return {**link, "image_tasks": images, "script_tasks": scripts, "active_script_task_id": project.get("script_task_id")}

    def use_script(self, aid, wid, pid, tid):
        _, state, link = self.binding(aid, wid, pid)
        result = self.scripts().detail(aid, wid, tid)
        if state.projects.get_task_placement(tid).subproject_id != link["subproject_id"] or result["status"] != "completed":
            raise ValueError("请选择当前项目已经完成的脚本")
        return self.videos.set_pipeline_script(aid, wid, pid, text=result["text"], task_id=tid, version_id=result["version_id"])

    def create_image(self, aid, wid, pid, *, request_id, prompt, aspect_ratio, reference_ids):
        _, state, link = self.binding(aid, wid, pid)
        if len(set(reference_ids)) != len(reference_ids):
            raise ValueError("参考图不能重复")
        inputs = [self.videos.asset_file(aid, wid, pid, _validate_id(rid))[0] for rid in reference_ids]
        if sum(path.stat().st_size for path in inputs) > 40 * 1024 * 1024:
            raise ValueError("本次生图参考文件合计不能超过40 MiB")
        task = state.create_task(link["main_project_id"], request_id=request_id, operation="generate",
                                prompt=prompt, aspect_ratio=aspect_ratio, inputs=inputs,
                                subproject_id=link["subproject_id"])
        return state.logical_task_view(task.task_id)

    def import_image(self, aid, wid, pid, artifact_id):
        _, state, link = self.binding(aid, wid, pid)
        artifact = state.storage.get_artifact(artifact_id)
        placement = state.projects.get_task_placement(artifact.task_id)
        if placement.subproject_id != link["subproject_id"] or artifact.kind != "output" or artifact.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("请选择当前项目的已生成图片")
        path = state.storage.get_artifact_path(artifact.artifact_id)
        if path.stat().st_size > self.videos.max_image_bytes:
            raise ValueError("图片产物超过大小限制")
        data = path.read_bytes()
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError("图片产物校验失败")
        imported = self.videos.add_assets(aid, wid, pid, [("生成图片-" + artifact.artifact_id[:8] + path.suffix, data)])
        return {"assets": [self.videos.associate_artifact(aid, wid, pid, item["asset_id"], artifact.artifact_id)
                           for item in imported]}

    def import_script(self, aid, wid, pid, request_id, text, *, filename, generation_type):
        _, _, link = self.binding(aid, wid, pid)
        result = self.scripts().import_text(aid, wid, link["subproject_id"], request_id, text,
                                           filename=filename, generation_type=generation_type)
        self.use_script(aid, wid, pid, result["task_id"])
        return result
