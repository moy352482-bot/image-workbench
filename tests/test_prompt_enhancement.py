"""Offline acceptance for prompt enhancement: skills, validation, fallback and HTTP."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from image_workbench import StorageConfig, StorageManager
from image_workbench.contracts.prompt_enhancements import (
    PromptEnhancementRequest, PromptEnhancementResponse,
)
from image_workbench.deepseek_flash import (
    DeepSeekFlashClient, FlashConfigError, FlashRequestError,
)
from image_workbench.prompt_enhancement import (
    CallBudget, DeepSeekPromptModel, MockPromptModel, PromptEnhancementService,
    PromptModelEmpty, PromptModelError, PromptModelResult, PromptModelTimeout,
    PromptModelUnavailable,
)
from image_workbench.prompt_enhancement_skills import PromptSkillRegistry, PromptStyleLibrary
from image_workbench.web import create_app

AID = "a" * 32
WID = "b" * 32


def request(**changes):
    payload = {"request_id": uuid4().hex, "prompt": "一只橘猫坐在窗台上，午后阳光", "mode": "standard",
               "task_type": "generate", "aspect_ratio": "auto"}
    payload.update(changes)
    return payload


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


class RealishModel:
    """Non-mock test double: exercises budget and cost paths without network."""

    provider = "deepseek_flash"
    model = "deepseek-flash"
    is_mock = False

    def __init__(self, *, available=True):
        self.available_flag = available
        self.calls = 0
        self.content = json.dumps({"enhanced_prompt": "清晨的咖啡馆\n保留原图主体与文字",
                                   "intent_summary": "咖啡馆", "preserved_user_requirements": [],
                                   "added_details": [], "negative_constraints": [],
                                   "edit_plan": None}, ensure_ascii=False)

    def available(self):
        return self.available_flag

    def complete(self, *, system, user, context):
        self.calls += 1
        return PromptModelResult(self.content, provider=self.provider, model=self.model,
                                 is_mock=False, input_tokens=1_000_000, output_tokens=500_000)


class FakeFlashClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def settings(self):
        return {"model": "deepseek-flash"}

    def complete(self, **kwargs):
        if self.error is not None:
            raise self.error
        return self.result


class PromptEnhancementServiceTests(unittest.TestCase):
    def setUp(self):
        self.model = CountingModel()
        self.service = PromptEnhancementService(
            skills=PromptSkillRegistry(), styles=PromptStyleLibrary(), model=self.model)

    def test_four_skills_and_existing_style_library_are_reused(self):
        self.assertEqual({skill.skill_id for skill in self.service.skills.list()},
                         {"image-generation", "image-editing", "consistency", "cinematic"})
        styles = self.service.styles.list()
        self.assertEqual(len(styles), 15)
        self.assertTrue(all(item["style_id"] and item["name"] for item in styles))
        style = self.service.styles.prompt_context("cinematic-realism")
        self.assertEqual(style["name"], "叙事电影写实")
        self.assertTrue(style["visual_language"]["lighting"])
        self.assertIsNone(self.service.styles.prompt_context("not-a-style"))

    def test_classification_profiles_are_deterministic(self):
        cases = [
            ({"prompt": "一只橘猫坐在窗台上", "task_type": "generate"}, "image-generation", "generate", None),
            ({"prompt": "电影感的雨夜街头，一个人回头", "mode": "creative"}, "cinematic", "generate", "cinematic-realism"),
            ({"prompt": "同一个人物站在教室门口", "task_type": "reference_generate",
              "references": [{"index": 1, "role": "identity", "usage": "锁定人物身份"}]},
             "consistency", "reference_generate", None),
            ({"prompt": "把背景换成海边，保持人物的脸和衣服不变", "task_type": "edit",
              "references": [{"index": 1, "role": "edit_target", "usage": "作为底图"}]},
             "image-editing", "edit", None),
        ]
        for changes, skill_id, task_type, style_id in cases:
            with self.subTest(changes=changes):
                result = self.service.classify(request(**changes))
                self.assertEqual((result["skill"], result["task_type"], result["style_id"]),
                                 (skill_id, task_type, style_id))

    def test_text_to_image_returns_editable_prompt_and_preserves_original(self):
        payload = request(prompt="一张海边日落，画面里写“限时五折”")
        result = self.service.enhance(AID, WID, payload)
        self.assertEqual(result["status"], "enhanced")
        self.assertEqual(result["original_prompt"], payload["prompt"])
        self.assertIn(payload["prompt"], result["enhanced_prompt"])
        self.assertIn("限时五折", result["enhanced_prompt"])
        self.assertEqual(result["skill"]["skill_id"], "image-generation")
        self.assertEqual(result["usage"]["provider"], "mock")
        self.assertTrue(result["usage"]["is_mock"])
        self.assertEqual(result["usage"]["calls"], 1)
        self.assertEqual(result["source"]["kind"], "prompt_enhancement")
        self.assertEqual(result["source"]["skill_id"], "image-generation")
        self.assertIsNone(result["fallback"])
        for invented in ("耐克", "可口可乐", "张三", "主角"):
            self.assertNotIn(invented, result["enhanced_prompt"])
        PromptEnhancementResponse.model_validate(result)
        self.assertEqual(self.model.calls, 1)

    def test_edit_plan_records_targets_roles_and_aspect_inheritance(self):
        payload = request(
            prompt="把背景换成海边，保持人物的脸和衣服不变", task_type="edit",
            references=[
                {"index": 1, "role": "edit_target", "usage": "作为本次修改的底图"},
                {"index": 2, "role": "face", "usage": "锁定人物脸部"},
            ],
            edit={"change_targets": ["背景"], "preserve_targets": ["人物的脸和衣服"],
                  "forbidden_changes": ["不得改变人物身份"], "inherit_aspect_ratio": True})
        result = self.service.enhance(AID, WID, payload)
        self.assertEqual(result["status"], "enhanced")
        plan = result["edit_plan"]
        self.assertEqual(plan["change_targets"][0], "背景")
        self.assertEqual(plan["preserve_targets"][0], "人物的脸和衣服")
        self.assertEqual(plan["forbidden_changes"][0], "不得改变人物身份")
        self.assertTrue(plan["inherit_aspect_ratio"])
        self.assertEqual([item["index"] for item in plan["reference_roles"]], [1, 2])
        self.assertTrue(all(item["usage"] for item in plan["reference_roles"]))
        self.assertEqual([(item["index"], item["role"]) for item in result["references"]],
                         [(1, "edit_target"), (2, "face")])

    def test_request_contract_rejects_ambiguous_references_and_edits(self):
        cases = [
            {"prompt": "x", "references": [{"index": 1, "role": "other"}, {"index": 2, "role": "other"}]},
            {"prompt": "x", "references": [{"index": 1, "role": "edit_target"}, {"index": 1, "role": "face"}]},
            {"prompt": "x", "task_type": "edit", "references": [{"index": 1, "role": "identity"}]},
            {"prompt": "x", "edit": {"change_targets": ["背景"]}},
            {"prompt": "   "},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                PromptEnhancementRequest.model_validate(request(**changes))
        # A single base reference is enough; edit constraints may be omitted and
        # left for the model to derive from the original request.
        valid = PromptEnhancementRequest.model_validate(request(
            prompt="把背景换成海边", task_type="edit",
            references=[{"index": 1, "role": "edit_target"}]))
        self.assertEqual(valid.task_type, "edit")

    def test_model_failures_fall_back_to_original_once(self):
        cases = [
            ("empty_response", ScriptedModel("")),
            ("invalid_json", ScriptedModel("{not-json")),
            ("invalid_schema", ScriptedModel('{"intent_summary": "missing prompt"}')),
            ("timeout", ScriptedModel(error=PromptModelTimeout("timeout"))),
            ("model_unavailable", ScriptedModel(error=PromptModelUnavailable("disabled"))),
            ("model_error", ScriptedModel(error=RuntimeError("boom"))),
        ]
        for code, model in cases:
            with self.subTest(code=code):
                service = PromptEnhancementService(skills=PromptSkillRegistry(), styles=PromptStyleLibrary(), model=model)
                payload = request(prompt="一张产品图")
                result = service.enhance(AID, WID, payload)
                self.assertEqual(result["status"], "fallback")
                self.assertEqual(result["fallback"]["code"], code)
                self.assertEqual(result["enhanced_prompt"], payload["prompt"])
                self.assertEqual(result["usage"]["calls"], 0 if code == "model_unavailable" else 1)
                self.assertEqual(model.calls, 1)
                PromptEnhancementResponse.model_validate(result)

    def test_incomplete_edit_plan_falls_back(self):
        content = json.dumps({"enhanced_prompt": "修改后的画面", "intent_summary": "修改",
                              "preserved_user_requirements": [], "added_details": [],
                              "negative_constraints": [], "edit_plan": None}, ensure_ascii=False)
        model = ScriptedModel(content)
        service = PromptEnhancementService(skills=PromptSkillRegistry(), styles=PromptStyleLibrary(), model=model)
        payload = request(prompt="把背景换成海边", task_type="edit",
                          references=[{"index": 1, "role": "edit_target", "usage": "底图"}])
        result = service.enhance(AID, WID, payload)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["fallback"]["code"], "invalid_edit_plan")
        self.assertEqual(result["enhanced_prompt"], payload["prompt"])
        self.assertIsNone(result["edit_plan"])
        self.assertTrue(result["warnings"])
        self.assertEqual(model.calls, 1)

    def test_duplicate_request_is_cached_and_conflicting_payload_is_rejected(self):
        payload = request()
        first = self.service.enhance(AID, WID, payload)
        second = self.service.enhance(AID, WID, payload)
        self.assertEqual(first["enhancement_id"], second["enhancement_id"])
        self.assertEqual(self.model.calls, 1)
        from image_workbench.domain_errors import ConflictError
        with self.assertRaises(ConflictError):
            self.service.enhance(AID, WID, {**payload, "prompt": "另一张图"})

    def test_unknown_skill_style_and_limits_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Skill 不存在"):
            self.service.enhance(AID, WID, request(skill_id="missing-skill"))
        with self.assertRaisesRegex(ValueError, "风格不存在"):
            self.service.enhance(AID, WID, request(style_id="missing-style"))
        limited = PromptEnhancementService(skills=PromptSkillRegistry(), styles=PromptStyleLibrary(),
                                           model=MockPromptModel(), limits={"max_input_chars": 5})
        with self.assertRaisesRegex(ValueError, "不能超过"):
            limited.enhance(AID, WID, request(prompt="六六六六六六"))

    def test_compiled_prompt_contains_skill_style_reference_and_edit_rules(self):
        payload = PromptEnhancementRequest.model_validate(request(
            prompt="把背景换成海边，保持人物的脸和衣服不变", task_type="edit",
            references=[{"index": 1, "role": "edit_target", "usage": "作为底图"}],
            edit={"change_targets": ["背景"], "preserve_targets": ["人物的脸和衣服"],
                  "forbidden_changes": ["不得改变人物身份"]}))
        skill = self.service.skills.get("image-editing")
        style = self.service.styles.prompt_context("cinematic-realism")
        system = self.service.compile_system(skill, style, payload, "edit")
        for text in ("图片修改", "修改对象", "保留对象", "不允许改变", "图1", "画幅继承",
                     "叙事电影写实", "只输出一个 JSON", "edit_plan 必须非空"):
            self.assertIn(text, system)
        self.assertIn(payload.prompt, self.service.compile_user(payload))

    def test_all_five_profiles_compile_distinct_rules(self):
        profiles = [
            ("标准强化-文生图", {"prompt": "一只橘猫坐在窗台上"}, "image-generation", "generate"),
            ("创意强化-文生图", {"prompt": "一只橘猫坐在窗台上", "mode": "creative"},
             "image-generation", "generate"),
            ("图片参考生成", {"prompt": "这个人物站在教室门口", "task_type": "reference_generate",
                              "references": [{"index": 1, "role": "identity", "usage": "锁定人物身份"}]},
             "consistency", "reference_generate"),
            ("精确图片修改", {"prompt": "把背景换成海边", "task_type": "edit",
                              "references": [{"index": 1, "role": "edit_target", "usage": "作为底图"}]},
             "image-editing", "edit"),
            ("电影感强化", {"prompt": "电影感的雨夜街头，一个人回头", "mode": "creative"},
             "cinematic", "generate"),
        ]
        for name, changes, skill_id, task_type in profiles:
            with self.subTest(name=name):
                parsed = PromptEnhancementRequest.model_validate(request(**changes))
                normalized = ("reference_generate" if parsed.task_type == "generate" and parsed.references
                              else parsed.task_type)
                skill = self.service._select_skill(parsed, normalized)
                style_id = parsed.style_id or skill.rules.style_default
                style = self.service.styles.prompt_context(style_id)
                system = self.service.compile_system(skill, style, parsed, normalized)
                self.assertEqual((skill.skill_id, normalized), (skill_id, task_type))
                self.assertIn("只输出一个 JSON", system)
                self.assertIn("强化模式：" + ("创意强化" if parsed.mode == "creative" else "标准强化"), system)
                if parsed.references:
                    self.assertIn("参考图片角色", system)
                    self.assertIn("图1", system)
                if task_type == "edit":
                    self.assertIn("edit_plan 必须非空", system)
                else:
                    self.assertIn("edit_plan 必须为 null", system)

    def test_real_provider_records_tokens_cost_and_enforces_budget(self):
        model = RealishModel()
        service = PromptEnhancementService(
            skills=PromptSkillRegistry(), styles=PromptStyleLibrary(), model=model,
            budget=CallBudget(per_hour=1, per_day=1),
            pricing={"input_per_million": 1.0, "output_per_million": 2.0, "currency": "USD"})
        first = service.enhance(AID, WID, request(prompt="一间清晨的咖啡馆"))
        self.assertEqual(first["status"], "enhanced")
        self.assertEqual(first["usage"]["input_tokens"], 1_000_000)
        self.assertEqual(first["usage"]["output_tokens"], 500_000)
        self.assertAlmostEqual(first["usage"]["estimated_cost"], 2.0)
        self.assertEqual(first["usage"]["currency"], "USD")
        second = service.enhance(AID, WID, request(prompt="另一间咖啡馆"))
        self.assertEqual(second["status"], "fallback")
        self.assertEqual(second["fallback"]["code"], "budget_exceeded")
        self.assertEqual(second["usage"]["calls"], 0)
        self.assertEqual(second["enhanced_prompt"], "另一间咖啡馆")
        self.assertEqual(model.calls, 1)
        # A sibling workspace has its own budget, so isolation is not bypassed
        # by one workspace exhausting another's allowance.
        other = service.enhance(AID, "c" * 32, request(prompt="第三间咖啡馆"))
        self.assertEqual(other["status"], "enhanced")
        self.assertEqual(model.calls, 2)

    def test_unavailable_real_provider_does_not_consume_budget_or_call(self):
        model = RealishModel(available=False)
        service = PromptEnhancementService(
            skills=PromptSkillRegistry(), styles=PromptStyleLibrary(), model=model,
            budget=CallBudget(per_hour=1, per_day=1))
        result = service.enhance(AID, WID, request())
        self.assertEqual(result["fallback"]["code"], "model_unavailable")
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(model.calls, 0)
        self.assertTrue(service.budget.allow((str(AID), str(WID))))

    def test_provider_factory_can_select_deepseek_without_making_a_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompt_enhancement.json"
            path.write_text(json.dumps({"provider": "deepseek_flash", "limits": {
                "max_output_tokens": 800, "llm_timeout_seconds": 7,
                "max_real_calls_per_hour": 2, "max_real_calls_per_day": 4}}), encoding="utf-8")
            service = PromptEnhancementService.from_config(path)
        self.assertIsInstance(service.model, DeepSeekPromptModel)
        self.assertFalse(service.model.is_mock)
        self.assertEqual(service.model.max_tokens, 800)
        self.assertEqual(service.model.timeout_seconds, 7)
        self.assertEqual((service.budget.per_hour, service.budget.per_day), (2, 4))

    def test_quoted_exact_text_loss_is_reported_as_warning(self):
        content = json.dumps({"enhanced_prompt": "一张海报", "intent_summary": "海报",
                              "preserved_user_requirements": [], "added_details": [],
                              "negative_constraints": [], "edit_plan": None}, ensure_ascii=False)
        service = PromptEnhancementService(skills=PromptSkillRegistry(), styles=PromptStyleLibrary(),
                                           model=ScriptedModel(content))
        result = service.enhance(AID, WID, request(prompt="海报上写“限时五折”"))
        self.assertEqual(result["status"], "enhanced")
        self.assertTrue(any("限时五折" in item for item in result["warnings"]))


class DeepSeekFlashAdapterTests(unittest.TestCase):
    def test_default_flash_reuses_the_deployed_text_api_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "deepseek.json"
            config.write_text(json.dumps({"api_key": "test-shared-key"}), encoding="utf-8")
            with patch.dict("os.environ", {"IMAGE_WORKBENCH_TEXT_API_CONFIG": str(config)}, clear=True):
                settings = DeepSeekFlashClient().settings()
            self.assertEqual(settings["endpoint"], "https://api.deepseek.com/chat/completions")
            self.assertEqual(settings["model"], "deepseek-flash")
            self.assertEqual(settings["api_key"], "test-shared-key")

    def test_invalid_shared_config_is_unavailable_without_a_request(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "deepseek.json"
            config.write_text("invalid json", encoding="utf-8")
            with patch.dict("os.environ", {"IMAGE_WORKBENCH_TEXT_API_CONFIG": str(config)}, clear=True):
                with patch("image_workbench.deepseek_flash.urllib.request.urlopen") as send:
                    with self.assertRaises(FlashConfigError):
                        DeepSeekFlashClient().complete(system="s", user="u")
                send.assert_not_called()

    def test_disabled_and_incomplete_configuration_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "script_routing.json").write_text(json.dumps(
                {"deepseek_flash": {"enabled": False, "endpoint_env": "E", "model_env": "M", "api_key_env": "K"}}),
                encoding="utf-8")
            with self.assertRaises(FlashConfigError) as caught:
                DeepSeekFlashClient(root).complete(system="s", user="u")
            self.assertEqual(caught.exception.reason, "disabled")

    def test_enabled_client_posts_json_and_parses_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "script_routing.json").write_text(json.dumps(
                {"deepseek_flash": {"enabled": True, "endpoint_env": "IW_TEST_ENDPOINT",
                                    "model_env": "IW_TEST_MODEL", "api_key_env": "IW_TEST_KEY",
                                    "timeout_seconds": 5}}), encoding="utf-8")
            response = io.BytesIO(json.dumps({
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode())
            response.__enter__ = lambda stream: stream
            response.__exit__ = lambda *args: False
            captured = {}

            def fake_urlopen(request, timeout=None):
                captured["url"], captured["timeout"], captured["body"] = request.full_url, timeout, json.loads(request.data)
                captured["auth"] = request.headers.get("Authorization")
                return response

            with patch.dict("os.environ", {"IW_TEST_ENDPOINT": "https://flash.example/v1/chat",
                                           "IW_TEST_MODEL": "deepseek-flash", "IW_TEST_KEY": "secret"}):
                with patch("image_workbench.deepseek_flash.urllib.request.urlopen", fake_urlopen):
                    result = DeepSeekFlashClient(root).complete(system="system", user="user", max_tokens=300)
            self.assertEqual(result["content"], '{"ok": true}')
            self.assertEqual(result["model"], "deepseek-flash")
            self.assertEqual(result["usage"]["prompt_tokens"], 7)
            self.assertEqual(captured["timeout"], 5)
            self.assertEqual(captured["auth"], "Bearer secret")
            self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
            self.assertEqual(captured["body"]["thinking"], {"type": "disabled"})
            self.assertEqual(captured["body"]["messages"][0], {"role": "system", "content": "system"})

    def test_timeout_is_reported_as_a_bounded_request_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "script_routing.json").write_text(json.dumps(
                {"deepseek_flash": {"enabled": True, "endpoint_env": "IW_TEST_ENDPOINT",
                                    "model_env": "IW_TEST_MODEL", "api_key_env": "IW_TEST_KEY",
                                    "timeout_seconds": 1}}), encoding="utf-8")
            with patch.dict("os.environ", {"IW_TEST_ENDPOINT": "https://flash.example/v1/chat",
                                           "IW_TEST_MODEL": "deepseek-flash", "IW_TEST_KEY": "secret"}):
                with patch("image_workbench.deepseek_flash.urllib.request.urlopen",
                           side_effect=TimeoutError("timed out")):
                    with self.assertRaises(FlashRequestError) as caught:
                        DeepSeekFlashClient(root).complete(system="s", user="u")
            self.assertEqual(caught.exception.reason, "timeout")


class DeepSeekPromptModelTests(unittest.TestCase):
    def test_adapter_usage_is_carried_into_the_result(self):
        client = FakeFlashClient(result={
            "content": '{"enhanced_prompt": "x"}', "model": "deepseek-flash-2026",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        result = DeepSeekPromptModel(client).complete(system="s", user="u", context={})
        self.assertEqual(result.model, "deepseek-flash-2026")
        self.assertFalse(result.is_mock)
        self.assertEqual((result.input_tokens, result.output_tokens), (7, 3))

    def test_adapter_errors_map_to_bounded_fallback_codes(self):
        cases = [
            (FlashConfigError("disabled"), PromptModelUnavailable, "model_unavailable"),
            (FlashRequestError("timeout"), PromptModelTimeout, "timeout"),
            (FlashRequestError("empty"), PromptModelEmpty, "empty_response"),
            (FlashRequestError("request_failed"), PromptModelError, "model_error"),
        ]
        for error, expected, code in cases:
            with self.subTest(code=code), self.assertRaises(expected) as caught:
                DeepSeekPromptModel(FakeFlashClient(error=error)).complete(system="s", user="u", context={})
            self.assertEqual(caught.exception.code, code)

    def test_adapter_empty_response_falls_back_without_losing_the_original(self):
        service = PromptEnhancementService(
            skills=PromptSkillRegistry(), styles=PromptStyleLibrary(),
            model=DeepSeekPromptModel(FakeFlashClient(error=FlashRequestError("empty"))))
        payload = request(prompt="一张产品图")
        result = service.enhance(AID, WID, payload)
        self.assertEqual(result["fallback"]["code"], "empty_response")
        self.assertEqual(result["enhanced_prompt"], payload["prompt"])
        self.assertEqual(result["usage"]["calls"], 1)
        self.assertFalse(result["usage"]["is_mock"])


class PromptEnhancementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.storage = StorageManager(StorageConfig(Path(self.temp.name) / "data"))
        self.app = create_app(self.storage, backend="local")
        self.alice = self.user("alice")
        self.bobby = self.user("bobby")
        self.counting = CountingModel()
        self.app.prompt_enhancement_service.model = self.counting

    def client(self):
        client = TestClient(self.app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def user(self, username):
        client = self.client()
        csrf = client.get("/api/auth/config").json()["csrf_token"]
        credentials = {"username": username, "password": "test-password-123"}
        headers = {"X-Workbench-CSRF": csrf}
        self.assertEqual(client.post("/api/auth/register", json=credentials, headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/auth/login", json=credentials, headers=headers).status_code, 200)
        client.headers["X-Workbench-CSRF"] = client.get("/api/config").json()["csrf_token"]
        return client

    def test_authenticated_enhancement_returns_editable_result_without_generation(self):
        wallet = self.alice.get("/api/credits").json()["credits"]
        payload = request(prompt="一间被晨光照亮的极简咖啡馆，木桌上放着一杯拿铁")
        response = self.alice.post("/api/prompt-enhancements", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "enhanced")
        self.assertEqual(body["original_prompt"], payload["prompt"])
        self.assertIn(payload["prompt"], body["enhanced_prompt"])
        self.assertTrue(body["usage"]["is_mock"])
        self.assertEqual(body["usage"]["calls"], 1)
        self.assertEqual(self.counting.calls, 1)
        self.assertEqual(self.alice.get("/api/credits").json()["credits"], wallet)
        self.assertEqual(self.app.state.workbench.list_jobs(), [])
        self.assertEqual(self.alice.get("/api/main-projects").json()["main_projects"], [])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_guard_requires_login_and_csrf(self):
        anonymous = self.client()
        csrf = anonymous.get("/api/auth/config").json()["csrf_token"]
        response = anonymous.post("/api/prompt-enhancements", json=request(), headers={"X-Workbench-CSRF": csrf})
        self.assertEqual(response.status_code, 401)
        response = self.alice.post("/api/prompt-enhancements", json=request(),
                                   headers={"X-Workbench-CSRF": "bad-token"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counting.calls, 0)

    def test_invalid_requests_are_rejected_before_any_model_call(self):
        cases = [
            ({"prompt": ""}, 422),
            ({"prompt": "x" * 4001}, 422),
            ({"references": [{"index": i, "role": "other", "usage": "用途"} for i in range(1, 10)]}, 422),
            ({"references": [{"index": 1, "role": "other"}, {"index": 2, "role": "other"}]}, 422),
            ({"task_type": "edit", "references": [{"index": 1, "role": "identity", "usage": "身份"}]}, 422),
            ({"skill_id": "missing-skill"}, 400),
            ({"style_id": "missing-style"}, 400),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                response = self.alice.post("/api/prompt-enhancements", json=request(**changes))
                self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.counting.calls, 0)

    def test_idempotency_and_workspace_isolation_do_not_share_cache(self):
        payload = request()
        first = self.alice.post("/api/prompt-enhancements", json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        repeated = self.alice.post("/api/prompt-enhancements", json=payload)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(first.json()["enhancement_id"], repeated.json()["enhancement_id"])
        self.assertEqual(self.counting.calls, 1)
        # The same request id from another account and workspace must not reuse
        # Alice's cached result.
        other = self.bobby.post("/api/prompt-enhancements", json=payload)
        self.assertEqual(other.status_code, 200, other.text)
        self.assertNotEqual(other.json()["enhancement_id"], first.json()["enhancement_id"])
        self.assertEqual(self.counting.calls, 2)
        workspace = self.alice.post("/api/workspaces", json={"name": "第二个空间"}).json()["workspace_id"]
        third = self.alice.post("/api/prompt-enhancements", json=payload,
                                headers={"X-Workspace-ID": workspace})
        self.assertEqual(third.status_code, 200, third.text)
        self.assertEqual(self.counting.calls, 3)

    def test_edit_http_contract_keeps_user_constraints(self):
        payload = request(
            prompt="把背景换成海边，保持人物的脸和衣服不变", task_type="edit",
            references=[{"index": 1, "role": "edit_target", "usage": "作为底图"},
                        {"index": 2, "role": "face", "usage": "锁定脸部"}],
            edit={"change_targets": ["背景"], "preserve_targets": ["人物的脸和衣服"],
                  "forbidden_changes": ["不得改变人物身份"]})
        response = self.alice.post("/api/prompt-enhancements", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["task_type"], "edit")
        self.assertEqual(body["skill"]["skill_id"], "image-editing")
        self.assertEqual([item["index"] for item in body["edit_plan"]["reference_roles"]], [1, 2])
        self.assertEqual(body["edit_plan"]["change_targets"], ["背景"])


if __name__ == "__main__":
    unittest.main()
