from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import LocalFileBackend, StorageConfig, StorageManager
from image_workbench.accounts import IdentityStore
from image_workbench.capacity import CapacityPolicy
from image_workbench.registration import RegistrationPolicy
from image_workbench.remote_access import RemoteAccess
from image_workbench.remote_queue import Complete
from image_workbench.remote_worker import RemoteWorker
from image_workbench.scheduling import SchedulingPolicy
from image_workbench.web import create_workspace_app


TOTAL_ENV = "IMAGE_WORKBENCH_MAX_ACTIVE_TOTAL"
ACCOUNT_ENV = "IMAGE_WORKBENCH_MAX_ACTIVE_PER_ACCOUNT"
_MISSING = object()


class SchedulingPolicyTests(unittest.TestCase):
    @staticmethod
    def job(status="queued", account_id=_MISSING):
        job = {"job_id": uuid4().hex, "status": status}
        if account_id is not _MISSING:
            job["account_id"] = account_id
        return job

    def test_defaults_admit_one_job_and_active_job_blocks_next_account(self):
        policy = SchedulingPolicy()
        first, second = self.job(account_id="alice"), self.job(account_id="bobby")
        self.assertEqual(policy.max_active_total, 1)
        self.assertEqual(policy.max_active_per_account, 1)
        self.assertEqual(policy.choose([first, second]), first)
        first["status"] = "running"
        self.assertIsNone(policy.choose([first, second]))

    def test_account_at_limit_does_not_block_first_eligible_other_account(self):
        policy = SchedulingPolicy(max_active_total=3, max_active_per_account=1)
        active = self.job("running", "alice")
        blocked = self.job(account_id="alice")
        first_other = self.job(account_id="bobby")
        later_other = self.job(account_id="charlie")
        jobs = [active, blocked, first_other, later_other]
        before = deepcopy(jobs)
        self.assertEqual(policy.choose(jobs), first_other)
        self.assertEqual(jobs, before)

    def test_uncertain_counts_toward_both_limits_but_terminal_jobs_do_not(self):
        policy = SchedulingPolicy(max_active_total=2, max_active_per_account=1)
        uncertain = self.job("uncertain", "alice")
        queued_a, queued_b = self.job(account_id="alice"), self.job(account_id="bobby")
        terminal = [self.job("completed", "bobby"), self.job("failed", "bobby")]
        self.assertEqual(policy.choose([uncertain, *terminal, queued_a, queued_b]), queued_b)
        other_active = self.job("running", "charlie")
        self.assertIsNone(policy.choose([uncertain, other_active, queued_b]))

    def test_missing_account_ids_share_one_legacy_account_bucket(self):
        policy = SchedulingPolicy(max_active_total=3, max_active_per_account=1)
        active, blocked = self.job("running"), self.job()
        named = self.job(account_id="alice")
        self.assertIsNone(policy.choose([active, blocked]))
        self.assertEqual(policy.choose([active, blocked, named]), named)
        relaxed = SchedulingPolicy(max_active_total=3, max_active_per_account=2)
        self.assertEqual(relaxed.choose([active, blocked]), blocked)

    def test_local_effective_limit_remains_one_when_remote_limit_is_higher(self):
        policy = SchedulingPolicy(max_active_total=4, max_active_per_account=3)
        first, next_job = self.job("running", "alice"), self.job(account_id="bobby")
        self.assertEqual(policy.choose([first, next_job], execution_mode="remote"), next_job)
        self.assertIsNone(policy.choose([first, next_job], execution_mode="local"))
        snapshot = policy.snapshot([first, next_job], execution_mode="local")
        self.assertEqual(snapshot["configured_max_active_total"], 4)
        self.assertEqual(snapshot["max_active_total"], 1)
        self.assertEqual(snapshot["max_active_per_account"], 3)
        self.assertEqual(snapshot["available_slots"], 0)
        self.assertEqual(snapshot["eligible_queued"], 0)

    def test_snapshot_counts_active_and_queued_without_counting_terminal_jobs(self):
        policy = SchedulingPolicy(max_active_total=3, max_active_per_account=1)
        jobs = [self.job("running", "alice"), self.job("uncertain", "bobby"),
                self.job(account_id="alice"), self.job(account_id="charlie"),
                self.job("completed", "david"), self.job("failed", "erica")]
        snapshot = policy.snapshot(jobs)
        expected = {"configured_max_active_total": 3, "max_active_total": 3,
                    "max_active_per_account": 1, "queued": 2, "running": 1,
                    "uncertain": 1, "active": 2, "available_slots": 1, "eligible_queued": 1}
        for key, value in expected.items():
            self.assertEqual(snapshot[key], value, key)

    def test_lowered_limit_never_produces_negative_available_slots(self):
        policy = SchedulingPolicy()
        jobs = [self.job("running", "alice"), self.job("uncertain", "bobby"),
                self.job(account_id="charlie")]
        self.assertIsNone(policy.choose(jobs))
        snapshot = policy.snapshot(jobs)
        self.assertEqual(snapshot["active"], 2)
        self.assertEqual(snapshot["available_slots"], 0)
        self.assertEqual(snapshot["eligible_queued"], 0)

    def test_empty_queue_snapshot_and_choose(self):
        policy = SchedulingPolicy()
        self.assertIsNone(policy.choose([]))
        snapshot = policy.snapshot([])
        for key in ("queued", "running", "uncertain", "active", "eligible_queued"):
            self.assertEqual(snapshot[key], 0, key)
        self.assertEqual(snapshot["available_slots"], 1)

    def test_constructor_accepts_integer_bounds_and_rejects_invalid_values(self):
        for value in (1, 32):
            policy = SchedulingPolicy(max_active_total=value, max_active_per_account=value)
            self.assertEqual(policy.max_active_total, value)
            self.assertEqual(policy.max_active_per_account, value)
        for field in ("max_active_total", "max_active_per_account"):
            for value in (0, -1, 33, True, False, 1.0, "2", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        SchedulingPolicy(**{field: value})

    def test_environment_defaults_values_and_invalid_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(SchedulingPolicy.from_environment(), SchedulingPolicy())
        with patch.dict(os.environ, {TOTAL_ENV: "32", ACCOUNT_ENV: "2"}, clear=True):
            policy = SchedulingPolicy.from_environment()
            self.assertEqual((policy.max_active_total, policy.max_active_per_account), (32, 2))
        for name in (TOTAL_ENV, ACCOUNT_ENV):
            for value in ("bad", "", "-1", "0", "33", "1.5", "true"):
                with self.subTest(name=name, value=value):
                    with patch.dict(os.environ, {name: value}, clear=True):
                        with self.assertRaises(ValueError):
                            SchedulingPolicy.from_environment()


class RemoteSchedulingTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {TOTAL_ENV: "1", ACCOUNT_ENV: "1"})
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.storage = StorageManager(StorageConfig(data_root=self.root / "server", max_image_bytes=2048))
        self.identity = IdentityStore(self.storage, registration_policy=RegistrationPolicy(),
                                      capacity_policy=CapacityPolicy())
        self.access = RemoteAccess("https://images.example", "worker-" + "s" * 40,
                                   "owner", "browser-" + "t" * 32)
        self.app = create_workspace_app(self.storage, backend="local", remote_access=self.access)
        self.state, self.queue = self.app.state.workbench, self.app.state.remote_queue
        self.state.identity = self.identity
        self.client = TestClient(self.app, base_url=self.access.origin, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.worker_headers = {"Authorization": self.access.worker_authorization.decode()}
        self.accounts, self.projects = {}, {}
        for name in ("alice", "bobby"):
            account = self.identity.register(name, "test-password-123")
            self.accounts[name] = account["account_id"]
            workspace = self.identity.workspaces(account["account_id"])[0]
            project = self.state.create_project(name + " scheduling", uuid4().hex)
            self.identity.assign_project(project.project_id, workspace["workspace_id"])
            self.projects[name] = project.project_id
        self.image = self.root / "fixture.png"
        Image.new("RGB", (24, 16), "coral").save(self.image)

    def submit(self, account="alice"):
        task = self.state.create_task(self.projects[account], request_id=uuid4().hex,
                                     operation="generate", prompt="本地调度测试图片")
        return self.state.submit(task.project_id, task.task_id, request_id=uuid4().hex, confirmed=True)

    def claim_http(self, worker_id):
        response = self.client.post("/api/worker/claim", headers=self.worker_headers,
                                    json={"worker_id": worker_id, "backend": "local"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["job"]

    def test_default_queue_keeps_one_global_active_job(self):
        first, second = self.submit("alice"), self.submit("bobby")
        self.assertEqual(self.state.scheduling_policy.max_active_total, 1)
        self.assertEqual(self.queue.claim(uuid4().hex, "local")["job_id"], first["job_id"])
        self.assertIsNone(self.queue.claim(uuid4().hex, "local"))
        self.assertEqual(self.state.get_job(second["project_id"], second["task_id"])["status"], "queued")

    def test_simultaneous_claims_enforce_global_and_account_limits(self):
        self.state.scheduling_policy = SchedulingPolicy(max_active_total=3, max_active_per_account=2)
        for account in ("alice", "alice", "alice", "bobby", "bobby", "bobby"):
            self.submit(account)
        barrier = threading.Barrier(8)

        def claim(worker_id):
            barrier.wait(timeout=10)
            return self.queue.claim(worker_id, "local")

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(claim, [uuid4().hex for _ in range(8)]))
        accepted = [response for response in responses if response]
        self.assertEqual(len(accepted), 3)
        self.assertEqual(len({response["job_id"] for response in accepted}), 3)
        active = [job for job in self.state.list_jobs() if job["status"] == "running"]
        self.assertEqual(len(active), 3)
        counts = Counter(job["account_id"] for job in active)
        self.assertEqual(len(counts), 2)
        self.assertTrue(all(count <= 2 for count in counts.values()))
        self.assertEqual(sum(self.identity.wallet(account)["frozen_credit"]
                             for account in self.accounts.values()), 240)

    def test_busy_account_does_not_starve_next_account(self):
        self.state.scheduling_policy = SchedulingPolicy(max_active_total=2, max_active_per_account=1)
        first, blocked, other = self.submit(), self.submit(), self.submit("bobby")
        self.assertEqual(self.queue.claim(uuid4().hex, "local")["job_id"], first["job_id"])
        self.assertEqual(self.queue.claim(uuid4().hex, "local")["job_id"], other["job_id"])
        self.assertEqual(self.state.get_job(blocked["project_id"], blocked["task_id"])["status"], "queued")

    def test_uncertain_retains_slot_and_credit_until_explicit_failed_result(self):
        self.state.scheduling_policy = SchedulingPolicy(max_active_total=2, max_active_per_account=1)
        first, blocked, other = self.submit(), self.submit(), self.submit("bobby")
        owner = uuid4().hex
        claim = self.queue.claim(owner, "local")
        uncertain = Complete(status="uncertain", failure_type="interrupted")
        self.assertEqual(self.queue.complete(first["job_id"], owner, claim["claim_id"], uncertain)["status"], "uncertain")
        self.assertEqual(self.identity.wallet(self.accounts["alice"])["frozen_credit"], 80)
        self.assertEqual(self.queue.claim(uuid4().hex, "local")["job_id"], other["job_id"])
        self.assertIsNone(self.queue.claim(uuid4().hex, "local"))
        self.assertEqual(self.queue.claim(owner, "local")["claim_id"], claim["claim_id"])
        failed = Complete(status="failed", failure_type="interrupted")
        self.queue.complete(first["job_id"], owner, claim["claim_id"], failed)
        self.assertEqual(self.identity.wallet(self.accounts["alice"])["frozen_credit"], 40)
        self.assertEqual(self.queue.claim(uuid4().hex, "local")["job_id"], blocked["job_id"])

    def test_lowering_limit_preserves_each_workers_existing_claim(self):
        self.state.scheduling_policy = SchedulingPolicy(max_active_total=2, max_active_per_account=1)
        first, second = self.submit(), self.submit("bobby")
        owners = [uuid4().hex, uuid4().hex]
        claims = [self.queue.claim(owner, "local") for owner in owners]
        self.assertEqual([claim["job_id"] for claim in claims], [first["job_id"], second["job_id"]])
        self.submit()
        self.state.scheduling_policy = SchedulingPolicy()
        for owner, claim in zip(owners, claims):
            self.assertEqual(self.queue.claim(owner, "local"), claim)
        self.assertIsNone(self.queue.claim(uuid4().hex, "local"))

    def test_online_worker_does_not_hide_other_workers_expired_heartbeat(self):
        self.state.scheduling_policy = SchedulingPolicy(max_active_total=2, max_active_per_account=1)
        first, second = self.submit(), self.submit("bobby")
        old_owner, online_owner = uuid4().hex, uuid4().hex
        old_claim = self.queue.claim(old_owner, "local")
        online_claim = self.queue.claim(online_owner, "local")
        heartbeat_path = self.queue.root / "workers" / (old_owner + ".json")
        heartbeat = self.storage._read_json(heartbeat_path)
        heartbeat["time"] = time.time() - 120
        self.storage._write_json(heartbeat_path, heartbeat)
        legacy_path = self.queue.root / "heartbeat.json"
        if legacy_path.exists():
            legacy = self.storage._read_json(legacy_path)
            if legacy.get("worker_id") == old_owner:
                legacy["time"] = time.time() - 120
                self.storage._write_json(legacy_path, legacy)
        self.queue.beat(second["job_id"], online_owner, online_claim["claim_id"])
        self.assertFalse(self.state.worker_running(old_owner))
        self.assertTrue(self.state.worker_running(online_owner))
        self.assertEqual(self.state.task_view(first["project_id"], first["task_id"])["status"], "uncertain")
        self.assertEqual(self.state.task_view(second["project_id"], second["task_id"])["status"], "generating")
        self.submit()
        self.assertIsNone(self.queue.claim(uuid4().hex, "local"))
        stored = self.state.get_job(first["project_id"], first["task_id"])
        self.assertEqual((stored["worker_id"], stored["claim_id"]), (old_owner, old_claim["claim_id"]))
        self.assertEqual(self.state.scheduling_policy.snapshot(self.state.list_jobs())["active"], 2)

    def test_http_completion_releases_slot_and_replay_does_not_settle_twice(self):
        self.state.scheduling_policy = SchedulingPolicy(max_active_total=2, max_active_per_account=1)
        first, next_job, other = self.submit(), self.submit(), self.submit("bobby")
        owner, other_owner, next_owner = uuid4().hex, uuid4().hex, uuid4().hex
        claim = self.claim_http(owner)
        self.assertEqual(claim["job_id"], first["job_id"])
        self.assertEqual(self.claim_http(other_owner)["job_id"], other["job_id"])
        self.assertIsNone(self.claim_http(next_owner))
        headers = {**self.worker_headers, "X-Worker-ID": owner, "X-Claim-ID": claim["claim_id"]}
        base = f"/api/worker/jobs/{first['job_id']}"
        start = self.client.post(base + "/start", headers=headers, json={"attempt_id": uuid4().hex})
        self.assertEqual(start.status_code, 200, start.text)
        data = self.image.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        upload = self.client.put(base + "/outputs/0", headers=headers, content=data)
        self.assertEqual(upload.status_code, 200, upload.text)
        self.assertEqual(upload.json()["sha256"], digest)
        for _ in range(2):
            result = self.client.post(base + "/complete", headers=headers,
                                      json={"status": "succeeded", "hashes": [digest]})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], "completed")
        self.assertEqual(self.claim_http(next_owner)["job_id"], next_job["job_id"])
        self.assertEqual(self.claim_http(other_owner)["job_id"], other["job_id"])
        self.assertEqual(self.identity.wallet(self.accounts["alice"])["used_credit"], 40)
        self.assertEqual(self.identity.wallet(self.accounts["alice"])["frozen_credit"], 40)
        charges = [row for row in self.identity.ledger(self.accounts["alice"])
                   if row["job_id"] == first["job_id"] and row["type"] == "consume"]
        self.assertEqual(len(charges), 1)
        outputs = [artifact for artifact in self.storage.list_task_artifacts(first["project_id"], first["task_id"])
                   if artifact.kind == "output"]
        self.assertEqual(len(outputs), 1)

    def test_two_remote_workers_generate_concurrently_and_archive_their_own_outputs(self):
        self.state.scheduling_policy = SchedulingPolicy(max_active_total=2, max_active_per_account=1)
        jobs = [self.submit("alice"), self.submit("bobby")]
        jobs_by_task = {job["task_id"]: job for job in jobs}
        barrier = threading.Barrier(2)

        class BarrierBackend(LocalFileBackend):
            def __init__(self, source):
                super().__init__([source])
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                barrier.wait(timeout=10)
                return super().generate(request)

        blue_image = self.root / "concurrent-blue.png"
        Image.new("RGB", (24, 16), "blue").save(blue_image)
        fixtures = [self.image, blue_image]
        expected_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in fixtures]
        self.assertNotEqual(expected_hashes[0], expected_hashes[1])
        backends = [BarrierBackend(path) for path in fixtures]
        workers = []
        for index, backend in enumerate(backends):
            client = TestClient(self.app, base_url=self.access.origin, raise_server_exceptions=False)
            self.addCleanup(client.close)
            worker = RemoteWorker(self.access.origin, self.access.worker_token,
                                  self.root / f"parallel-cache-{index}", backend, client=client)
            workers.append(worker)
        self.assertNotEqual(workers[0].worker_id, workers[1].worker_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker.run_once) for worker in workers]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(results, ["completed", "completed"])
        self.assertFalse(barrier.broken)

        executed_tasks = set()
        for backend, expected_hash in zip(backends, expected_hashes):
            self.assertEqual(len(backend.requests), 1)
            task = backend.requests[0].task
            executed_tasks.add(task.task_id)
            job = jobs_by_task[task.task_id]
            self.assertEqual(task.project_id, job["project_id"])
            self.assertEqual(self.state.get_job(task.project_id, task.task_id)["status"], "completed")
            outputs = [artifact for artifact in self.storage.list_task_artifacts(task.project_id, task.task_id)
                       if artifact.kind == "output"]
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0].sha256, expected_hash)
            archived_bytes = self.storage.get_artifact_path(outputs[0].artifact_id).read_bytes()
            self.assertEqual(hashlib.sha256(archived_bytes).hexdigest(), expected_hash)
            wallet = self.identity.wallet(job["account_id"])
            self.assertEqual((wallet["used_credit"], wallet["frozen_credit"]), (40, 0))
            charges = [row for row in self.identity.ledger(job["account_id"]) if row["type"] == "consume"]
            self.assertEqual(len(charges), 1)
            self.assertEqual(charges[0]["job_id"], job["job_id"])
        self.assertEqual(executed_tasks, set(jobs_by_task))
        self.assertEqual(self.state.scheduling_policy.snapshot(self.state.list_jobs())["active"], 0)
