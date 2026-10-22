"""Subproject image inputs backed by the existing StorageManager artifacts."""
import hashlib
from pathlib import PureWindowsPath
from uuid import NAMESPACE_URL, uuid4, uuid5

from .storage import _now, _validate_id
from .domain_errors import ConflictError

IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_UPLOAD_IMAGES = 9


class AssetLibrary:
    def __init__(self, state):
        self.state, self.storage = state, state.storage

    def require(self, subproject_id, artifact_id):
        sub = self.state.projects.get_subproject(subproject_id)
        artifact = self.storage.get_artifact(_validate_id(artifact_id))
        placement = self.state.projects.get_task_placement(artifact.task_id)
        if placement.subproject_id != sub.subproject_id or artifact.format not in IMAGE_FORMATS:
            raise ValueError("请选择当前子项目的图片素材")
        return artifact

    def view(self, artifact):
        options = self.state.task_options(artifact.project_id, artifact.task_id) or {}
        filename = options.get("asset_names", {}).get(artifact.artifact_id, artifact.original_filename)
        return {**self.state.artifact_dto(artifact), "filename": filename,
                "media_type": "image"}

    def list_images(self, subproject_id):
        self.state.projects.get_subproject(subproject_id)
        images = []
        for placement in self.state.projects.catalog_view()["task_placements"].values():
            if placement["subproject_id"] != subproject_id:
                continue
            try:
                artifacts = self.storage.list_task_artifacts(placement["storage_project_id"], placement["task_id"])
            except FileNotFoundError:
                if placement["state"] == "pending":
                    continue
                raise
            for artifact in artifacts:
                if artifact.format in IMAGE_FORMATS:
                    images.append(artifact)
        return sorted(images, key=lambda item: (item.created_at, item.artifact_id), reverse=True)

    def upload(self, subproject_id, files):
        """Validate the entire batch before recording anything; deduplicate under lock."""
        sub = self.state.projects.get_subproject(subproject_id)
        if not 1 <= len(files) <= MAX_UPLOAD_IMAGES:
            raise ValueError("一次可添加 1–9 张图片")
        if sum(len(data) for _, data in files) > MAX_UPLOAD_BYTES:
            raise ValueError("本次图片合计超过 40 MiB，请分批上传")
        temporary = self.storage.files.path(".workbench", "uploads", uuid4().hex)
        temporary.mkdir()
        paths, prepared = [], []
        try:
            for filename, data in files:
                if not isinstance(data, bytes) or not 0 < len(data) <= self.storage.config.max_image_bytes:
                    raise ValueError("图片为空或超过单张大小上限，请压缩后重试")
                filename = PureWindowsPath(str(filename)).name[:255] or "clipboard.png"
                path = self.storage.files.check(temporary / (uuid4().hex + ".upload"))
                paths.append(path)
                path.write_bytes(data)
                # Checks actual format, full decoding, dimensions and cumulative pixels.
                self.storage.inspect_image(path)
                prepared.append((path, filename, hashlib.sha256(data).hexdigest()))
            with self.state.control_lock():
                # Recheck the selected scope after decoding and before writing.
                self.state.projects.get_subproject(subproject_id)
                existing = {item.sha256: item for item in self.list_images(subproject_id)}
                task_id = uuid5(NAMESPACE_URL, "image-workbench:asset-library:" + subproject_id).hex
                pid = self.state.projects.ensure_storage_namespace(sub.main_project_id)
                task = None
                result = []
                for path, filename, digest in prepared:
                    artifact = existing.get(digest)
                    if artifact is None:
                        if task is None:
                            try:
                                task = self.storage.get_task(pid, task_id)
                            except FileNotFoundError:
                                self.state.projects.reserve_task(subproject_id, task_id, "项目素材")
                                task = self.storage.create_task(pid, "generate", "项目图片素材（仅存储）", task_id=task_id)
                            placement = self.state.projects.get_task_placement(task_id)
                            previous = self.state.task_options(pid, task_id)
                            recovering = previous is None and placement.name == "项目素材" and task.prompt == "项目图片素材（仅存储）"
                            if placement.subproject_id != subproject_id or (not recovering and not (previous or {}).get("asset_library")):
                                raise ConflictError("项目素材库归属记录不一致，请修复任务归属后重试")
                            options = {**(self.state.task_options(pid, task_id) or {}), "schema_version": 1, "project_id": pid, "task_id": task_id,
                                       "asset_library": True, "state": "ready", "aspect_ratio": "auto",
                                       "created_at": task.created_at}
                            self.storage.files.write_json(self.state._task_file("tasks", pid, task_id, create=True), options)
                            self.state.projects.finish_task(task_id)
                        # StorageManager owns the only durable image copy.
                        artifact = self.storage.add_input_image(pid, task.task_id, path, role="library")
                        options.setdefault("asset_names", {})[artifact.artifact_id] = filename
                        self.storage.files.write_json(self.state._task_file("tasks", pid, task_id), options)
                        existing[digest] = artifact
                    view = self.view(artifact)
                    view["filename"] = filename
                    if not any(item["artifact_id"] == artifact.artifact_id for item in result):
                        result.append(view)
                return result
        finally:
            for path in paths:
                self.storage.files.cleanup(path)
            self.storage.files.cleanup(temporary, directory=True)
