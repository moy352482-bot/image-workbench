"""Storyboard data validation. Integer milliseconds and stable shot identity are mandatory."""
from copy import deepcopy
import math
from uuid import NAMESPACE_URL, uuid5

from .script_models import signature
from .storage import _validate_id


RATIOS = {"16:9": "landscape", "9:16": "portrait", "1:1": "square"}
TEXT_FIELDS = {
    "title": 120, "scene_id": 120, "duration_reason": 4000, "visual_description": 12000,
    "action": 4000, "camera_framing": 200, "camera_movement": 1000, "emotion": 1000,
    "dialogue": 12000, "voiceover": 12000, "image_prompt": 12000, "video_prompt": 12000,
    "source_excerpt": 20000,
}
EDIT_FIELDS = set(TEXT_FIELDS) | {
    "narrative_duration_ms", "generation_duration_ms", "reference_artifact_ids",
    "start_frame_artifact_id", "end_frame_artifact_id", "aspect_ratio", "video_model_id",
}


def integer(value, field, minimum=1, maximum=3_600_000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须为 {minimum}–{maximum} 的整数")
    return value


def text(value, field, maximum=12000, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{field} 格式无效或超过 {maximum} 字")
    return value.strip()


def ids(value, *, limit=200, field="ID 列表"):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{field} 无效")
    for item in value:
        _validate_id(item)
    if len(set(value)) != len(value):
        raise ValueError(f"{field} 不能重复")
    return list(value)


def duration_capabilities(config, model_id="h3"):
    models = config.get("video_models", [])
    model = next((m for m in models if m["model_id"] == model_id), None)
    if not model:
        raise ValueError("视频模型未登记")
    return model


def generation_plan(narrative_ms, requested_ms, capability):
    """Do not invent unsupported durations or perform clipping."""
    integer(narrative_ms, "叙事时长")
    integer(requested_ms, "素材时长")
    supported = capability.get("supported_durations_ms")
    minimum, maximum = capability["min_duration_ms"], capability["max_duration_ms"]
    step = capability.get("duration_step_ms", 1000)
    wanted = max(narrative_ms, requested_ms, minimum)
    recommended = next((d for d in sorted(supported) if d >= wanted), None) if supported else minimum + math.ceil((wanted - minimum) / step) * step
    if recommended is None or recommended > maximum:
        return {"supported": False, "generation_duration_ms": requested_ms,
                "requires_segments": True, "max_duration_ms": maximum,
                "message": f"该镜头需要超过 {maximum / 1000:g} 秒素材；需要分段生成或调整镜头规划。"}
    exact = requested_ms in supported if supported else minimum <= requested_ms <= maximum and (requested_ms - minimum) % step == 0
    supported_request = exact and requested_ms >= narrative_ms
    message = (f"将生成 {recommended / 1000:g} 秒素材，最终成片使用其中 {narrative_ms / 1000:g} 秒；本阶段不执行剪辑。"
               if recommended != narrative_ms else f"叙事与素材均为 {recommended / 1000:g} 秒。")
    if not supported_request:
        message = f"当前素材时长不符合模型能力，请改为 {recommended / 1000:g} 秒。" + message
    return {"supported": supported_request, "generation_duration_ms": recommended,
            "requires_segments": False, "max_duration_ms": maximum, "message": message}


def normalize_shot(raw, *, shot_id, order, options):
    if not isinstance(raw, dict):
        raise ValueError("镜头必须是对象")
    result = {name: text(raw.get(name, ""), name, maximum, required=name in {"title", "visual_description", "duration_reason"})
              for name, maximum in TEXT_FIELDS.items()}
    result.update(shot_id=_validate_id(shot_id), order=integer(order, "镜头顺序", maximum=500),
                  narrative_duration_ms=integer(raw.get("narrative_duration_ms"), "叙事时长"),
                  generation_duration_ms=integer(raw.get("generation_duration_ms"), "素材时长"),
                  reference_artifact_ids=ids(raw.get("reference_artifact_ids", []), limit=9, field="参考资产"),
                  aspect_ratio=raw.get("aspect_ratio", options.get("aspect_ratio", "16:9")),
                  video_model_id=raw.get("video_model_id", options.get("video_model_id", "h3")),
                  source_shot_ids=ids(raw.get("source_shot_ids", [])), status="planned")
    if result["aspect_ratio"] not in RATIOS or result["video_model_id"] != "h3":
        raise ValueError("镜头画幅或视频模型无效")
    for name in ("start_frame_artifact_id", "end_frame_artifact_id"):
        value = raw.get(name)
        result[name] = _validate_id(value) if value else None
    if result["end_frame_artifact_id"] and not result["start_frame_artifact_id"]:
        raise ValueError("请先选择首帧，再选择尾帧")
    if not result["video_prompt"]:
        result["video_prompt"] = result["visual_description"]
    if not result["scene_id"]:
        result["scene_id"] = uuid5(NAMESPACE_URL, "scene:" + result["visual_description"][:80]).hex
    return result


def shot_fingerprint(shot):
    # Sorting/review/lineage changes do not change the media requirements.
    return signature({key: shot.get(key) for key in sorted(EDIT_FIELDS - {"duration_reason", "source_excerpt"})})


def duration_conflict(shots, target, planner_conflict=None):
    total = sum(s["narrative_duration_ms"] for s in shots)
    if planner_conflict is not None:
        if not isinstance(planner_conflict, dict):
            raise ValueError("duration_conflict 必须为对象")
        reason = text(planner_conflict.get("reason", ""), "时长冲突原因", 4000, required=True)
        reasonable = integer(planner_conflict.get("reasonable_duration_ms", total), "合理时长")
        suggestions = planner_conflict.get("suggestions", ["修改目标时长", "压缩剧情后重新规划", "手动调整分镜"])
        if not isinstance(suggestions, list) or len(suggestions) > 12:
            raise ValueError("时长建议无效")
        return {"target_duration_ms": target, "reasonable_duration_ms": reasonable, "actual_duration_ms": total,
                "reason": reason, "suggestions": [text(s, "时长建议", 1000) for s in suggestions]}
    if total != target:
        return {"target_duration_ms": target, "reasonable_duration_ms": total, "actual_duration_ms": total,
                "reason": f"当前镜头合计 {total / 1000:g} 秒，与目标 {target / 1000:g} 秒不一致。",
                "suggestions": ["修改目标时长", "调整镜头时长", "重新规划"]}
    return None


def normalize_plan(raw, options, task_id):
    if not isinstance(raw, dict) or not isinstance(raw.get("shots"), list) or not 1 <= len(raw["shots"]) <= 200:
        raise ValueError("规划结果需包含 1–200 个镜头")
    result = deepcopy(raw)
    result["shots"] = [normalize_shot(s, shot_id=uuid5(NAMESPACE_URL, task_id + ":" + str(i)).hex,
                                     order=i + 1, options=options) for i, s in enumerate(raw["shots"])]
    result["duration_conflict"] = duration_conflict(result["shots"], options["target_duration_ms"], raw.get("duration_conflict"))
    return {"shots": result["shots"], "duration_conflict": result["duration_conflict"]}
