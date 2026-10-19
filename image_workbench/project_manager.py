"""两级逻辑项目管理。唯一归属事实源是 catalog.json；图片仍由 StorageManager 管理。"""

from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from .locking import FileLock
from .storage import MetadataError, StorageManager, _now, _text, _validate_id


@dataclass(frozen=True)
class MainProject:
    main_project_id: str
    name: str
    description: str
    status: str
    created_at: str
    updated_at: str
    workspace_id: str | None = None


@dataclass(frozen=True)
class Subproject:
    subproject_id: str
    main_project_id: str
    name: str
    description: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskPlacement:
    task_id: str
    storage_project_id: str
    subproject_id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    state: str = "ready"


def _fields(name, description, status=None):
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("名称需要 1–120 个字符，不能全为空白")
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("描述最多 4000 个字符")
    if status is not None and status not in {"active", "archived"}:
        raise ValueError("项目状态只能为 active 或 archived")


class MigrationRequiredError(ValueError):
    pass


class PlacementConflictError(ValueError):
    pass


class ProjectManager:
    """不改写原 Project/Task/Artifact 文件，也不根据显示名称构造路径。

    storage_namespaces 只决定新任务写入的物理命名空间；TaskPlacement 的
    storage_project_id 是不可变定位信息，逻辑主项目始终由 subproject_id 推导。
    不保存子项目 task_ids 清单，也不复制 prompt、图片或执行状态。
    """

    def __init__(self, storage: StorageManager):
        self.storage = storage
        self.root = storage._path(".project_management")

    def _lock(self, *, readonly=False):
        if readonly:
            return nullcontext()  # catalog 的原子替换使读取无需落盘锁；dry-run 也不创建文件。
        self.root.mkdir(exist_ok=True)
        return FileLock(self.storage._path(".project_management", "catalog.lock"), timeout=30)

    @staticmethod
    def _empty():
        return {"schema_version": 1, "revision": 0, "main_projects": {}, "subprojects": {},
                "task_placements": {}, "storage_namespaces": {}, "default_subprojects": {}}

    def _load(self):
        path = self.storage._path(".project_management", "catalog.json")
        try:
            value = self.storage._read_json(path)
        except FileNotFoundError:
            return self._empty()
        self._validate(value)
        return value

    @property
    def migration_required(self):
        return not self.storage._path(".project_management", "catalog.json").exists() and bool(self.storage.list_projects())

    def catalog_view(self):
        """未迁移时提供纯内存兼容视图；不会把读取伪装成已批准的数据迁移。"""
        value = self._load()
        self._merge_legacy(value)
        self._validate(value)
        return value

    def _editable(self):
        if self.migration_required:
            raise MigrationRequiredError("已有数据需要先完成项目层级迁移；请先查看 dry-run 与备份，再明确批准应用。")
        return self.catalog_view()

    def _validate(self, value):
        try:
            if (set(value) != set(self._empty()) or type(value["schema_version"]) is not int
                    or value["schema_version"] != 1 or type(value["revision"]) is not int or value["revision"] < 0):
                raise ValueError("无效目录结构或修订号")
            for section, record_type, id_field in (("main_projects", MainProject, "main_project_id"),
                    ("subprojects", Subproject, "subproject_id"), ("task_placements", TaskPlacement, "task_id")):
                if not isinstance(value[section], dict):
                    raise ValueError("记录集合必须为对象")
                for key, raw in value[section].items():
                    _validate_id(key)
                    record = record_type(**raw)
                    if getattr(record, id_field) != key:
                        raise ValueError("记录 ID 与索引不一致")
                    _fields(record.name, record.description, getattr(record, "status", None))
                    for field in ("created_at", "updated_at"):
                        stamp = datetime.fromisoformat(getattr(record, field))
                        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                            raise ValueError("时间必须为 UTC")
            mains, subs = value["main_projects"], value["subprojects"]
            namespaces, defaults = value["storage_namespaces"], value["default_subprojects"]
            if not isinstance(namespaces, dict) or not isinstance(defaults, dict):
                raise ValueError("命名空间和默认子项目映射必须为对象")
            if set(namespaces) != set(mains) or set(defaults) != set(mains):
                raise ValueError("主项目映射不完整")
            if len(set(namespaces.values())) != len(namespaces):
                raise ValueError("物理命名空间不能被多个主项目占用")
            for mid, pid in namespaces.items():
                _validate_id(pid)
                sid = _validate_id(defaults[mid])
                if sid not in subs or subs[sid]["main_project_id"] != mid:
                    raise ValueError("默认子项目引用不正确")
            for sub in subs.values():
                if _validate_id(sub["main_project_id"]) not in mains:
                    raise ValueError("子项目引用了不存在的主项目")
            for placement in value["task_placements"].values():
                sid = _validate_id(placement["subproject_id"])
                pid = _validate_id(placement["storage_project_id"])
                if sid not in subs or pid != namespaces[subs[sid]["main_project_id"]]:
                    raise ValueError("任务归属与存储定位冲突")
                if placement["state"] not in {"pending", "ready"}:
                    raise ValueError("无效任务归属提交状态")
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise MetadataError(f"逻辑项目目录损坏，未覆盖原记录：{exc}") from exc

    def _save(self, value):
        self._validate(value)
        value["revision"] += 1
        self.storage._write_json(self.storage._path(".project_management", "catalog.json"), value)

    @staticmethod
    def _required(collection, key, label):
        _validate_id(key)
        try:
            return collection[key]
        except KeyError as exc:
            raise FileNotFoundError(f"{label}不存在") from exc

    @staticmethod
    def _default_id(main_project_id):
        return uuid5(NAMESPACE_URL, "image-workbench/default-subproject/" + main_project_id).hex

    def _add_main(self, value, mid, name, description, status, created_at, storage_id):
        sid = self._default_id(mid)
        value["main_projects"][mid] = asdict(MainProject(mid, name, description, status, created_at, created_at))
        value["subprojects"][sid] = asdict(Subproject(sid, mid, "未分类任务", "旧任务与未分类任务", "active", created_at, created_at))
        value["default_subprojects"][mid] = sid
        value["storage_namespaces"][mid] = storage_id

    def _merge_legacy(self, value):
        added_main = added_task = recovered = 0
        namespace_owner = {pid: mid for mid, pid in value["storage_namespaces"].items()}
        for project in self.storage.list_projects():
            mid = namespace_owner.get(project.project_id)
            if mid is None:
                mid = project.project_id
                if mid in value["main_projects"]:
                    raise MetadataError("旧项目 ID 与已有逻辑项目冲突")
                self._add_main(value, mid, project.name, "", "active", project.created_at, project.project_id)
                namespace_owner[project.project_id] = mid
                added_main += 1
            for task in self.storage.list_tasks(project.project_id):
                existing = value["task_placements"].get(task.task_id)
                if existing:
                    if existing["storage_project_id"] != project.project_id:
                        raise MetadataError("不同物理项目中出现重复 task_id；未猜测归属或改写 ID")
                    if existing["state"] == "pending":
                        existing["state"] = "ready"
                        recovered += 1
                    continue
                value["task_placements"][task.task_id] = asdict(TaskPlacement(task.task_id, project.project_id,
                    value["default_subprojects"][mid], task.prompt.strip().splitlines()[0][:120] or "未命名任务", "",
                    task.created_at, task.created_at))
                added_task += 1
        return {"added_main_projects": added_main, "added_subprojects": added_main, "added_tasks": added_task,
                "recovered_pending_tasks": recovered}

    def _inventory(self):
        """只读记录真实内容，元数据 SHA、稳定 ID、相对路径和版本边同时参与指纹。"""
        projects, tasks, artifacts, metadata = [], [], [], {}
        artifact_ids, task_ids = {}, set()
        for project in self.storage.list_projects():
            projects.append(asdict(project))
            for task in self.storage.list_tasks(project.project_id):
                if task.task_id in task_ids:
                    raise MetadataError("检测到重复 task_id；迁移未执行")
                task_ids.add(task.task_id)
                tasks.append(asdict(task))
                for artifact in self.storage.list_task_artifacts(project.project_id, task.task_id):
                    if artifact.artifact_id in artifact_ids:
                        raise MetadataError("检测到重复 artifact_id；迁移未执行")
                    path = self.storage._artifact_file(artifact)
                    raw = path.read_bytes()
                    if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.sha256:
                        raise MetadataError(f"图片内容与登记不符：{artifact.artifact_id}")
                    self.storage._inspect_image(path)
                    artifact_ids[artifact.artifact_id] = artifact
                    artifacts.append(asdict(artifact))
        for task in tasks:
            if task["base_artifact_id"]:
                base = artifact_ids.get(task["base_artifact_id"])
                if base is None or base.project_id != task["project_id"]:
                    raise MetadataError("编辑任务引用丢失或跨物理命名空间")
        for artifact in artifact_ids.values():
            if artifact.source_artifact_id:
                source = artifact_ids.get(artifact.source_artifact_id)
                if source is None or source.project_id != artifact.project_id:
                    raise MetadataError("图片版本来源丢失或越界")
        # 备份包括旧 JSON、队列与审核状态；日志正文和图片均保持原地。
        for name in ("projects", ".workbench"):
            directory = self.storage._path(name)
            if directory.exists():
                for path in sorted(directory.rglob("*.json")):
                    self.storage._check_path(path)
                    relative = path.relative_to(self.storage.data_root).as_posix()
                    metadata[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        result = {"projects": sorted(projects, key=lambda item: item["project_id"]),
                  "tasks": sorted(tasks, key=lambda item: item["task_id"]),
                  "artifacts": sorted(artifacts, key=lambda item: item["artifact_id"]), "metadata_hashes": metadata}
        result["counts"] = {"projects": len(projects), "tasks": len(tasks), "artifacts": len(artifacts),
                            "inputs": sum(item["kind"] == "input" for item in artifacts),
                            "outputs": sum(item["kind"] == "output" for item in artifacts)}
        return result

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _migration_materials(self):
        current = self._load()
        original = json.loads(json.dumps(current))
        inventory = self._inventory()
        changes = self._merge_legacy(current)
        self._validate(current)
        # 防止 inventory 与投影恰好读到了不同的活动任务集合。
        physical = {(item["project_id"], item["task_id"]) for item in inventory["tasks"]}
        ready = {(item["storage_project_id"], item["task_id"]) for item in current["task_placements"].values() if item["state"] == "ready"}
        if physical != ready:
            raise MetadataError("任务集合已变化或存在失效归属；请确认无写入后重做 dry-run")
        catalog_path = self.storage._path(".project_management", "catalog.json")
        catalog_bytes = catalog_path.read_bytes() if catalog_path.exists() else None
        fingerprint = self._fingerprint({"inventory": inventory, "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest() if catalog_bytes is not None else None})
        plan = {"schema_version": 1, "status": "dry_run", "fingerprint": fingerprint, "changes": changes,
                "before": inventory["counts"], "after": {**inventory["counts"], "main_projects": len(current["main_projects"]),
                    "subprojects": len(current["subprojects"])},
                "default_subprojects": [{"main_project_id": mid, "subproject_id": sid, "name": current["subprojects"][sid]["name"]}
                                        for mid, sid in current["default_subprojects"].items()],
                "image_files_moved": 0, "image_files_changed": 0, "generation_calls": 0,
                "already_current": original == current and catalog_bytes is not None}
        return plan, current, inventory, catalog_bytes

    def migration_plan(self):
        return self._migration_materials()[0]

    def _backup(self, directory, plan, inventory, catalog_bytes):
        directory = Path(directory).resolve()
        if directory.is_relative_to(self.storage.data_root):
            raise ValueError("迁移备份必须放在数据根目录外")
        destination = directory / (_now()[:19].replace(":", "").replace("-", "") + "-" + uuid4().hex[:8])
        destination.mkdir(parents=True, exist_ok=False)
        for relative, expected in inventory["metadata_hashes"].items():
            data = self.storage._path(*relative.split("/")).read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                raise MetadataError("备份过程中元数据变化；未提交迁移")
            target = destination / "metadata" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if catalog_bytes is not None:
            (destination / "catalog.before.json").write_bytes(catalog_bytes)
        record = {"schema_version": 1, "created_at": _now(), "data_root": str(self.storage.data_root),
                  "catalog_existed": catalog_bytes is not None, "plan": plan, "inventory": inventory}
        (destination / "backup.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return destination

    def backup_metadata(self, backup_directory):
        """准备可审阅备份；不应用迁移。应用前仍须重新核对指纹。"""
        plan, _, inventory, catalog_bytes = self._migration_materials()
        destination = self._backup(backup_directory, plan, inventory, catalog_bytes)
        if self.migration_plan()["fingerprint"] != plan["fingerprint"]:
            raise MetadataError("准备备份时数据已变化；请重新准备，当前备份未标记为可应用")
        return {**plan, "status": "backup_prepared", "backup_directory": str(destination)}

    def _restore_catalog(self, original):
        path = self.storage._path(".project_management", "catalog.json")
        if original is None:
            path.unlink(missing_ok=True)  # 仅移除本次新建逻辑目录，不触碰任何物理项目文件。
            return
        temporary = self.storage._path(".project_management", ".restore-" + uuid4().hex + ".json")
        try:
            with temporary.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_result(path, result):
        temporary = path.with_name(".result-" + uuid4().hex + ".json")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def migrate_legacy(self, *, dry_run=True, expected_fingerprint=None, backup_directory=None):
        if dry_run:
            return self.migration_plan()
        if not expected_fingerprint or backup_directory is None:
            raise ValueError("应用迁移需要 dry-run 指纹和备份目录")
        control = self.storage._path(".workbench", "control.lock")
        guard = FileLock(control, timeout=5) if control.parent.exists() else nullcontext()
        with guard, FileLock(self.storage._path(".execution.lock")), self._lock():
            plan, value, inventory, original = self._migration_materials()
            if expected_fingerprint != plan["fingerprint"]:
                raise ValueError("数据在 dry-run 后发生变化，请重新预览并确认，迁移未执行")
            if plan["already_current"]:
                return {**plan, "status": "unchanged", "integrity_verified": True, "backup_directory": None}
            destination = self._backup(backup_directory, plan, inventory, original)
            try:
                if self._inventory() != inventory:
                    raise MetadataError("备份后数据发生变化，迁移未提交")
                self._save(value)
                if self._inventory() != inventory:
                    raise MetadataError("迁移后完整性核对不一致")
                result = {**plan, "status": "applied", "integrity_verified": True, "backup_directory": str(destination),
                          "revision": value["revision"]}
                self._write_result(destination / "result.json", result)
            except BaseException:
                self._restore_catalog(original)
                (destination / "result.json").write_text(json.dumps({"status": "failed", "catalog_restored": True,
                    "original_files_modified_by_migration": False}, ensure_ascii=False, indent=2), encoding="utf-8")
                raise
            return result

    def create_main_project(self, name, description="", *, main_project_id=None):
        _fields(name, description, "active")
        mid = _validate_id(main_project_id) if main_project_id else uuid4().hex
        with self._lock():
            value = self._editable()
            if mid in value["main_projects"]:
                raise FileExistsError("主项目 ID 已存在")
            # 为兼容旧 API，当前分配与 main ID 相同的稳定命名空间；所有使用仍经过映射。
            self._add_main(value, mid, name, description, "active", _now(), mid)
            self._save(value)
            return MainProject(**value["main_projects"][mid])

    def list_main_projects(self):
        with self._lock(readonly=True):
            records = [MainProject(**item) for item in self.catalog_view()["main_projects"].values()]
        return sorted(records, key=lambda item: (item.created_at, item.main_project_id))

    def get_main_project(self, main_project_id):
        with self._lock(readonly=True):
            return MainProject(**self._required(self.catalog_view()["main_projects"], main_project_id, "主项目"))

    def update_main_project(self, main_project_id, *, name=None, description=None, status=None):
        return self._update("main_projects", main_project_id, MainProject, name, description, status)

    def update_subproject(self, subproject_id, *, name=None, description=None, status=None):
        return self._update("subprojects", subproject_id, Subproject, name, description, status)

    def _update(self, section, identity, record_type, name, description, status):
        with self._lock():
            value = self._editable()
            record = record_type(**self._required(value[section], identity, "项目"))
            changes = {key: val for key, val in (("name", name), ("description", description), ("status", status)) if val is not None}
            updated = replace(record, **changes)
            _fields(updated.name, updated.description, updated.status)
            if updated != record:
                updated = replace(updated, updated_at=_now())
                value[section][identity] = asdict(updated)
                self._save(value)
            return updated

    def create_subproject(self, main_project_id, name, description="", *, subproject_id=None):
        _fields(name, description, "active")
        sid = _validate_id(subproject_id) if subproject_id else uuid4().hex
        with self._lock():
            value = self._editable()
            self._required(value["main_projects"], main_project_id, "主项目")
            if sid in value["subprojects"]:
                raise FileExistsError("子项目 ID 已存在")
            stamp = _now()
            sub = Subproject(sid, main_project_id, name, description, "active", stamp, stamp)
            value["subprojects"][sid] = asdict(sub)
            value["main_projects"][main_project_id]["updated_at"] = stamp
            self._save(value)
            return sub

    def list_subprojects(self, main_project_id):
        with self._lock(readonly=True):
            value = self.catalog_view()
            self._required(value["main_projects"], main_project_id, "主项目")
            records = [Subproject(**item) for item in value["subprojects"].values() if item["main_project_id"] == main_project_id]
        return sorted(records, key=lambda item: (item.created_at, item.subproject_id))

    def get_subproject(self, subproject_id):
        with self._lock(readonly=True):
            return Subproject(**self._required(self.catalog_view()["subprojects"], subproject_id, "子项目"))

    def default_subproject(self, main_project_id):
        with self._lock(readonly=True):
            value = self.catalog_view()
            self._required(value["main_projects"], main_project_id, "主项目")
            return Subproject(**value["subprojects"][value["default_subprojects"][main_project_id]])

    def ensure_storage_namespace(self, main_project_id):
        with self._lock():
            value = self._editable()
            main = self._required(value["main_projects"], main_project_id, "主项目")
            pid = value["storage_namespaces"][main_project_id]
            try:
                self.storage.get_project(pid)
            except FileNotFoundError:
                # 只允许尚不存在的命名空间；已有损坏目录交给 StorageManager 拒绝。
                self.storage.create_project(main["name"], project_id=pid)
            return pid

    def reserve_task(self, subproject_id, task_id, name, description=""):
        """先保存创建意图，随后由原工作台或 StorageManager 建任务，崩溃不会丢失目标子项目。"""
        _validate_id(task_id)
        _fields(name, description)
        with self._lock():
            value = self._editable()
            sub = self._required(value["subprojects"], subproject_id, "子项目")
            pid = value["storage_namespaces"][sub["main_project_id"]]
            existing = value["task_placements"].get(task_id)
            if existing:
                if existing["storage_project_id"] != pid or existing["subproject_id"] != subproject_id:
                    raise ValueError("任务 ID 已有归属，不能通过创建接口移动任务")
                return TaskPlacement(**existing)
            stamp = _now()
            record = TaskPlacement(task_id, pid, subproject_id, name, description, stamp, stamp, "pending")
            value["task_placements"][task_id] = asdict(record)
            self._save(value)
            return record

    def finish_task(self, task_id):
        with self._lock():
            value = self._load()
            item = self._required(value["task_placements"], task_id, "任务归属")
            self.storage.get_task(item["storage_project_id"], task_id)
            if item["state"] != "ready":
                item["state"] = "ready"
                self._save(value)
            return TaskPlacement(**item)

    def get_task_placement(self, task_id, *, main_project_id=None):
        with self._lock(readonly=True):
            value = self.catalog_view()
            item = self._required(value["task_placements"], task_id, "任务归属")
            actual_main = value["subprojects"][item["subproject_id"]]["main_project_id"]
            if main_project_id is not None and main_project_id != actual_main:
                raise FileNotFoundError("该主项目下不存在此任务")
            return TaskPlacement(**item)

    def list_tasks(self, *, main_project_id=None, subproject_id=None):
        if main_project_id is None and subproject_id is None:
            raise ValueError("请指定主项目或子项目")
        with self._lock(readonly=True):
            value = self.catalog_view()
            if main_project_id is not None:
                self._required(value["main_projects"], main_project_id, "主项目")
            if subproject_id is not None:
                sub = self._required(value["subprojects"], subproject_id, "子项目")
                if main_project_id is not None and sub["main_project_id"] != main_project_id:
                    raise ValueError("子项目不属于指定主项目")
            placements = [TaskPlacement(**item) for item in value["task_placements"].values()
                          if (subproject_id is None or item["subproject_id"] == subproject_id)
                          and (main_project_id is None or value["subprojects"][item["subproject_id"]]["main_project_id"] == main_project_id)]
        results = []
        for item in placements:
            try:
                task = self.storage.get_task(item.storage_project_id, item.task_id)
            except FileNotFoundError:
                if item.state == "pending":
                    continue
                raise
            results.append((task, item))
        return sorted(results, key=lambda pair: (pair[0].created_at, pair[0].task_id))

    def move_task(self, task_id, target_subproject_id, *, expected_subproject_id=None):
        with self._lock():
            value = self._editable()
            item = self._required(value["task_placements"], task_id, "任务归属")
            target = self._required(value["subprojects"], target_subproject_id, "目标子项目")
            original = value["subprojects"][item["subproject_id"]]
            if original["main_project_id"] != target["main_project_id"]:
                raise ValueError("本阶段只支持同一主项目内移动任务")
            if expected_subproject_id is not None and item["subproject_id"] != expected_subproject_id:
                raise PlacementConflictError("任务归属已变化，请刷新后重试")
            self.storage.get_task(item["storage_project_id"], task_id)
            if item["subproject_id"] != target_subproject_id:
                stamp = _now()
                item.update(subproject_id=target_subproject_id, updated_at=stamp)
                original["updated_at"] = target["updated_at"] = stamp
                value["main_projects"][target["main_project_id"]]["updated_at"] = stamp
                self._save(value)
            return TaskPlacement(**item)

    def update_task(self, task_id, *, name=None, description=None):
        with self._lock():
            value = self._editable()
            item = self._required(value["task_placements"], task_id, "任务归属")
            self.storage.get_task(item["storage_project_id"], task_id)
            record = TaskPlacement(**item)
            updated = replace(record, **{key: val for key, val in (("name", name), ("description", description)) if val is not None})
            _fields(updated.name, updated.description)
            if updated != record:
                updated = replace(updated, updated_at=_now())
                value["task_placements"][task_id] = asdict(updated)
                self._save(value)
            return updated

    def create_task(self, subproject_id, operation, prompt, base_artifact_id=None, *, name=None, description="", task_id=None):
        _text(prompt, "提示词")
        if operation not in {"generate", "edit", "script", "screenplay"} or (operation == "generate" and base_artifact_id is not None):
            raise ValueError("任务类型或基底不正确")
        tid = _validate_id(task_id) if task_id else uuid4().hex
        title = name if name is not None else (prompt.strip().splitlines()[0][:120] or "未命名任务")
        _fields(title, description)
        sub = self.get_subproject(subproject_id)
        pid = self.ensure_storage_namespace(sub.main_project_id)
        if operation == "edit":
            self.storage._require_source(pid, base_artifact_id)
        self.reserve_task(subproject_id, tid, title, description)
        try:
            task = self.storage.get_task(pid, tid)
        except FileNotFoundError:
            task = self.storage.create_task(pid, operation, prompt, base_artifact_id, task_id=tid)
        else:
            if (task.operation, task.prompt, task.base_artifact_id) != (operation, prompt, base_artifact_id):
                raise ValueError("已有任务 ID 的内容与创建请求不一致")
        self.finish_task(tid)
        return task

    def summary(self, *, main_project_id=None, subproject_id=None):
        from .task_runner import TaskRunner
        entries = self.list_tasks(main_project_id=main_project_id, subproject_id=subproject_id)
        if subproject_id is not None:
            stamps = [self.get_subproject(subproject_id).updated_at]
        else:
            stamps = [self.get_main_project(main_project_id).updated_at,
                      *(item.updated_at for item in self.list_subprojects(main_project_id))]
        inputs = outputs = 0
        runner = TaskRunner(self.storage)
        for task, placement in entries:
            stamps.extend((task.created_at, placement.updated_at))
            for artifact in self.storage.list_task_artifacts(task.project_id, task.task_id):
                inputs += artifact.kind == "input" and artifact.format != "TXT"
                outputs += artifact.kind == "output" and artifact.format != "TXT"
                stamps.append(artifact.created_at)
            execution = runner.get_execution(task.project_id, task.task_id)
            if execution:
                stamps.append(execution.updated_at)
        return {"task_count": len(entries), "image_count": inputs + outputs, "input_count": inputs,
                "output_count": outputs, "updated_at": max(stamps)}
