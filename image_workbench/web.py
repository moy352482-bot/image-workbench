"""Stable public entry points; implementation is assembled in composition."""
from .composition import create_app
from .http_api.workspace import create_workspace_app
from .http_api.guard import LocalRequestGuard
from .contracts.images import (
    MAX_AUTH_BYTES, MAX_UPLOAD_BYTES, ID_PATTERN, Payload, CreateProject,
    ProjectUpdate, TaskUpdate, MoveTask, RunTask, RestoreTask,
)

__all__ = ["create_app", "create_workspace_app", "LocalRequestGuard",
           "MAX_AUTH_BYTES", "MAX_UPLOAD_BYTES", "ID_PATTERN", "Payload",
           "CreateProject", "ProjectUpdate", "TaskUpdate", "MoveTask", "RunTask", "RestoreTask"]
