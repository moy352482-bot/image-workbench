"""Offline acceptance matrix and statistics for prompt enhancement.

The matrix never calls a paid model or the image backend.  It uses the
deterministic mock plus scripted failures, so it is safe to run before the
real-image A/B plan is approved.
"""
import ast
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import unittest

from image_workbench import __file__ as package_file
from image_workbench.contracts.prompt_enhancements import PromptEnhancementResponse
from image_workbench.domain_errors import ConflictError
from image_workbench.prompt_enhancement import (
    MockPromptModel, PromptEnhancementService, PromptModelError, PromptModelResult,
    PromptModelTimeout, PromptModelUnavailable,
)
from image_workbench.prompt_enhancement_skills import PromptSkillRegistry, PromptStyleLibrary
from tests.prompt_enhancement_cases import (
    ACCOUNT_ID, CASES, WORKSPACE_ID, payload_for,
)


class CountingModel(MockPromptModel):
    def __init__(self):
        self.calls = 0

    def complete(self, *, system, user, context):
        self.calls += 1
        return super().complete(system=system, user=user, context=context)


class ScriptedModel:
    provider = "test"
    model = "scripted"
    is_mock = True

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = 0

    def complete(self, *, system, user, context):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return PromptModelResult(self.content, provider=self.provider, model=self.model, is_mock=True)


def _draft(**changes):
    payload = {"enhanced_prompt": "强化后的画面", "intent_summary": "测试",
               "preserved_user_requirements": [], "added_details": [],
               "negative_constraints": [], "edit_plan": None}
    payload.update(changes)
    return json.dumps(payload, ensure_ascii=False)


def build_model(kind):
    if kind == "mock":
        return CountingModel()
    if kind == "empty":
        return ScriptedModel("")
    if kind == "invalid_json":
        return ScriptedModel("{not-json")
    if kind == "invalid_schema":
        return ScriptedModel('{"intent_summary": "缺少强化结果"}')
    if kind == "too_long":
        return ScriptedModel(_draft(enhanced_prompt="长" * 12001))
    if kind == "missing_edit_plan":
        return ScriptedModel(_draft())
    if kind == "timeout":
        return ScriptedModel(error=PromptModelTimeout("timeout"))
    if kind == "unavailable":
        return ScriptedModel(error=PromptModelUnavailable("disabled"))
    if kind == "model_error":
        return ScriptedModel(error=PromptModelError("boom"))
    raise ValueError("未知的测试模型：" + kind)


def evaluate_case(case):
    model = build_model(case.get("model", "mock"))
    service = PromptEnhancementService(
        skills=PromptSkillRegistry(), styles=PromptStyleLibrary(), model=model)
    payload = payload_for(case)
    started = time.perf_counter()
    response = None
    error = None
    try:
        response = service.enhance(ACCOUNT_ID, WORKSPACE_ID, payload)
    except Exception as exc:  # A contract rejection is an expected outcome for some cases.
        error = exc
    elapsed_ms = (time.perf_counter() - started) * 1000
    schema_ok = False
    if response is not None:
        try:
            PromptEnhancementResponse.model_validate(response)
            schema_ok = True
        except Exception:
            schema_ok = False
    return {"case": case, "payload": payload, "response": response, "error": error,
            "elapsed_ms": elapsed_ms, "schema_ok": schema_ok, "model_calls": model.calls}


def case_failures(observed):
    case = observed["case"]
    expect = case["expect"]
    response = observed["response"]
    error = observed["error"]
    failures = []
    if expect["status"] == "rejected":
        if error is None:
            failures.append("expected a contract rejection")
        if observed["model_calls"] != expect.get("model_calls", 0):
            failures.append(f"model calls {observed['model_calls']} != {expect.get('model_calls', 0)}")
        return failures
    if error is not None:
        return [f"unexpected error: {type(error).__name__}: {error}"]
    if not isinstance(response, dict):
        return ["no response"]
    if response.get("status") != expect["status"]:
        failures.append(f"status {response.get('status')!r} != {expect['status']!r}")
    if "code" in expect:
        code = (response.get("fallback") or {}).get("code")
        if code != expect["code"]:
            failures.append(f"fallback code {code!r} != {expect['code']!r}")
    if "calls" in expect and response["usage"]["calls"] != expect["calls"]:
        failures.append(f"usage calls {response['usage']['calls']} != {expect['calls']}")
    if "skill" in expect and response["skill"]["skill_id"] != expect["skill"]:
        failures.append(f"skill {response['skill']['skill_id']!r} != {expect['skill']!r}")
    if "task_type" in expect and response["task_type"] != expect["task_type"]:
        failures.append(f"task_type {response['task_type']!r} != {expect['task_type']!r}")
    if expect.get("original_exact") and response["original_prompt"] != observed["payload"]["prompt"]:
        failures.append("original prompt was not preserved exactly")
    enhanced = response["enhanced_prompt"]
    for text in expect.get("contains", []):
        if text not in enhanced:
            failures.append(f"missing text: {text}")
    for text in expect.get("absent", []):
        if text in enhanced:
            failures.append(f"unexpected invented text: {text}")
    negative = "\n".join(response.get("negative_constraints") or [])
    for text in expect.get("negative_contains", []):
        if text not in negative:
            failures.append(f"negative constraint missing: {text}")
    warnings = "\n".join(response.get("warnings") or [])
    for text in expect.get("warnings_absent", []):
        if text in warnings:
            failures.append(f"unexpected warning: {text}")
    if "references" in expect:
        actual = [[item["index"], item["role"]] for item in response.get("references") or []]
        if actual != expect["references"]:
            failures.append(f"references {actual} != {expect['references']}")
    if expect.get("reference_usage"):
        if not all(item.get("usage") for item in response.get("references") or []):
            failures.append("a reference has no usage")
    plan = response.get("edit_plan")
    if "edit_change" in expect:
        if not plan or plan.get("change_targets") != expect["edit_change"]:
            failures.append(f"change_targets mismatch: {plan and plan.get('change_targets')}")
    if "edit_preserve" in expect:
        if not plan or plan.get("preserve_targets") != expect["edit_preserve"]:
            failures.append(f"preserve_targets mismatch: {plan and plan.get('preserve_targets')}")
    if "edit_forbidden" in expect:
        if not plan or plan.get("forbidden_changes") != expect["edit_forbidden"]:
            failures.append(f"forbidden_changes mismatch: {plan and plan.get('forbidden_changes')}")
    if expect.get("edit_nonempty"):
        if not plan or not (plan.get("change_targets") and plan.get("preserve_targets")
                            and plan.get("forbidden_changes")):
            failures.append("edit plan is incomplete")
    if "inherit" in expect and plan and plan.get("inherit_aspect_ratio") != expect["inherit"]:
        failures.append(f"inherit_aspect_ratio {plan.get('inherit_aspect_ratio')} != {expect['inherit']}")
    if response["usage"]["calls"] and observed["model_calls"] != 1:
        failures.append(f"expected exactly one model call, got {observed['model_calls']}")
    return failures


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)], 2)


def summarize(observed):
    failures = {item["case"]["id"]: case_failures(item) for item in observed}
    produced = [item for item in observed if item["response"] is not None]
    enhanced_expected = [item for item in observed if item["case"]["expect"]["status"] == "enhanced"]
    fallback_expected = [item for item in observed if item["case"]["expect"]["status"] == "fallback"]
    rejected_expected = [item for item in observed if item["case"]["expect"]["status"] == "rejected"]
    enhanced_ok = sum(1 for item in enhanced_expected
                      if item["response"] and item["response"]["status"] == "enhanced")
    fallback_ok = sum(1 for item in fallback_expected
                      if item["response"] and item["response"]["status"] == "fallback")
    rejected_ok = sum(1 for item in rejected_expected if item["error"] is not None)
    schema_pass = sum(1 for item in produced if item["schema_ok"])
    recovered = sum(1 for item in produced
                    if item["response"]["original_prompt"] == item["payload"]["prompt"])
    latencies = [item["elapsed_ms"] for item in produced]
    reasons = Counter(item["response"]["fallback"]["code"] for item in produced
                      if item["response"]["status"] == "fallback")
    stats = {
        "provider": "mock/offline",
        "total_requirements": len(observed),
        "enhanced_cases": len(enhanced_expected),
        "fallback_cases": len(fallback_expected),
        "rejected_cases": len(rejected_expected),
        "enhancement_success_rate": round(enhanced_ok / len(enhanced_expected), 4)
        if enhanced_expected else None,
        "fallback_handling_rate": round(fallback_ok / len(fallback_expected), 4)
        if fallback_expected else None,
        "contract_rejection_rate": round(rejected_ok / len(rejected_expected), 4)
        if rejected_expected else None,
        "schema_pass_rate": round(schema_pass / len(produced), 4) if produced else None,
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "failure_reasons": dict(sorted(reasons.items())),
        "original_recovery_rate": round(recovered / len(produced), 4) if produced else None,
        "unexpected_cases": sorted(case_id for case_id, items in failures.items() if items),
        "user_acceptance": "未采集（离线测试，无真实用户数据）",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": [{
            "id": item["case"]["id"], "category": item["case"]["category"],
            "model": item["case"].get("model", "mock"),
            "status": (item["response"] or {}).get("status") or "rejected",
            "fallback_code": ((item["response"] or {}).get("fallback") or {}).get("code"),
            "schema_ok": item["schema_ok"], "elapsed_ms": round(item["elapsed_ms"], 2),
        } for item in observed],
    }
    return stats, failures


class PromptEnhancementMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.observed = [evaluate_case(case) for case in CASES]
        cls.stats, cls.failures = summarize(cls.observed)

    def test_fixed_requirements_hold(self):
        self.assertGreaterEqual(len(CASES), 30, "at least 30 fixed requirements are required")
        for observed in self.observed:
            with self.subTest(case=observed["case"]["id"], category=observed["case"]["category"]):
                failures = case_failures(observed)
                if failures:
                    self.fail("; ".join(failures))

    def test_offline_statistics_meet_thresholds(self):
        stats = self.stats
        self.assertEqual(stats["unexpected_cases"], [])
        self.assertEqual(stats["enhancement_success_rate"], 1.0)
        self.assertEqual(stats["fallback_handling_rate"], 1.0)
        self.assertEqual(stats["contract_rejection_rate"], 1.0)
        self.assertEqual(stats["schema_pass_rate"], 1.0)
        self.assertEqual(stats["original_recovery_rate"], 1.0)
        self.assertEqual(stats["failure_reasons"], {
            "empty_response": 1, "invalid_json": 1, "invalid_schema": 2,
            "invalid_edit_plan": 1, "timeout": 1, "model_unavailable": 1, "model_error": 1,
        })
        self.assertIsNotNone(stats["average_latency_ms"])
        self.assertIsNotNone(stats["p95_latency_ms"])
        target = os.environ.get("IW_PROMPT_EVAL_REPORT")
        if target:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print("prompt enhancement offline stats: " + json.dumps({
            key: stats[key] for key in ("total_requirements", "enhancement_success_rate",
                                        "schema_pass_rate", "average_latency_ms",
                                        "p50_latency_ms", "p95_latency_ms",
                                        "failure_reasons", "original_recovery_rate")
        }, ensure_ascii=False))

    def test_unnecessary_calls_are_avoided_and_workspaces_are_isolated(self):
        model = CountingModel()
        service = PromptEnhancementService(
            skills=PromptSkillRegistry(), styles=PromptStyleLibrary(), model=model)
        payload = payload_for(CASES[0])
        first = service.enhance(ACCOUNT_ID, WORKSPACE_ID, payload)
        repeated = service.enhance(ACCOUNT_ID, WORKSPACE_ID, payload)
        self.assertEqual(first["enhancement_id"], repeated["enhancement_id"])
        self.assertEqual(model.calls, 1)
        with self.assertRaises(ConflictError):
            service.enhance(ACCOUNT_ID, WORKSPACE_ID, {**payload, "prompt": "另一张图"})
        self.assertEqual(model.calls, 1)
        other_workspace = service.enhance(ACCOUNT_ID, "c" * 32, payload)
        self.assertNotEqual(other_workspace["enhancement_id"], first["enhancement_id"])
        self.assertEqual(model.calls, 2)
        with self.assertRaises(ValueError):
            service.enhance(ACCOUNT_ID, WORKSPACE_ID, {**payload, "request_id": "d" * 32, "prompt": "   "})
        self.assertEqual(model.calls, 2)

    def test_enhancement_has_no_generation_or_storage_imports(self):
        source = Path(package_file).with_name("prompt_enhancement.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        forbidden = {"fastapi", "starlette", "image_workbench.storage", "image_workbench.identity"}
        self.assertFalse(any(name == item or name.startswith(item + ".") for name in names for item in forbidden))
        self.assertFalse(any("storage" in name.lower() or "identity" in name.lower()
                             or "image_backend" in name.lower() for name in names))
        self.assertNotIn("ImageBackend(", source)
        self.assertNotIn("StorageManager(", source)
        self.assertNotIn("IdentityStore(", source)
        self.assertNotIn("reserve(", source)
        self.assertNotIn("record_job(", source)


if __name__ == "__main__":
    unittest.main()
