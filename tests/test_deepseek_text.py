import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from image_workbench.deepseek_text import DeepSeekTextAdapter, MODEL
from image_workbench.domain_errors import TextGenerationError
from image_workbench.seedance_skill import SeedanceSkillPlanner, TASK_FIELDS
from image_workbench.script_api_models import DeepSeekModelRegistry
from image_workbench.script_models import DemandRouter


class DeepSeekTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.requests = []
        self.reply = {"id": "test-response", "choices": [{"finish_reason": "stop", "message": {"content": "完整正文"}}]}
        self.status = 200
        def request(req):
            self.requests.append(req)
            return httpx.Response(self.status, json=self.reply)
        self.client = httpx.Client(transport=httpx.MockTransport(request))
        self.addCleanup(self.client.close)
        self.adapter = DeepSeekTextAdapter("test-secret", client=self.client)
        self.args = {"prompt": "冻结的 Skill 与需求", "model_id": MODEL, "thinking_level": "high", "work_dir": self.work}

    def test_receipt_recovery_never_calls_api_or_stores_key(self):
        self.assertEqual(self.adapter.generate(**self.args), "完整正文")
        self.assertEqual(self.adapter.recover(**self.args), "完整正文")
        self.assertEqual(len(self.requests), 1)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], MODEL)
        self.assertEqual(body["messages"][0]["content"], self.args["prompt"])
        self.assertNotIn("tools", body)
        for path in self.work.iterdir():
            self.assertNotIn("test-secret", path.read_text(encoding="utf-8"))
        with self.assertRaises(TextGenerationError) as caught:
            self.adapter.generate(**self.args)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(self.requests), 1)

    def test_tampered_response_and_changed_prompt_cannot_recover(self):
        self.adapter.generate(**self.args)
        with self.assertRaises(TextGenerationError):
            self.adapter.recover(**{**self.args, "prompt": "changed"})
        (self.work / "response.txt").write_text("tampered", encoding="utf-8")
        with self.assertRaises(TextGenerationError):
            self.adapter.recover(**self.args)
        self.assertEqual(len(self.requests), 1)

    def test_incomplete_response_is_failed_without_receipt_or_retry(self):
        self.reply["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(TextGenerationError) as caught:
            self.adapter.generate(**self.args)
        self.assertFalse(caught.exception.uncertain)
        self.assertFalse((self.work / "completed.json").exists())
        self.assertEqual(len(self.requests), 1)

    def test_http_rejections_are_sanitized_and_unknown_results_are_uncertain(self):
        for code, uncertain in [(401, False), (402, False), (429, False), (500, True), (408, True)]:
            with self.subTest(code=code):
                self.status = code
                self.reply = {"error": "test-secret upstream details"}
                with self.assertRaises(TextGenerationError) as caught:
                    self.adapter.generate(**{**self.args, "work_dir": self.work / str(code)})
                self.assertEqual(caught.exception.uncertain, uncertain)
                self.assertNotIn("test-secret", str(caught.exception))

    def test_timeout_preserves_intent_without_automatic_replay(self):
        def timeout(req):
            raise httpx.ReadTimeout("private response details")
        with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            adapter = DeepSeekTextAdapter("test-secret", client=client)
            with self.assertRaises(TextGenerationError) as caught:
                adapter.generate(**self.args)
            self.assertTrue(caught.exception.uncertain)
            self.assertTrue((self.work / "request.json").exists())
            with self.assertRaises(TextGenerationError):
                adapter.recover(**self.args)

    def test_missing_key_and_wrong_model_do_not_make_requests(self):
        with self.assertRaises(TextGenerationError):
            DeepSeekTextAdapter("", client=self.client).generate(**self.args)
        with self.assertRaises(TextGenerationError):
            self.adapter.generate(**{**self.args, "model_id": "gpt-6-luna"})
        self.assertEqual(self.requests, [])

    def test_fixed_routing_preserves_skill_choice_without_classifier_call(self):
        registry = DeepSeekModelRegistry(available=True)
        router = DemandRouter(registry)
        with patch.object(router, "_flash", side_effect=AssertionError("unexpected extra API call")):
            choice, _, _ = router.route({"generation_type": "screenplay", "input_request": "写一段"},
                                        {"default_model_id": "gpt-6-astra", "default_thinking_level": "high"})
        self.assertEqual((choice["model_id"], choice["price"]), (MODEL, 0))
        self.assertEqual([m["model_id"] for m in registry.models()], [MODEL])

    def test_seedance_uses_frozen_skill_and_checks_parameters_and_dialogue(self):
        planner = SeedanceSkillPlanner(self.adapter)
        task = {"mode": "T2V", "duration": 5, "resolution": "720p", "aspect_ratio": "landscape",
                "width": 1280, "height": 720, "reference_ids": [], "prompt": "主角说：“回家吧。”"}
        request = planner.request(task)
        plan = {"schema_version": 1, **{k: task[k] for k in TASK_FIELDS}, "prompt": "海边，主角转身轻声说：“回家吧。”"}
        self.reply["choices"][0]["message"]["content"] = json.dumps(plan, ensure_ascii=False)
        self.assertEqual(planner.run(request, self.work), plan)
        self.assertEqual(planner.run(request, self.work), plan)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Seedance 2.0 镜头提示词", json.loads(self.requests[0].content)["messages"][0]["content"])
        for changes in ({"duration": 10}, {"prompt": "删掉原对白"}, {"reference_ids": ["invented"]}):
            with self.assertRaises(ValueError):
                planner.validate(json.dumps({**plan, **changes}), request)
