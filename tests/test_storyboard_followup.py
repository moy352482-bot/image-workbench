"""Follow-up acceptance: frozen planner inputs and unresolved narrative conflicts."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_storyboard_http as fixtures
from tests.test_storyboard_recovery import plan_result


class StoryboardFollowupTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.StoryboardHTTPTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.service = self.f.app.storyboard_service

    def test_queued_plan_uses_saved_prompt_when_skill_changes_before_execution(self):
        adapter = self.f.set_storyboard_pricing({"gpt-6-luna": {"high": 7}})
        adapter.generate.side_effect = None
        adapter.generate.return_value = json.dumps(plan_result())
        source = self.f.upload()
        body = self.f.options(mode="codex", selection_mode="manual", model_id="gpt-6-luna",
                              thinking_level="high", target_duration_ms=4000,
                              source_script_version=source["version_id"])
        quote = self.service.quote(self.f.aid, self.f.wid, body)
        queued = self.service.submit(self.f.aid, self.f.wid, {
            **body, "request_id": uuid4().hex, "confirmed": True, "quote_signature": quote["quote_signature"]})
        saved = self.service._task(self.f.aid, self.f.wid, queued["task_id"])["prompt"]
        with patch("image_workbench.storyboard_planner.build_prompt", return_value="changed after submission"):
            finished = self.service.run(self.f.aid, self.f.wid, queued["task_id"])
        self.assertEqual(finished["status"], "completed", finished)
        adapter.generate.assert_called_once()
        self.assertEqual(adapter.generate.call_args.kwargs["prompt"], saved)
        self.assertEqual(self.f.detail(finished["storyboard_id"])["prompt"], saved)

    def test_cosmetic_edit_cannot_clear_semantic_duration_conflict(self):
        # A model can return an exact numeric total while warning that the
        # dialogue/action still cannot reasonably fit the requested duration.
        result = plan_result()
        result["duration_conflict"] = {
            "reasonable_duration_ms": 9000, "reason": "对白与完整动作至少需要9秒",
            "suggestions": ["增加目标时长", "经审核压缩对白"]}
        with patch.object(self.service.planner, "plan", return_value=result):
            board = self.f.plan(target_duration_ms=4000)
        shot_id = board["shots"][0]["shot_id"]
        changed = self.f.edit(board, "update", shot_id=shot_id, changes={"title": "新标题"})
        self.assertIsNotNone(changed["duration_conflict"])
        self.assertEqual(changed["duration_conflict"]["reason"], result["duration_conflict"]["reason"])
        self.f.post(f"/api/storyboards/{board['storyboard_id']}/edit", {
            "request_id": uuid4().hex, "expected_version": changed["version"], "operation": "approve"}, status=409)

    def test_adjusting_duration_resolves_conflict_and_allows_explicit_approval(self):
        result = plan_result()
        result["duration_conflict"] = {
            "reasonable_duration_ms": 9000, "reason": "完整动作需要9秒",
            "suggestions": ["增加目标时长"]}
        with patch.object(self.service.planner, "plan", return_value=result):
            board = self.f.plan(target_duration_ms=4000)
        changed = self.f.edit(board, "update", shot_id=board["shots"][0]["shot_id"],
                              target_duration_ms=9000,
                              changes={"narrative_duration_ms": 9000, "generation_duration_ms": 9000})
        self.assertIsNone(changed["duration_conflict"])
        self.assertEqual(self.f.edit(changed, "approve")["status"], "approved")


if __name__ == "__main__":
    unittest.main()
