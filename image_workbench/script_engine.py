"""Resolve independent template/style/example layers into a frozen generation input."""
import copy
import json
import re

from .script_models import signature
from .script_templates import build_prompt


def resource_summary(template):
    return {
        "template": {**template["template_reference"], "template_id": template["template_id"],
                     "template_version": template.get("version", 1),
                     "sha256": signature({k: v for k, v in template.items() if k not in {"example_references", "style_references", "conflicts", "generation_skill"}})},
        "skill": {k: v for k, v in template.get("generation_skill", {}).items() if k != "instructions"},
        "styles": [{"style_id": s["style_id"], "style_version": s["version"], "sha256": signature(s)} for s in template.get("style_references", [])],
        "examples": [{k: v for k, v in e.items() if k not in {"text", "analysis"}}
                     | {"example_version": e.get("version", 1), "content_sha256": signature(e)}
                     for e in template.get("example_references", [])],
    }


def resolve_resources(loader, store, aid, wid, request):
    identifier = request["template_id"]
    version = request.get("template_version")
    if identifier == "none":
        template = {"template_id": "none", "name": "自由生成", "type": request["generation_type"], "version": 1,
                    "template_text": "根据用户明确要求自行组织正文。", "input_requirements": ["user_request"],
                    "example_references": [], "template_reference": {"template_id": "none", "template_version": 1}}
    elif identifier.startswith("personal_"):
        latest = store.get(aid, wid, "personal", identifier)
        if not latest["enabled"]:
            raise ValueError("个人模板已停用")
        row = store.get(aid, wid, "personal", identifier, version)
        if row["type"] != request["generation_type"]:
            raise ValueError("模板与内容类型不匹配")
        template = {**row, "template_text": json.dumps(row["structure"], ensure_ascii=False),
                    "input_requirements": row["required_fields"], "output_structure": [s["section"] for s in row["structure"]],
                    "example_references": [], "template_reference": {"template_id": identifier, "template_version": row["version"]}}
    else:
        template = loader.load(identifier, request["generation_type"])
        if version is not None and str(template.get("version", 1)) != str(version):
            raise ValueError("系统模板已更新，请刷新后选择当前版本")
    styles = {s["style_id"]: s for s in loader.list_styles()}
    selected = request.get("style_ids", [])
    if any(s not in styles for s in selected):
        raise ValueError("创作风格不存在或已停用")
    template["style_references"] = [styles[s] for s in selected]
    for reference in request.get("example_refs", []):
        latest = store.get(aid, wid, "example", reference["example_id"])
        if not latest["enabled"]:
            raise ValueError("参考案例已停用")
        example = store.get(aid, wid, "example", reference["example_id"], reference.get("version"))
        template["example_references"].append({**example, "reference_roles": reference["reference_roles"], "prompt_eligible": True})
    if "generation_skill" not in template:
        template["generation_skill"] = loader.load_skill(request["generation_type"])
    template["conflicts"] = find_conflicts(request, template)
    # Validate required fields and all references before any credit reservation.
    build_prompt(request, template)
    return template


def find_conflicts(request, template):
    result = []
    styles = template.get("style_references", [])
    paces = {s.get("traits", {}).get("pacing") for s in styles}
    if {"fast", "slow"} <= paces:
        result.append({"code": "style_pacing", "message": "所选风格同时要求快节奏和舒缓节奏，请确定侧重，或确认结合不同段落使用。"})
    conflicts = {s.get("traits", {}).get("conflict") for s in styles}
    if {"high", "low"} <= conflicts:
        result.append({"code": "style_conflict", "message": "所选风格同时强调高冲突和低冲突，请调整或确认以用户约束为准。"})
    explicit = " ".join(str(request.get(k, "")) for k in ("input_request", "tone", "constraints"))
    for pattern, trait, values, code, message in [
        (r"舒缓|慢节奏", "pacing", {"fast"}, "request_pacing", "用户要求舒缓节奏，与所选快节奏风格存在冲突。"),
        (r"快节奏|节奏快", "pacing", {"slow"}, "request_pacing", "用户要求快节奏，与所选舒缓风格存在冲突。"),
        (r"无冲突|不要冲突|避免冲突", "conflict", {"high"}, "request_conflict", "用户要求避免冲突，与所选高冲突风格存在冲突。"),
    ]:
        if re.search(pattern, explicit) and any(s.get("traits", {}).get(trait) in values for s in styles):
            result.append({"code": code, "message": message + "确认后保留用户明确约束。"})
    duration = str(request.get("duration", ""))
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(秒|s|分钟|min|minutes?)?\s*", duration, re.I)
    rules = template.get("duration_rules", {})
    if match and isinstance(rules, dict):
        seconds = float(match[1]) * (60 if (match[2] or "").lower() in {"分钟", "min", "minute", "minutes"} else 1)
        low, high = rules.get("min_seconds"), rules.get("max_seconds")
        if (isinstance(low, (int, float)) and seconds < low) or (isinstance(high, (int, float)) and seconds > high):
            result.append({"code": "duration", "message": "用户时长超出模板建议区间；确认后按用户时长压缩或扩展结构。"})
    return result


def creative_snapshot(template):
    """Keep bounded source fragments with frozen full-resource hashes."""
    snapshot = copy.deepcopy(template)
    for item in snapshot.get("example_references", []):
        full_text = item["text"]
        item["text"] = full_text[:1800]
        item["excerpt_is_truncated"] = len(full_text) > 1800
    return snapshot


def recommend(loader, store, aid, wid, request):
    kind, text = request.get("generation_type"), request.get("input_request")
    if kind not in {"script", "screenplay"} or not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValueError("请提供内容类型和 1–20000 字的需求")
    def rank(items):
        candidates = []
        for row in items:
            terms = row.get("keywords", [])
            hits = sorted({term for term in terms if term and term.lower() in text.lower()})
            score = round(min(len(hits), 4) / 4, 2)
            candidates.append({**row, "match_score": score, "matched_terms": hits})
        return sorted(candidates, key=lambda row: row["match_score"], reverse=True)
    templates = rank(loader.list_templates(kind) + [t for t in store.list(aid, wid, "personal") if t["type"] == kind])
    styles = rank(loader.list_styles())
    chosen = templates[0] if templates else None
    selected_styles = [s for s in styles if s["match_score"] > 0][:3]
    missing = []
    if not re.search(r"\d+\s*(秒|分钟|min|s)", text):
        missing.append("目标视频时长")
    if not chosen or chosen["match_score"] == 0:
        missing.append("内容用途或题材（当前仅提供类型兼容的候选）")
    return {"recommended_template": chosen, "recommended_styles": selected_styles,
            "alternatives": {"templates": templates[1:4], "styles": styles[len(selected_styles):len(selected_styles) + 3]},
            "match_score": chosen["match_score"] if chosen else 0,
            "score_method": "match_score = min(匹配的不同关键词数量, 4) / 4；启发式覆盖分，不是概率",
            "reasons": ["按内容类型筛选；关键词：" + "、".join(chosen["matched_terms"])] if chosen and chosen["matched_terms"] else ["没有明确关键词匹配；请手动选择结构和风格"],
            "missing_information": missing, "provider": "python_rules"}


def check_structure(text, snapshot):
    """Advisory only: don't reject valid narrative prose or retry inference."""
    fields = snapshot.get("quality_required_markers", [])
    missing = [field for field in fields if field not in text]
    warnings = []
    if missing:
        warnings.append("未识别模板字段：" + "、".join(missing))
    sections = snapshot.get("structure", [])
    labels = [item.get("section", "") for item in sections if isinstance(item, dict)]
    recognized = [label for label in labels if label and label.casefold() in text.casefold()]
    unrecognized = [label for label in labels if label not in recognized]
    if unrecognized:
        warnings.append("未直接识别结构栏目：" + "、".join(unrecognized) + "；自然叙事可能使用其他表达，请人工核对")
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if sections and len(paragraphs) < min(3, len(sections)):
        warnings.append("正文段落较少，请检查是否完整覆盖模板结构")
    if text.rstrip().endswith(("……待续", "[TRUNCATED]", "（未完）")) or text.count("“") != text.count("”"):
        warnings.append("正文存在可能截断的标记或未闭合引号，请人工检查")
    return {"encoding": "utf-8", "nonempty": bool(text.strip()), "structure_status": "needs_review" if warnings else "basic_checks_passed",
            "expected_sections": labels, "recognized_sections": recognized, "unrecognized_sections": unrecognized,
            "warnings": warnings, "literary_quality_scored": False, "auto_regenerated": False}
