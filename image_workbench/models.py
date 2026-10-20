"""可直接读取属性的不可变记录。磁盘 JSON 只使用基本类型。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class Task:
    task_id: str
    project_id: str
    operation: str
    prompt: str
    base_artifact_id: str | None
    created_at: str


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    project_id: str
    task_id: str
    kind: str
    role: str | None
    original_filename: str
    format: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    relative_path: str
    created_at: str
    source_artifact_id: str | None
