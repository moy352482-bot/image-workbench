"""脚本崩溃窗口、完整性凭据及本地停止确认的离线恢复测试。"""

import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import tests.test_script_service as service_fixtures
import tests.test_script_adapter as adapter_fixtures
from image_workbench.locking import LockBusyError
from image_workbench.script_adapter import CodexTextAdapter, ScriptGenerationError
from image_workbench.workbench_state import ConflictError


class ScriptRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Composition deliberately avoids inheriting/rerunning the service test suite.
        self.fixture = service_fixtures.ScriptServiceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service, self.storage = self.fixture.service, self.fixture.storage
        self.identity = self.fixture.identity
        self.aid, self.wid = self.fixture.aid, self.fixture.wid

    def running(self):
        public = self.fixture.submit()
        job = self.service._load(public["task_id"])
        job["status"] = "running"
        self.service._write(job)
        return job

    def recover(self, job):
        return self.service.recover(self.aid, self.wid, job["task_id"])

    def assert_frozen(self, job):
        self.fixture.assert_ledger(job, [("freeze", 40)])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)

    def install_codex_adapter(self):
        executable = self.fixture.root / ("codex.exe" if os.name == "nt" else "codex")
        executable.write_bytes(b"not executed")
        self.service.adapter = CodexTextAdapter(executable, timeout_seconds=1)
        return executable

    def publish_codex_response(self, job):
        executable = self.install_codex_adapter()
        processes = []
        def fake_popen(argv, **kwargs):
            self.assertEqual(Path(argv[0]), executable)
            process = adapter_fixtures.FakeProcess(argv, kwargs, body=service_fixtures.TEXT)
            processes.append(process)
            return process
        work = self.service._candidate(job).parent / "codex"
        with patch("image_workbench.script_adapter.subprocess.Popen", side_effect=fake_popen):
            self.service.adapter.generate(prompt=job["generation_prompt"], model_id=job["model_id"],
                                          thinking_level=job["thinking_level"], work_dir=work)
        self.assertEqual(len(processes), 1)
        return work

    def fail_version_write(self):
        write_json = self.storage._write_json
        def write(path, value):
            if Path(path).name == "script-versions.json":
                raise OSError("simulated version publication interruption")
            return write_json(path, value)
        return patch.object(self.storage, "_write_json", side_effect=write)

    def test_partial_candidate_without_completion_receipt_cannot_charge(self):
        job = self.running()
        self.service._candidate(job).write_text("第一场：未写完", encoding="utf-8")
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.recover(job)
        self.assert_frozen(job)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 0)
        self.assertEqual(self.fixture.adapter.calls, [])
        self.assertEqual(self.storage.list_task_artifacts(job["project_id"], job["task_id"]), [])

    def test_atomic_candidate_failure_does_not_publish_partial_text_and_response_recovers(self):
        job = self.running()
        work = self.publish_codex_response(job)
        candidate = self.service._candidate(job)
        replace = os.replace
        def interrupt_candidate(source, target):
            if Path(target) == candidate:
                raise OSError("simulated interruption before candidate publication")
            return replace(source, target)
        with patch("image_workbench.script_service.os.replace", side_effect=interrupt_candidate):
            with self.assertRaises(OSError):
                self.service._publish_candidate(job, service_fixtures.TEXT)
        self.assertFalse(candidate.exists())
        self.assertEqual(list(candidate.parent.glob(".candidate-*.tmp")), [])
        self.assertTrue((work / "completed.json").is_file())
        with patch("image_workbench.script_adapter.subprocess.Popen", side_effect=AssertionError("must not execute again")) as popen:
            result = self.recover(job)
        popen.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.fixture.assert_ledger(job, [("freeze", 40), ("consume", 40)])
        self.assertEqual(self.service.download(self.aid, self.wid, job["task_id"])[0].decode("utf-8"), service_fixtures.TEXT)

    def test_successful_codex_receipt_recovers_without_candidate_or_model_replay(self):
        job = self.running()
        work = self.publish_codex_response(job)
        receipt = json.loads((work / "completed.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["model_id"], receipt["thinking_level"], receipt["returncode"]),
                         (job["model_id"], job["thinking_level"], 0))
        self.assertEqual(receipt["prompt_sha256"], hashlib.sha256(job["generation_prompt"].encode("utf-8")).hexdigest())
        self.assertEqual(receipt["response_sha256"], hashlib.sha256((work / "response.txt").read_bytes()).hexdigest())
        self.assertFalse(self.service._candidate(job).exists())
        detail = self.service.detail(self.aid, self.wid, job["task_id"])
        self.assertEqual(detail["status"], "uncertain")
        self.assertTrue(detail["can_recover"])
        with patch("image_workbench.script_adapter.subprocess.Popen", side_effect=AssertionError("must not execute again")) as popen:
            completed = self.recover(job)
            repeated = self.recover(job)
            self.service.run(self.aid, self.wid, job["task_id"])
        popen.assert_not_called()
        self.assertEqual((completed["output_artifact_id"], completed["version_id"]),
                         (repeated["output_artifact_id"], repeated["version_id"]))
        self.fixture.assert_ledger(job, [("freeze", 40), ("consume", 40)])

    def test_codex_receipt_and_response_tampering_never_archives_or_executes(self):
        job = self.running()
        work = self.publish_codex_response(job)
        receipt_path = work / "completed.json"
        original = json.loads(receipt_path.read_text(encoding="utf-8"))
        for key, value in (("model_id", "gpt-6-luna"), ("thinking_level", "low"), ("returncode", 1),
                           ("prompt_sha256", "0" * 64), ("response_sha256", "0" * 64), ("response_size_bytes", 1)):
            receipt_path.write_text(json.dumps({**original, key: value}), encoding="utf-8")
            with self.subTest(field=key), patch("image_workbench.script_adapter.subprocess.Popen") as popen:
                with self.assertRaises(ScriptGenerationError):
                    self.recover(job)
                popen.assert_not_called()
            self.assert_frozen(job)
        receipt_path.write_text(json.dumps(original), encoding="utf-8")
        (work / "response.txt").write_text("被改写的正文", encoding="utf-8")
        with self.assertRaises(ScriptGenerationError):
            self.recover(job)
        self.assertEqual(self.storage.list_task_artifacts(job["project_id"], job["task_id"]), [])

    def test_diagnostic_log_failure_does_not_block_model_failure_refund(self):
        job = self.fixture.submit()
        self.fixture.adapter.error = ScriptGenerationError("synthetic model failure")
        write_text = Path.write_text
        def fail_error_log(path, *args, **kwargs):
            if path.name == "error.log":
                raise PermissionError("simulated diagnostics permission failure")
            return write_text(path, *args, **kwargs)
        with patch.object(Path, "write_text", fail_error_log):
            result = self.service.run(self.aid, self.wid, job["task_id"])
        self.assertEqual((result["status"], result["credit_status"]), ("failed", "refunded"))
        self.fixture.assert_ledger(job, [("freeze", 40), ("refund", 40)])
        self.assertEqual(len(self.fixture.adapter.calls), 1)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_registered_artifact_must_match_candidate_and_task_on_recovery(self):
        job = self.fixture.submit()
        with self.fail_version_write():
            result = self.service.run(self.aid, self.wid, job["task_id"])
        self.assertEqual(result["status"], "uncertain")
        current = self.service._load(job["task_id"])
        candidate = self.service._candidate(current)
        # Even a newly matching completion receipt cannot replace the registered immutable artifact.
        changed = "第一场：与已登记版本不同的正文。\n".encode("utf-8")
        candidate.write_bytes(changed)
        self.storage._write_json(candidate.with_suffix(".complete.json"), {
            "schema_version": 1, "task_id": job["task_id"], "request_fingerprint": current["request_fingerprint"],
            "size_bytes": len(changed), "sha256": hashlib.sha256(changed).hexdigest(),
        })
        with self.assertRaisesRegex(ValueError, "已有产物"):
            self.recover(job)
        self.assert_frozen(job)
        self.assertEqual(len(self.fixture.adapter.calls), 1)
        self.assertEqual(self.storage.get_artifact_path(job["output_artifact_id"]).read_text(encoding="utf-8"), service_fixtures.TEXT)

    def test_version_binding_mismatch_prevents_successful_settlement(self):
        job = self.fixture.submit()
        with self.fail_version_write():
            self.service.run(self.aid, self.wid, job["task_id"])
        current = self.service._load(job["task_id"])
        valid = {"version_id": current["version_id"], "document_id": current["document_id"], "version": 1,
                 "task_id": current["task_id"], "artifact_id": current["output_artifact_id"], "parent_version": None}
        path = self.storage._path(".workbench", "script-versions.json")
        for field in ("document_id", "task_id", "artifact_id", "parent_version"):
            self.storage._write_json(path, {"schema_version": 1, "versions": {current["version_id"]: {**valid, field: uuid4().hex}}})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "版本登记"):
                self.recover(job)
            self.assert_frozen(job)
        self.assertEqual(len(self.fixture.adapter.calls), 1)

    def test_candidate_and_codex_symlinks_cannot_read_or_write_outside_storage(self):
        job = self.running()
        candidate = self.service._candidate(job)
        outside = self.fixture.root / "outside.txt"
        outside.write_text("外部文件不可修改", encoding="utf-8")
        original = outside.read_bytes()
        try:
            candidate.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"当前平台不支持测试符号链接：{type(exc).__name__}")
        try:
            with self.assertRaisesRegex(ValueError, "符号链接|重解析点"):
                self.recover(job)
            self.assertEqual(outside.read_bytes(), original)
        finally:
            candidate.unlink()
        external_dir = self.fixture.root / "outside-run"
        external_dir.mkdir()
        linked_dir = candidate.parent / "codex"
        linked_dir.symlink_to(external_dir, target_is_directory=True)
        try:
            with self.assertRaisesRegex(ValueError, "符号链接|重解析点"):
                self.recover(job)
            self.assertEqual(list(external_dir.iterdir()), [])
        finally:
            linked_dir.unlink()
        self.assert_frozen(job)
        self.assertEqual(self.fixture.adapter.calls, [])

    def test_candidate_codex_and_receipt_reparse_points_are_rejected(self):
        job = self.running()
        candidate = self.service._candidate(job)
        lstat = Path.lstat
        for path in (candidate, candidate.parent / "codex", candidate.with_suffix(".complete.json")):
            if path.suffix == ".json":
                self.service._publish_candidate(job, service_fixtures.TEXT)
            def reparse(target, *args, **kwargs):
                if target == path:
                    return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
                return lstat(target, *args, **kwargs)
            with self.subTest(path=path.name), patch.object(Path, "lstat", reparse):
                with self.assertRaisesRegex(ValueError, "重解析点"):
                    self.recover(job)
        self.assert_frozen(job)
        self.assertEqual(self.fixture.adapter.calls, [])

    def test_candidate_republication_is_idempotent_without_replacing_receipt(self):
        job = self.running()
        self.service._publish_candidate(job, service_fixtures.TEXT)
        candidate = self.service._candidate(job)
        receipt = candidate.with_suffix(".complete.json")
        original_receipt, original_text = receipt.read_bytes(), candidate.read_bytes()
        with patch.object(self.storage, "_write_json", side_effect=AssertionError("must preserve original receipt")):
            self.service._publish_candidate(job, service_fixtures.TEXT)
            with self.assertRaises(ConflictError):
                self.service._publish_candidate(job, "不同的正文。")
        self.assertEqual(receipt.read_bytes(), original_receipt)
        self.assertEqual(candidate.read_bytes(), original_text)

    def test_manual_revision_retries_stable_ids_before_and_after_candidate_publication(self):
        parent = self.fixture.complete()
        edited = service_fixtures.TEXT.replace("回家吧", "我们一直等你")
        body = {"request_id": uuid4().hex, "text": edited, "change_reason": "修改末句对白"}
        with patch.object(self.service, "_publish_candidate", side_effect=OSError("simulated candidate write interruption")):
            with self.assertRaises(OSError):
                self.service.save_revision(self.aid, self.wid, parent["task_id"], body)
        staged = next(item for item in self.service.list_tasks(self.aid, self.wid) if item.get("source") == "manual_edit")
        self.assertFalse(self.service._candidate(staged).exists())
        with patch.object(self.service, "_archive", side_effect=OSError("simulated interruption after candidate publish")):
            with self.assertRaises(OSError):
                self.service.save_revision(self.aid, self.wid, parent["task_id"], body)
        candidate = self.service._candidate(staged)
        self.assertEqual(candidate.read_text(encoding="utf-8"), edited)
        receipt = candidate.with_suffix(".complete.json").read_bytes()
        completed = self.service.save_revision(self.aid, self.wid, parent["task_id"], body)
        self.assertEqual(completed["status"], "completed")
        for key in ("task_id", "output_artifact_id", "version_id", "document_id"):
            self.assertEqual(completed[key], staged[key])
        self.assertEqual(completed["output_version"], 2)
        self.assertEqual(candidate.with_suffix(".complete.json").read_bytes(), receipt)
        self.assertEqual(len(self.fixture.adapter.calls), 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(self.service.download(self.aid, self.wid, parent["task_id"])[0].decode("utf-8"), service_fixtures.TEXT)

    def test_confirm_stopped_requires_explicit_confirmation_and_releases_once(self):
        job = self.running()
        for confirmed in (False, None, 1, "yes"):
            with self.subTest(confirmed=confirmed), self.assertRaises(ValueError):
                self.service.confirm_stopped(self.aid, self.wid, job["task_id"], confirmed=confirmed)
        with self.service._lock(job["task_id"]):
            with self.assertRaises(LockBusyError):
                self.service.confirm_stopped(self.aid, self.wid, job["task_id"], confirmed=True)
        self.assert_frozen(job)
        result = self.service.confirm_stopped(self.aid, self.wid, job["task_id"], confirmed=True)
        self.service.confirm_stopped(self.aid, self.wid, job["task_id"], confirmed=True)
        self.service.run(self.aid, self.wid, job["task_id"])
        self.assertEqual((result["status"], result["credit_status"]), ("failed", "refunded"))
        self.fixture.assert_ledger(job, [("freeze", 40), ("refund", 40)])
        self.assertEqual(self.fixture.adapter.calls, [])

    def test_confirm_stopped_rejects_other_owners_and_version_already_registered(self):
        job = self.fixture.submit()
        record_job = self.identity.record_job
        def fail_consume(value):
            if value["status"] == "completed":
                raise OSError("simulated settlement interruption")
            return record_job(value)
        with patch.object(self.identity, "record_job", side_effect=fail_consume):
            result = self.service.run(self.aid, self.wid, job["task_id"])
        self.assertEqual(result["status"], "uncertain")
        self.assertIn(job["version_id"], self.service._versions()["versions"])
        for aid, wid in ((self.fixture.bid, self.fixture.bwid), (self.aid, self.fixture.awid2)):
            with self.subTest(account=aid, workspace=wid), self.assertRaises(FileNotFoundError):
                self.service.confirm_stopped(aid, wid, job["task_id"], confirmed=True)
        with self.assertRaisesRegex(ConflictError, "已登记版本"):
            self.service.confirm_stopped(self.aid, self.wid, job["task_id"], confirmed=True)
        self.assert_frozen(job)
        self.assertEqual(self.recover(job)["status"], "completed")
        self.fixture.assert_ledger(job, [("freeze", 40), ("consume", 40)])
        self.assertEqual(len(self.fixture.adapter.calls), 1)


if __name__ == "__main__":
    unittest.main()
