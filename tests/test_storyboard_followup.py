"""Follow-up acceptance: frozen planner inputs and unresolved narrative conflicts."""
import json
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from image_workbench.domain_errors import ConflictError
from image_workbench.storyboard_planner import SKILL
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

    def test_reference_changes_invalidate_quote_without_changing_queued_input(self):
        local_skill = self.f.root / "storyboard-skill"
        shutil.copytree(SKILL.parent, local_skill)
        adapter = self.f.set_storyboard_pricing({"gpt-6-luna": {"high": 7}})
        adapter.generate.side_effect = None
        adapter.generate.return_value = json.dumps(plan_result())
        source = self.f.upload()
        body = self.f.options(mode="codex", selection_mode="manual", model_id="gpt-6-luna",
                              thinking_level="high", target_duration_ms=4000,
                              source_script_version=source["version_id"])
        with patch("image_workbench.storyboard_planner.SKILL", local_skill / "SKILL.md"):
            quote = self.service.quote(self.f.aid, self.f.wid, body)
            submitted = {**body, "request_id": uuid4().hex, "confirmed": True, "quote_signature": quote["quote_signature"]}
            queued = self.service.submit(self.f.aid, self.f.wid, submitted)
            job = self.service._task(self.f.aid, self.f.wid, queued["task_id"])
            self.assertIn("# 镜头设计与时间分配", job["prompt"])
            self.assertIn("# 空间、身份与跨镜连续性", job["prompt"])
            self.assertNotIn("# 剧情剧本专项方法", job["prompt"])
            self.assertEqual(len(job["skill_reference"]["files"]), 3)
            ref = local_skill / "references/continuity.md"
            ref.write_text("新版连续性规则", encoding="utf-8")
            new_quote = self.service.quote(self.f.aid, self.f.wid, body)
            self.assertNotEqual(quote["quote_signature"], new_quote["quote_signature"])
            wallet = self.f.identity.wallet(self.f.aid)
            with self.assertRaises(ConflictError):
                self.service.submit(self.f.aid, self.f.wid, {**submitted, "request_id": uuid4().hex})
            ref.unlink()
            with self.assertRaisesRegex(ValueError, "分镜 Skill 不可读取"):
                self.service.submit(self.f.aid, self.f.wid, {**submitted, "request_id": uuid4().hex})
            self.assertEqual(self.f.identity.wallet(self.f.aid), wallet)
            self.assertEqual(self.service.submit(self.f.aid, self.f.wid, submitted)["task_id"], queued["task_id"])
            finished = self.service.run(self.f.aid, self.f.wid, queued["task_id"])
        self.assertEqual(finished["status"], "completed", finished)
        adapter.generate.assert_called_once()
        self.assertEqual(adapter.generate.call_args.kwargs["prompt"], job["prompt"])
        self.assertEqual(self.f.identity.wallet(self.f.aid)["used_credit"], 7)

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
