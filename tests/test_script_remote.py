import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import digest
from image_workbench.remote_access import RemoteAccess
from image_workbench.script_models import ScriptModelRegistry
from image_workbench.script_remote import RemoteScriptRegistry
from image_workbench.script_remote_worker import ScriptRemoteWorker
from image_workbench.storage import _now
from image_workbench.web import create_app
from image_workbench.workbench_state import ConflictError
from tests.test_script_service import TEXT


class RemoteScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "server"))
        self.token = "s" * 40
        self.app = create_app(self.storage, backend="codex", remote_access=RemoteAccess("https://scripts.example", self.token))
        # Explicit legacy catalog exercises recovery of accepted Codex jobs.
        self.app.script_service.registry = RemoteScriptRegistry(self.app.script_queue)
        self.app.script_service.router.registry = self.app.script_service.registry
        self.identity = self.app.identity
        self.app.video_store.enabled_backends = ("h3", "online-2.0")  # Exercise legacy accepted work and clients.
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "remote_writer", None)
        self.user = self.client()
        token, csrf = uuid4().hex, uuid4().hex
        with self.identity.transaction() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (digest(token), self.aid, csrf, self.wid, _now(), time.time()+3600, "remote-script-test"))
        self.user.cookies.set("iw_session", token)
        self.user.headers["X-Workbench-CSRF"] = csrf
        main = self.user.post("/api/main-projects", json={"request_id": uuid4().hex, "name": "远程脚本"}).json()
        self.sid = main["subprojects"][0]["subproject_id"]
        self.registry = ScriptModelRegistry(catalog={"models": [{"slug": mid, "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]}
            for mid in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")]})
        self.adapter = Mock()
        self.adapter.generate.return_value = TEXT
        self.app.script_service.adapter = Mock()
        self.worker = self.make_worker()

    def client(self):
        client = TestClient(self.app, base_url="https://scripts.example", raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def make_worker(self):
        worker = ScriptRemoteWorker("https://scripts.example", self.token, self.root / "worker", adapter=self.adapter, registry=self.registry, client=self.client())
        self.addCleanup(worker.close)
        return worker

    def body(self, **changes):
        body = {"subproject_id": self.sid, "generation_type": "script", "input_request": "写一个原创的阅读灯短视频脚本",
                "selection_mode": "manual", "model_id": "gpt-6-astra", "thinking_level": "high"}
        body.update(changes)
        return body

    def submit(self, **changes):
        self.worker.run_once()  # Announce idle model capabilities, without inference.
        body = self.body(**changes)
        quote = self.user.post("/api/scripts/quote", json=body)
        self.assertEqual(quote.status_code, 200, quote.text)
        result = self.user.post("/api/scripts/tasks", json={**body, "request_id": uuid4().hex, "quote_signature": quote.json()["quote_signature"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "queued")
        self.app.script_service.adapter.generate.assert_not_called()
        return result.json()

    def test_remote_models_require_live_worker_and_keep_exact_prices(self):
        config = self.user.get("/api/scripts/config").json()
        self.assertFalse(any(m["available"] for m in config["models"]))
        self.assertEqual(self.worker.run_once(), "idle")
        config = self.user.get("/api/scripts/config").json()
        astra = next(m for m in config["models"] if m["model_id"] == "gpt-6-astra")
        self.assertTrue(astra["available"])
        self.assertEqual([x["id"] for x in astra["thinking_levels"]], ["low", "medium", "high", "xhigh", "max", "ultra"])
        self.assertEqual(next(x["price"] for x in astra["thinking_levels"] if x["id"] == "high"), 40)
        with patch("image_workbench.script_remote.time.time", return_value=time.time()+121):
            self.assertFalse(any(m["available"] for m in self.user.get("/api/scripts/config").json()["models"]))
        self.adapter.generate.assert_not_called()

    def test_cloud_to_local_generation_download_and_settlement(self):
        task = self.submit()
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        self.assertEqual(self.worker.run_once(), "completed")
        detail = self.user.get("/api/scripts/tasks/" + task["task_id"]).json()
        self.assertEqual((detail["status"], detail["text"]), ("completed", TEXT))
        self.assertNotIn("claim_id", detail)
        self.assertNotIn("worker_id", detail)
        self.assertEqual(self.adapter.generate.call_args.kwargs["model_id"], "gpt-6-astra")
        self.assertEqual(self.adapter.generate.call_args.kwargs["thinking_level"], "high")
        self.assertEqual(self.user.get("/api/scripts/tasks/"+task["task_id"]+"/download").content, TEXT.encode())
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.worker.run_once(), "idle")
        self.assertEqual(self.adapter.generate.call_count, 1)

    def test_retransmit_after_lost_complete_response_does_not_reinvoke(self):
        task = self.submit()
        original = self.worker.request
        lost = False
        def request(method, path, *args, **kwargs):
            nonlocal lost
            result = original(method, path, *args, **kwargs)
            if path.endswith("/complete") and not lost:
                lost = True
                raise httpx.ReadError("simulated response loss")
            return result
        with patch.object(self.worker, "request", side_effect=request):
            with self.assertRaises(httpx.ReadError):
                self.worker.run_once()
        self.assertEqual(self.make_worker().run_once(), "completed")
        self.assertEqual(self.adapter.generate.call_count, 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.user.get("/api/scripts/tasks/"+task["task_id"]).json()["output_version"], 1)

    def test_explicit_generation_failure_refunds(self):
        self.submit()
        self.adapter.generate.side_effect = ValueError("model unavailable")
        self.assertEqual(self.worker.run_once(), "failed")
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_invalid_finished_text_is_refunded_even_when_codex_receipt_exists(self):
        self.submit()
        def invalid(**kwargs):
            work = kwargs["work_dir"]; work.mkdir(parents=True); (work/"completed.json").write_text("{}")
            return '{"not": "a finished script"}'
        self.adapter.generate.side_effect = invalid
        self.assertEqual(self.worker.run_once(), "failed")
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)

    def test_started_without_receipt_becomes_uncertain_and_never_replays(self):
        task = self.submit()
        queue = self.app.script_queue
        manifest = queue.claim(self.worker.worker_id, [{"model_id": "gpt-6-astra", "efforts": ["high"]}])
        attempt = uuid4().hex
        queue.start(task["task_id"], self.worker.worker_id, manifest["claim_id"], attempt)
        journal = {"manifest": manifest, "phase": "started", "attempt_id": attempt}
        self.worker._save(journal)
        self.assertEqual(self.worker.run_once(), "uncertain")
        self.assertEqual(self.worker.run_once(), "uncertain")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        self.adapter.generate.assert_not_called()
        self.worker.confirm_stopped(task["task_id"])
        self.assertEqual(self.worker.run_once(), "failed")
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)

    def test_completed_local_receipt_recovers_after_restart_without_inference(self):
        task = self.submit()
        queue = self.app.script_queue
        manifest = queue.claim(self.worker.worker_id, [{"model_id": "gpt-6-astra", "efforts": ["high"]}])
        attempt = uuid4().hex
        queue.start(task["task_id"], self.worker.worker_id, manifest["claim_id"], attempt)
        self.worker._save({"manifest": manifest, "phase": "started", "attempt_id": attempt})
        work = self.worker._directory(task["task_id"])/"codex"; work.mkdir(); (work/"completed.json").write_text("{}")
        self.adapter.recover.return_value = TEXT
        self.assertEqual(self.make_worker().run_once(), "completed")
        self.adapter.generate.assert_not_called()
        self.adapter.recover.assert_called_once()

    def test_active_remote_job_is_not_marked_uncertain_by_detail_read(self):
        task = self.submit()
        self.app.script_queue.claim(self.worker.worker_id, [{"model_id": "gpt-6-astra", "efforts": ["high"]}])
        self.assertEqual(self.user.get("/api/scripts/tasks/"+task["task_id"]).json()["status"], "running")
        with patch("image_workbench.script_service.time.time", return_value=time.time()+121):
            self.assertEqual(self.user.get("/api/scripts/tasks/"+task["task_id"]).json()["status"], "uncertain")

    def test_worker_auth_claim_and_attempt_boundaries(self):
        denied = self.client().post("/api/worker/scripts/claim", json={"worker_id": uuid4().hex, "models": []})
        self.assertEqual(denied.status_code, 401)
        task = self.submit()
        queue = self.app.script_queue
        manifest = queue.claim(self.worker.worker_id, [{"model_id": "gpt-6-astra", "efforts": ["high"]}])
        self.assertIsNone(queue.claim(uuid4().hex, [{"model_id": "gpt-6-astra", "efforts": ["high"]}]))
        with self.assertRaises(ConflictError):
            queue.start(task["task_id"], uuid4().hex, manifest["claim_id"], uuid4().hex)
        attempt = uuid4().hex
        queue.start(task["task_id"], self.worker.worker_id, manifest["claim_id"], attempt)
        with self.assertRaises(ConflictError):
            queue.start(task["task_id"], self.worker.worker_id, manifest["claim_id"], uuid4().hex)
        with self.assertRaises(ValueError):
            queue.complete(task["task_id"], self.worker.worker_id, manifest["claim_id"], {"attempt_id": attempt, "status": "succeeded", "text": TEXT, "sha256": "0"*64})
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)

    def test_zero_credit_remote_generation_and_unpriced_level_blocked(self):
        self.submit(model_id="gpt-6-luna")
        self.assertEqual(self.worker.run_once(), "completed")
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)
        response = self.user.post("/api/scripts/quote", json=self.body(model_id="gpt-6-luna", thinking_level="max"))
        self.assertEqual(response.status_code, 400)

