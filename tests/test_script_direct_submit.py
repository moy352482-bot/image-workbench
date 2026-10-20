"""Direct script submission keeps authoritative pricing and independent stages."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from image_workbench.accounts import CreditInsufficient
from image_workbench.capacity import CapacityExceeded, CapacityPolicy
from image_workbench.domain_errors import ConflictError
from image_workbench.project_manager import ProjectManager
from tests import test_script_service as service_fixtures
from tests import test_script_web as web_fixtures


def task_snapshot(storage):
    """Include placements and physical tasks so rejected requests cannot hide drafts."""
    tasks = [
        (project.project_id, task.task_id)
        for project in storage.list_projects()
        for task in storage.list_tasks(project.project_id)
    ]
    placements = ProjectManager(storage).catalog_view()["task_placements"]
    script_files = storage._path(".workbench", "script-tasks").glob("*.json")
    return sorted(tasks), sorted(placements), sorted(path.name for path in script_files)


def reservations(identity):
    with identity.connect() as db:
        return [
            (row["job_id"], row["amount"], row["status"])
            for row in db.execute("SELECT job_id,amount,status FROM reservations ORDER BY job_id")
        ]


class DirectScriptServiceTests(unittest.TestCase):
    setUp = service_fixtures.ScriptServiceTests.setUp
    body = service_fixtures.ScriptServiceTests.body
    change_price = service_fixtures.ScriptServiceTests.change_price

    def direct(self, **changes):
        return self.service.submit(self.aid, self.wid, self.body(**changes))

    def test_without_quote_signature_uses_current_registry_price_and_exact_model(self):
        self.change_price(37)
        submitted = self.direct()
        self.assertEqual((submitted["price"], submitted["model_id"], submitted["thinking_level"]),
                         (37, "gpt-6-astra", "high"))
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 37)
        self.assertEqual(self.adapter.calls, [])
        completed = self.service.run(self.aid, self.wid, submitted["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.adapter.calls[0]["model_id"], "gpt-6-astra")
        self.assertEqual(self.adapter.calls[0]["thinking_level"], "high")
        version = self.service._versions()["versions"][completed["version_id"]]
        self.assertEqual(version["price"], 37)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 37)

    def test_supplied_old_signature_still_conflicts_before_any_task_or_reservation(self):
        body = self.body()
        quote = self.service.quote(self.aid, self.wid, body)
        self.change_price(41)
        before = task_snapshot(self.storage)
        with self.assertRaises(ConflictError):
            self.service.submit(self.aid, self.wid, {**body, "quote_signature": quote["quote_signature"]})
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(reservations(self.identity), [])
        self.assertEqual(self.adapter.calls, [])

    def test_free_direct_generation_still_records_zero_freeze_and_zero_consume(self):
        task = self.direct(model_id="gpt-6-luna", thinking_level="high")
        completed = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(completed["status"], "completed")
        entries = [(item["type"], item["amount"]) for item in self.identity.ledger(self.aid)
                   if item["job_id"] == task["job_id"]]
        self.assertCountEqual(entries, [("freeze", 0), ("consume", 0)])
        self.assertEqual(completed["price"], 0)
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)

    def test_capacity_rejection_occurs_before_creating_task_or_calling_adapter(self):
        self.identity.capacity_policy = replace(CapacityPolicy(), max_pending_scripts_per_account=1)
        self.direct(model_id="gpt-6-luna", thinking_level="high")
        before, reserved = task_snapshot(self.storage), reservations(self.identity)
        with self.assertRaises(CapacityExceeded):
            self.direct(model_id="gpt-6-luna", thinking_level="high")
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(reservations(self.identity), reserved)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_insufficient_credit_creates_no_task_and_never_calls_adapter(self):
        with self.identity.transaction() as db:
            db.execute("UPDATE credit_accounts SET total_credit=9 WHERE account_id=?", (self.aid,))
        before = task_snapshot(self.storage)
        with self.assertRaises(CreditInsufficient):
            self.direct()
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(reservations(self.identity), [])
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 9)

    def test_manual_unavailable_model_is_not_substituted_before_or_after_submission(self):
        original = self.registry.catalog
        self.registry.catalog = {"models": [entry for entry in original["models"] if entry["slug"] == "gpt-6-luna"]}
        before = task_snapshot(self.storage)
        with self.assertRaises(ValueError):
            self.direct(model_id="gpt-6-astra", thinking_level="high")
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(self.adapter.calls, [])
        self.registry.catalog = original
        accepted = self.direct(model_id="gpt-6-astra", thinking_level="high")
        self.registry.catalog = {"models": []}
        failed = self.service.run(self.aid, self.wid, accepted["task_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["model_id"], "gpt-6-astra")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)

    def test_direct_request_replay_is_one_task_one_model_call_and_one_charge(self):
        body = self.body()
        first = self.service.submit(self.aid, self.wid, body)
        self.assertEqual(self.service.submit(self.aid, self.wid, body)["task_id"], first["task_id"])
        completed = self.service.run(self.aid, self.wid, first["task_id"])
        self.assertEqual(self.service.submit(self.aid, self.wid, body)["task_id"], first["task_id"])
        self.assertEqual(self.service.run(self.aid, self.wid, first["task_id"])["status"], "completed")
        with self.assertRaises(ConflictError):
            self.service.submit(self.aid, self.wid, {**body, "input_request": "同请求不能替换成另一个故事"})
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(len(reservations(self.identity)), 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], completed["price"])

    def test_unclear_recommended_input_does_not_invoke_external_classifier_or_create_task(self):
        fallback = Mock(side_effect=AssertionError("不得为了展示价格调用外部服务"))
        self.service.router.fallback = fallback
        body = self.body(generation_type="script", template_id="none",
                         selection_mode="recommended", input_request="你好")
        before = task_snapshot(self.storage)
        with self.assertRaises(ValueError):
            self.service.quote(self.aid, self.wid, body)
        with self.assertRaises(ValueError):
            self.service.submit(self.aid, self.wid, body)
        fallback.assert_not_called()
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(self.adapter.calls, [])

    def test_template_style_variables_survive_prompt_and_immutable_version(self):
        task = self.direct(
            style_ids=["cinematic_narrative"],
            variables={"location": "海边旧车站", "prop": "红色信封"},
            constraints="全片不使用旁白",
            conflicts_confirmed=True,
        )
        completed = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(completed["status"], "completed")
        prompt = self.adapter.calls[0]["prompt"]
        for fragment in ("episodic_screenplay_plaintext", "cinematic_narrative", "海边旧车站", "红色信封", "全片不使用旁白"):
            self.assertIn(fragment, prompt)
        version = self.service._versions()["versions"][completed["version_id"]]
        self.assertEqual(version["prompt"], prompt)
        self.assertIn("cinematic_narrative", json.dumps(version["resource_references"]))
        self.assertEqual(version["artifact_id"], completed["output_artifact_id"])


class DirectScriptHTTPTests(unittest.TestCase):
    client = web_fixtures.ScriptWebTests.client
    body = web_fixtures.ScriptWebTests.body

    def setUp(self):
        web_fixtures.ScriptWebTests.setUp(self)
        # The reusable HTTP fixture replaces ScriptService; reconnect its
        # independently composed storyboard stage without any real adapter.
        self.storyboards = self.app.storyboard_service
        self.storyboards.scripts = self.service
        self.storyboards.registry = self.registry
        self.service.storyboards = self.storyboards
        self.planner_adapter = Mock()
        self.planner_adapter.generate.side_effect = AssertionError("测试禁止真实分镜推理")
        self.storyboards.planner.adapter = self.planner_adapter

    def direct(self, **changes):
        return self.alice.post("/api/scripts/tasks",
                               json=self.body(request_id=uuid4().hex, **changes))

    def detail(self, tid):
        response = self.alice.get(f"/api/scripts/tasks/{tid}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_http_submission_needs_no_quote_roundtrip_and_replay_does_not_regenerate(self):
        body = self.body(request_id=uuid4().hex)
        first = self.alice.post("/api/scripts/tasks", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        task = self.detail(first.json()["task_id"])
        self.assertEqual((task["status"], task["price"]), ("completed", 40))
        second = self.alice.post("/api/scripts/tasks", json=body)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["task_id"], task["task_id"])
        self.adapter.generate.assert_called_once()
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(len(reservations(self.identity)), 1)

    def test_http_rejects_forged_price_fields_and_invalid_legacy_signature(self):
        before = task_snapshot(self.storage)
        for field in ("price", "credit_cost", "amount", "total_estimated_credit"):
            with self.subTest(field=field):
                response = self.direct(**{field: 0})
                self.assertEqual(response.status_code, 422, response.text)
        invalid = self.direct(quote_signature="obsolete-signature")
        self.assertEqual(invalid.status_code, 409, invalid.text)
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(reservations(self.identity), [])
        self.adapter.generate.assert_not_called()

    def test_http_free_limit_returns_429_without_new_task_or_additional_call(self):
        self.identity.capacity_policy = replace(CapacityPolicy(), max_script_submissions_per_window=1)
        first = self.direct(model_id="gpt-6-luna", thinking_level="high")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.detail(first.json()["task_id"])["status"], "completed")
        before, reserved = task_snapshot(self.storage), reservations(self.identity)
        rejected = self.direct(model_id="gpt-6-luna", thinking_level="high")
        self.assertEqual(rejected.status_code, 429, rejected.text)
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(reservations(self.identity), reserved)
        self.adapter.generate.assert_called_once()
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)

    def test_http_insufficient_credit_returns_402_without_task_or_call(self):
        with self.identity.transaction() as db:
            db.execute("UPDATE credit_accounts SET total_credit=9 WHERE account_id=?", (self.aid,))
        before = task_snapshot(self.storage)
        response = self.direct()
        self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(response.json()["expected_credit"], 40)
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(reservations(self.identity), [])
        self.adapter.generate.assert_not_called()

    def test_explicit_mock_storyboard_is_independent_and_does_not_call_real_planner(self):
        options = {"mode": "mock", "target_duration_ms": 12000}
        unconfirmed = self.direct(model_id="gpt-6-luna", storyboard_options=options)
        self.assertEqual(unconfirmed.status_code, 409, unconfirmed.text)
        self.adapter.generate.assert_not_called()
        response = self.direct(model_id="gpt-6-luna", storyboard_options=options, storyboard_confirmed=True)
        self.assertEqual(response.status_code, 200, response.text)
        task = self.detail(response.json()["task_id"])
        self.assertEqual(task["status"], "completed")
        stage = task["storyboard_task"]
        self.assertEqual((stage["kind"], stage["status"]), ("storyboard", "completed"))
        self.assertNotEqual(stage["task_id"], task["task_id"])
        self.assertEqual(stage["source_script_version"], task["version_id"])
        self.assertEqual(self.alice.get(f"/api/scripts/tasks/{task['task_id']}/download").content.decode("utf-8"),
                         web_fixtures.TEXT)
        self.adapter.generate.assert_called_once()
        self.planner_adapter.generate.assert_not_called()

    def test_mock_storyboard_failure_preserves_completed_script_and_its_zero_ledger(self):
        with patch.object(self.storyboards.planner, "plan", side_effect=RuntimeError("离线分镜失败")):
            response = self.direct(model_id="gpt-6-luna",
                storyboard_options={"mode": "mock", "target_duration_ms": 12000},
                storyboard_confirmed=True)
        self.assertEqual(response.status_code, 200, response.text)
        task = self.detail(response.json()["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["storyboard_task"]["status"], "failed")
        self.assertEqual(self.alice.get(f"/api/scripts/tasks/{task['task_id']}/download").content.decode("utf-8"),
                         web_fixtures.TEXT)
        entries = [(item["type"], item["amount"]) for item in self.identity.ledger(self.aid)
                   if item["job_id"] == task["job_id"]]
        self.assertCountEqual(entries, [("freeze", 0), ("consume", 0)])
        self.planner_adapter.generate.assert_not_called()

    def test_unpriced_real_storyboard_is_rejected_while_script_only_remains_available(self):
        pricing_path = Path(self.temp.name) / "storyboard-pricing.json"
        pricing_path.write_text(json.dumps({
            "schema_version": 1, "unit": "one_storyboard_plan", "mock_enabled": True,
            "real_enabled": True, "free_test_enabled": False, "models": {},
        }), encoding="utf-8")
        self.storyboards.pricing_path = pricing_path
        before = task_snapshot(self.storage)
        response = self.direct(model_id="gpt-6-luna",
            storyboard_options={"mode": "codex", "selection_mode": "manual",
                                "model_id": "gpt-6-luna", "thinking_level": "high"},
            storyboard_confirmed=True)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("定价", response.text)
        self.assertEqual(task_snapshot(self.storage), before)
        self.assertEqual(reservations(self.identity), [])
        self.adapter.generate.assert_not_called()
        self.planner_adapter.generate.assert_not_called()
        script = self.direct(model_id="gpt-6-luna")
        self.assertEqual(script.status_code, 200, script.text)
        completed = self.detail(script.json()["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertFalse(completed.get("storyboard_task_id"))
        self.adapter.generate.assert_called_once()
        self.planner_adapter.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
