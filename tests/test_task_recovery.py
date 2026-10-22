"""Read-only cross-feature recovery contract using real isolated stores."""
from contextlib import ExitStack
import hashlib
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests import test_script_service as scripts_fixture
from tests import test_storyboard_http as http_fixture
from tests import test_video as video_fixture
from tests import test_web as image_fixture


def json_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.json")}


class TaskRecoveryTests(unittest.TestCase):
    def fixture(self, kind):
        fixture = kind()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_script_query_verifies_candidate_without_settlement_writes_or_model_calls(self):
        f = self.fixture(scripts_fixture.ScriptServiceTests)
        task = f.submit()
        with patch.object(f.service, "_archive", side_effect=OSError("archive interrupted")):
            result = f.service.run(f.aid, f.wid, task["task_id"])
        self.assertEqual(result["status"], "uncertain")
        before, ledger = json_snapshot(f.root), f.identity.ledger(f.aid)
        with ExitStack() as stack:
            for target, method in ((f.identity, "record_job"), (f.identity, "reserve"), (f.service, "_write"),
                                   (f.service, "_reconcile"), (f.adapter, "generate")):
                stack.enter_context(patch.object(target, method, side_effect=AssertionError("query must stay read-only")))
            first = f.service.recovery_view(f.aid, f.wid, task["task_id"])
            self.assertEqual(first, f.service.recovery_view(f.aid, f.wid, task["task_id"]))
        self.assertTrue(first["evidence"]["candidate_verified"])
        self.assertEqual([a["code"] for a in first["actions"]], ["restore_archive"])
        self.assertFalse(any(a["may_invoke_model"] for a in first["actions"]))
        self.assertEqual(json_snapshot(f.root), before)
        self.assertEqual(f.identity.ledger(f.aid), ledger)
        job = f.service._load(task["task_id"])
        f.service._candidate(job).write_text("truncated", encoding="utf-8")
        damaged = f.service.recovery_view(f.aid, f.wid, task["task_id"])
        self.assertTrue(damaged["evidence"]["has_candidate"])
        self.assertFalse(damaged["evidence"]["candidate_verified"])
        self.assertNotIn("restore_archive", [a["code"] for a in damaged["actions"]])
        self.assertFalse(f.service.detail(f.aid, f.wid, task["task_id"])["can_recover"])

    def test_queued_script_query_does_not_create_candidate_directory(self):
        f = self.fixture(scripts_fixture.ScriptServiceTests)
        task = f.submit()
        job = f.service._load(task["task_id"])
        path = f.service._candidate(job, create=False)
        self.assertFalse(path.parent.exists())
        self.assertEqual(f.service.recovery_view(f.aid, f.wid, task["task_id"])["status"], "queued")
        self.assertFalse(path.parent.exists())
        self.assertEqual(f.adapter.calls, [])

    def test_video_pending_settlement_and_uncertain_output_are_only_observed(self):
        f = self.fixture(video_fixture.VideoTests)
        shot, job, worker, claim = f.ready()
        content = video_fixture.mp4()
        digest = hashlib.sha256(content).hexdigest()
        f.store.upload_output(job["job_id"], worker, claim, content, digest)
        f.store.complete(job["job_id"], worker, claim, "uncertain", error="lost completion receipt")
        view = f.store.recovery_view(f.aid, f.wid, f.pid, shot["shot_id"])
        self.assertEqual(view["status"], "uncertain")
        self.assertEqual(view["actions"][0]["code"], "finalize_output")
        self.assertEqual(view["actions"][0]["actor"], "worker")
        # Publish a terminal decision, then interrupt before SQLite settlement.
        with patch.object(f.identity, "record_job", side_effect=OSError("settlement interrupted")):
            with self.assertRaises(OSError):
                f.store.complete(job["job_id"], worker, claim, "succeeded", digest)
        before, ledger = json_snapshot(f.storage.config.data_root), f.identity.ledger(f.aid)
        with patch.object(f.store, "_jobs", side_effect=AssertionError("query must not reconcile")), \
             patch.object(f.identity, "record_job", side_effect=AssertionError("query must not settle")):
            view = f.store.recovery_view(f.aid, f.wid, f.pid, shot["shot_id"])
        self.assertEqual(view["status"], "completed")
        self.assertEqual(f.wallet()["frozen_credit"], 10)
        self.assertEqual(f.identity.ledger(f.aid), ledger)
        self.assertEqual(json_snapshot(f.storage.config.data_root), before)

    def test_image_archive_failure_lists_restore_without_executing_it(self):
        f = self.fixture(image_fixture.WebTests)
        task = f.task()
        with patch.object(f.storage, "register_output_image", side_effect=OSError("archive interrupted")):
            detail = f.execute(task)
        self.assertEqual(detail["status"], "failed")
        before = json_snapshot(f.root)
        with patch.object(f.state, "submit", side_effect=AssertionError("query must not submit")):
            response = f.client.get(f"/api/tasks/{task['task_id']}/recovery")
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()
        self.assertEqual(view["failure"]["code"], "register_failed")
        self.assertIn("restore_archive", [a["code"] for a in view["actions"]])
        self.assertEqual(view["evidence"]["execution_id"], detail["last_execution_id"])
        self.assertEqual(before, json_snapshot(f.root))

    def test_common_http_schema_and_workspace_boundaries_for_all_three_features(self):
        f = self.fixture(http_fixture.StoryboardHTTPTests)
        script = f.generate_script()
        response = f.alice.post(f"/api/subprojects/{f.link['subproject_id']}/tasks",
                                data={"request_id": uuid4().hex, "operation": "generate", "prompt": "测试图片"})
        self.assertEqual(response.status_code, 200, response.text)
        image = response.json()
        shot = f.post(f.prefix + "/shots", {"request_id": uuid4().hex, "name": "测试视频", "prompt": "海边镜头",
                                            "mode": "T2V", "duration": 5, "aspect_ratio": "landscape"})
        paths = [("image", f"/api/tasks/{image['task_id']}/recovery"),
                 ("script", f"/api/scripts/tasks/{script['task_id']}/recovery"),
                 ("video", f"{f.prefix}/shots/{shot['shot_id']}/recovery")]
        keys = None
        with patch.object(f.identity, "record_job", side_effect=AssertionError("read must not settle")), \
             patch.object(f.app.script_service, "_reconcile", side_effect=AssertionError("read must not reconcile")):
            for kind, path in paths:
                view = f.get(path)
                self.assertEqual(view["kind"], kind)
                self.assertFalse(view["automatic_retry"])
                self.assertEqual(set(view), keys or set(view))
                keys = set(view)
                self.assertEqual(f.bobby.get(path).status_code, 404)
                self.assertEqual(f.alice.get(path, headers={"X-Workspace-ID": f.other_wid}).status_code, 404)
                self.assertEqual(f.anon.get(path).status_code, 401)
        f.script_adapter.generate.assert_called_once()
