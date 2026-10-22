from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import CreditInsufficient, IdentityStore
from image_workbench.script_adapter import ScriptGenerationError
from image_workbench.script_models import CONFIG_ROOT, ScriptModelRegistry
from image_workbench.script_service import ScriptService
from image_workbench.script_templates import SKILL_ROOT, ScriptTemplateLoader
from image_workbench.workspace_scope import WorkspaceProjects
from image_workbench.workbench_state import ConflictError


TEXT = "《海边的灯》\n\n1-1 夜 外 海边\n人物：守灯人\n▲灯亮起，他朝归船挥手。\n守灯人：回家吧。\n【完】\n"


class FakeTextAdapter:
    def __init__(self):
        self.calls = []
        self.text = TEXT
        self.error = None
        self.entered = None
        self.release = None
        self._guard = threading.Lock()

    def generate(self, **kwargs):
        with self._guard:
            self.calls.append(kwargs)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(5):
            raise RuntimeError("测试并发同步超时")
        if self.error is not None:
            raise self.error
        return self.text


class ScriptServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.identity = IdentityStore(self.storage)
        self.identity.bootstrap([])
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "alice", None)
            self.bid, self.bwid = self.identity._account(db, "bobby", None)
        self.awid2 = self.identity.create_workspace(self.aid, "另一个空间")["workspace_id"]
        self.projects = WorkspaceProjects(self.storage, self.identity, self.aid, self.wid)
        self.main = self.projects.create_main_project("脚本测试", main_project_id=uuid4().hex)
        self.sid = self.projects.default_subproject(self.main.main_project_id).subproject_id
        self.config_root = self.root / "config"
        self.config_root.mkdir()
        for name in ("script_models.json", "script_pricing.json", "script_routing.json"):
            shutil.copyfile(CONFIG_ROOT / name, self.config_root / name)
        catalog = {"models": [
            {"slug": name, "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max", "ultra")]}
            for name in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna")
        ]}
        self.registry = ScriptModelRegistry(self.config_root, catalog=catalog)
        self.adapter = FakeTextAdapter()
        self.service = ScriptService(self.storage, self.identity, registry=self.registry, adapter=self.adapter)

    def body(self, **changes):
        value = {"subproject_id": self.sid, "generation_type": "screenplay", "template_id": "episodic_screenplay_plaintext",
                 "input_request": "写一个海边守灯人等待归船的原创故事", "selection_mode": "manual",
                 "model_id": "gpt-6-astra", "thinking_level": "high", "request_id": uuid4().hex}
        value.update(changes)
        return value

    def quoted(self, body=None):
        body = dict(body or self.body())
        quote = self.service.quote(self.aid, self.wid, body)
        return {**body, "quote_signature": quote["quote_signature"]}, quote

    def submit(self, body=None):
        body, _ = self.quoted(body)
        return self.service.submit(self.aid, self.wid, body)

    def complete(self, body=None):
        task = self.submit(body)
        result = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(result["status"], "completed", result)
        return result

    def test_skill_reference_update_requotes_but_queued_job_keeps_exact_prompt(self):
        skill_root = self.root / "skill"
        shutil.copytree(SKILL_ROOT, skill_root)
        self.service.templates = ScriptTemplateLoader(skill_root)
        body, before = self.quoted(self.body(template_id="none"))
        queued = self.service.submit(self.aid, self.wid, body)
        saved = self.service._load(queued["task_id"])
        self.assertIn("# 剧情剧本专项方法", saved["generation_prompt"])
        self.assertEqual(saved["resource_references"]["skill"]["files"][1]["path"], "references/screenplay-craft.md")
        ref = skill_root / "references/screenplay-craft.md"
        ref.write_text("更新后的剧情创作规则", encoding="utf-8")
        self.assertNotEqual(before["quote_signature"], self.service.quote(self.aid, self.wid, body)["quote_signature"])
        with self.assertRaises(ConflictError):
            self.service.submit(self.aid, self.wid, {**body, "request_id": uuid4().hex})
        self.assertEqual(self.service.submit(self.aid, self.wid, body)["task_id"], queued["task_id"])
        finished = self.service.run(self.aid, self.wid, queued["task_id"])
        self.assertEqual(finished["status"], "completed", finished)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0]["prompt"], saved["generation_prompt"])
        self.assertNotIn("更新后的剧情创作规则", self.adapter.calls[0]["prompt"])

    def test_missing_skill_reference_fails_before_reserving_or_invoking(self):
        skill_root = self.root / "skill"
        shutil.copytree(SKILL_ROOT, skill_root)
        self.service.templates = ScriptTemplateLoader(skill_root)
        body, _ = self.quoted()
        (skill_root / "references/screenplay-craft.md").unlink()
        wallet = self.identity.wallet(self.aid)
        with self.assertRaisesRegex(ValueError, "参考文件不可读取"):
            self.service.submit(self.aid, self.wid, body)
        self.assertEqual(self.identity.wallet(self.aid), wallet)
        self.assertFalse(self.adapter.calls)

    def ledger(self, job):
        return [row for row in self.identity.ledger(self.aid) if row["job_id"] == job["job_id"]]

    def assert_ledger(self, job, expected):
        self.assertCountEqual([(row["type"], row["amount"]) for row in self.ledger(job)], expected)

    def change_price(self, price):
        path = self.config_root / "script_pricing.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["models"]["gpt-6-astra"]["high"] = price
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_paid_generation_registers_utf8_artifact_version_and_exact_model(self):
        queued = self.submit()
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 160)
        self.assertIsNotNone(queued["credit_transaction_id"])
        completed = self.service.run(self.aid, self.wid, queued["task_id"])
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(completed["output_version"], 1)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.adapter.calls[0]["model_id"], "gpt-6-astra")
        self.assertEqual(self.adapter.calls[0]["thinking_level"], "high")
        self.assertIn("user_robot_boyfriend_ep1", self.adapter.calls[0]["prompt"])
        self.assertNotIn("镜组31", self.adapter.calls[0]["prompt"])
        artifact = self.storage.get_artifact(completed["output_artifact_id"])
        data, filename = self.service.download(self.aid, self.wid, completed["task_id"])
        self.assertEqual(artifact.format, "TXT")
        self.assertEqual(artifact.task_id, completed["task_id"])
        self.assertEqual(data.decode("utf-8"), TEXT)
        self.assertTrue(filename.endswith("_v1.txt"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), artifact.sha256)
        detail = self.service.detail(self.aid, self.wid, completed["task_id"])
        self.assertEqual(detail["text"], TEXT)
        self.assertEqual(detail["versions"][0]["artifact_id"], artifact.artifact_id)
        self.assertNotIn("generation_prompt", detail)
        self.assertNotIn("prompt", detail["versions"][0])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assert_ledger(completed, [("freeze", 40), ("consume", 40)])

    def test_free_luna_generation_still_has_task_reservation_and_zero_ledger(self):
        task = self.complete(self.body(model_id="gpt-6-luna", thinking_level="high"))
        self.assertEqual(task["price"], 0)
        self.assertIsNotNone(task["credit_transaction_id"])
        self.assertEqual(task["credit_status"], "completed")
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)
        self.assert_ledger(task, [("freeze", 0), ("consume", 0)])

    def test_insufficient_credit_never_calls_or_reserves_model(self):
        with self.identity.transaction() as db:
            db.execute("UPDATE credit_accounts SET total_credit=9 WHERE account_id=?", (self.aid,))
        body, quote = self.quoted()
        self.assertEqual((quote["remaining_credit"], quote["shortfall"], quote["can_generate"]), (9, 31, False))
        with self.assertRaises(CreditInsufficient) as caught:
            self.service.submit(self.aid, self.wid, body)
        self.assertEqual((caught.exception.expected, caught.exception.remaining), (40, 9))
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.service.list_tasks(self.aid, self.wid), [])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.identity.pending_jobs(), [])

    def test_model_failure_refunds_exactly_once_and_never_auto_retries(self):
        body, _ = self.quoted()
        task = self.service.submit(self.aid, self.wid, body)
        self.adapter.error = ScriptGenerationError("测试：Codex 不可用")
        failed = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual((failed["status"], failed["credit_status"]), ("failed", "refunded"))
        self.service.run(self.aid, self.wid, task["task_id"])
        self.service.submit(self.aid, self.wid, body)
        self.identity.record_job(self.service._load(task["task_id"]))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)
        self.assert_ledger(failed, [("freeze", 40), ("refund", 40)])

    def test_invalid_generated_json_is_failure_and_refunded(self):
        self.adapter.text = '{"text": "不是纯文本成品"}'
        task = self.submit()
        failed = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(failed["status"], "failed")
        self.assert_ledger(failed, [("freeze", 40), ("refund", 40)])
        with self.assertRaises(ConflictError):
            self.service.download(self.aid, self.wid, task["task_id"])

    def test_account_and_workspace_isolation_all_read_write_entrypoints(self):
        task = self.complete()
        for aid, wid in ((self.bid, self.bwid), (self.aid, self.awid2), (self.bid, self.wid)):
            with self.subTest(account=aid, workspace=wid):
                for operation in (
                    lambda: self.service.detail(aid, wid, task["task_id"]),
                    lambda: self.service.download(aid, wid, task["task_id"]),
                    lambda: self.service.run(aid, wid, task["task_id"]),
                    lambda: self.service.recover(aid, wid, task["task_id"]),
                    lambda: self.service.storyboard_source(aid, wid, task["version_id"]),
                    lambda: self.service.quote(aid, wid, self.body()),
                    lambda: self.service.save_revision(aid, wid, task["task_id"], {"request_id": uuid4().hex, "text": TEXT, "change_reason": "修改"}),
                ):
                    with self.assertRaises(FileNotFoundError):
                        operation()
        self.assertEqual(self.service.list_tasks(self.bid, self.bwid), [])
        self.assertEqual(self.service.list_tasks(self.aid, self.awid2), [])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.identity.wallet(self.bid)["remaining_credit"], 200)

    def test_manual_low_choice_warns_but_never_replaced(self):
        body, quote = self.quoted(self.body(input_request="最高质量的多线悬疑剧本", model_id="gpt-6-luna", thinking_level="low"))
        self.assertTrue(quote["warnings"])
        self.assertEqual((quote["model_id"], quote["thinking_level"]), ("gpt-6-luna", "low"))
        with self.assertRaises(ConflictError):
            self.service.submit(self.aid, self.wid, body)
        body["warnings_confirmed"] = True
        task = self.service.submit(self.aid, self.wid, body)
        self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual((self.adapter.calls[0]["model_id"], self.adapter.calls[0]["thinking_level"]), ("gpt-6-luna", "low"))

    def test_manual_revision_is_free_immutable_and_idempotent(self):
        parent = self.complete()
        old_data, _ = self.service.download(self.aid, self.wid, parent["task_id"])
        edited = TEXT.replace("回家吧", "灯会一直亮着")
        body = {"request_id": uuid4().hex, "text": edited, "change_reason": "调整最后一句对白"}
        revision = self.service.save_revision(self.aid, self.wid, parent["task_id"], body)
        repeated = self.service.save_revision(self.aid, self.wid, parent["task_id"], body)
        self.assertEqual(revision["task_id"], repeated["task_id"])
        self.assertEqual(revision["output_version"], 2)
        self.assertEqual(revision["parent_version_id"], parent["version_id"])
        self.assertEqual(revision["document_id"], parent["document_id"])
        self.assertNotEqual(revision["output_artifact_id"], parent["output_artifact_id"])
        self.assertEqual((revision["source"], revision["price"], revision["job_id"]), ("manual_edit", 0, None))
        self.assertEqual(self.service.download(self.aid, self.wid, parent["task_id"])[0], old_data)
        self.assertEqual(self.service.download(self.aid, self.wid, revision["task_id"])[0].decode("utf-8"), edited)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 40)
        self.assertEqual(len(self.service.detail(self.aid, self.wid, parent["task_id"])["versions"]), 2)
        with self.assertRaises(ConflictError):
            self.service.save_revision(self.aid, self.wid, parent["task_id"], {**body, "text": TEXT})

    def test_generated_revision_uses_parent_text_and_registers_parent_relation(self):
        parent = self.complete()
        self.adapter.text = TEXT.replace("回家吧", "我会为你守灯")
        revision = self.complete(self.body(parent_version_id=parent["version_id"], change_reason="修改结尾", model_id="gpt-6.1-sol", thinking_level="medium"))
        self.assertEqual(revision["output_version"], 2)
        self.assertEqual(revision["document_id"], parent["document_id"])
        self.assertEqual(revision["parent_version_id"], parent["version_id"])
        self.assertIn("parent_version_reference_data", self.adapter.calls[-1]["prompt"])
        self.assertIn("回家吧", self.adapter.calls[-1]["prompt"])
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 55)
        source = self.service.storyboard_source(self.aid, self.wid, parent["version_id"])
        self.assertEqual((source["version"], source["text"]), (1, TEXT))
        self.assertEqual(self.service.download(self.aid, self.wid, parent["task_id"])[0].decode("utf-8"), TEXT)

    def test_duplicate_request_id_is_one_task_one_generation_and_changed_input_rejected(self):
        body, _ = self.quoted()
        task = self.service.submit(self.aid, self.wid, body)
        self.assertEqual(self.service.submit(self.aid, self.wid, body)["task_id"], task["task_id"])
        self.service.run(self.aid, self.wid, task["task_id"])
        self.service.submit(self.aid, self.wid, body)
        self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(len(self.service.list_tasks(self.aid, self.wid)), 1)
        self.assert_ledger(task, [("freeze", 40), ("consume", 40)])
        with self.assertRaises(ConflictError):
            self.service.submit(self.aid, self.wid, {**body, "input_request": "改成另一个故事"})

    def test_concurrent_run_holds_single_model_execution(self):
        task = self.submit()
        self.adapter.entered, self.adapter.release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(self.service.run, self.aid, self.wid, task["task_id"])
            try:
                self.assertTrue(self.adapter.entered.wait(5))
                other = executor.submit(self.service.run, self.aid, self.wid, task["task_id"])
                observed = other.result(timeout=3)
                self.assertIn(observed["status"], ("running", "completed"))
            finally:
                self.adapter.release.set()
            self.assertEqual(first.result(timeout=5)["status"], "completed")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assert_ledger(task, [("freeze", 40), ("consume", 40)])

    def test_archive_failure_recovers_same_candidate_without_model_replay(self):
        task = self.submit()
        with patch.object(self.storage, "register_output_text", side_effect=OSError("测试归档失败")):
            failed = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(failed["status"], "uncertain")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        self.assertTrue(self.service.detail(self.aid, self.wid, task["task_id"])["can_recover"])
        self.service.run(self.aid, self.wid, task["task_id"])
        recovered = self.service.recover(self.aid, self.wid, task["task_id"])
        self.service.recover(self.aid, self.wid, task["task_id"])
        self.assertEqual((recovered["status"], recovered["output_version"]), ("completed", 1))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assert_ledger(task, [("freeze", 40), ("consume", 40)])
        self.assertEqual(self.service.download(self.aid, self.wid, task["task_id"])[0].decode("utf-8"), TEXT)

    def test_quote_changed_price_or_request_cannot_submit(self):
        body, _ = self.quoted()
        self.change_price(41)
        with self.assertRaises(ConflictError):
            self.service.submit(self.aid, self.wid, body)
        body, _ = self.quoted()
        with self.assertRaises(ConflictError):
            self.service.submit(self.aid, self.wid, {**body, "tone": "轻喜剧"})
        self.assertEqual(self.identity.pending_jobs(), [])
        self.assertEqual(self.adapter.calls, [])

    def test_version_write_failure_reuses_registered_artifact_on_recovery(self):
        task = self.submit()
        write_json = self.storage._write_json
        def fail_version(path, value):
            if Path(path).name == "script-versions.json":
                raise OSError("测试版本登记失败")
            return write_json(path, value)
        with patch.object(self.storage, "_write_json", side_effect=fail_version):
            result = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(result["status"], "uncertain")
        artifact = self.storage.get_artifact(result["output_artifact_id"])
        with patch.object(self.storage, "register_output_text", wraps=self.storage.register_output_text) as register:
            recovered = self.service.recover(self.aid, self.wid, task["task_id"])
        register.assert_not_called()
        self.assertEqual(recovered["output_artifact_id"], artifact.artifact_id)
        self.assertEqual(recovered["output_version"], 1)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assert_ledger(task, [("freeze", 40), ("consume", 40)])

    def test_truncated_candidate_cannot_recover_charge_or_replay_model(self):
        task = self.submit()
        with patch.object(self.storage, "register_output_text", side_effect=OSError("测试归档失败")):
            result = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(result["status"], "uncertain")
        candidate = self.service._candidate(self.service._load(task["task_id"]))
        candidate.write_text("《海边的灯》\n", encoding="utf-8", newline="")
        with self.assertRaises((ValueError, ConflictError)):
            self.service.recover(self.aid, self.wid, task["task_id"])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 0)
        self.assert_ledger(task, [("freeze", 40)])

    def test_submission_outbox_recovery_keeps_credit_transaction_trace(self):
        body, _ = self.quoted()
        with patch.object(self.service, "_write", side_effect=OSError("测试任务发布失败")):
            with self.assertRaises(OSError):
                self.service.submit(self.aid, self.wid, body)
        self.assertEqual(len(self.identity.pending_jobs()), 1)
        recovered = self.service.submit(self.aid, self.wid, body)
        self.assertIsNotNone(recovered.get("credit_transaction_id"), "冻结后发布失败恢复时仍需保留积分流水关联")
        completed = self.service.run(self.aid, self.wid, recovered["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(self.adapter.calls), 1)
        self.assert_ledger(recovered, [("freeze", 40), ("consume", 40)])

    def test_unavailable_manual_model_after_quote_refunds_without_substitution(self):
        task = self.submit()
        self.registry.catalog = {"models": [{"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "high"}]}]}
        result = self.service.run(self.aid, self.wid, task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.adapter.calls, [])
        self.assert_ledger(task, [("freeze", 40), ("refund", 40)])

    def test_recommended_route_uses_python_and_free_luna_for_simple_script(self):
        task = self.complete(self.body(generation_type="script", template_id="basic_video_script", input_request="单场景产品介绍短视频", selection_mode="recommended", model_id="gpt-6-astra", thinking_level="max"))
        self.assertEqual((task["model_id"], task["thinking_level"], task["price"]), ("gpt-6-luna", "high", 0))
        self.assertEqual(task["demand_spec"]["source"], "python")
        self.assertEqual(task["example_references"], [])

    def test_cross_kind_request_rejected_without_model_or_new_reservation(self):
        rid = uuid4().hex
        image_task = self.projects.create_task(self.sid, "generate", "图片测试")
        old_job = {"kind": "image", "job_id": uuid4().hex, "task_id": image_task.task_id, "project_id": image_task.project_id,
                   "request_id": rid, "status": "queued"}
        self.identity.reserve(old_job, {"account_id": self.aid, "workspace_id": self.wid}, 40)
        body, _ = self.quoted(self.body(request_id=rid))
        with self.assertRaisesRegex(ValueError, "另一项任务"):
            self.service.submit(self.aid, self.wid, body)
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(len(self.identity.pending_jobs()), 1)
        self.assertEqual(self.service.list_tasks(self.aid, self.wid), [])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)
        self.assertEqual(len(self.storage.list_tasks(image_task.project_id)), 1, "冲突拒绝不应创建孤立底层 Task")


if __name__ == "__main__":
    unittest.main()
