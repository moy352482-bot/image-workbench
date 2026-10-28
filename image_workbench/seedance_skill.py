"""Seedance 2.0 prompt planning through a text port, before video submission."""
import hashlib
import json
from pathlib import Path
import re

from .atomic_files import read_json, write_json
from .deepseek_text import MODEL
from .domain_errors import TextGenerationError


ROOT = Path(__file__).resolve().parents[1]
TASK_FIELDS = ("mode", "duration", "resolution", "aspect_ratio", "width", "height", "reference_ids")
PROMPT_FORMAT = "seedance-2-v1"


class SeedanceSkillPlanner:
    def __init__(self, adapter, *, root=ROOT):
        self.adapter = adapter
        self.skill = (Path(root) / "skills/seedance-2-video/SKILL.md").read_text(encoding="utf-8")
        if not self.skill.strip():
            raise ValueError("SD2.0 内置 Skill 为空")

    def request(self, task):
        inputs = {key: task[key] for key in (*TASK_FIELDS, "prompt")}
        if inputs["mode"] != "T2V" or inputs["reference_ids"] or inputs["duration"] not in {5, 10, 15} or inputs["resolution"] != "720p":
            raise ValueError("SD2.0 当前通道仅支持 720p 文生视频，时长 5/10/15 秒")
        return {"model_id": MODEL, "thinking_level": "high", "prompt_format": PROMPT_FORMAT,
                "skill_sha256": hashlib.sha256(self.skill.encode()).hexdigest(), "inputs": inputs,
                "prompt": self.skill + "\n\n已确认的镜头数据：\n" + json.dumps(inputs, ensure_ascii=False)}

    @staticmethod
    def validate(text, request):
        try:
            plan = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ValueError("SD2.0 Skill 未返回有效 JSON") from exc
        if (not isinstance(plan, dict) or set(plan) != {"schema_version", "prompt", *TASK_FIELDS}
                or type(plan["schema_version"]) is not int or plan["schema_version"] != 1):
            raise ValueError("SD2.0 Skill 输出结构无效")
        for key in TASK_FIELDS:
            expected = request["inputs"][key]
            if type(plan[key]) is not type(expected) or plan[key] != expected:
                raise ValueError("SD2.0 Skill 不能改变已确认的参数或参考图顺序")
        if not isinstance(plan["prompt"], str) or not 1 <= len(plan["prompt"].strip()) <= 6000:
            raise ValueError("SD2.0 视频提示词为空或过长")
        spoken = re.findall(r'[“「"]([^”」"\n]{2,})[”」"]', request["inputs"]["prompt"])
        if any(line not in plan["prompt"] for line in spoken):
            raise ValueError("SD2.0 Skill 不能删除或改写引号中的原文")
        return plan

    def run(self, request, work_dir):
        work_dir = Path(work_dir)
        options = {"prompt": request["prompt"], "model_id": request["model_id"],
                   "thinking_level": request["thinking_level"], "work_dir": work_dir}
        marker = work_dir / "invocation.json"
        if marker.exists():
            if read_json(marker) != request:
                raise TextGenerationError("SD2.0 Skill 调用检查点不匹配", uncertain=True)
            try:
                text = self.adapter.recover(**options)
            except (OSError, ValueError) as exc:
                raise TextGenerationError("提示词完成凭据尚未确认，不会重新调用 DeepSeek", uncertain=True) from exc
        else:
            write_json(marker, request)
            text = self.adapter.generate(**options)
        return self.validate(text, request)
