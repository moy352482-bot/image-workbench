"""Exercise the Codex JSON boundary with a fake adapter, never a model process."""
import json
import unittest
from uuid import uuid4

from image_workbench.script_models import ScriptModelRegistry
from image_workbench.storyboard_planner import MockStoryboardAdapter
from tests import test_storyboard_http as http_fixtures
from tests.test_script_service import TEXT


class StoryboardPlannerTests(unittest.TestCase):
    def setUp(self):
        # Reuse the HTTP fixture without inheriting and rerunning its test cases.
        self.http = http_fixtures.StoryboardHTTPTests()
        self.addCleanup(self.http.doCleanups)
        self.http.setUp()
        self.adapter = self.http.set_storyboard_pricing({"gpt-6.1-sol": {"medium": 7}})
        self.adapter.generate.side_effect = None
        self.http.app.storyboard_service.registry = ScriptModelRegistry(catalog={"models": [
            {"slug": "gpt-6.1-sol", "supported_reasoning_levels": [{"effort": "medium"}]},
            {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "high"}]},
        ]})

    def valid(self, **changes):
        shot = {"title": "海边挥手", "scene_id": "海边", "narrative_duration_ms": 4000,
                "generation_duration_ms": 4000, "duration_reason": "挥手动作与等待归船的情绪停顿需要4秒",
                "visual_description": "守灯人站在海边朝归船挥手", "action": "抬手挥动", "dialogue": "回家吧。",
                "reference_artifact_ids": [], "aspect_ratio": "16:9", "video_model_id": "h3"}
        shot.update(changes)
        return {"shots": [shot], "duration_conflict": None}

    def generate(self, output, **changes):
        source = self.http.upload()
        self.adapter.generate.return_value = output
        body = self.http.options(mode="codex", selection_mode="manual", model_id="gpt-6.1-sol", thinking_level="medium",
                                 target_duration_ms=4000, source_script_version=source["version_id"], **changes)
        quote = self.http.post("/api/storyboards/quote", body)
        self.assertTrue(quote["can_generate"], quote)
        task = self.http.post("/api/storyboards/tasks", {**body, "request_id": uuid4().hex,
                              "confirmed": True, "quote_signature": quote["quote_signature"]})
        return self.http.get(f"/api/storyboards/tasks/{task['task_id']}")

    def test_codex_uses_independent_model_effort_one_call_and_script_constraints(self):
        task = self.generate(json.dumps(self.valid(), ensure_ascii=False), visual_style="水彩风格", requirements="保留对白，按情绪和完整动作分配时长")
        self.assertEqual(task["status"], "completed", task)
        self.adapter.generate.assert_called_once()
        call = self.adapter.generate.call_args.kwargs
        self.assertEqual((call["model_id"], call["thinking_level"]), ("gpt-6.1-sol", "medium"))
        for value in ("守灯人", "回家吧", "水彩风格", "按情绪和完整动作分配时长", "narrative_duration_ms", "generation_duration_ms", "duration_conflict", "不固定", "selected_shots"):
            self.assertIn(value, call["prompt"])
        self.http.script_adapter.generate.assert_not_called()
        board = self.http.detail(task["storyboard_id"])
        self.assertEqual((board["model_id"], board["thinking_level"]), ("gpt-6.1-sol", "medium"))
        self.assertEqual(sum(s["narrative_duration_ms"] for s in board["shots"]), 4000)
        self.assertEqual(self.http.identity.wallet(self.http.aid)["used_credit"], 7)

    def test_non_json_or_fenced_output_fails_once_and_refunds(self):
        for output in ("这是纯TXT分镜", "```json\n{}\n```", None):
            with self.subTest(output=output):
                self.adapter.reset_mock()
                task = self.generate(output)
                self.assertEqual(task["status"], "failed", task)
                self.adapter.generate.assert_called_once()
                self.assertEqual(self.http.identity.wallet(self.http.aid)["remaining_credit"], 200)
                self.assertEqual(self.http.identity.wallet(self.http.aid)["frozen_credit"], 0)

    def test_json_structure_is_validated_before_publishing(self):
        for output in ({"shots": []}, {"shots": "非列表"}, self.valid(title=""), self.valid(narrative_duration_ms=True)):
            with self.subTest(output=output):
                self.adapter.reset_mock()
                task = self.generate(json.dumps(output, ensure_ascii=False))
                self.assertEqual(task["status"], "failed", task)
                self.adapter.generate.assert_called_once()
                self.assertEqual(self.http.get(f"/api/storyboards?video_project_id={self.http.pid}")["storyboards"], [])
                self.assertEqual(self.http.identity.wallet(self.http.aid)["used_credit"], 0)

    def test_model_unsupported_duration_fails_without_silent_clipping(self):
        task = self.generate(json.dumps(self.valid(generation_duration_ms=999000)))
        self.assertEqual(task["status"], "failed", task)
        self.assertIn("时长", task["error"])
        self.adapter.generate.assert_called_once()
        self.assertEqual(self.http.get(f"/api/storyboards?video_project_id={self.http.pid}")["storyboards"], [])
        self.assertEqual(self.http.identity.wallet(self.http.aid)["remaining_credit"], 200)

    def mock_options(self, target):
        capability = self.http.get("/api/storyboards/config")["video_models"][0]
        return {**self.http.options(target_duration_ms=target), "_video_capability": capability}

    def test_mock_allocates_integer_milliseconds_and_varies_shots_with_story_beats(self):
        planner = MockStoryboardAdapter()
        options = self.mock_options(8000)
        simple = planner.generate({"text": "海边灯光静静照亮归船。"}, options)
        complex_story = "场景一 海边\n他看向归船。\n突然发现船上无人。\n场景二 灯塔\n她打开门。\n终于转身拥抱父亲。"
        complex_plan = planner.generate({"text": complex_story}, options)
        self.assertNotEqual(len(simple["shots"]), len(complex_plan["shots"]))
        self.assertEqual(sum(s["narrative_duration_ms"] for s in simple["shots"]), 8000)
        for plan in (simple, complex_plan):
            for shot in plan["shots"]:
                self.assertIs(type(shot["narrative_duration_ms"]), int)
                self.assertIs(type(shot["generation_duration_ms"]), int)
                self.assertTrue(shot["duration_reason"])
        self.adapter.generate.assert_not_called()

    def test_long_dialogue_reports_duration_conflict_without_losing_text(self):
        dialogue = "我会一直在这里等你，直到灯火再次照亮海面。" * 30
        source = {"text": TEXT + "\n对白：“" + dialogue + "”"}
        plan = MockStoryboardAdapter().generate(source, self.mock_options(1000))
        self.assertIsNotNone(plan["duration_conflict"])
        self.assertGreater(plan["duration_conflict"]["reasonable_duration_ms"], 1000)
        self.assertTrue(plan["duration_conflict"]["suggestions"])
        self.assertIn(dialogue, "\n".join(s["source_excerpt"] for s in plan["shots"]))
        self.adapter.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
