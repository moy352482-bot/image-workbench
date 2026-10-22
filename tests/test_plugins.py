"""Offline extension acceptance: real services/storage/ledger, fake inference."""

from dataclasses import replace
import unittest
from unittest.mock import patch
from uuid import uuid4

from image_workbench.domain_errors import ConflictError
from image_workbench.plugin_api import PluginContext, PluginDescriptor, PluginDisabled, PluginRegistry
from image_workbench.script_adapter import ScriptGenerationError
from image_workbench.script_plugin import PLUGIN_ID, ScriptGenerationPlugin
from tests import test_script_service as service_fixtures
from tests import test_script_web as web_fixtures


class EchoPlugin:
    """Example of adding an independent extension without changing any feature."""

    descriptor = PluginDescriptor("echo-preview", "离线预览", "1.0.0", "Contract fixture",
                                  ("preview",), {"type": "object"}, {"type": "object"})

    def validate_input(self, context, body):
        return dict(body)

    def estimate_cost(self, context, body):
        return {"price": 0}

    def submit(self, context, body):
        return {"task_id": "offline-preview", "status": "queued"}

    def status(self, context, task_id):
        return {"task_id": task_id, "status": "queued"}

    def result(self, context, task_id):
        return {"output": None}


class PluginRegistryTests(unittest.TestCase):
    def test_register_another_plugin_and_toggle_without_feature_changes(self):
        registry = PluginRegistry()
        registry.register(EchoPlugin(), enabled=False)
        self.assertFalse(registry.describe("echo-preview")["enabled"])
        with self.assertRaises(PluginDisabled):
            registry.require_enabled("echo-preview")
        registry.set_enabled("echo-preview", True)
        registry.require_enabled("echo-preview")
        context = PluginContext("account", "workspace")
        self.assertEqual(registry.get("echo-preview").submit(context, {})["status"], "queued")
        with self.assertRaises(ValueError):
            registry.register(EchoPlugin())
        # Public metadata is a copy, not a mutable backdoor into the registry.
        registry.describe("echo-preview")["input_schema"]["type"] = "null"
        self.assertEqual(registry.describe("echo-preview")["input_schema"]["type"], "object")

    def test_missing_service_dependency_or_interface_is_rejected_before_registration(self):
        registry = PluginRegistry()
        plugin = EchoPlugin()
        plugin.descriptor = replace(plugin.descriptor, required_services=("missing",))
        with self.assertRaisesRegex(ValueError, "Missing services"):
            registry.register(plugin)
        self.assertEqual(registry.catalog(), [])
        plugin.descriptor = replace(plugin.descriptor, required_services=(), dependencies=("missing",))
        with self.assertRaisesRegex(ValueError, "Register dependencies"):
            registry.register(plugin)
        plugin.descriptor = replace(plugin.descriptor, dependencies=())
        plugin.submit = None
        with self.assertRaisesRegex(ValueError, "missing submit"):
            registry.register(plugin)
        self.assertEqual(registry.catalog(), [])

    def test_disabled_dependencies_and_disable_order_are_explicit(self):
        registry = PluginRegistry()
        registry.register(EchoPlugin(), enabled=False)
        child = EchoPlugin()
        child.descriptor = replace(child.descriptor, plugin_id="child-preview", dependencies=("echo-preview",))
        with self.assertRaises(PluginDisabled):
            registry.register(child)
        registry.set_enabled("echo-preview", True)
        registry.register(child)
        with self.assertRaisesRegex(ValueError, "dependent plugins"):
            registry.set_enabled("echo-preview", False)
        registry.set_enabled("child-preview", False)
        registry.set_enabled("echo-preview", False)
        self.assertIs(registry.get("child-preview"), child)


class ScriptPluginTests(unittest.TestCase):
    body = service_fixtures.ScriptServiceTests.body

    def setUp(self):
        service_fixtures.ScriptServiceTests.setUp(self)
        self.plugins = PluginRegistry(services=("script-service",))
        self.plugin = ScriptGenerationPlugin(lambda: self.service, self.plugins)
        self.plugins.register(self.plugin)
        self.context = PluginContext(self.aid, self.wid)

    def test_validate_estimate_submit_are_offline_and_share_task_credit_artifact_version(self):
        body = self.body()
        request = self.plugin.validate_input(self.context, body)
        self.assertEqual(request["subproject_id"], self.sid)
        quote = self.plugin.estimate_cost(self.context, body)
        self.assertEqual(quote["price"], 40)
        self.assertEqual(self.service.list_tasks(self.aid, self.wid), [])
        task = self.plugin.submit(self.context, body)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        self.assertEqual(self.storage.get_task(task["project_id"], task["task_id"]).operation, "screenplay")
        self.assertIsNone(self.plugin.result(self.context, task["task_id"])["output"])
        self.service.run(self.aid, self.wid, task["task_id"])
        result = self.plugin.result(self.context, task["task_id"])
        self.assertEqual(result["output"]["text"], service_fixtures.TEXT)
        self.assertEqual(self.storage.get_artifact(result["output"]["artifact_id"]).format, "TXT")
        detail = self.plugin.status(self.context, task["task_id"])
        self.assertEqual(detail["versions"][0]["version_id"], result["output"]["version_id"])
        self.plugin.submit(self.context, body)
        self.service.run(self.aid, self.wid, task["task_id"])
        ledger = [(row["type"], row["amount"]) for row in self.identity.ledger(self.aid)
                  if row["job_id"] == task["job_id"]]
        self.assertCountEqual(ledger, [("freeze", 40), ("consume", 40)])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_disabled_plugin_blocks_new_work_but_preserves_idempotency_pending_and_history(self):
        body = self.body()
        task = self.plugin.submit(self.context, body)
        self.plugins.set_enabled(PLUGIN_ID, False)
        self.assertEqual(self.plugin.submit(self.context, body)["task_id"], task["task_id"])
        with self.assertRaises(PluginDisabled):
            self.plugin.submit(self.context, self.body())
        with self.assertRaises(PluginDisabled):
            self.plugin.estimate_cost(self.context, body)
        with self.assertRaises(ConflictError):
            self.plugin.submit(self.context, {**body, "input_request": "另一段需求"})
        self.assertEqual(len(self.service.list_tasks(self.aid, self.wid)), 1)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        # Already admitted work drains through the original worker/service.
        self.service.run(self.aid, self.wid, task["task_id"])
        result = self.plugin.result(self.context, task["task_id"])
        self.assertEqual(result["status"], "completed")
        content, _ = self.service.download(self.aid, self.wid, task["task_id"])
        self.assertEqual(content.decode("utf-8"), result["output"]["text"])
        self.assertEqual(len(self.adapter.calls), 1)
        self.plugins.set_enabled(PLUGIN_ID, True)
        self.assertEqual(self.plugin.submit(self.context, self.body())["status"], "queued")

    def test_disabled_recovery_archives_candidate_without_replaying_inference(self):
        task = self.plugin.submit(self.context, self.body())
        with patch.object(self.service, "_archive", side_effect=OSError("simulated archive outage")):
            self.service.run(self.aid, self.wid, task["task_id"])
        self.plugins.set_enabled(PLUGIN_ID, False)
        failure = self.plugin.result(self.context, task["task_id"])["failure"]
        self.assertEqual(failure["code"], "execution_uncertain")
        self.assertTrue(failure["recoverable"])
        self.assertFalse(failure["retryable"])
        self.service.recover(self.aid, self.wid, task["task_id"])
        self.assertEqual(self.plugin.result(self.context, task["task_id"])["status"], "completed")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)

    def test_definite_failure_refunds_and_reports_structured_failure(self):
        self.adapter.error = ScriptGenerationError("offline provider rejection")
        task = self.plugin.submit(self.context, self.body())
        self.service.run(self.aid, self.wid, task["task_id"])
        result = self.plugin.result(self.context, task["task_id"])
        self.assertEqual(result["failure"]["code"], "execution_failed")
        self.assertFalse(result["failure"]["retryable"])
        wallet = self.identity.wallet(self.aid)
        self.assertEqual((wallet["used_credit"], wallet["frozen_credit"]), (0, 0))

    def test_validation_and_foreign_workspace_cannot_create_or_read_jobs(self):
        with self.assertRaises(ValueError):
            self.plugin.submit(self.context, {**self.body(), "input_request": ""})
        task = self.plugin.submit(self.context, self.body())
        foreign = PluginContext(self.bid, self.bwid)
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.plugin.validate_input(foreign, self.body())
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.plugin.result(foreign, task["task_id"])
        self.assertEqual(self.adapter.calls, [])


class ScriptPluginHTTPTests(unittest.TestCase):
    setUp = web_fixtures.ScriptWebTests.setUp
    client = web_fixtures.ScriptWebTests.client
    body = web_fixtures.ScriptWebTests.body

    def test_existing_http_routes_use_registry_and_keep_history_and_retries(self):
        body = {**self.body(), "request_id": uuid4().hex}
        response = self.alice.post("/api/scripts/tasks", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.app.plugins.set_enabled(PLUGIN_ID, False)
        info = self.alice.get("/api/scripts/config").json()["generation_plugin"]
        self.assertFalse(info["enabled"])
        self.assertIn("required_services", info)
        blocked = self.alice.post("/api/scripts/tasks", json={**body, "request_id": uuid4().hex})
        self.assertEqual(blocked.status_code, 409, blocked.text)
        replay = self.alice.post("/api/scripts/tasks", json=body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["task_id"], task["task_id"])
        detail = self.alice.get(f"/api/scripts/tasks/{task['task_id']}")
        self.assertEqual(detail.json()["status"], "completed")
        self.assertEqual(self.alice.get(f"/api/scripts/tasks/{task['task_id']}/download").status_code, 200)
        self.assertEqual(self.bobby.get(f"/api/scripts/tasks/{task['task_id']}").status_code, 404)
        self.adapter.generate.assert_called_once()

    def test_startup_configuration_disables_admission_without_unloading_service(self):
        from image_workbench.composition import create_app

        with patch.dict("os.environ", {"IMAGE_WORKBENCH_DISABLED_PLUGINS": PLUGIN_ID}):
            app = create_app(self.storage, backend="local")
        self.assertFalse(app.plugins.describe(PLUGIN_ID)["enabled"])
        self.assertIsNotNone(app.script_service)
        with self.assertRaisesRegex(ValueError, "Unknown disabled plugins"):
            create_app(self.storage, backend="local", disabled_plugins=("misspelled",))
