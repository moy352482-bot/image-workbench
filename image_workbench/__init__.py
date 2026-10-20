"""Stable public API, loaded on demand so importing one feature does not load all."""
from importlib import import_module

_EXPORTS = {
    "Artifact": "models", "Project": "models", "Task": "models",
    "StorageConfig": "config", "StorageManager": "storage", "MetadataError": "storage",
    "BackendError": "image_backend", "BackendRequest": "image_backend",
    "BackendResult": "image_backend", "ImageBackend": "image_backend", "LocalFileBackend": "image_backend",
    "CodexImageBackend": "codex_backend", "CandidateRecord": "task_runner",
    "ExecutionRecord": "task_runner", "TaskRunner": "task_runner",
    "MainProject": "project_manager", "Subproject": "project_manager",
    "TaskPlacement": "project_manager", "ProjectManager": "project_manager",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("." + _EXPORTS[name], __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
