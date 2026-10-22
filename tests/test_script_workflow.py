"""Cross-service stage coordination, including remote acknowledgments and replay."""
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from image_workbench.plugin_api import PluginDisabled
from image_workbench.script_plugin import PLUGIN_ID
from tests import test_storyboard_http as boards_fixture
from tests import test_script_remote as remote_fixture


class ScriptWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f = boards_fixture.StoryboardHTTPTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.flow = self.f.app.script_workflow

    def test_next_stage_runs_after_script_lock_and_settlement_commit(self):
        scripts, boards = self.f.app.script_service, self.f.app.storyboard_service
        submit = boards.submit

        def observe(aid, wid, body, **kwargs):
            script = kwargs["dependency"]
            with scripts._lock(script["task_id"]):
                stored = scripts._authorized(aid, wid, script["task_id"])
                self.assertEqual(stored["status"], "completed")
                self.assertEqual(self.f.identity.settled_job(stored["job_id"])["status"], "completed")
                self.assertTrue(scripts.download(aid, wid, script["task_id"])[0])
            return submit(aid, wid, body, **kwargs)

        with patch.object(boards, "submit", side_effect=observe):
            script = self.f.generate_script(storyboard_options=self.f.options())
        self.assertEqual(script["storyboard_task"]["status"], "completed", script)
        self.f.script_adapter.generate.assert_called_once()

    def test_accepted_stage_replay_survives_plugin_disable_and_pricing_unavailability(self):
        body = self.f.script_body(storyboard_options=self.f.options(), storyboard_confirmed=True, request_id=uuid4().hex)
        original = self.f.post("/api/scripts/tasks", body)
        self.f.app.plugins.set_enabled(PLUGIN_ID, False)
        with patch.object(self.f.app.storyboard_service, "quote_options", side_effect=AssertionError("old request must not reprice")):
            replay = self.f.post("/api/scripts/tasks", body)
        self.assertEqual(replay["task_id"], original["task_id"])
        self.assertEqual(replay["storyboard_task"]["status"], "completed")
        with self.assertRaises(PluginDisabled):
            self.flow.submit(self.f.aid, self.f.wid, {**body, "request_id": uuid4().hex},
                             admission=lambda: self.f.app.plugins.require_enabled(PLUGIN_ID))
        self.f.script_adapter.generate.assert_called_once()


class RemoteScriptWorkflowTests(unittest.TestCase):
    def test_lost_remote_ack_reuses_both_stages_and_credit(self):
        f = remote_fixture.RemoteScriptTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.worker.run_once()
        body = f.body(storyboard_options={"mode": "mock", "target_duration_ms": 12000},
                      storyboard_confirmed=True, request_id=uuid4().hex)
        response = f.user.post("/api/scripts/tasks", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        original = f.worker.request
        lost = False

        def request(method, path, *args, **kwargs):
            nonlocal lost
            result = original(method, path, *args, **kwargs)
            if path.endswith("/complete") and not lost:
                lost = True
                raise httpx.ReadError("simulated lost acknowledgment")
            return result

        with patch.object(f.worker, "request", side_effect=request):
            with self.assertRaises(httpx.ReadError):
                f.worker.run_once()
        self.assertEqual(f.make_worker().run_once(), "completed")
        detail = f.user.get(f"/api/scripts/tasks/{task['task_id']}").json()
        self.assertEqual(detail["storyboard_task"]["status"], "completed", detail)
        self.assertEqual(len(f.app.storyboard_service.list_storyboards(f.aid, f.wid)), 1)
        self.assertEqual(detail["storyboard_task"]["source_script_version"], detail["version_id"])
        self.assertEqual(f.identity.wallet(f.aid)["used_credit"], 40)
        f.adapter.generate.assert_called_once()
