"""Small, explicit extension contracts; no discovery, execution or persistence."""

from copy import deepcopy
from dataclasses import asdict, dataclass
import re
from threading import RLock
from typing import Protocol

from .domain_errors import ConflictError


class PluginDisabled(ConflictError):
    """New work is disabled; already admitted work and history remain accessible."""


@dataclass(frozen=True)
class PluginContext:
    account_id: str
    workspace_id: str


@dataclass(frozen=True)
class PluginDescriptor:
    plugin_id: str
    name: str
    version: str
    description: str
    capabilities: tuple[str, ...]
    input_schema: dict
    output_schema: dict
    dependencies: tuple[str, ...] = ()
    required_services: tuple[str, ...] = ()


class GenerationPlugin(Protocol):
    descriptor: PluginDescriptor

    def validate_input(self, context: PluginContext, body: dict) -> dict: ...
    def estimate_cost(self, context: PluginContext, body: dict) -> dict: ...
    def submit(self, context: PluginContext, body: dict) -> dict: ...
    def status(self, context: PluginContext, task_id: str) -> dict: ...
    def result(self, context: PluginContext, task_id: str) -> dict: ...


class PluginRegistry:
    """Trusted in-process extensions, registered once by composition.

    Enabled state gates admission, not workers, settlement, recovery or history.
    No package discovery, hot unloading, database access or model invocation.
    Runtime changes apply to this process; startup configuration makes them durable.
    """

    def __init__(self, *, services=()):
        self._services = frozenset(services)
        self._plugins: dict[str, GenerationPlugin] = {}
        self._descriptors: dict[str, PluginDescriptor] = {}
        self._enabled: set[str] = set()
        self._lock = RLock()

    def register(self, plugin: GenerationPlugin, *, enabled=True):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        descriptor = deepcopy(plugin.descriptor)
        pid = descriptor.plugin_id
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", pid):
            raise ValueError("Invalid plugin_id")
        if not descriptor.name or not descriptor.version:
            raise ValueError("Plugin name and version are required")
        for method in ("validate_input", "estimate_cost", "submit", "status", "result"):
            if not callable(getattr(plugin, method, None)):
                raise ValueError(f"Plugin {pid} is missing {method}")
        with self._lock:
            if pid in self._plugins:
                raise ValueError(f"Plugin already registered: {pid}")
            missing = set(descriptor.required_services) - self._services
            if missing:
                raise ValueError(f"Missing services for {pid}: {sorted(missing)}")
            missing = set(descriptor.dependencies) - self._plugins.keys()
            if missing:
                raise ValueError(f"Register dependencies before {pid}: {sorted(missing)}")
            if enabled:
                for dependency in descriptor.dependencies:
                    self.require_enabled(dependency)
            self._plugins[pid] = plugin
            self._descriptors[pid] = descriptor
            if enabled:
                self._enabled.add(pid)

    def get(self, plugin_id: str) -> GenerationPlugin:
        # Disabled plugins are intentionally still available for status/results.
        with self._lock:
            return self._plugins[plugin_id]

    def describe(self, plugin_id: str) -> dict:
        with self._lock:
            return {**asdict(self._descriptors[plugin_id]), "enabled": plugin_id in self._enabled}

    def catalog(self) -> list[dict]:
        with self._lock:
            return [self.describe(pid) for pid in sorted(self._plugins)]

    def require_enabled(self, plugin_id: str):
        with self._lock:
            descriptor = self._descriptors[plugin_id]
            if plugin_id not in self._enabled:
                raise PluginDisabled(f"{descriptor.name}已停用，不能提交新生成任务；已有任务和历史仍可访问")

    def set_enabled(self, plugin_id: str, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            descriptor = self._descriptors[plugin_id]
            if enabled:
                for dependency in descriptor.dependencies:
                    self.require_enabled(dependency)
                self._enabled.add(plugin_id)
            else:
                dependents = [pid for pid in self._enabled
                              if plugin_id in self._descriptors[pid].dependencies]
                if dependents:
                    raise ValueError(f"Disable dependent plugins first: {sorted(dependents)}")
                self._enabled.discard(plugin_id)
