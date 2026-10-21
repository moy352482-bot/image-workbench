"""Persistent per-account safeguards for free and paid script submissions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import IdentityStore
from image_workbench.capacity import CapacityExceeded, CapacityPolicy


class ScriptLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = StorageManager(StorageConfig(data_root=Path(temporary.name) / "data"))
        self.identity = IdentityStore(self.storage, capacity_policy=CapacityPolicy())
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "script_alice", None)
            self.bid, self.bwid = self.identity._account(db, "script_bobby", None)
        self.other_wid = self.identity.create_workspace(self.aid, "同账号的另一空间")["workspace_id"]
        self.owner = {"account_id": self.aid, "workspace_id": self.wid}
        self.other_space = {"account_id": self.aid, "workspace_id": self.other_wid}
        self.other_account = {"account_id": self.bid, "workspace_id": self.bwid}

    @staticmethod
    def job(kind="script"):
        return {
            "kind": kind, "job_id": uuid4().hex, "project_id": uuid4().hex,
            "task_id": uuid4().hex, "request_id": uuid4().hex, "status": "queued",
        }

    def reserve(self, *, owner=None, amount=0, kind="script"):
        return self.identity.reserve(self.job(kind), owner or self.owner, amount)

    def finish(self, job, status="completed"):
        self.identity.record_job({**job, "status": status})

    def counts(self):
        with self.identity.connect() as db:
            return (
                db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM credit_transactions").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM credit_rejections").fetchone()[0],
            )

    def policy(self, **changes):
        self.identity.capacity_policy = replace(CapacityPolicy(), **changes)

    def test_default_free_script_window_counts_completed_and_refunded_submissions(self):
        self.assertEqual(
            (self.identity.capacity_policy.script_window_seconds,
             self.identity.capacity_policy.max_script_submissions_per_window,
             self.identity.capacity_policy.max_pending_scripts_per_account),
            (60, 6, 3),
        )
        for index in range(6):
            self.finish(self.reserve(), "completed" if index % 2 else "failed")
        before = self.counts()
        with self.assertRaises(CapacityExceeded):
            self.identity.preflight_script_capacity(self.aid)
        for _ in range(2):
            with self.assertRaises(CapacityExceeded):
                self.reserve()
        self.assertEqual(self.counts(), before)
        wallet = self.identity.wallet(self.aid)
        self.assertEqual((wallet["remaining_credit"], wallet["used_credit"], wallet["frozen_credit"]), (200, 0, 0))
        entries = [item for item in self.identity.ledger(self.aid) if item["job_id"]]
        self.assertEqual(len(entries), 12)
        self.assertTrue(all(item["amount"] == 0 for item in entries))

    def test_pending_limit_includes_running_uncertain_and_old_jobs_until_settled(self):
        jobs = [self.reserve() for _ in range(3)]
        self.finish(jobs[0], "running")
        self.finish(jobs[1], "uncertain")
        with self.identity.transaction() as db:
            db.execute("UPDATE reservations SET created_at='2020-01-01T00:00:00+00:00'")
        before = self.counts()
        with self.assertRaises(CapacityExceeded):
            self.identity.preflight_script_capacity(self.aid)
        with self.assertRaises(CapacityExceeded):
            self.reserve()
        self.assertEqual(self.counts(), before)
        self.finish(jobs[0], "failed")
        accepted = self.reserve()
        self.assertEqual(accepted["credit_cost"], 0)
        self.assertEqual(accepted["credit_status"], "credit_frozen")

    def test_rate_window_expires_but_failures_do_not_reset_it_early(self):
        self.policy(max_script_submissions_per_window=1)
        job = self.reserve()
        self.finish(job, "failed")
        with self.assertRaises(CapacityExceeded):
            self.reserve()
        old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        with self.identity.transaction() as db:
            db.execute("UPDATE reservations SET created_at=? WHERE job_id=?", (old, job["job_id"]))
        self.identity.preflight_script_capacity(self.aid)
        self.assertEqual(self.reserve()["credit_cost"], 0)

    def test_same_account_shares_window_across_workspaces_and_other_account_is_independent(self):
        self.policy(max_script_submissions_per_window=2)
        self.finish(self.reserve())
        self.finish(self.reserve(owner=self.other_space), "failed")
        for owner in (self.owner, self.other_space):
            with self.assertRaises(CapacityExceeded):
                self.identity.reserve(self.job(), owner, 0)
        self.assertEqual(self.reserve(owner=self.other_account)["account_id"], self.bid)

    def test_same_account_shares_pending_limit_across_workspaces(self):
        self.policy(max_pending_scripts_per_account=1)
        self.reserve(owner=self.other_space)
        with self.assertRaises(CapacityExceeded):
            self.reserve(owner=self.owner)
        self.assertEqual(self.reserve(owner=self.other_account)["account_id"], self.bid)

    def test_idempotent_retries_do_not_consume_window_or_pending_slots(self):
        self.policy(max_script_submissions_per_window=1, max_pending_scripts_per_account=1)
        original = self.reserve()
        for _ in range(3):
            replay = self.identity.reserve(dict(original), self.owner, 0)
            self.assertEqual(replay["job_id"], original["job_id"])
        alternative_request = {**original, "job_id": uuid4().hex, "request_id": uuid4().hex}
        self.assertEqual(self.identity.reserve(alternative_request, self.owner, 0)["job_id"], original["job_id"])
        self.finish(original, "failed")
        replay = self.identity.reserve(dict(original), self.owner, 0)
        self.assertEqual(replay["status"], "failed")
        with self.assertRaises(CapacityExceeded):
            self.reserve()
        self.assertEqual(self.counts()[0], 1)
        freezes = [item for item in self.identity.ledger(self.aid) if item["type"] == "freeze"]
        self.assertEqual(len(freezes), 1)

    def test_concurrent_free_submissions_cannot_exceed_last_pending_slot(self):
        self.policy(max_pending_scripts_per_account=1, max_script_submissions_per_window=20)
        barrier = threading.Barrier(4)
        identities = [
            IdentityStore(self.storage, capacity_policy=self.identity.capacity_policy) for _ in range(4)
        ]

        def reserve(identity):
            barrier.wait(timeout=10)
            try:
                return identity.reserve(self.job(), self.owner, 0)
            except CapacityExceeded:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, identities))
        self.assertEqual(len([result for result in results if result]), 1)
        self.assertEqual(self.counts()[0], 1)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_concurrent_free_submissions_cannot_exceed_last_window_slot(self):
        self.policy(max_pending_scripts_per_account=20, max_script_submissions_per_window=2)
        self.finish(self.reserve(), "failed")
        barrier = threading.Barrier(4)

        def reserve(_):
            barrier.wait(timeout=10)
            try:
                return self.reserve()
            except CapacityExceeded:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, range(4)))
        self.assertEqual(len([result for result in results if result]), 1)
        self.assertEqual(self.counts()[0], 2)

    def test_concurrent_identical_requests_reserve_once_even_with_one_slot(self):
        self.policy(max_pending_scripts_per_account=1, max_script_submissions_per_window=1)
        job = self.job()
        barrier = threading.Barrier(4)

        def reserve(_):
            barrier.wait(timeout=10)
            return self.identity.reserve(dict(job), self.owner, 0)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, range(4)))
        self.assertEqual({result["job_id"] for result in results}, {job["job_id"]})
        self.assertEqual(self.counts()[0], 1)
        self.assertEqual(len([item for item in self.identity.ledger(self.aid) if item["type"] == "freeze"]), 1)

    def test_preflight_is_read_only_and_reserve_rechecks_after_a_successful_preflight(self):
        self.policy(max_pending_scripts_per_account=1)
        before = self.counts()
        for _ in range(3):
            self.identity.preflight_script_capacity(self.aid)
        self.assertEqual(self.counts(), before)
        self.reserve(owner=self.other_space)
        exhausted = self.counts()
        with self.assertRaises(CapacityExceeded):
            self.identity.preflight_script_capacity(self.aid)
        with self.assertRaises(CapacityExceeded):
            self.reserve()
        self.assertEqual(self.counts(), exhausted)

    def test_paid_script_limits_reject_without_freezing_and_ignore_image_video_jobs(self):
        self.policy(max_pending_scripts_per_account=1)
        for kind in ("image", "video"):
            for _ in range(4):
                self.reserve(kind=kind)
        self.reserve(amount=40)
        before = self.counts()
        with self.assertRaises(CapacityExceeded):
            self.reserve(amount=40)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 40)

    def test_limits_remain_enforced_after_identity_store_restart(self):
        self.policy(max_pending_scripts_per_account=1)
        self.reserve()
        reopened = IdentityStore(self.storage, capacity_policy=self.identity.capacity_policy)
        with self.assertRaises(CapacityExceeded):
            reopened.preflight_script_capacity(self.aid)
        with self.assertRaises(CapacityExceeded):
            reopened.reserve(self.job(), self.owner, 0)
        self.assertEqual(self.counts()[0], 1)

    def test_script_configuration_is_positive_and_cannot_disable_free_protection(self):
        fields = {
            "script_window_seconds": "IMAGE_WORKBENCH_SCRIPT_WINDOW_SECONDS",
            "max_script_submissions_per_window": "IMAGE_WORKBENCH_MAX_SCRIPT_SUBMISSIONS_PER_WINDOW",
            "max_pending_scripts_per_account": "IMAGE_WORKBENCH_MAX_PENDING_SCRIPTS_PER_ACCOUNT",
        }
        for field, variable in fields.items():
            for value in (0, -1, True, 1.5, "1"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        replace(CapacityPolicy(), **{field: value})
            for value in ("0", "-1", "bad", "1.5", ""):
                with self.subTest(variable=variable, value=value):
                    with patch.dict("os.environ", {variable: value}):
                        with self.assertRaises(ValueError):
                            CapacityPolicy.from_environment()
            with patch.dict("os.environ", {variable: "11"}):
                self.assertEqual(getattr(CapacityPolicy.from_environment(), field), 11)
        self.assertEqual(CapacityPolicy(max_pending_total=0).max_pending_total, 0)


if __name__ == "__main__":
    unittest.main()
