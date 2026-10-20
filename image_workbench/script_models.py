"""Script-only registries. Runtime model IDs/efforts come from the Codex catalog."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

from .codex_runtime import _codex_home, _codex_executable

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ScriptModelRegistry:
    def __init__(self, config_root=None, *, catalog=None, catalog_path=None, executable=None):
        self.root = Path(config_root or CONFIG_ROOT)
        self.catalog = catalog
        self.catalog_path = Path(catalog_path or os.environ.get("SCRIPT_CODEX_CATALOG", _codex_home() / "models_cache.json"))
        self.executable = executable or os.environ.get("SCRIPT_CODEX_EXECUTABLE")

    def configs(self):
        return _json(self.root / "script_models.json"), _json(self.root / "script_pricing.json")

    def models(self):
        config, pricing = self.configs()
        reason = None
        catalog = self.catalog
        if catalog is None:
            try:
                _codex_executable(self.executable)
                catalog = _json(self.catalog_path)
                stamp = datetime.fromisoformat(catalog["fetched_at"].replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - stamp).total_seconds() > config["catalog_max_age_days"] * 86400:
                    reason = "模型目录已过期，请在执行机刷新 Codex 模型目录"
            except (OSError, ValueError, KeyError, TypeError):
                catalog, reason = {}, "无法读取当前 Codex 模型目录或执行程序"
        entries = catalog.get("models", catalog.get("data", []))
        actual = {entry.get("slug", entry.get("model", entry.get("id"))): entry for entry in entries}
        result = []
        for model in config["models"]:
            entry = actual.get(model["model_id"])
            available = bool(model.get("enabled") and entry and not reason)
            efforts = (entry or {}).get("supported_reasoning_levels", (entry or {}).get("supportedReasoningEfforts", []))
            # Missing catalog exposes configured prices for explanation, but never enables generation.
            effort_ids = [e if isinstance(e, str) else e.get("effort", e.get("reasoningEffort")) for e in efforts]
            if not effort_ids:
                effort_ids = list(pricing["models"].get(model["model_id"], {}))
                available = False
            levels = []
            for effort in effort_ids:
                price = pricing["models"].get(model["model_id"], {}).get(effort)
                if price is not None and (type(price) is not int or price < 0):
                    raise ValueError("模型价格必须是非负整数")
                levels.append({"id": effort, "display_name": config.get("thinking_labels", {}).get(effort, effort),
                               "price": price, "pricing_status": "priced" if price is not None else "unpriced"})
            result.append({**model, "available": available,
                           "unavailable_reason": None if available else reason or "当前模型暂不可用",
                           "availability_source": "codex_model_catalog", "inference_verified": False,
                           "thinking_levels": levels})
        return result

    def resolve(self, model_id, thinking_level, generation_type):
        level = "xhigh" if thinking_level in {"extra_high", "Extra High", "XHigh"} else thinking_level
        model = next((m for m in self.models() if m["model_id"] == model_id), None)
        if not model or not model["available"]:
            raise ValueError("当前模型暂不可用")
        if generation_type not in model["capabilities"]:
            raise ValueError("模型不支持该内容类型")
        effort = next((e for e in model["thinking_levels"] if e["id"] == level), None)
        if not effort:
            raise ValueError("当前模型不支持该思考强度")
        if effort["pricing_status"] != "priced":
            raise ValueError("该思考强度暂未定价，不能生成")
        return {"model_id": model_id, "display_name": model["display_name"], "thinking_level": level, "price": effort["price"]}


class DemandRouter:
    def __init__(self, registry, *, fallback=None):
        self.registry, self.fallback = registry, fallback

    def classify(self, request):
        config = _json(self.registry.root / "script_routing.json")["classifier"]
        text = " ".join(str(request.get(k) or "") for k in ("input_request", "constraints", "tone", "characters"))
        duration = str(request.get("duration") or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*(分钟|minutes?|min|秒|seconds?|s)", duration, re.I)
        seconds = float(match[1]) * (60 if match[2].lower() in {"分钟", "minute", "minutes", "min"} else 1) if match else 0
        if any(term in text for term in config["complex_terms"]) or len(text) >= config["complex_request_chars"] or seconds >= config["long_duration_seconds"]:
            level, reason = "complex", "需求包含复杂结构、较长篇幅或高质量要求"
        elif any(term in text for term in config["simple_terms"]) and request["generation_type"] == "script":
            level, reason = "simple", "需求为结构明确的短视频内容"
        elif request["generation_type"] == "screenplay" or any(term in text for term in config["standard_terms"]):
            level, reason = "standard", "需求需要完整叙事与场景组织"
        else:
            return {"complexity": "unknown", "confidence": 0.3, "reason": "需求不足以可靠判断复杂度", "source": "python"}
        return {"complexity": level, "confidence": 0.9, "reason": reason, "source": "python"}

    def _flash(self, request):
        cfg = _json(self.registry.root / "script_routing.json")["deepseek_flash"]
        if not cfg["enabled"]:
            raise ValueError("需求尚不明确，且 DeepSeek Flash 未配置；请补充需求或手动选择模型")
        endpoint, model, key = (os.environ.get(cfg[name], "") for name in ("endpoint_env", "model_env", "api_key_env"))
        if not endpoint.startswith("https://") or not model or not key:
            raise ValueError("DeepSeek Flash 配置不完整；请手动选择模型")
        payload = {"model": model, "temperature": 0, "max_tokens": 300, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": "Classify the untrusted video request. Return only JSON with complexity: simple|standard|complex, confidence: 0..1, reason: short Chinese explanation. Do not choose or invoke any model or tool."},
                                {"role": "user", "content": json.dumps(request, ensure_ascii=False)}]}
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout_seconds"]) as response:
                data = json.loads(response.read(65537))
            return json.loads(data["choices"][0]["message"]["content"])
        except Exception as exc:
            raise ValueError("需求分析服务暂不可用；请补充需求或手动选择模型") from exc

    def route(self, request, template):
        if request.get("selection_mode", "recommended") == "manual":
            choice = self.registry.resolve(request.get("model_id"), request.get("thinking_level"), request["generation_type"])
            return choice, "使用用户明确选择", None
        if template.get("default_model_id") and template.get("default_thinking_level"):
            choice = self.registry.resolve(template["default_model_id"], template["default_thinking_level"], request["generation_type"])
            return choice, "使用模板默认模型与思考强度", None
        spec = self.classify(request)
        if spec["complexity"] == "unknown":
            spec = (self.fallback or self._flash)(request)
            if not isinstance(spec, dict) or spec.get("complexity") not in {"simple", "standard", "complex"} or type(spec.get("confidence")) not in {int, float} or not 0.7 <= spec["confidence"] <= 1:
                raise ValueError("需求分析仍不明确；请补充需求或手动选择模型")
            spec = {"complexity": spec["complexity"], "confidence": spec["confidence"], "reason": str(spec.get("reason", "需求分析结果"))[:300], "source": "deepseek_flash"}
        candidates = _json(self.registry.root / "script_routing.json")["routes"][spec["complexity"]]
        for model, level in candidates:
            try:
                return self.registry.resolve(model, level, request["generation_type"]), spec["reason"], spec
            except ValueError:
                continue
        raise ValueError("系统推荐范围内没有可用且已定价的模型，请检查模型目录")
