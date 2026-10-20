"""Text-generation request contracts, shared by HTTP adapters only."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from .storyboards import PlanningOptions

ID = r"^[0-9a-f]{32}$"


class ScriptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptRequest(ScriptBody):
    subproject_id: str = Field(pattern=ID)
    generation_type: Literal["script", "screenplay"]
    template_id: str | None = Field(default=None, max_length=120)
    template_version: StrictInt | None = Field(default=None, ge=1)
    style_ids: list[str] = Field(default_factory=list, max_length=4)
    example_refs: list[dict] = Field(default_factory=list, max_length=6)
    variables: dict[str, str] = Field(default_factory=dict)
    conflicts_confirmed: StrictBool = False
    input_request: str = Field(min_length=1, max_length=20000)
    selection_mode: Literal["recommended", "manual"] = "recommended"
    model_id: str | None = Field(default=None, max_length=100)
    thinking_level: str | None = Field(default=None, max_length=60)
    duration: str | StrictInt | None = None
    aspect_ratio: str | None = Field(default=None, max_length=120)
    target_audience: str | None = Field(default=None, max_length=4000)
    tone: str | None = Field(default=None, max_length=4000)
    characters: str | None = Field(default=None, max_length=10000)
    product_ip: str | None = Field(default=None, max_length=4000)
    constraints: str | None = Field(default=None, max_length=12000)
    parent_version_id: str | None = Field(default=None, pattern=ID)
    change_reason: str | None = Field(default=None, max_length=4000)
    storyboard_options: PlanningOptions | None = None


class ScriptSubmission(ScriptRequest):
    request_id: str = Field(pattern=ID)
    quote_signature: str | None = Field(default=None, min_length=1, max_length=256)
    warnings_confirmed: StrictBool = False
    storyboard_confirmed: StrictBool = False


class ScriptRevision(ScriptBody):
    request_id: str = Field(pattern=ID)
    text: str = Field(min_length=1, max_length=200000)
    change_reason: str = Field(min_length=1, max_length=2000)
