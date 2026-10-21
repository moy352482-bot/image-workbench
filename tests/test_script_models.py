"""脚本模型、固定报价与路由的离线合同测试。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock

from image_workbench.script_models import CONFIG_ROOT, DemandRouter, ScriptModelRegistry


MODEL_IDS = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")


def catalog(efforts=None):
    supported = efforts or {model: ["low", "medium", "high", "xhigh", "max", "ultra"] for model in MODEL_IDS}
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "models": [
        {"slug": model, "supported_reasoning_levels": [{"effort": level, "description": level} for level in levels]}
        for model, levels in supported.items()
    ]}


class ScriptModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("script_models.json", "script_pricing.json", "script_routing.json"):
            shutil.copyfile(CONFIG_ROOT / name, self.root / name)
        self.registry = ScriptModelRegistry(self.root, catalog=catalog())

    def update(self, filename, mutate):
        path = self.root / filename
        value = json.loads(path.read_text(encoding="utf-8"))
        mutate(value)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def request(self, **values):
        return {"generation_type": "script", "input_request": "咖啡产品介绍短视频广告", **values}

    def test_all_fourteen_prices_for_both_content_types(self):
        expected = {
            "gpt-6-astra": {"low": 30, "medium": 35, "high": 40, "xhigh": 45, "max": 50},
            "gpt-6.1-sol": {"low": 10, "medium": 15, "high": 20, "xhigh": 25, "max": 30},
            "gpt-6-luna": {"low": 0, "medium": 0, "high": 0, "xhigh": 5},
        }
        self.assertEqual(sum(len(levels) for levels in expected.values()), 14)
        for model, levels in expected.items():
            for level, price in levels.items():
                for generation_type in ("script", "screenplay"):
                    with self.subTest(model=model, level=level, generation_type=generation_type):
                        selected = self.registry.resolve(model, level, generation_type)
                        self.assertEqual((selected["model_id"], selected["thinking_level"], selected["price"]), (model, level, price))

    def test_unpriced_efforts_remain_visible_but_cannot_generate(self):
        models = {item["model_id"]: item for item in self.registry.models()}
        for model, level in (("gpt-6-luna", "max"), ("gpt-6-luna", "ultra"),
                             ("gpt-6-astra", "ultra"), ("gpt-6.1-sol", "ultra")):
            with self.subTest(model=model, level=level):
                effort = next(item for item in models[model]["thinking_levels"] if item["id"] == level)
                self.assertEqual((effort["price"], effort["pricing_status"]), (None, "unpriced"))
                with self.assertRaisesRegex(ValueError, "未定价"):
                    self.registry.resolve(model, level, "script")

    def test_efforts_come_from_catalog_and_extra_high_aliases_resolve_to_xhigh(self):
        source = catalog({"gpt-6-astra": ["none", "medium", "xhigh", "experimental"]})
        registry = ScriptModelRegistry(self.root, catalog=source)
        astra = registry.models()[0]
        self.assertEqual([item["id"] for item in astra["thinking_levels"]], ["none", "medium", "xhigh", "experimental"])
        self.assertEqual(astra["thinking_levels"][-1]["pricing_status"], "unpriced")
        with self.assertRaisesRegex(ValueError, "不支持该思考强度"):
            registry.resolve("gpt-6-astra", "high", "script")
        for alias in ("extra_high", "Extra High", "XHigh", "xhigh"):
            self.assertEqual(registry.resolve("gpt-6-astra", alias, "script")["thinking_level"], "xhigh")

    def test_catalog_file_is_reread_and_stale_or_missing_catalog_disables_models(self):
        executable = self.root / "codex.exe"
        executable.write_bytes(b"not executed")
        path = self.root / "catalog.json"
        source = catalog()
        path.write_text(json.dumps(source), encoding="utf-8")
        registry = ScriptModelRegistry(self.root, catalog_path=path, executable=executable)
        self.assertTrue(all(item["available"] for item in registry.models()))
        source["models"][0]["supported_reasoning_levels"] = [{"effort": "low"}]
        path.write_text(json.dumps(source), encoding="utf-8")
        self.assertEqual([item["id"] for item in registry.models()[0]["thinking_levels"]], ["low"])
        source["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        path.write_text(json.dumps(source), encoding="utf-8")
        self.assertTrue(all(not item["available"] for item in registry.models()))
        path.unlink()
        self.assertTrue(all(not item["available"] for item in registry.models()))

    def test_false_negative_and_non_integer_pricing_are_rejected(self):
        for invalid in (False, True, -1, 1.5, "30"):
            with self.subTest(price=invalid):
                self.update("script_pricing.json", lambda value: value["models"]["gpt-6-astra"].update(high=invalid))
                with self.assertRaisesRegex(ValueError, "非负整数"):
                    self.registry.resolve("gpt-6-astra", "high", "script")

    def test_manual_choice_wins_over_template_and_complexity_without_fallback(self):
        fallback = Mock(side_effect=AssertionError("manual request must not classify"))
        router = DemandRouter(self.registry, fallback=fallback)
        choice, _, spec = router.route(self.request(selection_mode="manual", model_id="gpt-6-luna", thinking_level="low",
                                                   input_request="最高质量电影级悬疑多时空反转"),
                                       {"default_model_id": "gpt-6-astra", "default_thinking_level": "max"})
        self.assertEqual((choice["model_id"], choice["thinking_level"], choice["price"]), ("gpt-6-luna", "low", 0))
        self.assertIsNone(spec)
        fallback.assert_not_called()

    def test_unavailable_manual_model_is_not_replaced(self):
        registry = ScriptModelRegistry(self.root, catalog=catalog({"gpt-6.1-sol": ["low", "high"]}))
        fallback = Mock(side_effect=AssertionError("manual request must not classify"))
        router = DemandRouter(registry, fallback=fallback)
        with self.assertRaisesRegex(ValueError, "当前模型暂不可用"):
            router.route(self.request(selection_mode="manual", model_id="gpt-6-astra", thinking_level="high"), {})
        fallback.assert_not_called()
        choice, _, spec = router.route(self.request(), {})
        self.assertEqual((choice["model_id"], choice["thinking_level"]), ("gpt-6.1-sol", "low"))
        self.assertEqual(spec["source"], "python")

    def test_template_default_precedes_recommendation_and_fallback(self):
        fallback = Mock(side_effect=AssertionError("template default must not classify"))
        choice, _, spec = DemandRouter(self.registry, fallback=fallback).route(
            self.request(input_request="请制作内容"),
            {"default_model_id": "gpt-6.1-sol", "default_thinking_level": "medium"})
        self.assertEqual((choice["model_id"], choice["thinking_level"]), ("gpt-6.1-sol", "medium"))
        self.assertIsNone(spec)
        fallback.assert_not_called()

    def test_python_rules_select_simple_standard_complex_and_long_duration(self):
        fallback = Mock(side_effect=AssertionError("recognized request must not call fallback"))
        router = DemandRouter(self.registry, fallback=fallback)
        cases = [(self.request(), "simple", "gpt-6-luna", "high"),
                 (self.request(generation_type="screenplay", input_request="两人重逢"), "standard", "gpt-6.1-sol", "high"),
                 (self.request(input_request="双线叙事、多时空和反转"), "complex", "gpt-6-astra", "high"),
                 (self.request(duration="12分钟"), "complex", "gpt-6-astra", "high")]
        for request, complexity, model, level in cases:
            with self.subTest(complexity=complexity, request=request):
                choice, _, spec = router.route(request, {})
                self.assertEqual((choice["model_id"], choice["thinking_level"]), (model, level))
                self.assertEqual((spec["source"], spec["complexity"]), ("python", complexity))
        fallback.assert_not_called()

    def test_fallback_only_supplies_demand_spec_and_cannot_choose_the_model(self):
        supplied = {"complexity": "simple", "confidence": 0.95, "reason": "单个视觉事件", "model_id": "gpt-6-astra",
                    "thinking_level": "max", "price": 999, "tool_calls": ["call_codex"]}
        fallback = Mock(return_value=deepcopy(supplied))
        choice, _, spec = DemandRouter(self.registry, fallback=fallback).route(self.request(input_request="请制作内容"), {})
        fallback.assert_called_once()
        self.assertEqual((choice["model_id"], choice["thinking_level"], choice["price"]), ("gpt-6-luna", "high", 0))
        self.assertEqual(set(spec), {"complexity", "confidence", "reason", "source"})
        self.assertEqual(spec["source"], "deepseek_flash")

    def test_ambiguous_invalid_or_low_confidence_fallback_does_not_route(self):
        invalid = (None, [], {"complexity": "unknown", "confidence": 0.9},
                   {"complexity": "complex", "confidence": 0.69}, {"complexity": "complex", "confidence": True},
                   {"complexity": "simple", "confidence": float("nan")})
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, "需求分析仍不明确"):
                DemandRouter(self.registry, fallback=lambda request: spec).route(self.request(input_request="请制作内容"), {})
        with self.assertRaisesRegex(ValueError, "未配置"):
            DemandRouter(self.registry).route(self.request(input_request="请制作内容"), {})


if __name__ == "__main__":
    unittest.main()
