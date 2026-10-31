"""Prompt-enhancement skill registry and a read-only view over the existing style library.

Skills hold creative rules and structural constraints as data.  The style
library adapter reads ``skills/image-asset-generation/styles``; it does not
create a second style registry or copy style content into Python.
"""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS_ROOT = PROJECT_ROOT / "skills" / "prompt-enhancement" / "skills"
DEFAULT_STYLES_ROOT = PROJECT_ROOT / "skills" / "image-asset-generation" / "styles"

TaskType = Literal["generate", "reference_generate", "edit"]
Mode = Literal["standard", "creative"]


class SkillSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    reference_roles: list[str] = Field(default_factory=list)
    style_ids: list[str] = Field(default_factory=list)


class SkillRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preserve: list[str] = Field(min_length=1)
    forbid_additions: list[str] = Field(min_length=1)
    structure: list[str] = Field(min_length=1)
    standard: list[str] = Field(min_length=1)
    creative: list[str] = Field(min_length=1)
    edit_plan_required: bool = False
    style_default: str | None = None


class PromptSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"]
    skill_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    task_types: list[TaskType] = Field(min_length=1)
    modes: list[Mode] = Field(min_length=1)
    priority: int = 0
    selection: SkillSelection
    rules: SkillRules
    output_requirements: list[str] = Field(min_length=1)


class PromptSkillRegistry:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_SKILLS_ROOT)
        self._skills = {}
        for path in sorted(self.root.glob("*.json")):
            try:
                skill = PromptSkill.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, ValidationError) as exc:
                raise ValueError("提示词 Skill 定义无效：" + path.name) from exc
            if skill.skill_id != path.stem:
                raise ValueError("提示词 Skill 文件名与 skill_id 不一致：" + path.name)
            if skill.skill_id in self._skills:
                raise ValueError("提示词 Skill 重复：" + skill.skill_id)
            self._skills[skill.skill_id] = skill
        if not self._skills:
            raise ValueError("未找到提示词 Skill 定义")

    def list(self):
        return list(self._skills.values())

    def get(self, skill_id):
        return self._skills.get(skill_id)

    def select(self, *, task_type, mode, prompt, references, style_id=None):
        """Select the strongest matching skill with deterministic tie-breaking."""
        text = prompt.lower()
        roles = {item.get("role", "other") for item in references}
        best = None
        for skill in self._skills.values():
            if task_type not in skill.task_types or mode not in skill.modes:
                continue
            score = skill.priority
            matched = False
            if roles & set(skill.selection.reference_roles):
                score += 50
                matched = True
            if style_id and style_id in skill.selection.style_ids:
                score += 30
                matched = True
            if any(keyword.lower() in text for keyword in skill.selection.keywords):
                score += 10
                matched = True
            if not matched and skill.skill_id != "image-generation":
                continue
            if not matched:
                score += 10
            candidate = (score, skill.priority, skill.skill_id, skill)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
        if best is not None:
            return best[3]
        if task_type == "edit":
            fallback = self._skills.get("image-editing")
            if fallback:
                return fallback
        return self._skills.get("image-generation")


class PromptStyleLibrary:
    """Read-only adapter over the existing image-asset-generation style registry."""

    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_STYLES_ROOT)
        try:
            index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("无法读取图片风格索引") from exc
        entries = index.get("styles") if isinstance(index, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError("图片风格索引为空")
        self._styles = {}
        for entry in entries:
            try:
                style_id = entry["style_id"]
                data = json.loads((self.root / entry["path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("无法读取图片风格定义") from exc
            if not isinstance(data, dict) or data.get("style_id") != style_id or not data.get("name"):
                raise ValueError("图片风格定义与索引不一致：" + str(style_id))
            self._styles[style_id] = data

    def list(self):
        return [self.summary(style_id) for style_id in self._styles]

    def get(self, style_id):
        return self._styles.get(style_id)

    def summary(self, style_id):
        data = self._styles.get(style_id)
        if not data:
            return None
        selection = data.get("selection") or {}
        return {"style_id": data["style_id"], "name": data["name"],
                "category": data.get("category", ""),
                "visual_intents": list(selection.get("visual_intents") or []),
                "use_cases": list(data.get("use_cases") or [])}

    def prompt_context(self, style_id):
        data = self._styles.get(style_id)
        if not data:
            return None
        language = data.get("visual_language") or {}
        return {
            "style_id": data["style_id"],
            "name": data["name"],
            "category": data.get("category", ""),
            "visual_positioning": data.get("visual_positioning", ""),
            "visual_language": {key: list(value) for key, value in language.items() if isinstance(value, list)},
            "negative_constraints": list(data.get("negative_constraints") or []),
            "source_content_to_avoid": list(data.get("source_content_to_avoid") or []),
        }
