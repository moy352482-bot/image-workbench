"""图片生成与修改工作台的本地存储接口。"""

from .config import StorageConfig
from .models import Artifact, Project, Task
from .storage import MetadataError, StorageManager
from .image_backend import BackendError, BackendRequest, BackendResult, ImageBackend, LocalFileBackend
from .codex_backend import CodexImageBackend
from .task_runner import CandidateRecord, ExecutionRecord, TaskRunner
from .project_manager import MainProject, Subproject, TaskPlacement, ProjectManager

__all__ = [
    "Artifact", "Project", "Task", "StorageConfig", "StorageManager", "MetadataError",
    "BackendError", "BackendRequest", "BackendResult", "ImageBackend", "LocalFileBackend",
    "CodexImageBackend", "CandidateRecord", "ExecutionRecord", "TaskRunner",
    "MainProject", "Subproject", "TaskPlacement", "ProjectManager",
]
