"""Fixed offline acceptance requirements for prompt enhancement.

Each case is a user-facing requirement.  ``model`` selects an offline scripted
failure; the default ``mock`` is deterministic and never calls a real model.
The matrix is shared by the unit test and the evaluation report.
"""
from copy import deepcopy
import hashlib

ACCOUNT_ID = "a" * 32
WORKSPACE_ID = "b" * 32
INVENTED = ["张三", "李四", "王五", "小明", "耐克", "阿迪达斯", "星巴克", "可口可乐", "香奈儿", "路易威登"]


def ref(index, role, usage=None, label=""):
    item = {"index": index, "role": role}
    if usage:
        item["usage"] = usage
    if label:
        item["label"] = label
    return item


def payload_for(case):
    payload = deepcopy(case["request"])
    payload.setdefault("request_id", hashlib.sha256(case["id"].encode("utf-8")).hexdigest()[:32])
    payload.setdefault("mode", "standard")
    payload.setdefault("task_type", "generate")
    payload.setdefault("aspect_ratio", "auto")
    return payload


CASES = [
    # --- Plain text-to-image -------------------------------------------------
    {
        "id": "PE-01", "category": "普通文生图",
        "request": {"prompt": "一只橘猫坐在窗台上，午后阳光"},
        "expect": {"status": "enhanced", "skill": "image-generation", "task_type": "generate",
                   "contains": ["橘猫", "窗台", "午后阳光"], "absent": INVENTED},
    },
    {
        "id": "PE-02", "category": "普通文生图",
        "request": {"prompt": "一间极简咖啡馆，木桌上放着一杯拿铁"},
        "expect": {"status": "enhanced", "contains": ["极简咖啡馆", "木桌", "拿铁"], "absent": INVENTED},
    },
    {
        "id": "PE-03", "category": "创意强化",
        "request": {"prompt": "城市夜景，雨后霓虹，一个穿风衣的人走过", "mode": "creative"},
        "expect": {"status": "enhanced", "contains": ["城市夜景", "霓虹", "风衣"], "absent": INVENTED},
    },
    {
        "id": "PE-04", "category": "电影感",
        "request": {"prompt": "电影感的雨夜街头，一个人回头", "mode": "creative"},
        "expect": {"status": "enhanced", "skill": "cinematic", "contains": ["电影感", "雨夜", "叙事电影写实"]},
    },
    {
        "id": "PE-05", "category": "中文文字约束",
        "request": {"prompt": "一张海边日落海报，画面里写“限时五折”"},
        "expect": {"status": "enhanced", "contains": ["限时五折"], "warnings_absent": ["未完整保留"],
                   "negative_contains": ["文字"]},
    },
    {
        "id": "PE-06", "category": "中文文字约束",
        "request": {"prompt": "春日上新海报，主标题“春日上新”，副标题“全场八折”"},
        "expect": {"status": "enhanced", "contains": ["春日上新", "全场八折"], "warnings_absent": ["未完整保留"]},
    },
    {
        "id": "PE-07", "category": "模糊需求",
        "request": {"prompt": "一只猫"},
        "expect": {"status": "enhanced", "contains": ["一只猫"], "absent": INVENTED,
                   "negative_contains": ["人物", "品牌", "水印"]},
    },
    {
        "id": "PE-08", "category": "完整需求",
        "request": {"prompt": "一位穿红色连衣裙的女性站在海边礁石上，黄昏侧逆光，85mm 镜头，低饱和胶片色调"},
        "expect": {"status": "enhanced",
                   "contains": ["红色连衣裙", "海边礁石", "黄昏侧逆光", "85mm", "低饱和", "胶片"],
                   "absent": INVENTED},
    },
    # --- Reference images and consistency -----------------------------------
    {
        "id": "PE-09", "category": "人物一致性",
        "request": {"prompt": "同一个人物站在教室门口，保持脸部和发型一致",
                    "task_type": "reference_generate",
                    "references": [ref(1, "identity", "锁定人物身份、脸型和发型")]},
        "expect": {"status": "enhanced", "skill": "consistency", "task_type": "reference_generate",
                   "references": [[1, "identity"]], "reference_usage": True},
    },
    {
        "id": "PE-10", "category": "人物一致性",
        "request": {"prompt": "同一个人物在公园长椅上阅读，保持脸和服装一致",
                    "task_type": "reference_generate",
                    "references": [ref(1, "face", "锁定脸部特征"), ref(2, "clothing", "锁定服装款式与配色")]},
        "expect": {"status": "enhanced", "skill": "consistency",
                   "references": [[1, "face"], [2, "clothing"]], "reference_usage": True},
    },
    {
        "id": "PE-11", "category": "人物一致性",
        "request": {"prompt": "同一个角色全身照，保持身高比例和服装一致",
                    "task_type": "reference_generate",
                    "references": [ref(1, "full_body", "锁定身高观感与身体比例"),
                                   ref(2, "clothing", "锁定服装裁片与配件")]},
        "expect": {"status": "enhanced", "skill": "consistency",
                   "references": [[1, "full_body"], [2, "clothing"]], "reference_usage": True},
    },
    {
        "id": "PE-12", "category": "产品一致性",
        "request": {"prompt": "同款产品放在新的浅灰石台上，保持外形、材质和包装一致",
                    "task_type": "reference_generate",
                    "references": [ref(1, "product", "锁定产品外形、结构、材质和固定颜色")]},
        "expect": {"status": "enhanced", "skill": "consistency",
                   "references": [[1, "product"]], "reference_usage": True},
    },
    {
        "id": "PE-13", "category": "产品一致性",
        "request": {"prompt": "产品广告图，保持产品和 logo 不变，只更换背景",
                    "task_type": "reference_generate",
                    "references": [ref(1, "product", "锁定产品外形与材质"),
                                   ref(2, "logo", "锁定标识形状、位置与可读性")]},
        "expect": {"status": "enhanced", "skill": "consistency",
                   "references": [[1, "product"], [2, "logo"]], "reference_usage": True},
    },
    {
        "id": "PE-14", "category": "参考图片",
        "request": {"prompt": "参考这张图的风格画一只猫",
                    "task_type": "reference_generate",
                    "references": [ref(1, "style", "只参考视觉风格，不继承主体或品牌")]},
        "expect": {"status": "enhanced", "task_type": "reference_generate",
                   "references": [[1, "style"]], "reference_usage": True, "absent": INVENTED},
    },
    {
        "id": "PE-15", "category": "参考图片",
        "request": {"prompt": "参考这张图的构图，画一间咖啡馆",
                    "task_type": "reference_generate",
                    "references": [ref(1, "composition", "参考构图与空间关系")]},
        "expect": {"status": "enhanced", "task_type": "reference_generate",
                   "references": [[1, "composition"]], "reference_usage": True},
    },
    {
        "id": "PE-16", "category": "多张参考图",
        "request": {"prompt": "同一个人物在同一场景，保持身份和衣服一致",
                    "task_type": "reference_generate",
                    "references": [ref(1, "identity", "锁定人物身份"), ref(2, "clothing", "锁定服装")]},
        "expect": {"status": "enhanced", "skill": "consistency",
                   "references": [[1, "identity"], [2, "clothing"]], "reference_usage": True},
    },
    {
        "id": "PE-17", "category": "多张参考图",
        "request": {"prompt": "产品保持同款，背景参考第二张图",
                    "task_type": "reference_generate",
                    "references": [ref(1, "product", "锁定产品外形与材质"),
                                   ref(2, "background", "参考环境与背景结构")]},
        "expect": {"status": "enhanced", "references": [[1, "product"], [2, "background"]],
                   "reference_usage": True},
    },
    {
        "id": "PE-18", "category": "多张参考图",
        "request": {"prompt": "只参考第一张图的风格和第二张图的构图，不继承人物、品牌或文字",
                    "task_type": "reference_generate",
                    "references": [ref(1, "style", "只参考视觉风格"), ref(2, "composition", "只参考构图")]},
        "expect": {"status": "enhanced", "references": [[1, "style"], [2, "composition"]],
                   "reference_usage": True, "absent": INVENTED},
    },
    {
        "id": "PE-19", "category": "多张参考图",
        "request": {"prompt": "参考这两张图", "task_type": "reference_generate",
                    "references": [ref(1, "other"), ref(2, "other")]},
        "expect": {"status": "rejected", "model_calls": 0},
    },
    {
        "id": "PE-20", "category": "请求边界",
        "request": {"prompt": "同一人物", "task_type": "reference_generate",
                    "references": [ref(1, "identity", "身份"), ref(1, "face", "脸")]},
        "expect": {"status": "rejected", "model_calls": 0},
    },
    # --- Local edits ---------------------------------------------------------
    {
        "id": "PE-21", "category": "局部修改",
        "request": {"prompt": "把背景换成海边，保持人物的脸和衣服不变", "task_type": "edit",
                    "references": [ref(1, "edit_target", "作为本次修改的底图")],
                    "edit": {"change_targets": ["背景"], "preserve_targets": ["人物的脸和衣服"],
                             "forbidden_changes": ["不得改变人物身份"], "inherit_aspect_ratio": True}},
        "expect": {"status": "enhanced", "skill": "image-editing", "task_type": "edit",
                   "edit_change": ["背景"], "edit_preserve": ["人物的脸和衣服"],
                   "edit_forbidden": ["不得改变人物身份"], "inherit": True,
                   "references": [[1, "edit_target"]], "reference_usage": True},
    },
    {
        "id": "PE-22", "category": "局部修改",
        "request": {"prompt": "删除桌面上的杯子，其他物体和构图保持不变", "task_type": "edit",
                    "references": [ref(1, "edit_target", "作为底图")],
                    "edit": {"change_targets": ["桌面上的杯子"], "preserve_targets": ["其他物体和构图"],
                             "forbidden_changes": ["不得新增物体"], "inherit_aspect_ratio": True}},
        "expect": {"status": "enhanced", "edit_change": ["桌面上的杯子"],
                   "edit_preserve": ["其他物体和构图"], "edit_forbidden": ["不得新增物体"],
                   "inherit": True, "references": [[1, "edit_target"]]},
    },
    {
        "id": "PE-23", "category": "局部修改",
        "request": {"prompt": "把衣服颜色改成深蓝，保持款式和人物身份不变", "task_type": "edit",
                    "references": [ref(1, "edit_target", "作为底图")],
                    "edit": {"change_targets": ["衣服颜色改为深蓝"], "preserve_targets": ["服装款式和人物身份"],
                             "forbidden_changes": ["不得改变脸"], "inherit_aspect_ratio": True}},
        "expect": {"status": "enhanced", "edit_change": ["衣服颜色改为深蓝"],
                   "edit_preserve": ["服装款式和人物身份"], "edit_forbidden": ["不得改变脸"],
                   "inherit": True, "references": [[1, "edit_target"]]},
    },
    {
        "id": "PE-24", "category": "局部修改",
        "request": {"prompt": "把背景换成海边", "task_type": "edit",
                    "references": [ref(1, "edit_target", "作为底图")]},
        "expect": {"status": "enhanced", "edit_nonempty": True, "inherit": True,
                   "references": [[1, "edit_target"]]},
    },
    {
        "id": "PE-25", "category": "局部修改",
        "request": {"prompt": "把背景换成海边，保持画幅不变", "task_type": "edit",
                    "references": [ref(1, "edit_target", "作为底图")],
                    "edit": {"change_targets": ["背景"], "preserve_targets": ["人物与服装"],
                             "forbidden_changes": ["不得改变人物身份"], "inherit_aspect_ratio": False}},
        "expect": {"status": "enhanced", "inherit": False, "references": [[1, "edit_target"]]},
    },
    {
        "id": "PE-26", "category": "局部修改",
        "request": {"prompt": "把背景换成海边", "task_type": "edit",
                    "references": [ref(1, "identity", "锁定人物身份")]},
        "expect": {"status": "rejected", "model_calls": 0},
    },
    {
        "id": "PE-27", "category": "局部修改",
        "request": {"prompt": "把背景换成海边，保持人物不变", "task_type": "edit",
                    "references": [ref(1, "base", "作为基底图片")]},
        "expect": {"status": "enhanced", "edit_nonempty": True, "inherit": True,
                   "references": [[1, "base"]]},
    },
    {
        "id": "PE-28", "category": "请求边界",
        "request": {"prompt": "一张海报", "task_type": "generate",
                    "edit": {"change_targets": ["背景"]}},
        "expect": {"status": "rejected", "model_calls": 0},
    },
    {
        "id": "PE-29", "category": "修改计划",
        "request": {"prompt": "把背景换成海边", "task_type": "edit",
                    "references": [ref(1, "edit_target", "作为底图")]},
        "model": "missing_edit_plan",
        "expect": {"status": "fallback", "code": "invalid_edit_plan", "calls": 1},
    },
    # --- Complex posters -----------------------------------------------------
    {
        "id": "PE-30", "category": "复杂海报",
        "request": {"prompt": "一张春季新品海报，主标题“春日上新”，副标题“全场八折”，产品居中，"
                              "浅绿背景，右下角品牌标识留白"},
        "expect": {"status": "enhanced", "contains": ["主标题“春日上新”", "副标题“全场八折”", "产品居中",
                                                      "浅绿背景", "右下角品牌标识留白"],
                   "absent": INVENTED, "negative_contains": ["文字", "品牌"]},
    },
    {
        "id": "PE-31", "category": "复杂海报",
        "request": {"prompt": "海报上方写“城市漫游”，下方写“10月1日开幕”，文字必须准确，不得错字"},
        "expect": {"status": "enhanced", "contains": ["城市漫游", "10月1日开幕", "不得错字"],
                   "warnings_absent": ["未完整保留"]},
    },
    {
        "id": "PE-32", "category": "复杂海报",
        "request": {"prompt": "左侧主标题“山野”，右侧产品图，底部小字“限量发售”，整体留白"},
        "expect": {"status": "enhanced", "contains": ["左侧主标题“山野”", "右侧产品图", "底部小字“限量发售”"]},
    },
    {
        "id": "PE-33", "category": "复杂海报",
        "request": {"prompt": "只保留标题“新品”，不要添加其他文字或水印"},
        "expect": {"status": "enhanced", "contains": ["新品"], "absent": INVENTED,
                   "negative_contains": ["文字", "水印"], "warnings_absent": ["未完整保留"]},
    },
    # --- Intent and unauthorized additions ----------------------------------
    {
        "id": "PE-34", "category": "意图保留",
        "request": {"prompt": "一位老人坐在藤椅上晒太阳，旁边放着一杯热茶"},
        "expect": {"status": "enhanced", "original_exact": True,
                   "contains": ["一位老人坐在藤椅上晒太阳", "一杯热茶"], "absent": INVENTED},
    },
    {
        "id": "PE-35", "category": "不得新增人物",
        "request": {"prompt": "一间空房间，木地板和落地窗"},
        "expect": {"status": "enhanced", "contains": ["空房间", "木地板", "落地窗"],
                   "absent": INVENTED + ["主角", "路人"], "negative_contains": ["人物"]},
    },
    {
        "id": "PE-36", "category": "不得新增道具",
        "request": {"prompt": "一杯咖啡放在木桌上"},
        "expect": {"status": "enhanced", "contains": ["一杯咖啡", "木桌"],
                   "absent": ["鲜花", "书本", "笔记本电脑", "蛋糕"], "negative_contains": ["道具"]},
    },
    {
        "id": "PE-37", "category": "不得新增品牌",
        "request": {"prompt": "一只白色马克杯放在浅色桌面上"},
        "expect": {"status": "enhanced", "contains": ["白色马克杯", "浅色桌面"],
                   "absent": INVENTED, "negative_contains": ["品牌", "logo", "水印"]},
    },
    {
        "id": "PE-38", "category": "精确文字",
        "request": {"prompt": "海报上写“限时五折”，不要改字"},
        "expect": {"status": "enhanced", "contains": ["限时五折"], "warnings_absent": ["未完整保留"]},
    },
    # --- Bounded model failures ---------------------------------------------
    {
        "id": "PE-39", "category": "空响应",
        "request": {"prompt": "一张产品图"}, "model": "empty",
        "expect": {"status": "fallback", "code": "empty_response", "calls": 1},
    },
    {
        "id": "PE-40", "category": "格式错误",
        "request": {"prompt": "一张产品图"}, "model": "invalid_json",
        "expect": {"status": "fallback", "code": "invalid_json", "calls": 1},
    },
    {
        "id": "PE-41", "category": "结构错误",
        "request": {"prompt": "一张产品图"}, "model": "invalid_schema",
        "expect": {"status": "fallback", "code": "invalid_schema", "calls": 1},
    },
    {
        "id": "PE-42", "category": "超长输出",
        "request": {"prompt": "一张产品图"}, "model": "too_long",
        "expect": {"status": "fallback", "code": "invalid_schema", "calls": 1},
    },
    {
        "id": "PE-43", "category": "超时",
        "request": {"prompt": "一张产品图"}, "model": "timeout",
        "expect": {"status": "fallback", "code": "timeout", "calls": 1},
    },
    {
        "id": "PE-44", "category": "模型不可用",
        "request": {"prompt": "一张产品图"}, "model": "unavailable",
        "expect": {"status": "fallback", "code": "model_unavailable", "calls": 0},
    },
    {
        "id": "PE-45", "category": "模型错误",
        "request": {"prompt": "一张产品图"}, "model": "model_error",
        "expect": {"status": "fallback", "code": "model_error", "calls": 1},
    },
]
