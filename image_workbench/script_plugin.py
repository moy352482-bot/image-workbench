"""Script generation extension over the existing application service."""

from collections.abc import Callable
from typing import Protocol

from .contracts.scripts import ScriptSubmission
from .plugin_api import PluginContext, PluginDescriptor, PluginRegistry

PLUGIN_ID = "script-generation"


class ScriptServicePort(Protocol):
    """The service owns identity, persistence, pricing, execution and recovery."""

    def validate_input(self, account_id, workspace_id, body): ...
    def quote(self, account_id, workspace_id, body): ...
    def submit(self, account_id, workspace_id, body, *, admission=None): ...
    def detail(self, account_id, workspace_id, task_id): ...


class ScriptGenerationPlugin:
    descriptor = PluginDescriptor(
        plugin_id=PLUGIN_ID,
        name="脚本与剧本生成",
        version="1.0.0",
        description="通过既有脚本服务提交持久任务并查询结构化结果。",
        capabilities=("script", "screenplay", "async-submit", "immutable-versions"),
        input_schema=ScriptSubmission.model_json_schema(),
        output_schema={
            "type": "object",
            "required": ["plugin_id", "task_id", "status", "output", "failure"],
            "properties": {
                "plugin_id": {"const": PLUGIN_ID},
                "task_id": {"type": "string"},
                "status": {"type": "string"},
                "output": {"anyOf": [
                    {"type": "null"},
                    {"type": "object", "required": ["text", "artifact_id", "version_id", "version"],
                     "properties": {"text": {"type": "string"}, "artifact_id": {"type": "string"},
                                    "version_id": {"type": "string"}, "version": {"type": "integer"}}},
                ]},
                "failure": {"anyOf": [
                    {"type": "null"},
                    {"type": "object", "required": ["code", "message", "retryable", "recoverable"],
                     "properties": {"code": {"type": "string"}, "message": {"type": "string"},
                                    "retryable": {"const": False}, "recoverable": {"type": "boolean"}}},
                ]},
            },
        },
        required_services=("script-service",),
    )

    def __init__(self, service: Callable[[], ScriptServicePort], registry: PluginRegistry):
        self._service = service
        self._registry = registry

    def validate_input(self, context: PluginContext, body: dict) -> dict:
        request = ScriptSubmission.model_validate(body).model_dump(exclude_none=True)
        return self._service().validate_input(context.account_id, context.workspace_id, request)

    def estimate_cost(self, context: PluginContext, body: dict) -> dict:
        # Reuse the authoritative price/config and workspace checks; no inference.
        self._registry.require_enabled(PLUGIN_ID)
        return self._service().quote(context.account_id, context.workspace_id, body)

    def submit(self, context: PluginContext, body: dict) -> dict:
        request = ScriptSubmission.model_validate(body).model_dump(exclude_none=True)
        # Admission is checked inside the service AFTER idempotent replay lookup,
        # but BEFORE task creation/reservation. Disabling cannot break old retries.
        return self._service().submit(
            context.account_id, context.workspace_id, request,
            admission=lambda: self._registry.require_enabled(PLUGIN_ID),
        )

    def status(self, context: PluginContext, task_id: str) -> dict:
        return self._service().detail(context.account_id, context.workspace_id, task_id)

    def result(self, context: PluginContext, task_id: str) -> dict:
        task = self.status(context, task_id)
        output = failure = None
        if task["status"] == "completed":
            output = {"text": task["text"], "artifact_id": task["output_artifact_id"],
                      "version_id": task["version_id"], "version": task["output_version"]}
        elif task["status"] in {"failed", "uncertain"}:
            failure = {"code": "execution_" + task["status"], "message": task.get("error") or "",
                       "retryable": False, "recoverable": bool(task.get("can_recover"))}
        return {"plugin_id": PLUGIN_ID, "task_id": task_id, "status": task["status"],
                "output": output, "failure": failure}
