"""Image/text/video orchestration independent of HTTP and concrete file layout."""
import hashlib
from typing import Callable, Protocol
from uuid import NAMESPACE_URL, uuid5

from .domain_errors import ConflictError
from .storage import _validate_id
from .asset_library import AssetLibrary


class PipelineVideoPort(Protocol):
    """The video module owns persistence, locking and ownership validation."""
    @property
    def max_image_bytes(self) -> int: ...

    def pipeline_project(self, account_id, workspace_id, project_id): ...
    def ensure_pipeline_binding(self, account_id, workspace_id, project_id, create_link): ...
    def set_pipeline_script(self, account_id, workspace_id, project_id, *, text, task_id, version_id): ...
    def asset_file(self, account_id, workspace_id, project_id, asset_id): ...
    def add_assets(self, account_id, workspace_id, project_id, files): ...
    def link_artifacts(self, account_id, workspace_id, project_id, artifacts): ...
    def reference_output(self, account_id, workspace_id, project_id, shot_id): ...


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
        main = state.projects.get_main_project(link["main_project_id"])
        sub = state.projects.get_subproject(link["subproject_id"])
        return {**link, "main_project_name": main.name, "subproject_name": sub.name,
                "image_tasks": images, "script_tasks": scripts, "active_script_task_id": project.get("script_task_id")}

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
        refs = []
        library = AssetLibrary(state)
        for rid in reference_ids:
            path, _ = self.videos.asset_file(aid, wid, pid, _validate_id(rid))
            try:
                refs.append(library.require(link["subproject_id"], rid).artifact_id)
            except FileNotFoundError:
                # Legacy video files are adopted once; their old paths remain valid.
                refs.append(library.upload(link["subproject_id"], [(path.name, path.read_bytes())])[0]["artifact_id"])
        task = state.create_task(link["main_project_id"], request_id=request_id, operation="generate",
                                prompt=prompt, aspect_ratio=aspect_ratio, reference_artifact_ids=refs,
                                subproject_id=link["subproject_id"])
        return state.logical_task_view(task.task_id)

    def upload_images(self, aid, wid, pid, files):
        _, state, link = self.binding(aid, wid, pid, create=True)
        assets = AssetLibrary(state).upload(link["subproject_id"], files)
        return self.videos.link_artifacts(aid, wid, pid, assets)

    def from_subproject(self, aid, wid, sid):
        state = self.image_state(aid, wid)
        sub = state.projects.get_subproject(sid)
        main = state.projects.get_main_project(sub.main_project_id)
        for item in self.videos.projects(aid, wid):
            project = self.videos.pipeline_project(aid, wid, item["project_id"])
            if project.get("pipeline", {}).get("subproject_id") == sid:
                return {**self.videos.project(aid, wid, item["project_id"]), **project["pipeline"],
                        "main_project_name": main.name, "subproject_name": sub.name}
        pid = uuid5(NAMESPACE_URL, "image-workbench:video-subproject:" + sid).hex
        self.videos.create_project(aid, wid, pid, sub.name)
        link = {"main_project_id": sub.main_project_id, "subproject_id": sid}
        self.videos.bind_subproject(aid, wid, pid, link)
        return {**self.videos.project(aid, wid, pid), **link,
                "main_project_name": main.name, "subproject_name": sub.name}

    def bind_existing(self, aid, wid, pid, sid):
        """Explicitly adopt a standalone video project into a shared subproject."""
        project = self.videos.pipeline_project(aid, wid, pid)
        state = self.image_state(aid, wid)
        sub = state.projects.get_subproject(sid)
        existing = project.get("pipeline")
        if existing:
            if existing.get("subproject_id") == sid:
                main = state.projects.get_main_project(sub.main_project_id)
                return {**self.videos.project(aid, wid, pid), **existing,
                        "main_project_name": main.name, "subproject_name": sub.name}
            raise ConflictError("视频项目已关联其他子项目")
        for item in self.videos.projects(aid, wid):
            if item["project_id"] != pid and item.get("pipeline", {}).get("subproject_id") == sid:
                raise ConflictError("该子项目已有视频项目")
        link = {"main_project_id": sub.main_project_id, "subproject_id": sid}
        self.videos.bind_subproject(aid, wid, pid, link)
        main = state.projects.get_main_project(sub.main_project_id)
        return {**self.videos.project(aid, wid, pid), **link,
                "main_project_name": main.name, "subproject_name": sub.name}

    def _tray_candidates(self, aid, wid, pid):
        project, state, link = self.binding(aid, wid, pid)
        library = AssetLibrary(state)
        assets = [library.view(item) for item in library.list_images(link["subproject_id"])]
        for shot in self.videos.project(aid, wid, pid)["shots"]:
            if shot.get("output_url"):
                asset_id = self.videos.reference_output(aid, wid, pid, shot["shot_id"])
                assets.append({"artifact_id": asset_id, "media_type": "video", "shot_id": shot["shot_id"],
                               "filename": shot["name"], "url": shot["output_url"],
                               "download_url": shot["download_url"], "created_at": shot["created_at"]})
        return project, assets

    def tray(self, aid, wid, pid):
        project, assets = self._tray_candidates(aid, wid, pid)
        tray = project.get("asset_tray", {})
        key = lambda item: item["media_type"] + ":" + item["artifact_id"]
        hidden, recent = set(tray.get("hidden", [])), tray.get("recent", [])
        assets = [item for item in assets if key(item) not in hidden]
        assets.sort(key=lambda item: (item["created_at"], item["artifact_id"]), reverse=True)
        assets.sort(key=lambda item: recent.index(key(item)) if key(item) in recent else len(recent))
        return {"assets": assets[:60], "total": len(assets)}

    def change_tray(self, aid, wid, pid, *, artifact_id, media_type, action, shot_id=None):
        _, assets = self._tray_candidates(aid, wid, pid)
        asset = next((item for item in assets if item["artifact_id"] == artifact_id and item["media_type"] == media_type), None)
        if not asset or (shot_id and asset.get("shot_id") != shot_id):
            raise FileNotFoundError("素材不属于当前项目，或已经不存在")
        if action not in {"add", "remove"}:
            raise ValueError("素材操作无效")
        if action == "add" and media_type == "image":
            self.videos.link_artifacts(aid, wid, pid, [asset])
        self.videos.update_tray(aid, wid, pid, media_type + ":" + artifact_id, action)
        return self.tray(aid, wid, pid)

    def import_image(self, aid, wid, pid, artifact_id):
        _, state, link = self.binding(aid, wid, pid)
        library = AssetLibrary(state)
        artifact = library.require(link["subproject_id"], artifact_id)
        path = state.storage.get_artifact_path(artifact.artifact_id)
        if path.stat().st_size > self.videos.max_image_bytes:
            raise ValueError("图片产物超过大小限制")
        data = path.read_bytes()
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError("图片产物校验失败")
        return {"assets": self.videos.link_artifacts(aid, wid, pid, [library.view(artifact)])}

    def import_script(self, aid, wid, pid, request_id, text, *, filename, generation_type):
        _, _, link = self.binding(aid, wid, pid)
        result = self.scripts().import_text(aid, wid, link["subproject_id"], request_id, text,
                                           filename=filename, generation_type=generation_type)
        self.use_script(aid, wid, pid, result["task_id"])
        return result
