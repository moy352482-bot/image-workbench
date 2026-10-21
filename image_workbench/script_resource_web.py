"""Authenticated creative resource endpoints, separate from model execution routes."""
import json
from typing import Literal
from urllib.parse import quote

from fastapi import File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field, StrictBool, StrictInt
from starlette.concurrency import run_in_threadpool

from .script_resources import Schema, MAX_UPLOAD_BYTES
from .script_engine import recommend


class Empty(Schema):
    pass


class ExamplePatch(Schema):
    expected_version: StrictInt = Field(ge=1)
    name: str | None = Field(default=None, max_length=180)
    enabled: StrictBool | None = None
    reference_roles: list[str] | None = Field(default=None, max_length=5)
    analysis: dict | None = None


class Recommendation(Schema):
    input_request: str = Field(min_length=1, max_length=20000)
    generation_type: Literal["script", "screenplay"]


class DraftRequest(Schema):
    source_type: Literal["existing_artifact", "uploaded_file", "manual"]
    type: Literal["script", "screenplay"]
    extraction_method: Literal["structure", "ai"] = "ai"
    task_id: str | None = Field(default=None, max_length=64)
    example_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class TemplateSave(Schema):
    draft_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmed: StrictBool
    template: dict


class TemplatePatch(Schema):
    expected_version: StrictInt = Field(ge=1)
    template: dict


class CopyRequest(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=120)


def install_script_resources(app, get_service):
    def context(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        return get_service().resources, session["account_id"], request.scope["workspace_id"]

    @app.get("/api/scripts/examples")
    def examples(request: Request, subproject_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$")):
        resources, aid, wid = context(request)
        return {"examples": resources.list(aid, wid, "example", subproject_id=subproject_id)}

    @app.post("/api/scripts/examples")
    async def upload_example(request: Request, file: UploadFile = File(), subproject_id: str = Form(),
                             generation_type: str = Form(), reference_roles: str = Form('["comprehensive"]')):
        resources, aid, wid = context(request)
        try:
            data = await file.read(MAX_UPLOAD_BYTES + 1)
            try:
                selected = json.loads(reference_roles)
            except (TypeError, ValueError):
                raise ValueError("案例用途需为 JSON 数组") from None
            return await run_in_threadpool(resources.upload, aid, wid, subproject_id, generation_type,
                                          file.filename, data, selected)
        finally:
            await file.close()

    @app.get("/api/scripts/examples/{identifier}")
    def example_detail(identifier: str, request: Request, version: int | None = Query(default=None, ge=1)):
        resources, aid, wid = context(request)
        return resources.get(aid, wid, "example", identifier, version)

    @app.get("/api/scripts/examples/{identifier}/download")
    def download_example(identifier: str, request: Request, version: int | None = Query(default=None, ge=1)):
        resources, aid, wid = context(request)
        raw, filename = resources.download(aid, wid, identifier, version)
        return Response(raw, media_type="application/octet-stream", headers={
            "Content-Disposition": f"attachment; filename=\"example\"; filename*=UTF-8''{quote(filename, safe='')}",
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/scripts/examples/{identifier}/analyze")
    def analyze_example(identifier: str, body: Empty, request: Request):
        resources, aid, wid = context(request)
        return resources.analyze(aid, wid, identifier)

    @app.patch("/api/scripts/examples/{identifier}")
    def update_example(identifier: str, body: ExamplePatch, request: Request):
        resources, aid, wid = context(request)
        return resources.update_example(aid, wid, identifier, body.model_dump(exclude_none=True))

    @app.post("/api/scripts/recommend")
    def recommendation(body: Recommendation, request: Request):
        resources, aid, wid = context(request)
        return recommend(get_service().templates, resources, aid, wid, body.model_dump())

    @app.post("/api/scripts/template-drafts")
    def draft(body: DraftRequest, request: Request):
        resources, aid, wid = context(request)
        return resources.create_draft(aid, wid, body.model_dump(exclude_none=True), get_service())

    @app.get("/api/scripts/templates")
    def personal_templates(request: Request, include_disabled: bool = False):
        resources, aid, wid = context(request)
        return {"templates": resources.list(aid, wid, "personal", include_disabled=include_disabled)}

    @app.post("/api/scripts/templates")
    def save_template(body: TemplateSave, request: Request):
        resources, aid, wid = context(request)
        return resources.save_template(aid, wid, body.model_dump())

    @app.get("/api/scripts/templates/{identifier}")
    def template_detail(identifier: str, request: Request):
        resources, aid, wid = context(request)
        return resources.template_detail(aid, wid, identifier)

    @app.get("/api/scripts/templates/{identifier}/versions/{version}")
    def template_version(identifier: str, version: int, request: Request):
        resources, aid, wid = context(request)
        return resources.get(aid, wid, "personal", identifier, version)

    @app.patch("/api/scripts/templates/{identifier}")
    def update_template(identifier: str, body: TemplatePatch, request: Request):
        resources, aid, wid = context(request)
        return resources.update_template(aid, wid, identifier, body.model_dump())

    @app.post("/api/scripts/templates/{identifier}/copy")
    def copy_template(identifier: str, body: CopyRequest, request: Request):
        resources, aid, wid = context(request)
        return resources.copy_template(aid, wid, identifier, body.name)
