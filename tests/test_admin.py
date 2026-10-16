import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.admin_operations import AdminOperations, file_sha, parse_test_report, source_fingerprint
from image_workbench.web import create_app
from image_workbench.locking import FileLock
from image_workbench.workbench_state import ConflictError


ROOT = Path(__file__).resolve().parents[1]


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        for name in ("image_workbench/web_static", "tests", "scripts", "deploy"):
            (self.source / name).mkdir(parents=True, exist_ok=True)
        (self.source / "image_workbench/__init__.py").write_text("# fixture\n")
        (self.source / "image_workbench/web_static/example.js").write_text("const fixture = true;\n")
        (self.source / "tests/test_web.py").write_text("# synthetic tests\n")
        for name in ("requirements.txt", "requirements-web.txt"):
            (self.source / name).write_text("# fixture\n")
        for name in ("deploy_release.py", "apply_release.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.source / "scripts" / name)
        key = self.root / "test-key"
        key.write_text("synthetic placeholder, never used")
        self.config = {"host": "192.0.2.10", "user": "ubuntu", "port": 22, "private_key": str(key)}
        (self.source / "deploy/server.json").write_text(json.dumps(self.config))
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.app = create_app(self.storage, backend="local")
        self.service = AdminOperations(self.storage, source_root=self.source)
        self.app.admin_operations = self.service
        self.admin = self.client(admin=True)
        self.user = self.client(admin=False)

    def client(self, *, admin):
        identity = self.app.identity
        with identity.transaction() as db:
            if admin:
                row = db.execute("SELECT account_id FROM accounts WHERE username='moy'").fetchone()
                aid = row[0]
                wid = db.execute("SELECT workspace_id FROM workspaces WHERE account_id=?", (aid,)).fetchone()[0]
            else:
                aid, wid = identity._account(db, "fixture_" + uuid4().hex[:12], None)
            token, csrf = uuid4().hex, uuid4().hex
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), aid, csrf, wid, "2026-10-13", time.time() + 3600, "synthetic"))
        client = TestClient(self.app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(client.close)
        client.cookies.set("iw_session", token)
        client.headers["X-Workbench-CSRF"] = csrf
        return client

    def pending(self, kind="checks"):
        with patch("image_workbench.admin_operations.subprocess.Popen", return_value=SimpleNamespace(pid=987654)):
            return self.service.start(kind, "test-admin")

    def fake_check(self, args, **kwargs):
        if "unittest" in args:
            kwargs["stdout"].write(b"test_fixture ... ok\nRan 3 tests in 0.01s\n\nOK (skipped=1)\n")
        return SimpleNamespace(returncode=0)

    def prepared(self):
        op = self.pending("prepare")
        with patch("image_workbench.admin_operations.subprocess.run", side_effect=self.fake_check), patch("image_workbench.admin_operations.shutil.which", return_value="node"):
            self.service.run(op["id"])
        result = self.service.read("operations", op["id"])
        self.assertEqual(result["status"], "succeeded", result)
        return result

    def test_admin_authorization_csrf_and_disabled_account(self):
        anon = TestClient(self.app, base_url="http://localhost", follow_redirects=False)
        self.addCleanup(anon.close)
        self.assertEqual(anon.get("/admin").status_code, 303)
        for path in ("/api/admin/overview", "/api/admin/reports", "/api/admin/operations"):
            self.assertEqual(anon.get(path).status_code, 401)
            self.assertEqual(self.user.get(path).status_code, 403)
        self.assertEqual(self.user.get("/admin").status_code, 403)
        with patch.object(self.service, "start") as start:
            self.assertEqual(self.user.post("/api/admin/checks", json={}).status_code, 403)
            self.assertEqual(self.admin.post("/api/admin/checks", json={}, headers={"X-Workbench-CSRF": "wrong"}).status_code, 403)
            self.assertEqual(self.admin.post("/api/admin/checks", json={}, headers={"Origin": "http://untrusted.example"}).status_code, 403)
            start.assert_not_called()
        with self.app.identity.transaction() as db:
            db.execute("UPDATE accounts SET status='disabled' WHERE username='moy'")
        self.assertEqual(self.admin.get("/api/admin/overview").status_code, 401)

    def test_overview_reports_are_private_and_exclude_credentials(self):
        result = self.admin.get("/api/admin/overview")
        self.assertEqual(result.status_code, 200, result.text)
        payload = result.json()
        self.assertEqual(payload["counts"]["accounts"], 2)
        self.assertEqual(payload["policy"]["credits"]["generate"], 40)
        self.assertEqual(payload["policy"]["credits"]["reference_or_edit"], 60)
        self.assertEqual(payload["policy"]["scheduling"]["max_active_total"], 1)
        self.assertEqual(payload["policy"]["scheduling"]["active"], 0)
        self.assertEqual(payload["policy"]["scheduling"]["available_slots"], 1)
        for secret in ("private_key", "password_hash", "activation_token", str(self.root)):
            self.assertNotIn(secret, result.text)
        self.assertIn("no-store", result.headers["cache-control"])
        page = self.admin.get("/admin")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/static/admin.js", page.text)
        self.assertIn("no-store", page.headers["cache-control"])

    def test_actions_reject_arbitrary_commands_big_bodies_and_false_confirmation(self):
        for body in ({"command": "whoami"}, {"path": "../data"}, {"config": "remote"}):
            self.assertEqual(self.admin.post("/api/admin/checks", json=body).status_code, 422)
        self.assertEqual(self.admin.post("/api/admin/checks", content=b"x" * 16385).status_code, 413)
        self.assertEqual(self.admin.post("/api/admin/deployments", json={"token": "x" * 40, "confirm": False}).status_code, 400)
        self.assertEqual(self.admin.post("/api/admin/deployments", json={"token": "x" * 40, "confirm": "true"}).status_code, 422)
        self.assertEqual(self.admin.post("/api/admin/deployments/preview", json={"release_id": "../../data"}).status_code, 422)
        self.assertEqual(self.service.operations(), [])

    def test_double_click_starts_one_fixed_background_process(self):
        with patch("image_workbench.admin_operations.subprocess.Popen", return_value=SimpleNamespace(pid=1234)) as popen:
            first = self.admin.post("/api/admin/checks", json={})
            second = self.admin.post("/api/admin/checks", json={})
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(popen.call_count, 1)
        args, options = popen.call_args
        self.assertIn("image_workbench.admin_operations", args[0])
        self.assertNotIn("shell", options)
        self.assertNotIn("runner_pid", first.json())
        self.assertNotIn("account_id", first.json())

    def test_check_report_is_bound_to_code_and_test_changes(self):
        op = self.pending()
        with patch("image_workbench.admin_operations.subprocess.run", side_effect=self.fake_check), patch("image_workbench.admin_operations.shutil.which", return_value="node"):
            self.service.run(op["id"])
        report = self.service.reports()[0]
        self.assertEqual((report["total"], report["passed"], report["skipped"]), (3, 2, 1))
        self.assertTrue(report["current_source"])
        (self.source / "tests/test_web.py").write_text("# test coverage changed\n")
        self.assertFalse(self.service.reports()[0]["current_source"])

    def test_code_changes_during_checks_block_preparation(self):
        def changed(args, **kwargs):
            value = self.fake_check(args, **kwargs)
            if "unittest" in args:
                (self.source / "image_workbench/__init__.py").write_text("# modified during test\n")
            return value
        op = self.pending("prepare")
        with patch("image_workbench.admin_operations.subprocess.run", side_effect=changed), patch("image_workbench.admin_operations.shutil.which", return_value="node"):
            self.service.run(op["id"])
        self.assertEqual(self.service.read("operations", op["id"])["status"], "failed")
        self.assertEqual(self.service.reports()[0]["status"], "stale")
        self.assertFalse(list((self.source / ".deploy").glob("*/admin-prepared.json")))

    def test_missing_node_does_not_claim_all_checks_passed(self):
        op = self.pending("prepare")
        with patch("image_workbench.admin_operations.subprocess.run", side_effect=self.fake_check), patch("image_workbench.admin_operations.shutil.which", return_value=None):
            self.service.run(op["id"])
        report = self.service.reports()[0]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["checks"][-1]["status"], "skipped")
        self.assertFalse(list((self.source / ".deploy").glob("*/admin-prepared.json")))

    def test_failed_cases_are_visible_without_raw_traceback_or_private_content(self):
        def failed(args, **kwargs):
            if "unittest" in args:
                kwargs["stdout"].write(b"FAIL: test_boundary (test_suite.Example)\nprivate-secret-in-traceback\nRan 2 tests in 0.3s\nFAILED (failures=1)\n")
            return SimpleNamespace(returncode=1 if "unittest" in args else 0)
        op = self.pending()
        with patch("image_workbench.admin_operations.subprocess.run", side_effect=failed), patch("image_workbench.admin_operations.shutil.which", return_value="node"):
            self.service.run(op["id"])
        response = self.admin.get("/api/admin/reports")
        self.assertIn("test_boundary", response.text)
        self.assertNotIn("private-secret-in-traceback", response.text)
        self.assertEqual(response.json()["reports"][0]["failed"], 1)

    def test_deployment_confirmation_is_one_time_and_bound_to_session(self):
        prepared = self.prepared()
        with patch("image_workbench.admin_operations.shutil.which", return_value="ssh"):
            preview = self.service.preview(prepared["release_id"], "admin-a", "session-a")
        with patch("image_workbench.admin_operations.subprocess.Popen", return_value=SimpleNamespace(pid=2)) as launch:
            with self.assertRaises(ConflictError):
                self.service.start("deploy", "admin-a", session_key="session-b", token=preview["token"])
            with self.assertRaises(ConflictError):
                self.service.start("deploy", "admin-b", session_key="session-a", token=preview["token"])
            launch.assert_not_called()
            started = self.service.start("deploy", "admin-a", session_key="session-a", token=preview["token"])
            op = self.service.read("operations", started["id"])
            op["status"] = "succeeded"
            self.service.write("operations", op)
            with self.assertRaises(ConflictError):
                self.service.start("deploy", "admin-a", session_key="session-a", token=preview["token"])
        self.assertEqual(launch.call_count, 1)
        response = self.admin.get("/api/admin/operations")
        self.assertNotIn(preview["token"], response.text)
        self.assertNotIn("approval", response.text)
        self.assertNotIn("session-a", response.text)

    def test_expired_or_changed_target_confirmation_does_not_launch(self):
        prepared = self.prepared()
        with patch("image_workbench.admin_operations.shutil.which", return_value="ssh"):
            preview = self.service.preview(prepared["release_id"], "admin-a", "session-a")
        (self.source / "deploy/server.json").write_text(json.dumps({**self.config, "host": "192.0.2.11"}))
        with patch("image_workbench.admin_operations.subprocess.Popen") as launch:
            with self.assertRaises(ConflictError):
                self.service.start("deploy", "admin-a", session_key="session-a", token=preview["token"])
            launch.assert_not_called()
        (self.source / "deploy/server.json").write_text(json.dumps(self.config))
        with patch("image_workbench.admin_operations.time.time", return_value=preview["expires_at"] + 1):
            with self.assertRaises(ConflictError):
                self.service.start("deploy", "admin-a", session_key="session-a", token=preview["token"])

    def test_changed_archive_script_and_manifest_are_rejected(self):
        prepared = self.prepared()
        directory = self.service.release_path(prepared["release_id"])
        for name in ("release.tar.gz", "apply_release.sh", "manifest.json"):
            path = directory / name
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaises(ConflictError):
                self.service.verified_release(prepared["release_id"])
            path.write_bytes(original)
        self.assertEqual(self.service.verified_release(prepared["release_id"])["release_id"], prepared["release_id"])

    def test_deployment_uses_approved_archive_digest_and_pinned_script(self):
        prepared = self.prepared()
        with patch("image_workbench.admin_operations.shutil.which", return_value="ssh"):
            preview = self.service.preview(prepared["release_id"], "admin-a", "session-a")
        with patch("image_workbench.admin_operations.subprocess.Popen", return_value=SimpleNamespace(pid=2)):
            started = self.service.start("deploy", "admin-a", session_key="session-a", token=preview["token"])
        op = self.service.read("operations", started["id"])
        module = self.service.deploy_module()
        with patch.object(module, "deploy") as deploy, patch.object(self.service, "deploy_module", return_value=module):
            self.service.execute_deploy(op)
        self.assertEqual(deploy.call_args.kwargs["expected_archive_sha"], op["approval"]["archive_sha"])
        self.assertEqual(hashlib.sha256(deploy.call_args.kwargs["script_bytes"]).hexdigest(), op["approval"]["script_sha"])

    def test_remote_failure_is_uncertain_and_does_not_replay(self):
        prepared = self.prepared()
        with patch("image_workbench.admin_operations.shutil.which", return_value="ssh"):
            preview = self.service.preview(prepared["release_id"], "admin-a", "session-a")
        with patch("image_workbench.admin_operations.subprocess.Popen", return_value=SimpleNamespace(pid=2)):
            started = self.service.start("deploy", "admin-a", session_key="session-a", token=preview["token"])
        module = self.service.deploy_module()
        with patch.object(module, "deploy", side_effect=RuntimeError("private deployment output")) as deploy, patch.object(self.service, "deploy_module", return_value=module):
            self.service.run(started["id"])
            self.service.run(started["id"])
        self.assertEqual(deploy.call_count, 1)
        result = self.service.public_operation(self.service.read("operations", started["id"]))
        self.assertEqual(result["status"], "needs_attention")
        self.assertNotIn("private deployment output", result["summary"])

    def test_incomplete_installation_is_read_only(self):
        (self.source / "tests/test_web.py").unlink()
        status = self.service.deployment_status()
        self.assertFalse(status["available"])
        self.assertFalse(status["package_available"])
        with self.assertRaises(ConflictError):
            self.service.start("checks", "admin")

    def test_stale_runner_is_reported_without_automatic_replay(self):
        op = self.pending()
        stored = self.service.read("operations", op["id"])
        stored["heartbeat_at"] = time.time() - 100
        self.service.write("operations", stored)
        self.assertEqual(self.service.operations()[0]["status"], "needs_attention")
        with self.assertRaises(ConflictError):
            self.pending()

    def test_acknowledge_requires_explicit_confirmation_and_released_execution_lock(self):
        op = self.pending()
        endpoint = f"/api/admin/operations/{op['id']}/acknowledge"
        self.assertEqual(self.admin.post(endpoint, json={"confirm": True}).status_code, 409)
        stored = self.service.read("operations", op["id"])
        stored["heartbeat_at"] = time.time() - 100
        self.service.write("operations", stored)
        self.assertEqual(self.admin.post(endpoint, json={"confirm": False}).status_code, 400)
        (self.source / ".deploy").mkdir(exist_ok=True)
        with FileLock(self.source / ".deploy/admin-operation.lock"):
            self.assertEqual(self.admin.post(endpoint, json={"confirm": True}).status_code, 409)
        with patch("image_workbench.admin_operations.subprocess.Popen") as launch:
            response = self.admin.post(endpoint, json={"confirm": True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "acknowledged")
            launch.assert_not_called()
        self.assertEqual(self.admin.post(endpoint, json={"confirm": True}).status_code, 409)
        self.assertEqual(self.pending()["status"], "queued")

    def test_parser_does_not_treat_interrupted_output_as_passing(self):
        self.assertEqual(parse_test_report("test_example ... ok\n", 1)["status"], "failed")
        value = parse_test_report("Ran 10 tests in 2.1s\nFAILED (failures=2, errors=1, skipped=2)\n", 1)
        self.assertEqual((value["total"], value["passed"], value["failed"], value["skipped"]), (10, 5, 3, 2))
        self.assertEqual(parse_test_report("Ran 0 tests in 0.0s\nOK\n", 0)["status"], "failed")

    def test_duplicate_runner_cannot_overwrite_running_operation(self):
        op = self.pending()
        entered, release = threading.Event(), threading.Event()
        def blocking_checks(record, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            record["summary"] = "single execution"
        with patch.object(self.service, "execute_checks", side_effect=blocking_checks) as checks:
            first = threading.Thread(target=self.service.run, args=(op["id"],))
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                self.service.run(op["id"])
                self.assertEqual(self.service.read("operations", op["id"])["status"], "running")
            finally:
                release.set()
                first.join(5)
            self.assertEqual(checks.call_count, 1)
        self.assertEqual(self.service.read("operations", op["id"])["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
