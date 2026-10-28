"""Strict HTTP contracts for storyboard planning and version mutations."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

ID = r"^[0-9a-f]{32}$"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanningOptions(Body):
    mode: Literal["mock", "codex", "api"] = "mock"
    selection_mode: Literal["recommended", "manual"] = "recommended"
    model_id: str | None = Field(default=None, max_length=100)
    thinking_level: str | None = Field(default=None, max_length=100)
    target_duration_ms: StrictInt = Field(default=60000, ge=1000, le=3600000)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    visual_style: str = Field(default="", max_length=12000)
    characters: str = Field(default="", max_length=12000)
    scenes: str = Field(default="", max_length=12000)
    requirements: str = Field(default="", max_length=12000)
    reference_artifact_ids: list[str] = Field(default_factory=list, max_length=9)
    video_model_id: Literal["h3", "online-2.0"] = "h3"
    video_project_id: str | None = Field(default=None, pattern=ID)


class PlanRequest(PlanningOptions):
    source_script_version: str = Field(pattern=ID)


class Submission(PlanRequest):
    request_id: str = Field(pattern=ID)
    quote_signature: str = Field(min_length=1, max_length=256)
    confirmed: StrictBool = False


class Edit(Body):
    request_id: str = Field(pattern=ID)
    expected_version: StrictInt = Field(ge=1)
    operation: Literal["add", "update", "delete", "split", "merge", "reorder", "save", "approve", "revision_required"]
    shot_id: str | None = Field(default=None, pattern=ID)
    shot_ids: list[str] = Field(default_factory=list, max_length=200)
    changes: dict = Field(default_factory=dict)
    parts: list[dict] | None = Field(default=None, min_length=2, max_length=20)
    reason: str = Field(default="手动编辑", max_length=2000)
    target_duration_ms: StrictInt | None = Field(default=None, ge=1000, le=3600000)


class Replan(PlanningOptions):
    expected_version: StrictInt = Field(ge=1)
    shot_ids: list[str] = Field(default_factory=list, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class ReplanSubmission(Replan):
    request_id: str = Field(pattern=ID)
    quote_signature: str = Field(min_length=1, max_length=256)
    confirmed: StrictBool = False


class Accept(Body):
    request_id: str = Field(pattern=ID)
    task_id: str = Field(pattern=ID)
    expected_version: StrictInt = Field(ge=1)


class VideoDraft(Body):
    request_id: str = Field(pattern=ID)
    expected_version: StrictInt = Field(ge=1)
    resolution: Literal["720p", "1080p"] = "720p"
    allow_draft_test: StrictBool = False
