"""H3 视频分区。独立媒体存储，沿用账号空间和事务积分账本。"""
import hashlib
import io
import os
from pathlib import Path
import re
import time
from uuid import uuid4, uuid5, NAMESPACE_URL

from PIL import Image

from .accounts import IdentityStore
from .locking import FileLock
from .storage import _now, _validate_id
from .domain_errors import ConflictError
from .task_recovery import recovery_projection


RATIOS = {"landscape": (1280, 720), "portrait": (720, 1280), "square": (720, 720)}
RESOLUTIONS = {
    "720p": RATIOS,
    "1080p": {"landscape": (1920, 1080), "portrait": (1080, 1920), "square": (1080, 1080)},
}
DEFAULT_RATES = {"720p": 40, "1080p": 80}
REBUILD_REASON = "旧版低分辨率分镜不能按高清价格运行，请新建高清分镜"
DISABLED_RESOLUTION_REASON = "1080p暂不可用，请新建720p分镜"
from .media_validation import MAX_OUTPUT_BYTES
FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
ACTIVE = {"queued", "running", "uncertain"}


def _string(value, label, limit, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"{label}需为{'0' if empty else '1'}–{limit}个字符")
    return value


from .media_validation import inspect_mp4


class VideoStore:
    def __init__(self, storage, identity=None):
        self.storage = storage
        self.identity = identity or IdentityStore(storage)
        self.files = storage.files.scope(".video")
        self.root = self.files.root
        self.root.mkdir(exist_ok=True)
        for name in ("projects", "jobs", "outputs", "workers"):
            self._path(name).mkdir(exist_ok=True)

    def _path(self, *parts):
        return self.files.path(*parts)

    def lock(self):
        return FileLock(self._path("control.lock"), timeout=30)

    def _read(self, path):
        return self.files.read_json(self.files.check(path))

    def _optional(self, path):
        try:
            return self._read(path)
        except FileNotFoundError:
            return None

    def _write(self, path, value):
        self.files.write_json(self.files.check(path), {"schema_version": 1, **value})

    def _bytes(self, path, data):
        path = self.files.check(path)
        temporary = self.files.check(path.parent / (".tmp-" + uuid4().hex))
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            self.files.cleanup(temporary)

    def config(self):
        saved = self._optional(self._path("config.json")) or {}
        rates = saved.get("rates", DEFAULT_RATES)
        self._validate_rates(rates)
        legacy_rate = saved.get("legacy_credits_per_second", saved.get("credits_per_second"))
        if legacy_rate is not None and (type(legacy_rate) is not int or not 1 <= legacy_rate <= 10000):
            raise ValueError("视频积分配置无效")
        online = False
        for path in self._path("workers").glob("*.json"):
            info = self._read(path)
            online |= 0 <= time.time() - info.get("time", 0) <= 120
        return {"rates": dict(rates), "configured": True, "legacy_credits_per_second": legacy_rate,
                "enabled_resolutions": self._enabled_resolutions(),
                "worker_online": online, "modes": ["T2V", "I2V", "R2V"],
                "durations": {"min": 4, "max": 15},
                "video_models": [{"model_id": "h3", "display_name": "H3",
                                  "supported_durations_ms": list(range(4000, 15001, 1000)),
                                  "min_duration_ms": 4000, "max_duration_ms": 15000, "duration_step_ms": 1000,
                                  "supported_durations": list(range(4, 16)), "min_duration": 4,
                                  "max_duration": 15, "duration_step": 1}],
                "ratios": {key: {"width": value[0], "height": value[1]} for key, value in RATIOS.items()},
                "resolutions": {resolution: {ratio: {"width": size[0], "height": size[1]} for ratio, size in ratios.items()}
                                for resolution, ratios in RESOLUTIONS.items()},
                "max_reference_images": 9, "max_output_bytes": MAX_OUTPUT_BYTES,
                "max_asset_bytes": self.storage.config.max_image_bytes}

    @staticmethod
    def _enabled_resolutions():
        return ["720p", "1080p"] if os.environ.get("IMAGE_WORKBENCH_ENABLE_1080P") == "1" else ["720p"]

    @staticmethod
    def _validate_rates(rates):
        if (not isinstance(rates, dict) or set(rates) != set(RESOLUTIONS)
                or any(type(value) is not int or not 1 <= value <= 10000 for value in rates.values())):
            raise ValueError("请分别设置720p和1080p每秒积分，均需为1–10000的整数")

    def set_config(self, account_id, rates):
        with self.identity.connect() as db:
            row = db.execute("SELECT is_admin,status FROM accounts WHERE account_id=?", (_validate_id(account_id),)).fetchone()
        if not row or not row["is_admin"] or row["status"] != "active":
            raise PermissionError("只有管理员可以设置视频单价")
        self._validate_rates(rates)
        with self.lock():
            # The old scalar belonged to the former low-resolution workflow.
            # Preserve it for history; it never becomes an HD tier implicitly.
            previous = self._optional(self._path("config.json")) or {}
            legacy_rate = previous.get("legacy_credits_per_second", previous.get("credits_per_second"))
            self._write(self._path("config.json"), {"rates": dict(rates), "legacy_credits_per_second": legacy_rate, "updated_at": _now()})
        return self.config()

    def _project_path(self, pid):
        return self._path("projects", _validate_id(pid), "project.json")

    def _project(self, account_id, workspace_id, pid):
        self.identity.require_workspace(account_id, workspace_id)
        value = self._read(self._project_path(pid))
        if (value.get("account_id"), value.get("workspace_id"), value.get("project_id")) != (account_id, workspace_id, pid):
            raise FileNotFoundError("视频项目不存在")
        return value

    def create_project(self, account_id, workspace_id, request_id, name, script=""):
        self.identity.require_workspace(account_id, workspace_id)
        _validate_id(request_id)
        _string(name, "项目名称", 120)
        _string(script, "脚本", 100000, empty=True)
        with self.lock():
            path = self._project_path(request_id)
            old = self._optional(path)
            if old:
                old = self._project(account_id, workspace_id, request_id)
                if (old["name"], old["script"]) != (name, script):
                    raise ConflictError("项目请求标识已用于不同内容")
            else:
                directory = path.parent
                directory.mkdir(exist_ok=True)
                for sub in ("assets", "shots"):
                    self._path("projects", request_id, sub).mkdir(exist_ok=True)
                value = {"project_id": request_id, "account_id": account_id, "workspace_id": workspace_id,
                         "name": name, "script": script, "created_at": _now()}
                self._write(path, value)
        return self.project(account_id, workspace_id, request_id)

    def pipeline_project(self, account_id, workspace_id, project_id):
        """Read a pipeline binding after enforcing account/workspace ownership."""
        with self.lock():
            return self._project(account_id, workspace_id, project_id)

    @property
    def max_image_bytes(self):
        """Public import limit for cross-feature image transfers."""
        return self.storage.config.max_image_bytes

    def ensure_pipeline_binding(self, account_id, workspace_id, project_id, create_link):
        """Create the link once under the same lock as other video mutations."""
        with self.lock():
            project = self._project(account_id, workspace_id, project_id)
            if not project.get("pipeline"):
                project["pipeline"] = create_link(project)
                self._write(self._project_path(project_id), project)
            return project

    def set_pipeline_script(self, account_id, workspace_id, project_id, *, text, task_id, version_id):
        with self.lock():
            project = self._project(account_id, workspace_id, project_id)
            project.update(script=text, script_task_id=task_id, script_version_id=version_id, updated_at=_now())
            self._write(self._project_path(project_id), project)
        return self.project(account_id, workspace_id, project_id)

    def projects(self, account_id, workspace_id):
        self.identity.require_workspace(account_id, workspace_id)
        result = []
        for directory in self._path("projects").iterdir():
            self.files.check(directory)
            if not directory.is_dir():
                continue
            value = self._optional(directory / "project.json")
            if value and (value.get("account_id"), value.get("workspace_id")) == (account_id, workspace_id):
                result.append({**{key: value[key] for key in ("project_id", "name", "script", "created_at")},
                               "pipeline": dict(value.get("pipeline") or {})})
        return sorted(result, key=lambda p: (p["created_at"], p["project_id"]), reverse=True)

    def _asset(self, pid, aid):
        value = self._read(self._path("projects", _validate_id(pid), "assets", _validate_id(aid) + ".json"))
        if value.get("asset_id") != aid or value.get("project_id") != pid or value.get("format") not in FORMATS:
            raise ValueError("参考资产记录无效")
        return value

    def _asset_path(self, pid, aid):
        asset = self._asset(pid, aid)
        if asset.get("artifact_id"):
            artifact = self.storage.get_artifact(_validate_id(asset["artifact_id"]))
            project = self._read(self._project_path(pid))
            from .project_manager import ProjectManager
            placement = ProjectManager(self.storage).get_task_placement(artifact.task_id)
            if placement.subproject_id != project.get("pipeline", {}).get("subproject_id"):
                raise ValueError("素材已不属于当前子项目，请重新选择")
            owner = self.identity.ownership(ProjectManager(self.storage).get_subproject(placement.subproject_id).main_project_id)
            if owner["deleted"] or (owner["account_id"], owner["workspace_id"]) != (project["account_id"], project["workspace_id"]):
                raise FileNotFoundError("素材不存在于当前工作空间")
            return self.storage.get_artifact_path(artifact.artifact_id)
        return self._path("projects", pid, "assets", aid + FORMATS[asset["format"]])

    def link_artifacts(self, account_id, workspace_id, pid, artifacts):
        """Video assets are aliases to existing image artifacts, never media copies."""
        from .project_manager import ProjectManager
        projects = ProjectManager(self.storage)
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            sub_id = project.get("pipeline", {}).get("subproject_id")
            result = []
            for item in artifacts:
                aid = _validate_id(item["artifact_id"])
                artifact = self.storage.get_artifact(aid)
                if artifact.format not in FORMATS or projects.get_task_placement(artifact.task_id).subproject_id != sub_id:
                    raise ValueError("请选择当前子项目的图片素材")
                owner = self.identity.ownership(projects.get_subproject(sub_id).main_project_id)
                if owner["deleted"] or (owner["account_id"], owner["workspace_id"]) != (account_id, workspace_id):
                    raise FileNotFoundError("素材不存在于当前工作空间")
                asset = {"asset_id": aid, "artifact_id": aid, "project_id": pid,
                         "filename": item.get("filename") or artifact.original_filename,
                         **{key: getattr(artifact, key) for key in ("format", "width", "height", "size_bytes", "sha256", "created_at")}}
                self._write(self._path("projects", pid, "assets", aid + ".json"), asset)
                result.append(self._asset_view(asset, workspace_id))
            return result

    def bind_subproject(self, account_id, workspace_id, pid, link):
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            if project.get("pipeline") and project["pipeline"] != link:
                raise ConflictError("视频项目已有其他子项目关联")
            if link.get("subproject_id"):
                for directory in self._path("projects").iterdir():
                    if directory.name == pid:
                        continue
                    other = self._optional(directory / "project.json")
                    if other and (other.get("pipeline") or {}).get("subproject_id") == link["subproject_id"]:
                        raise ConflictError("该子项目已有视频项目")
            project["pipeline"] = link
            self._write(self._project_path(pid), project)

    def update_tray(self, account_id, workspace_id, pid, key, action):
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            tray = project.setdefault("asset_tray", {"hidden": [], "recent": []})
            tray["hidden"] = [value for value in tray["hidden"] if value != key]
            tray["recent"] = [value for value in tray["recent"] if value != key]
            if action == "remove":
                tray["hidden"].append(key)
            else:
                tray["recent"].insert(0, key)
            self._write(self._project_path(pid), project)

    def bind_clip(self, account_id, workspace_id, pid, sid, source_shot_id, artifact_id):
        for value in (sid, source_shot_id, artifact_id):
            _validate_id(value)
        if sid == source_shot_id:
            raise ValueError("请选择另一个待制作镜头")
        self.output_file(account_id, workspace_id, pid, source_shot_id)
        with self.lock():
            self._project(account_id, workspace_id, pid)
            source, target = self._shot(pid, source_shot_id), self._shot(pid, sid)
            jobs = self._jobs()
            source_job = self._current(pid, source_shot_id, jobs)
            actual_id = source.get("output", {}).get("artifact_id") or (source_job or {}).get("output_artifact_id") or source.get("output_artifacts", {}).get((source_job or {}).get("job_id"))
            if artifact_id != actual_id:
                raise ValueError("视频版本已变化，请刷新素材暂存区")
            clip = {"artifact_id": artifact_id, "source_shot_id": source_shot_id,
                    "url": f"/api/video/projects/{pid}/shots/{source_shot_id}/output?workspace_id={workspace_id}"}
            if target.get("source") == "upload" or self._current(pid, sid, jobs):
                raise ConflictError("只能绑定尚未生成的镜头，已有视频不会覆盖")
            if target.get("bound_clip") and target["bound_clip"] != clip:
                raise ConflictError("镜头已经绑定其他视频，请选择空镜头")
            target["bound_clip"] = clip
            self._write(self._path("projects", pid, "shots", sid + ".json"), target)
            return self._shot_view(target, workspace_id, jobs)

    def _asset_view(self, asset, wid):
        return {**asset, "url": f"/api/video/projects/{asset['project_id']}/assets/{asset['asset_id']}?workspace_id={wid}"}

    def assets(self, account_id, workspace_id, pid):
        self._project(account_id, workspace_id, pid)
        return [self._asset_view(self._asset(pid, path.stem), workspace_id)
                for path in self._path("projects", pid, "assets").glob("*.json")]

    def add_assets(self, account_id, workspace_id, pid, files):
        if not 1 <= len(files) <= 9:
            raise ValueError("每次请上传1–9张参考图")
        if sum(len(data) for _, data in files) > MAX_OUTPUT_BYTES:
            raise ValueError("本次参考图总大小超过40 MiB")
        prepared = []
        for filename, data in files:
            if not isinstance(data, bytes) or not 0 < len(data) <= self.storage.config.max_image_bytes:
                raise ValueError("参考图片为空或超过单张大小上限")
            _string(filename, "文件名", 255)
            try:
                with Image.open(io.BytesIO(data)) as picture:
                    fmt, width, height = picture.format, picture.width, picture.height
                    if fmt not in FORMATS or width * height > self.storage.config.max_image_pixels:
                        raise ValueError("参考图格式或像素超限")
                    total = 0
                    for frame in range(getattr(picture, "n_frames", 1)):
                        picture.seek(frame)
                        total += picture.width * picture.height
                        if total > self.storage.config.max_image_pixels:
                            raise ValueError("参考图累计像素超限")
                        picture.load()
            except (OSError, Image.DecompressionBombError) as exc:
                raise ValueError("参考图无法解码") from exc
            prepared.append((Path(filename).name, data, fmt, width, height, hashlib.sha256(data).hexdigest()))
        with self.lock():
            existing = self.assets(account_id, workspace_id, pid)
            by_hash = {a["sha256"]: a for a in existing}
            additions = {item[5] for item in prepared} - set(by_hash)
            if len(existing) + len(additions) > 9:
                raise ValueError("每个视频项目最多保留9张参考图")
            result = []
            for filename, data, fmt, width, height, digest in prepared:
                if digest not in by_hash:
                    aid = uuid4().hex
                    asset = {"asset_id": aid, "project_id": pid, "filename": filename, "format": fmt,
                             "width": width, "height": height, "size_bytes": len(data), "sha256": digest, "created_at": _now()}
                    self._bytes(self._path("projects", pid, "assets", aid + FORMATS[fmt]), data)
                    self._write(self._path("projects", pid, "assets", aid + ".json"), asset)
                    by_hash[digest] = self._asset_view(asset, workspace_id)
                result.append(by_hash[digest])
            return result

    def asset_file(self, account_id, workspace_id, pid, aid):
        self._project(account_id, workspace_id, pid)
        asset = self._asset(pid, aid)
        path = self._asset_path(pid, aid)
        self._verify_file(path, asset)
        return path, {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[asset["format"]]

    def associate_artifact(self, account_id, workspace_id, pid, asset_id, artifact_id):
        """Keep the original image identity when importing an existing Artifact."""
        from .workspace_scope import WorkspaceProjects
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
            artifact = self.storage.get_artifact(_validate_id(artifact_id))
            placement = projects.get_task_placement(artifact.task_id)
            sub = projects.get_subproject(placement.subproject_id)
            if sub.main_project_id != project.get("pipeline", {}).get("main_project_id") or artifact.format not in FORMATS:
                raise ValueError("图片 Artifact 不属于当前视频项目")
            asset = self._asset(pid, asset_id)
            if (asset["sha256"], asset["size_bytes"]) != (artifact.sha256, artifact.size_bytes):
                raise ValueError("参考图与原 Artifact 内容不匹配")
            self._verify_file(self.storage.get_artifact_path(artifact_id),
                              {"sha256": artifact.sha256, "size_bytes": artifact.size_bytes})
            asset["artifact_id"] = artifact_id
            self._write(self._path("projects", pid, "assets", asset_id + ".json"), asset)
            return self._asset_view(asset, workspace_id)

    def _validate_storyboard_inputs(self, account_id, workspace_id, pid, shot):
        if not shot.get("storyboard_id"):
            return
        from .workspace_scope import WorkspaceProjects
        projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
        projects.get_subproject(shot["subproject_id"])
        references = list(shot.get("reference_artifact_ids", []))
        references.extend(shot[k] for k in ("start_frame_artifact_id", "end_frame_artifact_id") if shot.get(k))
        for reference in set(references):
            try:
                artifact = self.storage.get_artifact(reference)
                if artifact.project_id != shot["storage_project_id"] or artifact.format not in FORMATS:
                    raise ValueError("引用图片不属于当前分镜项目")
                self._verify_file(self.storage.get_artifact_path(reference),
                                  {"sha256": artifact.sha256, "size_bytes": artifact.size_bytes})
            except FileNotFoundError as exc:
                raise ValueError("引用图片 Artifact 缺失：" + reference) from exc
        for asset_id in shot["reference_ids"]:
            self.asset_file(account_id, workspace_id, pid, asset_id)

    def _shot(self, pid, sid):
        value = self._read(self._path("projects", _validate_id(pid), "shots", _validate_id(sid) + ".json"))
        if (value.get("project_id"), value.get("shot_id")) != (pid, sid):
            raise ValueError("分镜记录不匹配")
        return value

    def create_shot(self, account_id, workspace_id, pid, *, request_id, name, prompt, mode, duration, aspect_ratio, reference_ids,
                    resolution="720p", storyboard_binding=None):
        _validate_id(request_id)
        _string(name, "分镜名称", 120)
        _string(prompt, "分镜提示词", 12000)
        if mode not in {"T2V", "I2V", "R2V"} or aspect_ratio not in RATIOS:
            raise ValueError("请选择有效的视频模式和画幅")
        if not isinstance(resolution, str) or resolution not in RESOLUTIONS:
            raise ValueError("视频分辨率只支持720p或1080p")
        if resolution not in self._enabled_resolutions():
            raise ConflictError(DISABLED_RESOLUTION_REASON)
        if type(duration) is not int or not 4 <= duration <= 15:
            raise ValueError("视频时长需为4–15秒的整数")
        if not isinstance(reference_ids, list) or len(reference_ids) > 9 or len(set(reference_ids)) != len(reference_ids):
            raise ValueError("参考图列表无效")
        for aid in reference_ids:
            _validate_id(aid)
        if (mode == "T2V" and reference_ids) or (mode == "I2V" and not 1 <= len(reference_ids) <= 2) or (mode == "R2V" and not reference_ids):
            raise ValueError("T2V不使用参考图；I2V需1–2张首尾帧；R2V需1–9张参考图")
        width, height = RESOLUTIONS[resolution][aspect_ratio]
        payload = {"project_id": pid, "shot_id": request_id, "name": name, "prompt": prompt, "mode": mode,
                   "duration": duration, "aspect_ratio": aspect_ratio, "reference_ids": reference_ids,
                   "resolution": resolution, "width": width, "height": height}
        if storyboard_binding is not None:
            # Only StoryboardService calls this bridge; the general HTTP Shot body rejects these fields.
            for field in ("storyboard_id", "storyboard_shot_id", "storyboard_artifact_id", "storage_project_id", "subproject_id"):
                _validate_id(storyboard_binding.get(field))
            payload.update(storyboard_binding)
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            link = project.get("pipeline") or {}
            if storyboard_binding is None and link.get("subproject_id"):
                # Manual shots inherit the selected project; this is the only place
                # that stamps ownership, so old shots stay readable unchanged.
                payload["main_project_id"] = link.get("main_project_id")
                payload["subproject_id"] = link["subproject_id"]
            elif storyboard_binding is not None and link.get("subproject_id") == payload.get("subproject_id"):
                payload.setdefault("main_project_id", link.get("main_project_id"))
            for directory in self._path("projects").iterdir():
                if directory.name != pid and self.files.check(directory / "shots" / (request_id + ".json")).exists():
                    raise ConflictError("分镜标识已使用，请创建新的请求标识")
            for aid in reference_ids:
                self._asset(pid, aid)
            path = self._path("projects", pid, "shots", request_id + ".json")
            old = self._optional(path)
            if old:
                if not old.get("subproject_id") and payload.get("subproject_id"):
                    # Adopt an existing shot into the newly selected subproject.
                    old = {**old, "main_project_id": payload.get("main_project_id"),
                           "subproject_id": payload["subproject_id"]}
                    self._write(path, old)
                if any(old.get(key) != value for key, value in payload.items()):
                    raise ConflictError("分镜请求标识已用于不同内容")
            else:
                self._write(path, {**payload, "created_at": _now()})
        return self.shot(account_id, workspace_id, pid, request_id)

    def _job_path(self, jid):
        return self._path("jobs", _validate_id(jid) + ".json")

    def _save_job(self, job):
        settled = self.identity.settled_job(job["job_id"])
        if settled:
            job.clear()
            job.update(settled)
        else:
            job["updated_at"] = _now()
        self._write(self._job_path(job["job_id"]), job)
        self.identity.record_job(job)
        self._write(self._job_path(job["job_id"]), job)

    def _jobs(self):
        # Frozen outbox and terminal JSON both recover without re-executing work.
        for pending in self.identity.pending_jobs():
            if pending.get("kind") != "video":
                continue
            existing = self._optional(self._job_path(pending["job_id"]))
            if not existing:
                self._save_job(pending)
            elif existing["status"] in {"completed", "failed"}:
                self._save_job(existing)
        jobs = [self._read(path) for path in self._path("jobs").glob("*.json")]
        return sorted(jobs, key=lambda j: (j["created_at"], j["job_id"]))

    def _current(self, pid, sid, jobs=None):
        matches = [j for j in (self._jobs() if jobs is None else jobs) if j["project_id"] == pid and j["task_id"] == sid]
        return matches[-1] if matches else None

    @staticmethod
    def public_job(job):
        if not job:
            return None
        keys = ("job_id", "project_id", "task_id", "status", "credit_status", "credit_cost", "credits_per_second",
                "resolution", "duration", "created_at", "started_at", "finished_at", "updated_at", "error",
                "storyboard_id", "storyboard_version", "storyboard_shot_id", "main_project_id", "subproject_id",
                "output_task_id", "output_artifact_id", "video_version")
        return {**{key: job[key] for key in keys if key in job}, "recovery": VideoStore._recovery(job, job["task_id"])}

    @staticmethod
    def _recovery(job, task_id):
        job = job or {}
        status, actions = job.get("status", "draft"), []
        if status == "uncertain":
            actions.append("finalize_output" if job.get("output") else
                           "reconcile_backend" if job.get("attempt_id") else "inspect_original")
        elif status == "failed":
            actions.append("retry")
        return recovery_projection(
            "video", task_id, status, job_id=job.get("job_id"), credit_status=job.get("credit_status"),
            stage="archive" if job.get("output") and status in {"running", "uncertain"} else None,
            failure_message=job.get("error"),
            evidence={"has_backend_reference": bool(job.get("prompt_id")), "has_output": bool(job.get("output"))},
            actions=actions)

    def recovery_view(self, account_id, workspace_id, pid, sid):
        """Read JSON/outbox snapshots without running _jobs' settlement recovery."""
        self._project(account_id, workspace_id, pid)
        shot = self._shot(pid, sid)
        if shot.get("source") == "upload":
            return recovery_projection("video", sid, "completed", evidence={"has_output": True})
        jobs = {j["job_id"]: j for j in self.identity.pending_jobs() if j.get("kind") == "video"}
        for path in self._path("jobs").glob("*.json"):
            if not path.name.startswith(".tmp-"):
                job = self._read(path)
                jobs[job["job_id"]] = job
        ordered = sorted(jobs.values(), key=lambda j: (j["created_at"], j["job_id"]))
        return self._recovery(self._current(pid, sid, ordered), sid)

    @staticmethod
    def _is_hd_shot(shot):
        resolution = shot.get("resolution")
        ratio = shot.get("aspect_ratio")
        return (isinstance(resolution, str) and resolution in RESOLUTIONS
                and isinstance(ratio, str) and ratio in RESOLUTIONS[resolution]
                and (shot.get("width"), shot.get("height")) == RESOLUTIONS[resolution][ratio])

    def _shot_view(self, shot, wid, jobs):
        if shot.get("source") == "upload":
            base = f"/api/video/projects/{shot['project_id']}/shots/{shot['shot_id']}/output?workspace_id={wid}"
            return {**shot, "status": "completed", "current_job": None, "estimated_credit": 0,
                    "requires_rebuild": False, "run_blocked_reason": "已上传的视频无需生成",
                    "output_url": base, "download_url": base + "&download=1", "artifact_id": shot.get("output", {}).get("artifact_id")}
        job = self._current(shot["project_id"], shot["shot_id"], jobs)
        supported = self._is_hd_shot(shot)
        config = self.config()
        rate = config["rates"][shot["resolution"]] if supported else None
        blocked_reason = REBUILD_REASON if not supported else (
            DISABLED_RESOLUTION_REASON if shot["resolution"] not in config["enabled_resolutions"] else None)
        base = f"/api/video/projects/{shot['project_id']}/shots/{shot['shot_id']}/output?workspace_id={wid}"
        completed = bool(job and job["status"] == "completed")
        return {**shot, "status": job["status"] if job else "draft", "current_job": self.public_job(job),
                "estimated_credit": shot["duration"] * rate if rate is not None else None,
                "requires_rebuild": not supported, "run_blocked_reason": blocked_reason,
                "output_url": base if completed else None, "download_url": base + "&download=1" if completed else None,
                "artifact_id": (job.get("output_artifact_id") or shot.get("output_artifacts", {}).get(job["job_id"])) if job else None}

    def shot(self, account_id, workspace_id, pid, sid):
        self._project(account_id, workspace_id, pid)
        with self.lock():
            return self._shot_view(self._shot(pid, sid), workspace_id, self._jobs())

    def project(self, account_id, workspace_id, pid):
        value = self._project(account_id, workspace_id, pid)
        link = dict(value.get("pipeline") or {})
        with self.lock():
            jobs = self._jobs()
            shots = [self._shot_view(self._shot(pid, p.stem), workspace_id, jobs)
                     for p in self._path("projects", pid, "shots").glob("*.json")]
        return {**{k: value[k] for k in ("project_id", "name", "script", "created_at")},
                "main_project_id": link.get("main_project_id"), "subproject_id": link.get("subproject_id"),
                "pipeline": link, "assets": self.assets(account_id, workspace_id, pid),
                "shots": sorted(shots, key=lambda s: (s["created_at"], s["shot_id"]))}

    def submit(self, account_id, workspace_id, pid, sid, *, request_id, confirm_generation, expected_credit, retry_of=None):
        _validate_id(request_id)
        if confirm_generation is not True:
            raise ValueError("请点击生成按钮提交视频")
        if type(expected_credit) is not int or expected_credit < 1:
            raise ValueError("请确认本次视频积分报价")
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            shot = self._shot(pid, sid)
            if shot.get("source") == "upload":
                raise ConflictError("已上传的视频无需生成，请创建新分镜")
            previous = self.identity.request_job(account_id, request_id, pid, sid)
            if previous:
                if previous.get("kind") != "video":
                    raise ConflictError("请求标识已用于其他任务类型")
                return self.public_job(previous)
            if not self._is_hd_shot(shot):
                raise ConflictError(REBUILD_REASON)
            job = self._current(pid, sid)
            if job and job["status"] in ACTIVE | {"completed"}:
                return self.public_job(job)
            if shot["resolution"] not in self._enabled_resolutions():
                raise ConflictError(DISABLED_RESOLUTION_REASON)
            if job and retry_of != job["job_id"]:
                raise ConflictError("已有失败记录，请确认后显式重试")
            if not job and retry_of is not None:
                raise ConflictError("重试任务不存在")
            self._validate_storyboard_inputs(account_id, workspace_id, pid, shot)
            rate = self.config()["rates"][shot["resolution"]]
            cost = shot["duration"] * rate
            if expected_credit != cost:
                raise ConflictError("视频单价已更新，请刷新报价后重新提交")
            job = {"schema_version": 1, "kind": "video", "backend": "h3", "job_id": uuid4().hex,
                   "project_id": pid, "task_id": sid, "request_id": request_id, "action": "generate", "confirmed": True,
                   "duration": shot["duration"], "resolution": shot["resolution"], "credits_per_second": rate,
                   "status": "queued", "created_at": _now(),
                   "started_at": None, "finished_at": None, "error": None, "attempt_id": None, "prompt_id": None}
            if shot.get("storyboard_id"):
                job.update({key: shot[key] for key in ("storyboard_id", "storyboard_version", "storyboard_shot_id",
                            "storyboard_artifact_id", "shot_fingerprint", "storage_project_id", "subproject_id")})
            link = project.get("pipeline") or {}
            subproject_id = shot.get("subproject_id") or link.get("subproject_id")
            if subproject_id:
                job["subproject_id"] = subproject_id
                job["main_project_id"] = shot.get("main_project_id") or (
                    link.get("main_project_id") if link.get("subproject_id") == subproject_id else None)
                if not job["main_project_id"]:
                    from .project_manager import ProjectManager
                    job["main_project_id"] = ProjectManager(self.storage).get_subproject(subproject_id).main_project_id
            job = self.identity.reserve(job, {"account_id": account_id, "workspace_id": workspace_id}, cost)
            self._save_job(job)
            return self.public_job(job)

    def _beat(self, worker_id):
        self._write(self._path("workers", _validate_id(worker_id) + ".json"), {"worker_id": worker_id, "time": time.time()})

    def _manifest(self, job):
        project = self._project(job["account_id"], job["workspace_id"], job["project_id"])
        task = self._shot(job["project_id"], job["task_id"])
        return {**{key: job.get(key) for key in ("job_id", "claim_id", "worker_id", "status", "attempt_id", "prompt_id")},
                "task": task, "project": {key: project[key] for key in ("project_id", "name", "script")},
                "assets": [self._asset(job["project_id"], aid) for aid in task["reference_ids"]], "max_output_bytes": MAX_OUTPUT_BYTES}

    def claim(self, worker_id):
        _validate_id(worker_id)
        with self.lock():
            self._beat(worker_id)
            jobs = self._jobs()
            active = [job for job in jobs if job["status"] in {"running", "uncertain"}]
            own = next((job for job in active if job.get("worker_id") == worker_id), None)
            if own:
                return self._manifest(own)
            if active:  # One H3/GPU execution at a time; uncertain work retains its slot.
                return None
            job = next((job for job in jobs if job["status"] == "queued"), None)
            if not job:
                return None
            self.identity.require_reservation(job["job_id"], job["project_id"], job["task_id"])
            job.update(status="running", worker_id=worker_id, claim_id=uuid4().hex, started_at=_now())
            self._save_job(job)
            return self._manifest(job)

    def _owned(self, job_id, worker_id, claim_id):
        _validate_id(worker_id)
        _validate_id(claim_id)
        job = self._read(self._job_path(job_id))
        if job.get("kind") != "video" or job.get("job_id") != job_id:
            raise ValueError("视频作业记录无效")
        if (job.get("worker_id"), job.get("claim_id")) != (worker_id, claim_id):
            raise ConflictError("视频作业不属于此执行器")
        settled = self.identity.settled_job(job_id)
        return settled or job

    def heartbeat(self, job_id, worker_id, claim_id):
        with self.lock():
            self._owned(job_id, worker_id, claim_id)
            self._beat(worker_id)
            return {"ok": True}

    def start(self, job_id, worker_id, claim_id, attempt_id, prompt_id=None):
        _validate_id(attempt_id)
        if prompt_id is not None:
            _string(prompt_id, "工作流任务标识", 200)
        with self.lock():
            job = self._owned(job_id, worker_id, claim_id)
            if job["status"] not in {"running", "uncertain"}:
                raise ConflictError("此视频任务已经结束")
            if job.get("attempt_id") and job["attempt_id"] != attempt_id:
                raise ConflictError("视频已经开始执行，不能重复提交工作流")
            if job["status"] == "uncertain" and not job.get("attempt_id"):
                raise ConflictError("待确认的视频不能自动重新执行")
            if job.get("prompt_id") and prompt_id and job["prompt_id"] != prompt_id:
                raise ConflictError("工作流任务标识不能改变")
            self.identity.require_reservation(job_id, job["project_id"], job["task_id"])
            job["attempt_id"] = attempt_id
            if prompt_id:
                job["prompt_id"] = prompt_id
                # 执行器已通过原回执或精确工作流对账确认同一次执行。
                # 恢复运行状态不建立新预约、不改变积分，也不允许换 attempt。
                job.update(status="running", error=None)
            self._save_job(job)
            self._beat(worker_id)
            return {"ok": True, "job": self._manifest(job)}

    @staticmethod
    def _verify_file(path, metadata):
        if not 0 < path.stat().st_size <= MAX_OUTPUT_BYTES or path.stat().st_size != metadata["size_bytes"]:
            raise ValueError("媒体文件校验不匹配")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        if digest.hexdigest() != metadata["sha256"]:
            raise ValueError("媒体文件校验不匹配")

    def worker_asset(self, job_id, worker_id, claim_id, asset_id):
        with self.lock():
            job = self._owned(job_id, worker_id, claim_id)
            shot = self._shot(job["project_id"], job["task_id"])
            if asset_id not in shot["reference_ids"]:
                raise FileNotFoundError("参考图不属于此视频任务")
            asset = self._asset(job["project_id"], asset_id)
            path = self._asset_path(job["project_id"], asset_id)
            self._verify_file(path, asset)
            return path.read_bytes()

    def upload_output(self, job_id, worker_id, claim_id, data, sha256):
        if not isinstance(data, bytes) or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256) or hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("视频SHA256校验不匹配")
        inspect_mp4(data)
        with self.lock():
            job = self._owned(job_id, worker_id, claim_id)
            metadata = {"sha256": sha256, "size_bytes": len(data)}
            if job.get("output"):
                if job["output"] != metadata:
                    raise ConflictError("同一视频任务已经上传了不同结果")
                self._verify_file(self._path("outputs", job_id + ".mp4"), metadata)
                return metadata
            if job["status"] not in {"running", "uncertain"} or not job.get("attempt_id"):
                raise ConflictError("此视频任务不能接收输出")
            path = self._path("outputs", job_id + ".mp4")
            if path.exists():
                # A crash may have written bytes before publishing their metadata.
                # Recover those exact bytes; never silently replace an old result.
                self._verify_file(path, metadata)
            else:
                self._bytes(path, data)
            job["output"] = metadata
            self._save_job(job)
            return metadata

    def complete(self, job_id, worker_id, claim_id, status, sha256=None, error=None):
        if status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("视频完成状态无效")
        if error is not None:
            _string(error, "错误说明", 2000, empty=True)
        with self.lock():
            job = self._owned(job_id, worker_id, claim_id)
            target = "completed" if status == "succeeded" else status
            if job["status"] in {"completed", "failed"}:
                if job["status"] != target or (target == "completed" and sha256 != job.get("output", {}).get("sha256")):
                    raise ConflictError("视频任务已经结算，不能改变结果")
                return self.public_job(job)
            if target == "completed":
                if not job.get("attempt_id") or not job.get("output") or sha256 != job["output"]["sha256"]:
                    raise ValueError("视频输出尚未上传或校验值不匹配")
                self._verify_file(self._path("outputs", job_id + ".mp4"), job["output"])
                if job.get("subproject_id"):
                    self._register_output(job)
            job.update(status=target, error=error, finished_at=_now() if target in {"completed", "failed"} else None)
            self._save_job(job)
            self._beat(worker_id)
            return self.public_job(job)

    def _register_output(self, job):
        """Archive through StorageManager before settling a successful video job."""
        from .workspace_scope import WorkspaceProjects
        projects = WorkspaceProjects(self.storage, self.identity, job["account_id"], job["workspace_id"])
        storyboard = bool(job.get("storyboard_id"))
        prefix = "storyboard-video" if storyboard else "video"
        tid = uuid5(NAMESPACE_URL, prefix + "-task:" + job["job_id"]).hex
        artifact_id = uuid5(NAMESPACE_URL, prefix + "-artifact:" + job["job_id"]).hex
        if storyboard:
            task = projects.create_task(job["subproject_id"], "video", "分镜视频结果",
                                        job["storyboard_artifact_id"], task_id=tid)
        else:
            task = projects.create_task(job["subproject_id"], "video", "视频结果", task_id=tid)
        source = self._path("outputs", job["job_id"] + ".mp4")
        artifact = self.storage.register_output_document(
            task.project_id, tid, source.read_bytes(), artifact_id=artifact_id,
            format="MP4", source_path=source)
        job.update(output_artifact_id=artifact.artifact_id, output_task_id=task.task_id,
                   video_version={"version": 1, "artifact_id": artifact.artifact_id,
                                  "storyboard_id": job.get("storyboard_id"),
                                  "storyboard_version": job.get("storyboard_version"),
                                  "shot_id": job.get("storyboard_shot_id") or job["task_id"],
                                  "created_at": artifact.created_at})

    def reference_output(self, account_id, workspace_id, pid, sid):
        """Return a real Artifact for an existing immutable clip, without copying it."""
        from .workspace_scope import WorkspaceProjects
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            link = project.get("pipeline")
            if not link:
                raise ConflictError("请先初始化项目，再引用视频素材")
            projects = WorkspaceProjects(self.storage, self.identity, account_id, workspace_id)
            sub = projects.get_subproject(link["subproject_id"])
            if sub.main_project_id != link["main_project_id"]:
                raise ValueError("视频项目关联无效")
            shot = self._shot(pid, sid)
            uploaded = shot.get("source") == "upload"
            job = None if uploaded else self._current(pid, sid)
            if not uploaded and (not job or job["status"] != "completed"):
                raise FileNotFoundError("视频尚未完成")
            version_key = sid if uploaded else job["job_id"]
            existing = (shot.get("output", {}).get("artifact_id") if uploaded else
                        job.get("output_artifact_id") or shot.get("output_artifacts", {}).get(version_key))
            if existing:
                artifact = self.storage.get_artifact(existing)
                if projects.get_task_placement(artifact.task_id).subproject_id != sub.subproject_id or artifact.format != "MP4":
                    raise ValueError("视频素材不属于当前子项目")
                self._verify_file(self.storage.get_artifact_path(existing), {"sha256":artifact.sha256,"size_bytes":artifact.size_bytes})
                return artifact.artifact_id
            source = self._path("projects", pid, "shots", sid + ".mp4") if uploaded else self._path("outputs", version_key + ".mp4")
            self._verify_file(source, shot["output"] if uploaded else job["output"])
            tid = uuid5(NAMESPACE_URL, "video-reference-task:" + pid + ":" + version_key).hex
            aid = uuid5(NAMESPACE_URL, "video-reference-artifact:" + pid + ":" + version_key).hex
            task = projects.create_task(sub.subproject_id, "video", "视频素材：" + shot["name"], task_id=tid)
            artifact = self.storage.register_output_document(task.project_id, tid, source.read_bytes(), artifact_id=aid, format="MP4", source_path=source)
            if uploaded:
                shot["output"]["artifact_id"] = artifact.artifact_id
            else:
                shot.setdefault("output_artifacts", {})[version_key] = artifact.artifact_id
            self._write(self._path("projects", pid, "shots", sid + ".json"), shot)
            return artifact.artifact_id

    def output_file(self, account_id, workspace_id, pid, sid):
        self._project(account_id, workspace_id, pid)
        shot = self._shot(pid, sid)
        if shot.get("source") == "upload":
            path = self._path("projects", pid, "shots", sid + ".mp4")
            self._verify_file(path, shot["output"])
            return path
        with self.lock():
            job = self._current(pid, sid)
            if not job or job["status"] != "completed" or not job.get("output"):
                raise FileNotFoundError("视频尚未完成")
            path = self._path("outputs", job["job_id"] + ".mp4")
            self._verify_file(path, job["output"])
            return path

    def upload_video(self, account_id, workspace_id, pid, request_id, name, data):
        _validate_id(request_id)
        _string(name, "视频名称", 120)
        inspect_mp4(data)
        digest = hashlib.sha256(data).hexdigest()
        with self.lock():
            project = self._project(account_id, workspace_id, pid)
            link = project.get("pipeline") or {}
            path = self._path("projects", pid, "shots", request_id + ".json")
            old = self._optional(path)
            if old:
                if old.get("source") != "upload" or old.get("name") != name or old.get("output", {}).get("sha256") != digest:
                    raise ConflictError("此上传标识已用于不同内容")
            else:
                self._bytes(self._path("projects", pid, "shots", request_id + ".mp4"), data)
                self._write(path, {"project_id": pid, "shot_id": request_id, "name": name, "prompt": "用户上传视频",
                    "mode": "upload", "source": "upload", "duration": None, "reference_ids": [], "created_at": _now(),
                    "main_project_id": link.get("main_project_id"), "subproject_id": link.get("subproject_id"),
                    "output": {"sha256": digest, "size_bytes": len(data)}})
        if link.get("subproject_id"):
            # Uploaded clips are real outputs too; archive them in the same
            # subproject so they are visible to both workbenches.
            self.reference_output(account_id, workspace_id, pid, request_id)
        return self.shot(account_id, workspace_id, pid, request_id)
