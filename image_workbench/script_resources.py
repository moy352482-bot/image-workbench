"""Private, immutable creative resources; source documents are data, never commands."""
from __future__ import annotations

import copy
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
from typing import Literal
from uuid import uuid4
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .locking import FileLock
from .storage import _now, _validate_id
from .domain_errors import ConflictError
from .workspace_scope import WorkspaceProjects

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 200000
ROLES = frozenset({"structure", "style", "dialogue", "format", "comprehensive"})
RESOURCE_ID = re.compile(r"^(example|personal)_[0-9a-f]{32}$")


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Section(Schema):
    section: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=1000)


class Variable(Schema):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z_0-9]{0,63}$")
    label: str = Field(default="", max_length=100)
    default: str = Field(default="", max_length=2000)


class TemplateContent(Schema):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    type: Literal["script", "screenplay"]
    structure: list[Section] = Field(min_length=1, max_length=40)
    required_fields: list[str] = Field(default_factory=lambda: ["user_request"], max_length=30)
    optional_fields: list[str] = Field(default_factory=list, max_length=30)
    variables: list[Variable] = Field(default_factory=list, max_length=30)
    style_guidance: list[str] = Field(default_factory=list, max_length=30)
    format_rules: list[str] = Field(default_factory=list, max_length=30)
    prompt_guidance: list[str] = Field(default_factory=list, max_length=30)
    enabled: StrictBool = True


class Analysis(Schema):
    content_type: Literal["script", "screenplay"]
    structure: str = Field(max_length=1000)
    scene_count: int | None = Field(default=None, ge=0, le=10000)
    character_count: int | None = Field(default=None, ge=0, le=10000)
    dialogue_style: str = Field(max_length=1000)
    emotional_tone: list[str] = Field(default_factory=list, max_length=15)
    emotional_progression: str = Field(default="", max_length=1000)
    pacing: str = Field(max_length=1000)
    conflict_style: str = Field(default="", max_length=1000)
    narrative_perspective: str = Field(default="", max_length=1000)
    visual_style: list[str] = Field(default_factory=list, max_length=15)
    format_features: list[str] = Field(default_factory=list, max_length=20)
    reusable_elements: list[str] = Field(default_factory=list, max_length=20)
    recommended_templates: list[str] = Field(default_factory=list, max_length=10)
    recommended_styles: list[str] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=15)


def checked(model, value):
    try:
        result = model.model_validate(value).model_dump()
        if len(json.dumps(result, ensure_ascii=False)) > 50000:
            raise ValueError("结构化内容过长")
        for key in ("required_fields", "optional_fields", "style_guidance", "format_rules", "prompt_guidance"):
            if any(not s.strip() or len(s) > 2000 for s in result.get(key, [])):
                raise ValueError("规则或字段为空或过长")
        if model is TemplateContent:
            names = [item["name"] for item in result["variables"]]
            if len(names) != len(set(names)):
                raise ValueError("模板变量名重复")
            known = {"user_request", "input_request", "duration", "video_duration", "aspect_ratio",
                     "target_audience", "tone", "characters", "product_ip", "constraints", *names}
            if any(key not in known for key in result["required_fields"] + result["optional_fields"]):
                raise ValueError("字段需使用已支持参数或已定义的模板变量")
        return result
    except ValidationError as exc:
        # Do not include full source text or model output in error logs.
        raise ValueError("结构化内容不符合 Schema，请检查字段类型、长度和必填项") from None


def roles(value):
    if not isinstance(value, list) or not value or len(value) > 5 or any(not isinstance(v, str) or v not in ROLES for v in value):
        raise ValueError("请选择有效的案例用途")
    return sorted(set(value))


def extract_document(filename, raw):
    """Bound file type, expanded DOCX size, PDF pages and extracted text."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_UPLOAD_BYTES:
        raise ValueError("上传文件需为 1 字节至 5 MiB")
    name = str(filename).replace("\\", "/").split("/")[-1]
    if not name or len(name) > 180 or any(ord(c) < 32 for c in name):
        raise ValueError("文件名无效")
    suffix = PurePosixPath(name).suffix.lower()
    try:
        if suffix == ".txt":
            text = raw.decode("utf-8-sig")
        elif suffix == ".docx":
            from docx import Document
            with ZipFile(BytesIO(raw)) as archive:
                entries = archive.infolist()
                if (len(entries) > 500 or sum(e.file_size for e in entries) > 20 * 1024 * 1024
                        or any(e.file_size > 5 * 1024 * 1024 or e.flag_bits & 1 for e in entries)
                        or "word/document.xml" not in archive.namelist()):
                    raise ValueError("DOCX 内容过大、加密或结构无效")
                for entry in entries:
                    if entry.filename.endswith((".xml", ".rels")):
                        data = archive.read(entry)
                        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                            raise ValueError("DOCX 不允许文档实体声明")
            document = Document(BytesIO(raw))
            parts = []
            for block in document.iter_inner_content():
                if hasattr(block, "rows"):
                    parts.extend(" | ".join(cell.text for cell in row.cells) for row in block.rows)
                else:
                    parts.append(block.text)
            text = "\n".join(parts)
        elif suffix == ".pdf":
            from pypdf import PdfReader
            if not raw.startswith(b"%PDF-"):
                raise ValueError("文件不是有效 PDF")
            reader = PdfReader(BytesIO(raw), strict=True)
            if reader.is_encrypted or not 0 < len(reader.pages) <= 100:
                raise ValueError("PDF 不支持加密，且最多 100 页")
            parts, total = [], 0
            for page in reader.pages:
                value = page.extract_text() or ""
                total += len(value)
                if total > MAX_TEXT_CHARS:
                    raise ValueError("提取文本超过 200000 字符")
                parts.append(value)
            text = "\n".join(parts)
        else:
            raise ValueError("只支持 TXT、DOCX、PDF")
    except ValueError:
        raise
    except Exception:
        raise ValueError("无法读取文档；TXT 需 UTF-8，DOCX/PDF 需可读取且未加密") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError("文档无可读取文本或超过 200000 字符；扫描 PDF 请先转为文字")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise ValueError("文档包含无效控制字符")
    return name, suffix, text


class RuleExampleAnalyzer:
    """Offline fallback. Estimates remain editable, and are never labelled AI facts."""
    provider = "python_rules"

    def analyze(self, text, generation_type):
        scenes = re.findall(r"(?m)^\s*(?:\d+[-－]\d+|第[一二三四五六七八九十\d]+场|场景\s*\d+)", text)
        characters = re.findall(r"(?m)^人物[：:]\s*(.{1,100})$", text)
        has_dialogue = bool(re.search(r"(?m)^[^\n：:]{1,12}[：:]", text))
        return {
            "content_type": generation_type, "structure": "分场组织" if scenes else "段落推进",
            "scene_count": len(scenes) if scenes else None,
            "character_count": len(set(re.split(r"[、,，/ ]+", "、".join(characters)))) if characters else None,
            "dialogue_style": "包含人物或旁白标记，具体语气需人工确认" if has_dialogue else "未识别明确对白标记",
            "emotional_tone": [], "emotional_progression": "需人工确认情绪起点、转折与结尾",
            "pacing": "按场次推进" if scenes else "按段落推进",
            "conflict_style": "需人工确认人物目标与阻碍",
            "narrative_perspective": "需人工确认",
            "visual_style": [], "format_features": ["分场编号"] if scenes else ["自然段"],
            "reusable_elements": ["场景组织", "动作与声音分离"] if scenes else ["开场、推进、结尾"],
            "recommended_templates": [], "recommended_styles": [],
            "limitations": ["免费规则结果，不是语义模型结论；数量仅统计可识别标记", "请审核并修改后确认"],
        }

    def extract_template(self, text, generation_type):
        """Abstract roles, never copy names, story beats or unique dialogue."""
        sections = [
            {"section": "opening", "purpose": "建立人物状态、环境与核心目标"},
            {"section": "development", "purpose": "用可见事件推进目标，呈现阻碍及人物选择"},
            {"section": "turning_point", "purpose": "揭示目标与现实的冲突，让选择产生后果"},
            {"section": "ending", "purpose": "通过行动和视觉变化完成情绪表达"},
        ]
        if generation_type == "script":
            sections = [
                {"section": "opening", "purpose": "用具体情境或问题引出主题"},
                {"section": "development", "purpose": "用事件与可见证据展开核心信息"},
                {"section": "ending", "purpose": "回扣主题，明确结尾情绪或行动"},
            ]
        analysis = self.analyze(text, generation_type)
        # Preserve observable organization without preserving story-specific prose.
        headings = {
            "主题": "概括本次内容的核心主题", "核心创意": "说明创意如何回应目标受众",
            "开场": "建立人物、环境与核心目标", "发展": "通过事件推进目标并呈现阻碍",
            "转折": "让新信息或人物选择改变事件方向", "结尾": "通过行动完成主题与情绪表达",
            "旁白": "用简洁声音补充画面无法表达的信息", "对白": "通过角色意图推动情节",
            "画面": "写明可见事件与人物行为", "人物": "说明角色目标、关系与行为动机",
        }
        observed = []
        for line in text.splitlines():
            match = re.match(r"^\s*(?:[一二三四五六七八九十\d]+[、.．]\s*)?(" + "|".join(headings) + r")\s*(?:[：:]|$)", line)
            if match and match[1] not in observed:
                observed.append(match[1])
        if len(observed) >= 2:
            sections = [{"section": heading, "purpose": headings[heading]} for heading in observed]
        elif analysis["scene_count"]:
            sections = [{"section": f"场次 {number + 1}", "purpose": "标明时空与出场人物，用动作、对白和事件后果推进核心目标"}
                        for number in range(min(analysis["scene_count"], 20))]
        elif text.strip():
            paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
            if 2 <= len(paragraphs) <= 20:
                sections = [{"section": f"段落 {number + 1}", "purpose":
                             "建立内容主题、人物状态或具体情境" if number == 0 else
                             "回扣核心主题并完成情绪表达" if number == len(paragraphs) - 1 else
                             "加入新的事件、信息或人物选择，推进内容"}
                            for number in range(len(paragraphs))]
        return {"name": "我的分场剧本模板" if generation_type == "screenplay" else "我的视频脚本模板",
                "description": "按可识别栏目、场次或段落提炼；无法识别时提供基础结构，请按作品审核。",
                "type": generation_type, "structure": sections, "required_fields": ["user_request"],
                "optional_fields": ["duration", "characters", "target_audience"],
                "variables": [{"name": n, "label": label, "default": ""} for n, label in
                              [("protagonist", "主角"), ("location", "地点"), ("goal", "核心目标"),
                               ("conflict", "冲突事件"), ("turning_point", "关键转折"), ("ending_emotion", "结尾情绪")]],
                "style_guidance": ["通过人物行动表达情绪，不沿用原作品人名与独特情节"],
                "format_rules": analysis["format_features"],
                "prompt_guidance": ["每一段都推进目标或带来新信息；变量以用户本次输入为准"], "enabled": True}


class MockExampleAnalyzer(RuleExampleAnalyzer):
    """Injectable test double; no network or billing behavior."""
    provider = "mock"

    def __init__(self, *, analysis=None, template=None, error=None):
        self.analysis, self.template, self.error = analysis, template, error

    def analyze(self, text, generation_type):
        if self.error:
            raise ValueError("分析失败，原始文件已保留，可手动创建模板")
        return copy.deepcopy(self.analysis) if self.analysis is not None else super().analyze(text, generation_type)

    def extract_template(self, text, generation_type):
        if self.error:
            raise ValueError("分析失败，原始文件已保留，可手动创建模板")
        return copy.deepcopy(self.template) if self.template is not None else super().extract_template(text, generation_type)


class ScriptResourceStore:
    def __init__(self, storage, identity, *, analyzer=None):
        self.storage, self.identity = storage, identity
        self.analyzer = analyzer or RuleExampleAnalyzer()

    def _files(self, aid, wid):
        self.identity.require_workspace(aid, wid)
        files = self.storage.files.scope(".workbench", "script-resources", _validate_id(aid), _validate_id(wid))
        files.root.mkdir(parents=True, exist_ok=True)
        return files

    def _scope(self, aid, wid):
        return self._files(aid, wid).root

    def _folder(self, aid, wid, kind, identifier):
        if not isinstance(identifier, str) or not RESOURCE_ID.fullmatch(identifier) or not identifier.startswith(kind + "_"):
            raise FileNotFoundError("资源不存在")
        return self._files(aid, wid).path(identifier)

    def _lock(self, aid, wid):
        return FileLock(self._scope(aid, wid) / "resources.lock", timeout=30)

    def get(self, aid, wid, kind, identifier, version=None):
        folder = self._folder(aid, wid, kind, identifier)
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError("资源版本必须为正整数")
        files = self._files(aid, wid)
        head = files.read_json(folder / "latest.json")
        record = files.read_json(folder / f"v{version or head['version']}.json")
        if record.get("owner_account_id") != aid or record.get("workspace_id") != wid:
            raise FileNotFoundError("资源不存在")
        return record

    def _write(self, aid, wid, kind, identifier, value):
        folder = self._folder(aid, wid, kind, identifier)
        folder.mkdir(exist_ok=True)
        files = self._files(aid, wid)
        path = files.check(folder / f"v{value['version']}.json")
        if path.exists():
            raise ConflictError("不能覆盖已有资源版本")
        files.write_json(path, value)
        files.write_json(folder / "latest.json", {"schema_version": 1, "version": value["version"]})
        return copy.deepcopy(value)

    def list(self, aid, wid, kind, *, include_disabled=False, subproject_id=None):
        if subproject_id:
            WorkspaceProjects(self.storage, self.identity, aid, wid).get_subproject(subproject_id)
        rows = []
        for folder in self._scope(aid, wid).glob(kind + "_*"):
            if not (folder / "latest.json").is_file():
                continue
            row = self.get(aid, wid, kind, folder.name)
            if (include_disabled or row.get("enabled")) and (not subproject_id or row.get("subproject_id") == subproject_id):
                rows.append({k: v for k, v in row.items() if k != "text"})
        return sorted(rows, key=lambda v: (v["updated_at"], v["version"]), reverse=True)

    def upload(self, aid, wid, sid, generation_type, filename, raw, reference_roles):
        sub = WorkspaceProjects(self.storage, self.identity, aid, wid).get_subproject(sid)
        if generation_type not in {"script", "screenplay"}:
            raise ValueError("内容类型无效")
        selected_roles = roles(reference_roles)
        name, suffix, text = extract_document(filename, raw)
        identifier = "example_" + uuid4().hex
        with self._lock(aid, wid):
            folder = self._folder(aid, wid, "example", identifier)
            folder.mkdir()
            with self._files(aid, wid).check(folder / ("original" + suffix)).open("xb") as output:
                output.write(raw)
            value = {"schema_version": 1, "example_id": identifier, "version": 1, "workspace_id": wid,
                     "owner_account_id": aid, "main_project_id": sub.main_project_id, "subproject_id": sid,
                     "name": name, "filename": name, "generation_type": generation_type, "reference_roles": selected_roles,
                     "enabled": True, "sha256": hashlib.sha256(raw).hexdigest(), "text": text, "analysis": None,
                     "analysis_confirmed": False, "created_at": _now(), "updated_at": _now()}
            return self._write(aid, wid, "example", identifier, value)

    def download(self, aid, wid, identifier, version=None):
        row = self.get(aid, wid, "example", identifier, version)
        suffix = PurePosixPath(row["filename"]).suffix.lower()
        if suffix not in {".txt", ".docx", ".pdf"}:
            raise ValueError("文件类型不匹配")
        raw = self._files(aid, wid).check(self._folder(aid, wid, "example", identifier) / ("original" + suffix)).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("原文件校验失败")
        return raw, row["filename"]

    def analyze(self, aid, wid, identifier):
        row = self.get(aid, wid, "example", identifier)
        result = checked(Analysis, self.analyzer.analyze(row["text"][:12000], row["generation_type"]))
        if len(row["text"]) > 12000:
            result["limitations"].append("本次仅分析前 12000 字符，不能代表完整作品")
        return {"analysis": result, "provider": self.analyzer.provider, "requires_confirmation": True,
                "example_id": identifier, "example_version": row["version"]}

    def update_example(self, aid, wid, identifier, body):
        with self._lock(aid, wid):
            row = self.get(aid, wid, "example", identifier)
            if type(body.get("expected_version")) is not int or body["expected_version"] != row["version"]:
                raise ConflictError("案例已变化，请刷新后编辑")
            if set(body) - {"expected_version", "analysis", "reference_roles", "enabled", "name"}:
                raise ValueError("不支持的案例修改字段")
            if "analysis" in body:
                row["analysis"] = checked(Analysis, body["analysis"])
                row["analysis_confirmed"] = True
            if "reference_roles" in body:
                row["reference_roles"] = roles(body["reference_roles"])
            if "enabled" in body:
                if type(body["enabled"]) is not bool:
                    raise ValueError("enabled 必须为布尔值")
                row["enabled"] = body["enabled"]
            if "name" in body:
                if not isinstance(body["name"], str) or not 0 < len(body["name"].strip()) <= 180:
                    raise ValueError("名称必须为 1–180 字符")
                row["name"] = body["name"].strip()
            row.update(version=row["version"] + 1, updated_at=_now())
            return self._write(aid, wid, "example", identifier, row)

    def create_draft(self, aid, wid, body, service):
        self._scope(aid, wid)
        source, method, kind = body.get("source_type"), body.get("extraction_method", "ai"), body.get("type")
        if source not in {"manual", "existing_artifact", "uploaded_file"} or method not in {"structure", "ai"} or kind not in {"script", "screenplay"}:
            raise ValueError("模板来源、类型或提取方式无效")
        text = ""
        provenance = {"source_type": source, "source_artifact_id": None, "source_version": None, "source_example_id": None}
        if source == "existing_artifact":
            job = service._authorized(aid, wid, body.get("task_id"))
            if job["status"] != "completed" or job["generation_type"] != kind:
                raise ValueError("请选择已完成且类型相符的脚本或剧本")
            text = service._read_artifact(job["output_artifact_id"])[0].decode("utf-8")
            provenance.update(source_artifact_id=job["output_artifact_id"], source_version=job["output_version"])
        elif source == "uploaded_file":
            example = self.get(aid, wid, "example", body.get("example_id"))
            if not example["enabled"]:
                raise ValueError("案例已停用")
            text = example["text"]
            provenance.update(source_example_id=example["example_id"], source_version=example["version"])
        analyzer = self.analyzer if method == "ai" and source != "manual" else RuleExampleAnalyzer()
        content = checked(TemplateContent, analyzer.extract_template(text[:12000], kind))
        self._abstract_only(content, text[:12000])
        for key in ("name", "description"):
            if body.get(key):
                content[key] = body[key]
        content = checked(TemplateContent, content)
        identifier = uuid4().hex
        record = {"schema_version": 1, "draft_id": identifier, "template": content, **provenance,
                  "extraction_method": "manual" if source == "manual" else method, "analysis_model": analyzer.provider,
                  "owner_account_id": aid, "workspace_id": wid, "created_at": _now(),
                  "source_guard_text": text[:12000],
                  "template_id": "personal_" + uuid4().hex}
        files = self._files(aid, wid)
        directory = files.path("drafts")
        directory.mkdir(exist_ok=True)
        files.write_json(directory / (identifier + ".json"), record)
        return {"draft_id": identifier, "template": content, "source_preview": text[:4000],
                "provider": analyzer.provider, "notice": "免费规则提取预览，未调用 AI 模型；请修改结构后确认保存。" if analyzer.provider == "python_rules"
                else "分析结果需人工审核，确认前不会创建正式模板。"}

    @staticmethod
    def _abstract_only(content, source):
        normalized = re.sub(r"\s+", "", source)
        if len(normalized) < 24:
            return
        rules = [s["purpose"] for s in content["structure"]]
        rules += content["style_guidance"] + content["format_rules"] + content["prompt_guidance"]
        for rule in rules:
            value = re.sub(r"\s+", "", rule)
            if any(value[i:i + 24] in normalized for i in range(max(0, len(value) - 23))):
                raise ValueError("模板规则包含来源作品原文，请改写为可复用的结构与变量")

    def save_template(self, aid, wid, body):
        if body.get("confirmed") is not True:
            raise ValueError("请预览并确认模板后保存")
        draft_id = _validate_id(body.get("draft_id"))
        with self._lock(aid, wid):
            files = self._files(aid, wid)
            draft = files.read_json(files.path("drafts", draft_id + ".json"))
            if draft["owner_account_id"] != aid or draft["workspace_id"] != wid:
                raise FileNotFoundError("草稿不存在")
            content = checked(TemplateContent, body.get("template"))
            self._abstract_only(content, draft.get("source_guard_text", ""))
            identifier = draft["template_id"]
            try:
                existing = self.get(aid, wid, "personal", identifier)
            except FileNotFoundError:
                existing = None
            if existing:
                # Retry must not silently accept changed confirmation content.
                if any(existing.get(k) != v for k, v in content.items()):
                    raise ConflictError("草稿已保存；请在模板管理中编辑")
                return existing
            row = {**{k: v for k, v in draft.items() if k not in {"template", "draft_id", "source_guard_text"}},
                   **content, "version": 1, "visibility": "private", "origin": "personal",
                   "updated_at": _now()}
            return self._write(aid, wid, "personal", identifier, row)

    def update_template(self, aid, wid, identifier, body):
        with self._lock(aid, wid):
            row = self.get(aid, wid, "personal", identifier)
            if type(body.get("expected_version")) is not int or body["expected_version"] != row["version"]:
                raise ConflictError("模板已变化，请刷新后编辑")
            patch = body.get("template")
            if not isinstance(patch, dict) or set(patch) - set(TemplateContent.model_fields):
                raise ValueError("只能编辑模板内容，不能修改来源或权限")
            merged = {key: row[key] for key in TemplateContent.model_fields if key in row}
            row.update(checked(TemplateContent, {**merged, **patch}))
            row.update(version=row["version"] + 1, updated_at=_now())
            return self._write(aid, wid, "personal", identifier, row)

    def copy_template(self, aid, wid, identifier, name=None):
        with self._lock(aid, wid):
            row = self.get(aid, wid, "personal", identifier)
            name = name or row["name"][:115] + " 副本"
            if not isinstance(name, str) or not 0 < len(name.strip()) <= 120:
                raise ValueError("模板名称需为 1–120 字符")
            row.update(template_id="personal_" + uuid4().hex, name=name, version=1, enabled=True,
                       copied_from={"template_id": identifier, "version": row["version"]},
                       created_at=_now(), updated_at=_now())
            return self._write(aid, wid, "personal", row["template_id"], row)

    def template_detail(self, aid, wid, identifier):
        row = self.get(aid, wid, "personal", identifier)
        row["versions"] = [{k: v for k, v in self.get(aid, wid, "personal", identifier, number).items()
                            if k in {"version", "name", "updated_at", "enabled"}}
                           for number in range(1, row["version"] + 1)]
        return row
