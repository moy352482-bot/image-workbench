"""Prompt enhancement HTTP routes; no image generation, storage or credit changes."""
from fastapi import HTTPException, Request

from ..contracts.prompt_enhancements import PromptEnhancementRequest


def install_routes(app, get_service):
    def owner(request):
        session = request.scope.get("account_session")
        if not session:
            raise HTTPException(401, "请先登录")
        return session["account_id"], request.scope["workspace_id"]

    @app.post("/api/prompt-enhancements")
    def enhance(body: PromptEnhancementRequest, request: Request):
        return get_service().enhance(*owner(request), body.model_dump(exclude_none=True))
