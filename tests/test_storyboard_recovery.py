"""Durability, billing and concurrency checks using temporary stores and mocked inference."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from image_workbench.storyboard_service import StoryboardService
from image_workbench.script_adapter import CodexTextAdapter
from tests import test_storyboard_http as fixtures


def plan_result(duration=4000, **changes):
    return {"shots": [{"title": "清晨", "visual_description": "人物推开教室门，抬头望向黑板。",
                       "action": "推门、抬头", "narrative_duration_ms": duration,
                       "generation_duration_ms": max(4000, duration), "duration_reason": "完整动作与反应",
                       "video_prompt": "清晨教室，人物推门并抬头", **changes}], "duration_conflict": None}


class StoryboardRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.StoryboardHTTPTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.service = self.f.app.storyboard_service

    def request(self, *, real=False, request_id=None):
        source = self.f.upload()
        if real:
            self.f.set_storyboard_pricing({"gpt-6-luna": {"high": 7}})
        body = self.f.options(source_script_version=source["version_id"], target_duration_ms=4000)
        if real:
            body.update(mode="codex", selection_mode="manual", model_id="gpt-6-luna", thinking_level="high")
        quote = self.service.quote(self.f.aid, self.f.wid, body)
        return {**body, "request_id": request_id or uuid4().hex, "confirmed": True, "quote_signature": quote["quote_signature"]}

    def test_paid_plan_reserves_consumes_once_and_keeps_independent_price(self):
        body = self.request(real=True)
        with patch.object(self.service.planner, "plan", return_value=plan_result()) as planner:
            task = self.service.submit(self.f.aid, self.f.wid, body)
            self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 7)
            finished = self.service.run(self.f.aid, self.f.wid, task["task_id"])
            self.assertEqual(finished["status"], "completed", finished)
            self.service.submit(self.f.aid, self.f.wid, body)
            self.service.run(self.f.aid, self.f.wid, task["task_id"])
            planner.assert_called_once()
        wallet = self.f.identity.wallet(self.f.aid)
        self.assertEqual((wallet["remaining_credit"], wallet["used_credit"], wallet["frozen_credit"]), (193, 7, 0))

    def test_missing_confirmation_cannot_reserve_or_run(self):
        body = self.request(real=True)
        body["confirmed"] = False
        with self.assertRaisesRegex(ValueError, "确认"):
            self.service.submit(self.f.aid, self.f.wid, body)
        self.assertEqual(self.f.identity.wallet(self.f.aid)["remaining_credit"], 200)

    def test_invalid_model_output_refunds_and_keeps_script(self):
        body = self.request(real=True)
        with patch.object(self.service.planner, "plan", return_value={"shots": []}) as planner:
            task = self.service.submit(self.f.aid, self.f.wid, body)
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
            self.assertEqual(result["status"], "failed")
            planner.assert_called_once()
        self.assertEqual(self.f.identity.wallet(self.f.aid)["remaining_credit"], 200)
        self.assertTrue(self.f.app.script_service.storyboard_source(self.f.aid, self.f.wid, body["source_script_version"])["text"])

    def test_reserved_outbox_recovers_without_second_reservation(self):
        body = self.request(real=True)
        with patch.object(self.service, "_write_task", side_effect=OSError("publish interrupted")):
            with self.assertRaises(OSError):
                self.service.submit(self.f.aid, self.f.wid, body)
        self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 7)
        task = self.service.submit(self.f.aid, self.f.wid, body)
        with patch.object(self.service.planner, "plan", return_value=plan_result()) as planner:
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
            self.assertEqual(result["status"], "completed", result)
            planner.assert_called_once()
        self.assertEqual(self.f.identity.wallet(self.f.aid)["used_credit"], 7)

    def test_archive_failure_recovers_candidate_without_repeating_model(self):
        body = self.request()
        task = self.service.submit(self.f.aid, self.f.wid, body)
        with patch.object(self.service.planner, "plan", return_value=plan_result()) as planner:
            with patch.object(self.service, "_finish_version", side_effect=OSError("disk temporarily unavailable")):
                failed = self.service.run(self.f.aid, self.f.wid, task["task_id"])
                self.assertEqual(failed["status"], "uncertain", failed)
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
            self.assertEqual(result["status"], "completed", result)
            planner.assert_called_once()
        board = self.service.detail(self.f.aid, self.f.wid, result["storyboard_id"])
        self.assertEqual(len(board["versions"]), 1)
        self.assertEqual(self.f.storage.get_artifact(board["artifact_id"]).format, "JSON")

    def test_restart_closes_script_completed_dependency_gap(self):
        with patch.object(self.f.app.script_workflow, "dependency_completed", return_value=None):
            script = self.f.generate_script(storyboard_options=self.f.options())
        self.assertEqual(script["status"], "completed")
        self.assertIsNone(script["storyboard_task"])
        restarted = StoryboardService(self.f.storage, self.f.identity, self.f.app.script_service.storyboard_source,
                                      self.f.app.video_store, registry=self.f.app.script_service.registry)
        self.f.app.storyboard_service = restarted
        self.f.app.script_workflow.resume_pending()
        self.f.app.script_workflow.resume_pending()
        boards = restarted.list_storyboards(self.f.aid, self.f.wid, video_project_id=self.f.pid)
        self.assertEqual(len(boards), 1)
        self.f.script_adapter.generate.assert_called_once()

    def test_unknown_running_task_never_replays_inference(self):
        body = self.request(real=True)
        task = self.service.submit(self.f.aid, self.f.wid, body)
        job = self.service._task(self.f.aid, self.f.wid, task["task_id"])
        job["status"] = "running"
        self.service._write_task(job)
        with patch.object(self.service.planner, "plan") as planner:
            self.service.resume_pending()
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
            planner.assert_not_called()
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 7)

    def test_cancel_queued_plan_refunds_without_invoking(self):
        body = self.request(real=True)
        task = self.service.submit(self.f.aid, self.f.wid, body)
        with patch.object(self.service.planner, "plan") as planner:
            cancelled = self.service.cancel(self.f.aid, self.f.wid, task["task_id"])
            self.service.run(self.f.aid, self.f.wid, task["task_id"])
            planner.assert_not_called()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.f.identity.wallet(self.f.aid)["remaining_credit"], 200)

    def test_concurrent_duplicate_submissions_and_runs_invoke_once(self):
        body = self.request()
        def submit(_):
            return self.service.submit(self.f.aid, self.f.wid, body)["task_id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            task_ids = list(pool.map(submit, range(4)))
        self.assertEqual(len(set(task_ids)), 1)
        def slow(*args, **kwargs):
            time.sleep(0.15)
            return plan_result()
        with patch.object(self.service.planner, "plan", side_effect=slow) as planner:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: self.service.run(self.f.aid, self.f.wid, task_ids[0]), range(4)))
            planner.assert_called_once()
        self.assertEqual(len(self.service.list_storyboards(self.f.aid, self.f.wid)), 1)

    def test_stale_ai_preview_cannot_overwrite_manual_edit(self):
        board = self.f.plan()
        body = self.f.options(expected_version=board["version"], shot_ids=[], reason="重规划")
        base = f"/api/storyboards/{board['storyboard_id']}"
        quote = self.f.post(base + "/replan/quote", body)
        task = self.f.post(base + "/replan", {**body, "request_id": uuid4().hex, "confirmed": True, "quote_signature": quote["quote_signature"]})
        edited = self.f.edit(board, "update", shot_id=board["shots"][0]["shot_id"], changes={"title": "用户明确修改"})
        self.f.post(base + "/accept", {"request_id": uuid4().hex, "task_id": task["task_id"], "expected_version": edited["version"]}, 409)
        self.assertEqual(self.f.detail(board["storyboard_id"])["shots"][0]["title"], "用户明确修改")

    def test_pricing_change_invalidates_prior_confirmation(self):
        body = self.request(real=True)
        pricing = json.loads(self.service.pricing_path.read_text(encoding="utf-8"))
        pricing["models"]["gpt-6-luna"]["high"] = 9
        self.service.pricing_path.write_text(json.dumps(pricing), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "报价"):
            self.service.submit(self.f.aid, self.f.wid, body)
        self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 0)

    def test_unknown_reference_from_model_is_rejected_without_substitution(self):
        body = self.request()
        task = self.service.submit(self.f.aid, self.f.wid, body)
        with patch.object(self.service.planner, "plan", return_value=plan_result(reference_artifact_ids=[uuid4().hex])):
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("资产缺失", result["error"])
        self.assertEqual(self.service.list_storyboards(self.f.aid, self.f.wid), [])

    def test_manual_save_does_not_clear_semantic_duration_conflict(self):
        body = self.request()
        result = plan_result()
        result["duration_conflict"] = {"reasonable_duration_ms": 8000, "reason": "对白需要8秒", "suggestions": ["增加时长"]}
        with patch.object(self.service.planner, "plan", return_value=result):
            task = self.service.submit(self.f.aid, self.f.wid, body)
            self.service.run(self.f.aid, self.f.wid, task["task_id"])
        board = self.f.detail(task["storyboard_id"])
        saved = self.f.edit(board, "save")
        self.assertIsNotNone(saved["duration_conflict"])
        self.f.post(f"/api/storyboards/{board['storyboard_id']}/edit",
                    {"request_id": uuid4().hex, "expected_version": saved["version"], "operation": "approve"}, 409)

    def test_restart_reconciles_cancelled_terminal_json_before_refund(self):
        body = self.request(real=True)
        task = self.service.submit(self.f.aid, self.f.wid, body)
        with patch.object(self.f.identity, "record_job", side_effect=OSError("interrupted before refund")):
            with self.assertRaises(OSError):
                self.service.cancel(self.f.aid, self.f.wid, task["task_id"])
        self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 7)
        self.service.resume_pending()
        self.service.resume_pending()
        self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 0)
        self.assertEqual(self.service.cancel(self.f.aid, self.f.wid, task["task_id"])["status"], "cancelled")

    def test_successful_codex_receipt_recovers_after_real_mode_is_disabled(self):
        task = self.service.submit(self.f.aid, self.f.wid, self.request(real=True))
        job = self.service._task(self.f.aid, self.f.wid, task["task_id"])
        directory = self.service._work(job) / "codex"
        directory.mkdir()
        data = json.dumps(plan_result(), ensure_ascii=False).encode()
        (directory / "response.txt").write_bytes(data)
        (directory / "completed.json").write_text(json.dumps({
            "model_id": job["model_id"], "thinking_level": job["thinking_level"], "returncode": 0,
            "prompt_sha256": hashlib.sha256(job["prompt"].encode()).hexdigest(),
            "response_sha256": hashlib.sha256(data).hexdigest(), "response_size_bytes": len(data),
        }), encoding="utf-8")
        job["status"] = "running"
        self.service._write_task(job)
        config = json.loads(self.service.pricing_path.read_text())
        config["real_enabled"] = False
        self.service.pricing_path.write_text(json.dumps(config))
        self.service.planner.adapter = CodexTextAdapter()
        with patch.object(self.service.planner.adapter, "generate", side_effect=AssertionError("must not invoke")):
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.f.identity.wallet(self.f.aid)["used_credit"], 7)

    def test_document_publish_interruption_recovers_without_partial_final_or_inference(self):
        task = self.service.submit(self.f.aid, self.f.wid, self.request())
        replace = os.replace
        def interrupted(source, destination):
            if source.name.startswith(".document-"):
                raise OSError("interrupted before atomic publication")
            return replace(source, destination)
        with patch.object(self.service.planner, "plan", return_value=plan_result()) as planner:
            with patch("image_workbench.storage.os.replace", side_effect=interrupted):
                result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
            self.assertEqual(result["status"], "uncertain", result)
            self.assertEqual(list(self.f.storage.data_root.glob("projects/*/tasks/*/outputs/*.json")), [])
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
            self.assertEqual(result["status"], "completed", result)
            planner.assert_called_once()

    def test_model_cannot_attach_unprovided_same_project_image(self):
        self.f.alice.post(self.f.prefix + "/assets", files=[("files", ("hero.png", fixtures.png(), "image/png"))])
        board = self.f.plan()
        image = self.f.get(f"/api/storyboards/{board['storyboard_id']}/assets")["assets"][0]["artifact_id"]
        task = self.service.submit(self.f.aid, self.f.wid, self.request())
        with patch.object(self.service.planner, "plan", return_value=plan_result(reference_artifact_ids=[image])):
            result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("未提供", result["error"])

    def test_missing_original_image_after_video_draft_blocks_credit_reservation(self):
        self.f.alice.post(self.f.prefix + "/assets", files=[("files", ("hero.png", fixtures.png(), "image/png"))])
        board = self.f.plan()
        image = self.f.get(f"/api/storyboards/{board['storyboard_id']}/assets")["assets"][0]["artifact_id"]
        board = self.f.edit(board, "update", shot_id=board["shots"][0]["shot_id"],
                            changes={"start_frame_artifact_id": image})
        draft = self.f.draft(board)
        self.f.storage.get_artifact_path(image).unlink()
        response = self.f.post(self.f.prefix + f"/shots/{draft['shot_id']}/run",
                               {"request_id": uuid4().hex, "confirm_generation": True,
                                "expected_credit": draft["estimated_credit"]}, 400)
        self.assertIn("缺失", response["detail"])
        self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 0)

    def test_active_planning_prevents_project_delete(self):
        task = self.service.submit(self.f.aid, self.f.wid, self.request())
        response = self.f.alice.delete(f"/api/project-actions/{self.f.link['main_project_id']}")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("分镜", response.text)
        self.assertEqual(self.service.task(self.f.aid, self.f.wid, task["task_id"])["status"], "queued")

    def test_imported_generated_image_preserves_original_artifact_identity(self):
        image_task = self.f.post(self.f.prefix + "/images",
                                 {"request_id": uuid4().hex, "prompt": "人物在教室", "reference_ids": []})
        path = self.f.root / "original.png"
        path.write_bytes(fixtures.png())
        artifact = self.f.storage.register_output_image(image_task["project_id"], image_task["task_id"], path)
        imported = self.f.post(self.f.prefix + "/images/from-artifact", {"artifact_id": artifact.artifact_id})
        self.assertEqual(imported["assets"][0]["artifact_id"], artifact.artifact_id)
        board = self.f.plan()
        assets = self.f.get(f"/api/storyboards/{board['storyboard_id']}/assets")["assets"]
        self.assertEqual([a["artifact_id"] for a in assets], [artifact.artifact_id])

    def test_atomic_json_temporary_files_are_not_enumerated_as_tasks(self):
        body = self.request()
        legacy = self.service.tasks_root / ".tmp-interrupted.json"
        legacy.write_text('{"partial":', encoding="utf-8")
        board_legacy = self.service.root / ".tmp-interrupted.json"
        board_legacy.write_text('{"partial":', encoding="utf-8")
        script_legacy = self.f.app.script_service.root / ".tmp-interrupted.json"
        script_legacy.write_text('{"partial":', encoding="utf-8")
        replace = os.replace
        writes = []
        def inspect(source, destination):
            if source.name.startswith(".tmp-"):
                writes.append(source)
                self.assertNotEqual(source.suffix, ".json")
            return replace(source, destination)
        with patch("image_workbench.storage.os.replace", side_effect=inspect):
            task = self.service.submit(self.f.aid, self.f.wid, body)
        self.assertTrue(writes)
        self.assertEqual(len(self.service.pending_jobs()), 1)
        self.assertEqual(self.service.list_storyboards(self.f.aid, self.f.wid), [])
        self.assertEqual(len(self.f.app.script_service.list_tasks(self.f.aid, self.f.wid)), 1)
        self.service.resume_pending()
        self.assertEqual(self.service.task(self.f.aid, self.f.wid, task["task_id"])["status"], "completed")

    def test_moved_source_project_blocks_old_workspace_video_submission(self):
        board = self.f.plan()
        draft = self.f.draft(board)
        self.f.identity.change_project(self.f.aid, self.f.wid, self.f.link["main_project_id"],
                                       target_workspace_id=self.f.other_wid)
        response = self.f.alice.post(self.f.prefix + f"/shots/{draft['shot_id']}/run", json={
            "request_id": uuid4().hex, "confirm_generation": True, "expected_credit": draft["estimated_credit"]})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.f.identity.wallet(self.f.aid)["frozen_credit"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows sharing violation regression")
    def test_windows_catalog_read_race_retries_publication_without_replaying_model(self):
        body = self.request()
        replace = os.replace
        denied = []
        def sharing_violation(source, destination):
            if destination.name == "catalog.json" and not denied:
                denied.append(True)
                error = PermissionError("catalog reader has not released its handle")
                error.winerror = 5
                raise error
            return replace(source, destination)
        with patch("image_workbench.storage.os.replace", side_effect=sharing_violation):
            task = self.service.submit(self.f.aid, self.f.wid, body)
            with patch.object(self.service.planner, "plan", return_value=plan_result()) as planner:
                result = self.service.run(self.f.aid, self.f.wid, task["task_id"])
                planner.assert_called_once()
        self.assertTrue(denied)
        self.assertEqual(result["status"], "completed", result)


if __name__ == "__main__":
    unittest.main()
