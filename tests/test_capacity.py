from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import IdentityStore
from image_workbench.capacity import CapacityExceeded, CapacityPolicy


class CapacityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        storage = StorageManager(StorageConfig(data_root=Path(temp.name) / "data"))
        self.identity = IdentityStore(storage)
        self.owners = []
        for username in ("alice", "bobby"):
            account = self.identity.register(username, "test-password-123")
            wid = self.identity.workspaces(account["account_id"])[0]["workspace_id"]
            self.owners.append({"account_id": account["account_id"], "workspace_id": wid})

    @staticmethod
    def job():
        return {"job_id": uuid4().hex, "project_id": uuid4().hex, "task_id": uuid4().hex, "request_id": uuid4().hex, "status": "queued"}

    def test_account_limit_includes_uncertain_until_explicit_refund(self):
        self.identity.capacity_policy = CapacityPolicy(max_pending_per_account=1)
        owner = self.owners[0]
        job = self.identity.reserve(self.job(), owner, 40)
        self.identity.record_job({**job, "status": "uncertain"})
        with self.assertRaises(CapacityExceeded):
            self.identity.reserve(self.job(), owner, 40)
        self.assertEqual(self.identity.wallet(owner["account_id"])["frozen_credit"], 40)
        self.identity.record_job({**job, "status": "failed"})
        self.identity.reserve(self.job(), owner, 40)
        self.assertEqual(self.identity.wallet(owner["account_id"])["frozen_credit"], 40)

    def test_global_last_slot_is_atomic_across_accounts_and_replay_still_succeeds(self):
        self.identity.capacity_policy = CapacityPolicy(max_pending_total=1)
        barrier = threading.Barrier(2)
        def reserve(owner):
            barrier.wait(timeout=5)
            try:
                return self.identity.reserve(self.job(), owner, 40)
            except CapacityExceeded:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, self.owners))
        accepted = [item for item in results if item]
        self.assertEqual(len(accepted), 1)
        job = accepted[0]
        owner = next(item for item in self.owners if item["account_id"] == job["account_id"])
        self.assertEqual(self.identity.reserve(job, owner, 40)["job_id"], job["job_id"])
        self.assertEqual(sum(self.identity.wallet(item["account_id"])["frozen_credit"] for item in self.owners), 40)
        with self.identity.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_transactions WHERE type='freeze'").fetchone()[0], 1)

    def test_daily_budget_counts_refunds_and_admin_and_resets_on_next_utc_day(self):
        self.identity.capacity_policy = CapacityPolicy(max_daily_submitted_credit=40)
        owner = self.owners[0]
        first = self.identity.reserve(self.job(), owner, 40)
        self.identity.record_job({**first, "status": "failed"})
        self.assertEqual(self.identity.wallet(owner["account_id"])["remaining_credit"], 200)
        admin = self.identity.bootstrap([])
        with self.assertRaises(CapacityExceeded):
            self.identity.reserve(self.job(), admin, 40)
        with self.identity.transaction() as db:
            db.execute("UPDATE reservations SET created_at='2020-01-01T00:00:00+00:00'")
        self.identity.reserve(self.job(), admin, 40)
        self.assertEqual(self.identity.wallet(admin["account_id"])["frozen_credit"], 40)

    def test_disabled_limits_preserve_behavior_and_invalid_configuration_fails(self):
        for _ in range(5):
            self.identity.reserve(self.job(), self.owners[0], 40)
        self.assertEqual(self.identity.wallet(self.owners[0]["account_id"])["frozen_credit"], 200)
        for value in ("bad", "-1"):
            with patch.dict("os.environ", {"IMAGE_WORKBENCH_MAX_PENDING_TOTAL": value}):
                with self.assertRaises(ValueError):
                    CapacityPolicy.from_environment()
