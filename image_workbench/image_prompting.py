"""Default image Skill and optional visual styles; no model calls or transactions."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "image-generation-default"


class ImagePromptCompiler:
    def __init__(self, root=SKILL_ROOT):
        root = Path(root)
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        self.rules = skill.split("---", 2)[2].strip()
        catalog = json.loads((root / "styles.json").read_text(encoding="utf-8"))
        self.skill_id = catalog["skill_id"]
        self.version = catalog["version"]
        self.styles = {item["id"]: item for item in catalog["styles"]}
        self.digest = hashlib.sha256((skill + json.dumps(catalog, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()

    def style(self, style_id=None):
        if style_id in (None, ""):
            return None
        if not isinstance(style_id, str) or style_id not in self.styles:
            raise ValueError("图片风格不存在，请重新选择")
        return self.styles[style_id]

    def catalog(self):
        styles = deepcopy(list(self.styles.values()))
        for item in styles:
            item["preview_url"] = f"/static/image-styles/{item.pop('preview')}?v={self.version}"
        return {"skill_id": self.skill_id, "version": self.version, "default_enabled": True, "styles": styles}

    def compile(self, prompt, *, operation="generate", style_id=None, aspect_ratio="auto", reference_count=0):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("请填写创作需求")
        if operation not in {"generate", "edit"} or aspect_ratio not in {"auto", "1:1", "3:2", "2:3"}:
            raise ValueError("无效模式或比例")
        if isinstance(reference_count, bool) or not isinstance(reference_count, int) or not 0 <= reference_count <= 8:
            raise ValueError("最多引用8张参考图片")
        style = self.style(style_id)
        images = []
        if operation == "edit":
            images.append({"image": 1, "role": "编辑基底；只修改本次要求的部分，其余保持"})
        for index in range(reference_count):
            images.append({"image": len(images) + 1, "role": f"参考图 {index + 1}；按用户描述使用，没有明确身份要求时不擅自替换主体"})
        spec = {
            "operation": operation,
            "primary_request": prompt,
            "aspect_ratio": aspect_ratio,
            "style": {"id": style["id"], "name": style["name"], "guidance": style["prompt"]} if style else None,
            "input_images": images,
        }
        compiled = (f"Image Workbench · {self.skill_id} · {self.version}\n\n{self.rules}\n\n"
                    "## 本次视觉需求（JSON 数据）\n" + json.dumps(spec, ensure_ascii=False, indent=2))
        return {"skill_id": self.skill_id, "skill_version": self.version, "skill_sha256": self.digest,
                "style_id": style["id"] if style else None, "style_name": style["name"] if style else None,
                "source_prompt": prompt, "compiled_prompt": compiled, "spec": spec}


@lru_cache(maxsize=1)
def default_image_skill():
    return ImagePromptCompiler()


def execution_prompt(task, options=None, *, reference_count=0):
    """Reuse a saved snapshot; legacy tasks receive the default Skill at execution."""
    snapshot = (options or {}).get("image_prompt")
    if snapshot is not None:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("compiled_prompt"), str) or not snapshot["compiled_prompt"].strip():
            raise ValueError("任务的图片提示词快照无效")
        return snapshot["compiled_prompt"]
    return default_image_skill().compile(task.prompt, operation=task.operation,
                                        aspect_ratio=(options or {}).get("aspect_ratio", "auto"),
                                        reference_count=reference_count)["compiled_prompt"]
