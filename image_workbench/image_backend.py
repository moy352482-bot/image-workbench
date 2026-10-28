"""后端只负责产生本次候选文件，任务状态和正式登记统一交给 TaskRunner。"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
from typing import Protocol, Sequence

from .models import Task
from .performance import PerformanceTrace


@dataclass(frozen=True)
class BackendRequest:
    execution_id: str
    task: Task
    work_dir: Path
    reference_images: tuple[Path, ...]
    edit_image: Path | None
    stdout_path: Path
    stderr_path: Path
    timeout_seconds: float
    max_image_bytes: int
    performance: PerformanceTrace | None = field(default=None, compare=False, repr=False)
    aspect_ratio: str = "auto"


@dataclass(frozen=True)
class BackendResult:
    image_paths: tuple[Path, ...]
    returncode: int = 0
    timing_events: list[dict] = field(default_factory=list)


class BackendError(Exception):
    def __init__(self, message: str, *, status: str = "execution_failed", returncode: int | None = None,
                 failure_type: str | None = None, timing_events: list[dict] | None = None):
        super().__init__(message)
        if status not in {"execution_failed", "timed_out", "missing_artifact", "invalid_artifact", "archive_failed", "interrupted"}:
            raise ValueError("无效后端错误状态")
        self.status = status
        self.returncode = returncode
        self.failure_type = failure_type
        self.timing_events = timing_events or []


class ImageBackend(Protocol):
    name: str

    def generate(self, request: BackendRequest) -> BackendResult:
        """返回 work/products/ 内本次明确产生的文件，不能只报告命令退出成功。"""
        ...


def copy_product(source: Path, destination: Path, limit: int) -> None:
    """只复制，不覆盖目标；失败保留部分产物供排错，不把它登记为有效图片。"""
    info = source.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("候选源必须是普通文件")
    if info.st_size > limit:
        raise ValueError("产物超过 max_image_bytes 限制")
    total = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while block := reader.read(min(1024 * 1024, limit - total + 1)):
            total += len(block)
            if total > limit:
                raise ValueError("产物超过 max_image_bytes 限制")
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())


class LocalFileBackend:
    """测试/导入后端：确定性复制可信脚本指定的现有文件，不调用模型。"""

    name = "local"

    def __init__(self, source_paths: Sequence[str | Path]) -> None:
        if isinstance(source_paths, (str, Path)):
            raise ValueError("source_paths 应为文件路径列表")
        self.source_paths = tuple(Path(path).expanduser().absolute() for path in source_paths)

    def generate(self, request: BackendRequest) -> BackendResult:
        products = request.work_dir / "products"
        paths = []
        with request.stdout_path.open("a", encoding="utf-8") as log:
            for index, source in enumerate(self.source_paths, start=1):
                # 扩展名只作显示；TaskRunner 按真实内容验证。
                suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".bin"
                target = products / f"{index:04d}{suffix}"
                log.write(f"复制本地测试产物: {source} -> {target.name}\n")
                log.flush()
                try:
                    copy_product(source, target, request.max_image_bytes)
                except FileNotFoundError as exc:
                    raise BackendError(str(exc), status="missing_artifact") from exc
                except ValueError as exc:
                    raise BackendError(str(exc), status="invalid_artifact") from exc
                except OSError as exc:
                    raise BackendError(f"本地产物复制失败: {exc}", status="archive_failed") from exc
                paths.append(target)
        return BackendResult(tuple(paths))
