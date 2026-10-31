"""Prompt enhancement HTTP and LLM-output contracts; no routes or business services."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .images import Payload

MAX_PROMPT_CHARS = 4000
MAX_REFERENCE_USAGE_CHARS = 300
MAX_LIST_ITEMS = 20

EnhancementMode = Literal["standard", "creative"]
PromptTaskType = Literal["generate", "reference_generate", "edit"]
ReferenceRole = Literal[
    "edit_target", "base", "identity", "face", "full_body", "clothing",
    "product", "logo", "composition", "style", "background", "other",
]
REFERENCE_ROLES = set(ReferenceRole.__args__)


class PromptReference(BaseModel):
    """A reference image described by the caller; the service never opens the file."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=8)
    role: ReferenceRole = "other"
    label: str = Field(default="", max_length=80)
    usage: str = Field(default="", max_length=MAX_REFERENCE_USAGE_CHARS)
    artifact_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("label", "usage")
    @classmethod
    def strip_text(cls, value):
        return value.strip()


class PromptEditConstraints(BaseModel):
    """User-authored edit requirements; these override model suggestions."""

    model_config = ConfigDict(extra="forbid")

    change_targets: list[str] = Field(default_factory=list, max_length=10)
    preserve_targets: list[str] = Field(default_factory=list, max_length=10)
    forbidden_changes: list[str] = Field(default_factory=list, max_length=10)
    inherit_aspect_ratio: bool = True

    @field_validator("change_targets", "preserve_targets", "forbidden_changes")
    @classmethod
    def clean_items(cls, values):
        result = []
        for value in values:
            text = value.strip()
            if text and text not in result:
                if len(text) > MAX_REFERENCE_USAGE_CHARS:
                    raise ValueError("修改要求单条不能超过 300 个字符")
                result.append(text)
        return result


class PromptEnhancementRequest(Payload):
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    mode: EnhancementMode = "standard"
    task_type: PromptTaskType = "generate"
    skill_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    style_id: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    aspect_ratio: Literal["auto", "1:1", "3:2", "2:3"] = "auto"
    references: list[PromptReference] = Field(default_factory=list, max_length=8)
    edit: PromptEditConstraints | None = None

    @field_validator("prompt")
    @classmethod
    def require_visible_text(cls, value):
        text = value.strip()
        if not text:
            raise ValueError("创作需求不能为空")
        return text

    @model_validator(mode="after")
    def validate_reference_roles(self):
        if len({item.index for item in self.references}) != len(self.references):
            raise ValueError("参考图片序号不能重复")
        if len(self.references) > 1:
            missing = [item.index for item in self.references if not item.usage and item.role == "other"]
            if missing:
                raise ValueError("多张参考图片需分别标明用途，缺少用途的序号：" + "、".join(map(str, missing)))
        if self.task_type == "edit" and not any(item.role in {"edit_target", "base"} for item in self.references):
            raise ValueError("修改图片需要明确一张基底图片（角色 edit_target）")
        if self.edit is not None and self.task_type != "edit":
            raise ValueError("只有修改图片任务可以填写修改对象与保留对象")
        return self


class EditPlanReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=8)
    role: str = Field(min_length=1, max_length=40)
    usage: str = Field(default="", max_length=MAX_REFERENCE_USAGE_CHARS)


class EditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_targets: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    preserve_targets: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    forbidden_changes: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    reference_roles: list[EditPlanReference] = Field(default_factory=list, max_length=8)
    inherit_aspect_ratio: bool = True


class EnhancementDraft(BaseModel):
    """The single JSON object requested from the LLM. Unknown keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    enhanced_prompt: str = Field(min_length=1, max_length=12000)
    intent_summary: str = Field(default="", max_length=500)
    preserved_user_requirements: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    added_details: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    negative_constraints: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    edit_plan: EditPlan | None = None

    @field_validator("enhanced_prompt")
    @classmethod
    def require_prompt(cls, value):
        text = value.strip()
        if not text:
            raise ValueError("强化结果不能为空")
        return text

    @field_validator("intent_summary")
    @classmethod
    def strip_summary(cls, value):
        return value.strip()

    @field_validator("preserved_user_requirements", "added_details", "negative_constraints")
    @classmethod
    def clean_items(cls, values):
        result = []
        for value in values:
            text = value.strip()
            if text and text not in result:
                result.append(text[:500])
        return result


class SkillRef(BaseModel):
    skill_id: str
    name: str
    version: int


class StyleRef(BaseModel):
    style_id: str
    name: str
    category: str = ""


class UsageInfo(BaseModel):
    provider: str
    model: str
    is_mock: bool
    calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    input_chars: int = 0
    output_chars: int = 0
    elapsed_ms: int = 0
    estimated_cost: float | None = None
    currency: str | None = None


class FallbackInfo(BaseModel):
    code: str
    message: str


class SourceInfo(BaseModel):
    kind: Literal["prompt_enhancement"] = "prompt_enhancement"
    enhancement_id: str
    skill_id: str
    skill_version: int
    mode: str
    task_type: str


class PromptEnhancementResponse(BaseModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    enhancement_id: str
    request_id: str
    status: Literal["enhanced", "fallback"]
    mode: str
    task_type: str
    skill: SkillRef
    style: StyleRef | None = None
    original_prompt: str
    enhanced_prompt: str
    intent_summary: str = ""
    preserved_user_requirements: list[str] = Field(default_factory=list)
    added_details: list[str] = Field(default_factory=list)
    negative_constraints: list[str] = Field(default_factory=list)
    edit_plan: EditPlan | None = None
    references: list[EditPlanReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fallback: FallbackInfo | None = None
    usage: UsageInfo
    source: SourceInfo
