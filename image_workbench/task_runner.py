"""确定性执行流程及归档恢复；不在这里实现任何图片生成模型。"""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
import hashlib
import math
from pathlib import Path
import re
import stat
import traceback
from uuid import uuid4

from .image_backend import BackendError, BackendRequest, BackendResult, ImageBackend
from .storage import MetadataError, StorageManager, _now, _text, _validate_id
from .image_references import task_input_artifacts
from .image_prompting import execution_prompt
from .timing import measure, phase_seconds
from .locking import FileLock
from .performance import PerformanceTrace


_ANY_PREVIOUS = object()


_ACTIVE = {"running", "archiving"}
_STATUSES = _ACTIVE | {
    "execution_failed", "timed_out", "missing_artifact", "invalid_artifact",
    "archive_failed", "interrupted", "succeeded",
}


@dataclass
class CandidateRecord:
    relative_path: str
    artifact_id: str
    sha256: str | None = None
    size_bytes: int | None = None


@dataclass
class ExecutionRecord:
    execution_id: str
    project_id: str
    task_id: str
    backend: str
    status: str
    created_at: str
    updated_at: str
    returncode: int | None = None
    error: str | None = None
    candidates: list[CandidateRecord] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)
    failure_type: str | None = None
    timing_events: list[dict] = field(default_factory=list)
    prepare_seconds: float = 0.0
    backend_seconds: float = 0.0
    codex_seconds: float | None = None
    validate_seconds: float = 0.0
    register_seconds: float = 0.0
    total_seconds: float = 0.0


class StateUpdateError(OSError):
    """携带未能完整持久化的记录，让批处理保留独立失败证据。"""

    def __init__(self, record, cause):
        super().__init__(f"执行状态写入失败: {cause}")
        self.record = record


class TaskRunner:
    def __init__(self, storage: StorageManager) -> None:
        self.storage = storage

    def _directory(self, project_id: str, task_id: str, execution_id: str | None = None) -> Path:
        parts = ["projects", _validate_id(project_id), "tasks", _validate_id(task_id), "logs", "runs"]
        if execution_id is not None:
            parts.append(_validate_id(execution_id))
        return self.storage.files.path(*parts)

    def get_execution_directory(self, project_id: str, task_id: str, execution_id: str) -> Path:
        self.get_execution(project_id, task_id, execution_id)
        return self._directory(project_id, task_id, execution_id)

    def _record_directory(self, record: ExecutionRecord) -> Path:
        return self._directory(record.project_id, record.task_id, record.execution_id)

    def _save(self, record: ExecutionRecord) -> None:
        record.updated_at = _now()
        self.storage.files.write_json(self._record_directory(record) / "execution.json", {
            "schema_version": 1, **asdict(record),
        })

    def _save_checked(self, record: ExecutionRecord) -> None:
        for phase in ("prepare", "backend", "codex", "validate", "register", "total"):
            setattr(record, phase + "_seconds", phase_seconds(record.timing_events, phase, None if phase == "codex" else 0.0))
        try:
            self._save(record)
        except (OSError, ValueError) as exc:
            record.status = "archive_failed" if record.candidates else "execution_failed"
            record.failure_type = "state_update_failed"
            record.error = f"执行状态写入失败: {exc}"
            raise StateUpdateError(record, exc) from exc

    def _candidate_path(self, record: ExecutionRecord, candidate: CandidateRecord) -> Path:
        relative = candidate.relative_path
        if not isinstance(relative, str) or "\\" in relative or ":" in relative:
            raise ValueError("候选路径必须是规范相对路径，不能包含 Windows ADS")
        path = self.storage.data_root / relative
        products = self._record_directory(record) / "work" / "products"
        if not path.is_relative_to(products) or path == products:
            raise ValueError("候选图片不属于本次执行的 products 目录")
        self.storage.files.check(path)
        if path.relative_to(self.storage.data_root).as_posix() != relative:
            raise ValueError("候选相对路径不规范")
        return path

    def get_execution(
        self, project_id: str, task_id: str, execution_id: str | None = None,
    ) -> ExecutionRecord | None:
        self.storage.get_task(project_id, task_id)
        if execution_id is None:
            records = self.list_executions(project_id, task_id)
            return records[-1] if records else None
        payload = self.storage.files.read_json(self._directory(project_id, task_id, execution_id) / "execution.json")
        try:
            values = {key: value for key, value in payload.items() if key != "schema_version"}
            entries = values.pop("candidates")
            if not isinstance(entries, list):
                raise ValueError("候选清单必须是列表")
            record = ExecutionRecord(**values, candidates=[CandidateRecord(**entry) for entry in entries])
            if (record.project_id, record.task_id, record.execution_id) != (project_id, task_id, execution_id):
                raise ValueError("执行 ID 与目录不一致")
            _text(record.backend, "backend")
            if record.status not in _STATUSES:
                raise ValueError("未知执行状态")
            for timestamp in (record.created_at, record.updated_at):
                if datetime.fromisoformat(timestamp).tzinfo is None:
                    raise ValueError("执行时间缺少时区")
            if record.returncode is not None and type(record.returncode) is not int:
                raise ValueError("returncode 必须是整数或 None")
            if record.error is not None and not isinstance(record.error, str):
                raise ValueError("error 必须是字符串或 None")
            if record.failure_type is not None and not isinstance(record.failure_type, str):
                raise ValueError("failure_type 必须是字符串或 None")
            for phase in ("prepare", "backend", "codex", "validate", "register", "total"):
                seconds = getattr(record, phase + "_seconds")
                if phase == "codex" and seconds is None:
                    continue
                if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                    raise ValueError("耗时必须是有限非负数")
            for event in record.timing_events:
                if event["phase"] not in {"prepare", "backend", "codex", "validate", "register", "total"}:
                    raise ValueError("未知计时阶段")
                for key in ("started_at", "ended_at"):
                    if event[key] is not None and datetime.fromisoformat(event[key]).tzinfo is None:
                        raise ValueError("阶段时间缺少时区")
                if event["seconds"] is not None and (type(event["seconds"]) not in (int, float) or not math.isfinite(event["seconds"]) or event["seconds"] < 0):
                    raise ValueError("阶段耗时无效")
            ids = set()
            paths = set()
            for candidate in record.candidates:
                _validate_id(candidate.artifact_id)
                self._candidate_path(record, candidate)
                if candidate.artifact_id in ids or candidate.relative_path in paths:
                    raise ValueError("候选 ID 或路径重复")
                ids.add(candidate.artifact_id)
                paths.add(candidate.relative_path)
                if candidate.sha256 is None:
                    if candidate.size_bytes is not None:
                        raise ValueError("候选校验信息不完整")
                elif (not isinstance(candidate.sha256, str)
                      or re.fullmatch(r"[0-9a-f]{64}", candidate.sha256) is None
                      or type(candidate.size_bytes) is not int or candidate.size_bytes <= 0):
                    raise ValueError("候选哈希或大小无效")
            if not isinstance(record.output_artifact_ids, list):
                raise ValueError("输出 ID 必须是列表")
            for artifact_id in record.output_artifact_ids:
                _validate_id(artifact_id)
            if len(set(record.output_artifact_ids)) != len(record.output_artifact_ids) or not set(record.output_artifact_ids) <= ids:
                raise ValueError("输出 ID 与候选清单不一致")
            if record.status == "succeeded" and (not ids or ids != set(record.output_artifact_ids) or record.returncode != 0):
                raise ValueError("成功状态缺少有效输出")
            return record
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise MetadataError(f"执行记录无效: {exc}") from exc

    def list_executions(self, project_id: str, task_id: str) -> list[ExecutionRecord]:
        self.storage.get_task(project_id, task_id)
        directory = self._directory(project_id, task_id)
        if not directory.exists():
            return []
        records = [self.get_execution(project_id, task_id, eid) for eid in self.storage.files.child_ids(directory)]
        return sorted(records, key=lambda record: (record.created_at, record.execution_id))

    def _finish(self, record: ExecutionRecord, status: str, error: str | None = None, failure_type: str | None = None) -> ExecutionRecord:
        record.status = status
        record.error = error[:8000] if error else None
        record.failure_type = failure_type or ({
            "timed_out": "codex_timeout", "missing_artifact": "artifact_not_found",
            "invalid_artifact": "artifact_invalid", "archive_failed": "register_failed",
            "execution_failed": "unexpected_exception", "interrupted": "interrupted",
        }.get(status))
        if error:
            try:
                log = self.storage.files.check(self._record_directory(record) / "stderr.log")
                with log.open("a", encoding="utf-8") as stream:
                    stream.write(f"\n[TaskRunner: {status}] {record.error}\n")
            except (OSError, ValueError):
                pass  # 状态 JSON 写入仍必须成功，否则 OSError 向上传播。
        return record

    def run(
        self, project_id: str, task_id: str, backend: ImageBackend,
        *, timeout_seconds: float = 300,
        idempotency_key: str | None = None, expected_previous_execution_id=_ANY_PREVIOUS,
    ) -> ExecutionRecord:
        # Web Worker 使用稳定作业 ID；重发同一作业只读取已存在执行，不再次调用模型。
        if self.storage.get_task(project_id, task_id).operation not in {"generate", "edit"}:
            raise ValueError("文本任务请使用脚本生成服务")
        with FileLock(self.storage.files.path(".execution.lock")):
            if idempotency_key is not None:
                _validate_id(idempotency_key)
                if self._directory(project_id, task_id, idempotency_key).exists():
                    return self.get_execution(project_id, task_id, idempotency_key)
            if expected_previous_execution_id is not _ANY_PREVIOUS:
                previous = self.get_execution(project_id, task_id)
                if (previous.execution_id if previous else None) != expected_previous_execution_id:
                    raise ValueError("任务已出现新的执行记录；请刷新状态，不能重放旧执行请求")
            if self.storage.files.path(".identity", "accounts.sqlite3").exists():
                from .accounts import IdentityStore
                IdentityStore(self.storage).require_reservation(idempotency_key, project_id, task_id)
            return self._run_locked(project_id, task_id, backend, timeout_seconds=timeout_seconds,
                                    execution_id=idempotency_key)

    def _run_locked(self, project_id, task_id, backend, *, timeout_seconds, execution_id=None):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限正数")
        performance = PerformanceTrace()
        task = self.storage.get_task(project_id, task_id)
        _text(backend.name, "backend.name")
        if any(record.status in _ACTIVE for record in self.list_executions(project_id, task_id)):
            raise ValueError("该任务仍有未结束的执行；确认旧进程已停止后可手动 mark_interrupted")
        execution_id = execution_id or uuid4().hex
        record = ExecutionRecord(execution_id, project_id, task_id, backend.name, "running", _now(), _now())
        self._directory(project_id, task_id).mkdir(exist_ok=True)
        directory = self._record_directory(record)
        directory.mkdir()
        try:
            with measure(record.timing_events, "total"):
                try:
                    self._save_checked(record)
                    self._execute(record, task, backend, timeout_seconds, performance)
                except StateUpdateError as exc:
                    self._finish(record, record.status, str(exc), "state_update_failed")
            performance.mark("runner_prepare_end")  # Also close preparation on an early failure.
            with performance.span("final_state_save"):
                self._save_checked(record)
            return record
        finally:
            performance.save(self.storage, task, record)

    def _execute(self, record, task, backend, timeout_seconds, performance):
        project_id, task_id, execution_id = record.project_id, record.task_id, record.execution_id
        directory = self._record_directory(record)
        phase = "prepare"
        try:
            with measure(record.timing_events, "prepare"):
                work = self.storage.files.check(directory / "work")
                work.mkdir()
                references = self.storage.files.check(work / "references")
                references.mkdir()
                self.storage.files.check(work / "products").mkdir()
                stdout = self.storage.files.check(directory / "stdout.log")
                stderr = self.storage.files.check(directory / "stderr.log")
                stdout.touch(exist_ok=False)
                stderr.touch(exist_ok=False)
                inputs = task_input_artifacts(self.storage, project_id, task_id)
                performance.reference_count = len(inputs) + int(task.base_artifact_id is not None)
                reference_paths = []
                for item in inputs:
                    source = self.storage.get_artifact_path(item.artifact_id)
                    destination = self.storage.files.check(references / (item.artifact_id + source.suffix))
                    copied_size, copied_hash = self.storage.copy_image_source(source, destination)
                    if (copied_size, copied_hash) != (item.size_bytes, item.sha256):
                        raise ValueError("参考图片内容与登记记录不一致")
                    self.storage.inspect_image(destination)
                    reference_paths.append(destination)
                edit_image = None
                if task.base_artifact_id is not None:
                    base = self.storage.get_artifact(task.base_artifact_id)
                    source = self.storage.get_artifact_path(task.base_artifact_id)
                    edit_image = self.storage.files.check(references / ("base-" + task.base_artifact_id + source.suffix))
                    copied_size, copied_hash = self.storage.copy_image_source(source, edit_image)
                    if (copied_size, copied_hash) != (base.size_bytes, base.sha256):
                        raise ValueError("编辑基底图片内容与登记记录不一致")
                    self.storage.inspect_image(edit_image)
                try:
                    options = self.storage.files.read_json(self.storage.files.path(".workbench", "tasks", project_id, task_id + ".json"))
                except FileNotFoundError:
                    options = None
                execution_task = replace(task, prompt=execution_prompt(task, options, reference_count=len(inputs)))
                request = BackendRequest(
                    execution_id, execution_task, work, tuple(reference_paths), edit_image,
                    stdout, stderr, timeout_seconds, self.storage.config.max_image_bytes,
                    performance, aspect_ratio=(options or {}).get("aspect_ratio", "auto"),
                )
                self.storage.files.write_json(work / "request.json", {
                    "schema_version": 1, "execution_id": execution_id, "task": asdict(execution_task),
                    "reference_images": [path.relative_to(self.storage.data_root).as_posix() for path in reference_paths],
                    "edit_image": edit_image.relative_to(self.storage.data_root).as_posix() if edit_image else None,
                    "timeout_seconds": timeout_seconds,
                })
            performance.mark("runner_prepare_end")
            phase = "backend"
            with measure(record.timing_events, "backend"):
                result = backend.generate(request)
            if isinstance(result, BackendResult):
                record.timing_events.extend(result.timing_events)
            if not isinstance(result, BackendResult) or type(result.returncode) is not int:
                raise BackendError("后端没有返回合法 BackendResult")
            record.returncode = result.returncode
            if result.returncode != 0:
                raise BackendError(f"后端以非零状态退出: {result.returncode}", returncode=result.returncode)
            if not result.image_paths:
                raise BackendError("后端没有返回任何图片产物", status="missing_artifact", returncode=0)
            phase = "archive"
            candidates = []
            seen = set()
            if len(result.image_paths) > 32:
                raise ValueError("单次执行最多接收 32 个候选产物")
            for path in result.image_paths:
                path = Path(path)
                if not path.is_absolute():
                    raise ValueError("后端返回的产物路径必须是绝对路径")
                candidate = CandidateRecord(path.relative_to(self.storage.data_root).as_posix(), uuid4().hex)
                checked = self._candidate_path(record, candidate)
                if checked in seen:
                    raise ValueError("后端返回了重复的产物路径")
                seen.add(checked)
                candidates.append(candidate)
            record.candidates = candidates
            return self._archive_guard(record)
        except StateUpdateError:
            raise
        except BackendError as exc:
            record.timing_events.extend(exc.timing_events)
            record.returncode = exc.returncode
            return self._finish(record, exc.status, str(exc), exc.failure_type)
        except KeyboardInterrupt:
            return self._finish(record, "interrupted", "用户中断执行；不会自动重试生成")
        except ValueError as exc:
            failure_type = "artifact_invalid" if phase == "archive" else ("prepare_failed" if phase == "prepare" else "unexpected_exception")
            return self._finish(record, "invalid_artifact" if phase == "archive" else "execution_failed", str(exc), failure_type)
        except Exception as exc:
            failure_type = "prepare_failed" if phase == "prepare" and isinstance(exc, OSError) else "unexpected_exception"
            return self._finish(record, "archive_failed" if phase == "archive" else "execution_failed", traceback.format_exc(), failure_type)

    def _hash_file(self, path: Path) -> tuple[int, str]:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("候选图片必须是普通文件")
        if info.st_size > self.storage.config.max_image_bytes:
            raise ValueError("候选图片超过 max_image_bytes 限制")
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                total += len(block)
                if total > self.storage.config.max_image_bytes:
                    raise ValueError("候选图片超过 max_image_bytes 限制")
                digest.update(block)
        return total, digest.hexdigest()

    def _existing_output(self, record: ExecutionRecord, candidate: CandidateRecord):
        try:
            existing = self.storage.get_artifact(candidate.artifact_id)
        except FileNotFoundError:
            return None
        task = self.storage.get_task(record.project_id, record.task_id)
        if (existing.project_id != record.project_id or existing.task_id != record.task_id
                or existing.kind != "output" or candidate.sha256 is None
                or existing.sha256 != candidate.sha256 or existing.size_bytes != candidate.size_bytes
                or existing.source_artifact_id != task.base_artifact_id):
            raise ValueError("预分配图片 ID 已被不同产物占用，拒绝覆盖")
        if self._hash_file(self.storage.get_artifact_path(existing.artifact_id)) != (candidate.size_bytes, candidate.sha256):
            raise ValueError("已登记的图片内容发生变化")
        return existing

    def _archive_guard(self, record: ExecutionRecord) -> ExecutionRecord:
        phase = "validate"
        try:
            record.status = "archiving"
            record.error = None
            record.failure_type = None
            self._save_checked(record)  # 先持久化候选 ID，归档重试保持同一 ID。
            existing_outputs = {}
            with measure(record.timing_events, "validate"):
                for candidate in record.candidates:
                    existing = self._existing_output(record, candidate)
                    if existing is not None:
                        existing_outputs[candidate.artifact_id] = existing
                        continue
                    path = self._candidate_path(record, candidate)
                    size, digest = self._hash_file(path)
                    self.storage.inspect_image(path)
                    if candidate.sha256 is not None and (size, digest) != (candidate.size_bytes, candidate.sha256):
                        raise ValueError("候选产物与上次归档校验结果不同，拒绝重复登记")
                    candidate.sha256, candidate.size_bytes = digest, size
            self._save_checked(record)
            phase = "register"
            with measure(record.timing_events, "register"):
                for candidate in record.candidates:
                    artifact = existing_outputs.get(candidate.artifact_id)
                    if artifact is None:
                        artifact = self.storage.register_output_image(
                            record.project_id, record.task_id, self._candidate_path(record, candidate),
                            artifact_id=candidate.artifact_id,
                        )
                    if artifact.artifact_id not in record.output_artifact_ids:
                        record.output_artifact_ids.append(artifact.artifact_id)
                    self._save_checked(record)
            return self._finish(record, "succeeded")
        except StateUpdateError:
            raise
        except FileNotFoundError as exc:
            return self._finish(record, "missing_artifact" if phase == "validate" else "archive_failed", str(exc),
                                "artifact_not_found" if phase == "validate" else "register_failed")
        except ValueError as exc:
            return self._finish(record, "invalid_artifact" if phase == "validate" else "archive_failed", str(exc),
                                "artifact_invalid" if phase == "validate" else "register_failed")
        except KeyboardInterrupt:
            return self._finish(record, "interrupted", "归档被中断，可从原候选产物继续归档")
        except Exception:
            return self._finish(record, "archive_failed", traceback.format_exc(),
                                "register_failed" if phase == "register" else "unexpected_exception")

    def retry_archive(self, project_id: str, task_id: str, execution_id: str) -> ExecutionRecord:
        with FileLock(self.storage.files.path(".execution.lock")):
            return self._retry_archive_locked(project_id, task_id, execution_id)

    def _retry_archive_locked(self, project_id, task_id, execution_id):
        record = self.get_execution(project_id, task_id, execution_id)
        if record.status == "succeeded":
            return record
        if record.status in _ACTIVE:
            raise ValueError("执行仍为活动状态；确认旧进程停止后先 mark_interrupted")
        if not record.candidates:
            raise ValueError("没有完整候选清单；请根据日志使用 local 后端显式导入已有图片")
        with measure(record.timing_events, "total"):
            try:
                self._archive_guard(record)
            except StateUpdateError as exc:
                self._finish(record, record.status, str(exc), "state_update_failed")
        self._save_checked(record)
        return record

    def mark_interrupted(self, project_id: str, task_id: str, execution_id: str) -> ExecutionRecord:
        record = self.get_execution(project_id, task_id, execution_id)
        if record.status not in _ACTIVE:
            raise ValueError("只能标记 running 或 archiving 记录；该命令不会终止操作系统进程")
        self._finish(record, "interrupted", "调用方已确认原执行进程停止，手动标记中断")
        self._save_checked(record)
        return record
