"""Snapshot the externally visible routes and validation schemas, without requests."""
from pathlib import Path
import tempfile

from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from image_workbench import StorageConfig, StorageManager
from image_workbench.remote_access import RemoteAccess
from image_workbench.web import create_app


def routes(app):
    result = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        parameters = {}
        for location in ("path", "query", "body"):
            fields = getattr(route.dependant, location + "_params")
            parameters[location] = [
                {"name": field.alias, "required": field.field_info.is_required(),
                 "schema": TypeAdapter(field.field_info.annotation).json_schema(),
                 **({} if field.field_info.is_required() else {"default": field.default})}
                for field in fields
            ]
        for method in sorted(route.methods):
            key = method + " " + route.path
            if key in result:
                raise AssertionError("Duplicate route: " + key)
            result[key] = {"parameters": parameters, "status_code": route.status_code}
    return result


def snapshot():
    with tempfile.TemporaryDirectory() as folder:
        storage = StorageManager(StorageConfig(Path(folder) / "data"))
        app = create_app(storage, backend="local", remote_access=RemoteAccess("https://example.test", "x" * 40))
        return {"account": routes(app.auth), "workspace": routes(app.worker_app)}
