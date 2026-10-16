"""单进程本地文件存储。所有受管理路径都由此模块构造和检查。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from threading import RLock
from uuid import uuid4
import warnings

from PIL import Image, UnidentifiedImageError

from .config import StorageConfig
from .models import Artifact, Project, Task


class MetadataError(ValueError):
    """JSON 内容或记录与目录不一致；不应静默跳过此类错误。"""


_ID = re.compile(r"[0-9a-f]{32}")
_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_CHUNK_BYTES = 1024 * 1024
_metadata_reads = ContextVar("storage_metadata_reads", default=None)


@contextmanager
def metadata_read_snapshot():
    """复用单次只读汇总中的不可变 Project/Task，不缓存权限或路径检查。

    同一记录在汇总中采用第一次成功读取的内容；上下文结束即丢弃，下一次
    请求重新读取。只由 GET 汇总入口启用，写入、队列和 Worker 不使用它。
    """
    records = {}
    token = _metadata_reads.set(records)
    try:
        yield
    finally:
        records.clear()
        _metadata_reads.reset(token)


def _validate_id(value: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError("ID 必须是 32 位小写十六进制字符串")
    return value


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空白字符串")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorageManager:
    def __init__(self, config: StorageConfig | None = None) -> None:
        self.config = config if config is not None else StorageConfig()
        self.data_root = self.config.data_root
        self._path().mkdir(parents=True, exist_ok=True)
        self._path("projects").mkdir(exist_ok=True)
        # 仅预留目录；模板记录、用户身份和访问权限由后续阶段实现。
        self._path("public").mkdir(exist_ok=True)
        self._path("public", "template_libraries").mkdir(exist_ok=True)
        self._path("users").mkdir(exist_ok=True)
        self._artifact_index_signature = None
        self._artifact_index_by_id = None
        self._artifact_index_lock = RLock()

    # 路径检查不信任磁盘 JSON，也拒绝 Windows junction 等重解析点。
    def _path(self, *parts: str) -> Path:
        root = self.data_root
        candidate = root.joinpath(*parts)
        if not candidate.is_relative_to(root):
            raise ValueError("受管理路径超出数据根目录")
        relative = candidate.relative_to(root)
        if ".." in relative.parts:
            raise ValueError("受管理路径包含路径穿越")
        current = root
        for part in (None, *relative.parts):
            if part is not None:
                current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError(f"受管理路径不允许符号链接或重解析点: {current}")
        if not candidate.resolve().is_relative_to(root):
            raise ValueError("受管理路径解析后超出数据根目录")
        return candidate

    def _project_dir(self, project_id: str) -> Path:
        return self._path("projects", _validate_id(project_id))

    def _task_dir(self, project_id: str, task_id: str) -> Path:
        return self._path("projects", _validate_id(project_id), "tasks", _validate_id(task_id))

    def _check_path(self, path: Path) -> Path:
        return self._path(*path.relative_to(self.data_root).parts)

    def _cleanup(self, path: Path, *, directory: bool = False) -> None:
        # 仅对本次创建的路径调用。清理失败不掩盖最初的异常；残留不进入清单。
        try:
            self._check_path(path)
            if directory:
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def _write_json(self, path: Path, payload: dict) -> None:
        self._check_path(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", prefix=".tmp-",
                suffix=".json", dir=path.parent, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._check_path(path)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                self._cleanup(temporary)

    def _read_json(self, path: Path) -> dict:
        self._check_path(path)
        try:
            with path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataError(f"元数据不是有效的 UTF-8 JSON: {path}") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
        ):
            raise MetadataError(f"不支持的元数据结构或版本: {path}")
        return payload

    @staticmethod
    def _record_payload(record: Project | Task) -> dict:
        return {"schema_version": 1, **asdict(record)}

    @staticmethod
    def _decode_record(record_type, payload: dict):
        try:
            if not isinstance(payload, dict):
                raise ValueError("记录必须为对象")
            record = record_type(**payload)
            timestamp = datetime.fromisoformat(_text(record.created_at, "created_at"))
            if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
                raise ValueError("时间必须带 UTC 时区")
            _validate_id(record.project_id)
            if isinstance(record, Project):
                _text(record.name, "name")
            elif isinstance(record, Task):
                _validate_id(record.task_id)
                _text(record.prompt, "prompt")
                if record.operation not in ("generate", "edit"):
                    raise ValueError("未知任务操作")
                if record.operation == "generate" and record.base_artifact_id is not None:
                    raise ValueError("generate 不接受基底图片")
                if record.operation == "edit":
                    _validate_id(record.base_artifact_id)
            else:
                _validate_id(record.task_id)
                _validate_id(record.artifact_id)
                _text(record.original_filename, "original_filename")
                if record.kind not in ("input", "output"):
                    raise ValueError("未知图片类型")
                if record.kind == "input":
                    _text(record.role, "role")
                    if record.source_artifact_id is not None:
                        raise ValueError("输入图片不能携带版本来源")
                elif record.role is not None:
                    raise ValueError("输出图片的 role 必须为 None")
                if record.format not in _FORMATS:
                    raise ValueError("未知图片格式")
                for field in ("width", "height", "size_bytes"):
                    if type(getattr(record, field)) is not int or getattr(record, field) <= 0:
                        raise ValueError(f"{field} 必须为正整数")
                if not isinstance(record.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", record.sha256) is None:
                    raise ValueError("无效 SHA-256")
                if record.source_artifact_id is not None:
                    _validate_id(record.source_artifact_id)
                    if record.source_artifact_id == record.artifact_id:
                        raise ValueError("图片不能引用自身")
            return record
        except (TypeError, ValueError, AttributeError) as exc:
            raise MetadataError(f"{record_type.__name__} 记录无效: {exc}") from exc

    def _child_ids(self, directory: Path) -> list[str]:
        self._check_path(directory)
        ids = []
        for child in directory.iterdir():
            # 隐藏临时目录未发布，.gitkeep 也不是记录。
            if child.name.startswith("."):
                continue
            self._check_path(child)
            if child.is_dir():
                try:
                    ids.append(_validate_id(child.name))
                except ValueError as exc:
                    raise MetadataError(f"无效的资源目录: {child}") from exc
        return ids

    def create_project(self, name: str, *, project_id: str | None = None) -> Project:
        _text(name, "name")
        project = Project(uuid4().hex if project_id is None else _validate_id(project_id), name, _now())
        final = self._project_dir(project.project_id)
        if final.exists():
            raise FileExistsError(final)
        prior_signature = self._prepare_cached_write()
        temporary = self._path("projects", ".tmp-" + uuid4().hex)
        temporary.mkdir()
        try:
            (temporary / "tasks").mkdir()
            self._write_json(temporary / "project.json", self._record_payload(project))
            self._check_path(final)
            temporary.rename(final)
            self._advance_cached_write(prior_signature, {
                "projects", f"project:{project.project_id}",
                f"project-json:{project.project_id}", f"tasks:{project.project_id}",
            })
        finally:
            self._cleanup(temporary, directory=True)
        return project

    def get_project(self, project_id: str) -> Project:
        _validate_id(project_id)
        records = _metadata_reads.get()
        key = (self, Project, project_id)
        if records is not None and key in records:
            self._path("projects", project_id, "project.json")
            return records[key]
        directory = self._project_dir(project_id)
        payload = self._read_json(directory / "project.json")
        project = self._decode_record(Project, {k: v for k, v in payload.items() if k != "schema_version"})
        if project.project_id != project_id:
            raise MetadataError("项目 ID 与目录不匹配")
        if records is not None:
            records[key] = project
        return project

    def list_projects(self) -> list[Project]:
        projects = [self.get_project(pid) for pid in self._child_ids(self._path("projects"))]
        return sorted(projects, key=lambda item: (item.created_at, item.project_id))

    def create_task(
        self, project_id: str, operation: str, prompt: str,
        base_artifact_id: str | None = None,
        *, task_id: str | None = None,
    ) -> Task:
        self.get_project(project_id)
        _text(prompt, "prompt")
        if operation not in ("generate", "edit"):
            raise ValueError("operation 必须为 generate 或 edit")
        if operation == "generate" and base_artifact_id is not None:
            raise ValueError("generate 任务不能指定 base_artifact_id")
        if operation == "edit":
            self._require_source(project_id, base_artifact_id)
        task = Task(uuid4().hex if task_id is None else _validate_id(task_id), project_id, operation, prompt, base_artifact_id, _now())
        final = self._task_dir(project_id, task.task_id)
        if final.exists():
            raise FileExistsError(final)
        prior_signature = self._prepare_cached_write()
        temporary = self._path("projects", project_id, "tasks", ".tmp-" + uuid4().hex)
        temporary.mkdir()
        try:
            for name in ("inputs", "outputs", "logs"):
                (temporary / name).mkdir()
            self._write_json(temporary / "task.json", self._record_payload(task))
            self._write_json(temporary / "artifacts.json", {"schema_version": 1, "artifacts": []})
            self._check_path(final)
            temporary.rename(final)
            self._advance_cached_write(prior_signature, {
                f"tasks:{project_id}", f"task:{project_id}:{task.task_id}",
                f"task.json:{project_id}:{task.task_id}", f"artifacts.json:{project_id}:{task.task_id}",
            })
        finally:
            self._cleanup(temporary, directory=True)
        return task

    def get_task(self, project_id: str, task_id: str) -> Task:
        _validate_id(project_id)
        _validate_id(task_id)
        records = _metadata_reads.get()
        key = (self, Task, project_id, task_id)
        if records is not None and key in records:
            self._path("projects", project_id, "tasks", task_id, "task.json")
            return records[key]
        directory = self._task_dir(project_id, task_id)
        self.get_project(project_id)
        payload = self._read_json(directory / "task.json")
        task = self._decode_record(Task, {k: v for k, v in payload.items() if k != "schema_version"})
        if task.project_id != project_id or task.task_id != task_id:
            raise MetadataError("任务 ID 与目录不匹配")
        if records is not None:
            records[key] = task
        return task

    def list_tasks(self, project_id: str) -> list[Task]:
        self.get_project(project_id)
        directory = self._path("projects", project_id, "tasks")
        tasks = [self.get_task(project_id, tid) for tid in self._child_ids(directory)]
        return sorted(tasks, key=lambda item: (item.created_at, item.task_id))

    def _artifact_file(self, artifact: Artifact, *, must_exist: bool = True) -> Path:
        folder = "inputs" if artifact.kind == "input" else "outputs"
        parts = (
            "projects", artifact.project_id, "tasks", artifact.task_id,
            folder, artifact.artifact_id + _FORMATS[artifact.format],
        )
        if artifact.relative_path != "/".join(parts):
            raise MetadataError("图片相对路径与所属任务、类型或 ID 不匹配")
        path = self._path(*parts)
        if must_exist:
            info = path.stat()  # 丢失文件保留 FileNotFoundError
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("图片路径必须指向普通文件")
        return path

    def list_task_artifacts(self, project_id: str, task_id: str) -> list[Artifact]:
        self.get_task(project_id, task_id)
        payload = self._read_json(self._task_dir(project_id, task_id) / "artifacts.json")
        if set(payload) != {"schema_version", "artifacts"} or not isinstance(payload["artifacts"], list):
            raise MetadataError("artifacts 清单结构无效")
        artifacts = []
        seen = set()
        for entry in payload["artifacts"]:
            artifact = self._decode_record(Artifact, entry)
            if artifact.project_id != project_id or artifact.task_id != task_id:
                raise MetadataError("图片的项目或任务 ID 与目录不匹配")
            if artifact.artifact_id in seen:
                raise MetadataError("同一任务存在重复的图片 ID")
            seen.add(artifact.artifact_id)
            self._artifact_file(artifact, must_exist=False)
            artifacts.append(artifact)
        return sorted(artifacts, key=lambda item: (item.created_at, item.artifact_id))

    @staticmethod
    def _stat_signature(path: Path) -> tuple:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError(f"受管理路径不允许符号链接或重解析点: {path}")
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
            getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
        )

    @staticmethod
    def _record_children(directory: Path) -> list[tuple[str, tuple]]:
        """List one managed directory level and stat each visible child once."""
        children = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or (
                    getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    raise ValueError(f"受管理路径不允许符号链接或重解析点: {entry.path}")
                signature = (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
                    getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
                )
                if stat.S_ISDIR(info.st_mode):
                    try:
                        _validate_id(entry.name)
                    except ValueError as exc:
                        raise MetadataError(f"无效的资源目录: {entry.path}") from exc
                    children.append((entry.name, signature))
        return sorted(children)

    def _artifact_tree_signature(self) -> tuple:
        """Cheaply detect record tree changes before using this instance's artifact index."""
        self._path()
        projects_dir = self._path("projects")
        signature = [("projects", self._stat_signature(projects_dir))]
        for project_id, project_stat in self._record_children(projects_dir):
            project_dir = projects_dir / project_id
            signature.append((f"project:{project_id}", project_stat))
            project_file = project_dir / "project.json"
            signature.append((f"project-json:{project_id}", self._stat_signature(project_file)))
            tasks_dir = project_dir / "tasks"
            signature.append((f"tasks:{project_id}", self._stat_signature(tasks_dir)))
            for task_id, task_stat in self._record_children(tasks_dir):
                task_dir = tasks_dir / task_id
                signature.append((f"task:{project_id}:{task_id}", task_stat))
                for filename in ("task.json", "artifacts.json"):
                    metadata = task_dir / filename
                    signature.append((f"{filename}:{project_id}:{task_id}", self._stat_signature(metadata)))
        return tuple(signature)

    def _get_artifact_index(self) -> dict[str, list[Artifact]]:
        # JSON remains authoritative. A per-instance index is reusable only while
        # every project/task/artifact manifest and record-directory stat is stable.
        with self._artifact_index_lock:
            signature = self._artifact_tree_signature()
            if signature == self._artifact_index_signature and self._artifact_index_by_id is not None:
                return self._artifact_index_by_id

            # A write from another StorageManager can land while the index is read.
            # Verify the cheap signature afterward and retry instead of caching a
            # mixed snapshot. Continuous concurrent writes fail closed.
            for _ in range(3):
                index: dict[str, list[Artifact]] = {}
                for project in self.list_projects():
                    for task in self.list_tasks(project.project_id):
                        for artifact in self.list_task_artifacts(project.project_id, task.task_id):
                            index.setdefault(artifact.artifact_id, []).append(artifact)
                after = self._artifact_tree_signature()
                if signature == after:
                    self._artifact_index_signature = after
                    self._artifact_index_by_id = index
                    return index
                signature = after
            raise MetadataError("存储元数据在构建图片索引期间持续变化，请重试")

    def _find_artifacts(self, artifact_id: str) -> list[Artifact]:
        return self._get_artifact_index().get(artifact_id, [])

    def _prepare_cached_write(self) -> tuple | None:
        if self._artifact_index_by_id is None:
            return None
        self._get_artifact_index()
        return self._artifact_index_signature

    def _advance_cached_write(
        self,
        prior_signature: tuple | None,
        expected_keys: set[str],
        artifact: Artifact | None = None,
    ) -> None:
        if prior_signature is None:
            return
        with self._artifact_index_lock:
            if self._artifact_index_signature != prior_signature or self._artifact_index_by_id is None:
                return
            try:
                updated_signature = self._artifact_tree_signature()
            except (OSError, ValueError, MetadataError):
                self._artifact_index_signature = None
                return
            prior = dict(prior_signature)
            updated = dict(updated_signature)
            changed = {key for key in prior.keys() & updated.keys() if prior[key] != updated[key]}
            added = updated.keys() - prior.keys()
            removed = prior.keys() - updated.keys()
            if removed or not (changed | added) <= expected_keys:
                # Another writer changed a path we did not create. Leave the old
                # cache unpublished so the next lookup rebuilds from JSON.
                self._artifact_index_signature = None
                return
            if artifact is not None:
                matches = self._artifact_index_by_id.get(artifact.artifact_id)
                if matches:
                    self._artifact_index_signature = None
                    return
                self._artifact_index_by_id[artifact.artifact_id] = [artifact]
            self._artifact_index_signature = updated_signature

    def get_artifact(self, artifact_id: str) -> Artifact:
        _validate_id(artifact_id)
        matches = self._find_artifacts(artifact_id)
        if not matches:
            raise FileNotFoundError(f"图片不存在: {artifact_id}")
        if len(matches) != 1:
            raise MetadataError(f"图片 ID 重复: {artifact_id}")
        # The index accelerates discovery, but each hit still checks that the
        # target's managed path has not become a symlink/junction or escaped root.
        self._artifact_file(matches[0], must_exist=False)
        return matches[0]

    def get_artifact_path(self, artifact_id: str) -> Path:
        return self._artifact_file(self.get_artifact(artifact_id))

    def _require_source(self, project_id: str, artifact_id: str) -> Artifact:
        artifact = self.get_artifact(artifact_id)
        if artifact.project_id != project_id:
            raise ValueError("来源图片必须属于同一项目")
        self._artifact_file(artifact)
        return artifact

    def add_input_image(
        self, project_id: str, task_id: str, source_path: str | Path,
        role: str = "reference",
    ) -> Artifact:
        self.get_task(project_id, task_id)
        _text(role, "role")
        return self._store_image(project_id, task_id, source_path, "input", role, None)

    def register_output_image(
        self, project_id: str, task_id: str, source_path: str | Path,
        source_artifact_id: str | None = None,
        *, artifact_id: str | None = None,
    ) -> Artifact:
        task = self.get_task(project_id, task_id)
        if task.operation == "edit":
            if source_artifact_id is None:
                source_artifact_id = task.base_artifact_id
            elif source_artifact_id != task.base_artifact_id:
                raise ValueError("编辑输出来源必须与任务的 base_artifact_id 一致")
        if source_artifact_id is not None:
            self._require_source(project_id, source_artifact_id)
        return self._store_image(
            project_id, task_id, source_path, "output", None, source_artifact_id,
            artifact_id=artifact_id,
        )

    def _copy_source(self, source: Path, target: Path) -> tuple[int, str]:
        # 检查已有大小，也在流式复制过程中限量，避免源文件变大绕过限制。
        info = source.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("源图片必须是普通文件")
        if info.st_size > self.config.max_image_bytes:
            raise ValueError("图片文件超过 max_image_bytes 限制")
        digest = hashlib.sha256()
        total = 0
        with source.open("rb") as reader, target.open("wb") as writer:
            while block := reader.read(min(_CHUNK_BYTES, self.config.max_image_bytes - total + 1)):
                total += len(block)
                if total > self.config.max_image_bytes:
                    raise ValueError("图片文件超过 max_image_bytes 限制")
                writer.write(block)
                digest.update(block)
            writer.flush()
            os.fsync(writer.fileno())
        return total, digest.hexdigest()

    def _inspect_image(self, path: Path) -> tuple[str, int, int]:
        # 不改动 Pillow 的全局安全阈值或截断图片配置。
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path, formats=["PNG", "JPEG", "WEBP"]) as picture:
                    image_format = picture.format
                    if image_format not in _FORMATS:
                        raise ValueError("仅支持真实内容为 PNG、JPEG、WEBP 的图片")
                    width, height = picture.size
                    if width * height > self.config.max_image_pixels:
                        raise ValueError("图片超过 max_image_pixels 限制")
                    picture.verify()
                with Image.open(path, formats=["PNG", "JPEG", "WEBP"]) as picture:
                    total_pixels = 0
                    for index in range(getattr(picture, "n_frames", 1)):
                        picture.seek(index)
                        total_pixels += picture.width * picture.height
                        if total_pixels > self.config.max_image_pixels:
                            raise ValueError("图片累计帧像素超过 max_image_pixels 限制")
                        picture.load()
            return image_format, width, height
        except (FileNotFoundError, PermissionError):
            raise
        except (UnidentifiedImageError, OSError, SyntaxError, EOFError,
                Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError(f"图片内容损坏、无法解码或像素数不安全: {path.name}") from exc

    def _store_image(
        self, project_id: str, task_id: str, source_path: str | Path,
        kind: str, role: str | None, source_artifact_id: str | None,
        *, artifact_id: str | None = None,
    ) -> Artifact:
        if not isinstance(source_path, (str, Path)) or not str(source_path).strip():
            raise ValueError("source_path 必须是可信本地文件路径")
        source = Path(source_path).expanduser()
        artifacts = self.list_task_artifacts(project_id, task_id)
        artifact_id = uuid4().hex if artifact_id is None else _validate_id(artifact_id)
        # UUID 碰撞时直接失败，绝不覆盖任何既有记录（包括图片已丢失的记录）。
        if self._find_artifacts(artifact_id):
            raise FileExistsError(f"图片 ID 已存在: {artifact_id}")
        prior_signature = self._artifact_index_signature
        folder = "inputs" if kind == "input" else "outputs"
        directory = self._path("projects", project_id, "tasks", task_id, folder)
        temporary = None
        final = None
        final_created = False
        committed = False
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".tmp-", delete=False) as stream:
                temporary = Path(stream.name)
            size_bytes, sha256 = self._copy_source(source, temporary)
            image_format, width, height = self._inspect_image(temporary)
            relative_path = "/".join((
                "projects", project_id, "tasks", task_id, folder,
                artifact_id + _FORMATS[image_format],
            ))
            artifact = Artifact(
                artifact_id, project_id, task_id, kind, role, source.name,
                image_format, width, height, size_bytes, sha256,
                relative_path, _now(), source_artifact_id,
            )
            final = self._artifact_file(artifact, must_exist=False)
            # xb 保证目标文件已存在时失败，连未登记的历史/残留文件也不覆盖。
            with final.open("xb") as writer:
                final_created = True
                with temporary.open("rb") as reader:
                    shutil.copyfileobj(reader, writer, length=_CHUNK_BYTES)
                writer.flush()
                os.fsync(writer.fileno())
            # A second manager may have committed to this task while the image
            # bytes were being copied. Refresh global uniqueness and this task's
            # manifest before replacing its JSON authority.
            if self._find_artifacts(artifact_id):
                raise FileExistsError(f"图片 ID 已存在: {artifact_id}")
            artifacts = self.list_task_artifacts(project_id, task_id)
            prior_signature = self._artifact_index_signature
            self._write_json(self._task_dir(project_id, task_id) / "artifacts.json", {
                "schema_version": 1,
                "artifacts": [asdict(item) for item in [*artifacts, artifact]],
            })
            committed = True
            self._advance_cached_write(
                prior_signature,
                {f"artifacts.json:{project_id}:{task_id}", f"task:{project_id}:{task_id}"},
                artifact,
            )
            return artifact
        finally:
            if temporary is not None:
                self._cleanup(temporary)
            if final_created and not committed:
                self._cleanup(final)
