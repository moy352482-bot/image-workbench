import hashlib
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import LocalFileBackend, StorageConfig, StorageManager
from image_workbench.accounts import CreditCostCalculator, IdentityStore, password_hash, password_matches
from image_workbench.capacity import CapacityPolicy
from image_workbench.account_migration import migrate_accounts
from image_workbench.remote_access import RemoteAccess
from image_workbench.remote_worker import RemoteWorker
from image_workbench.task_runner import TaskRunner
from image_workbench.web import create_app
from image_workbench.worker import LocalWorker, confirm_stopped
from image_workbench.workbench_state import WorkbenchState


class CountingBackend(LocalFileBackend):
    def __init__(self, images):
        super().__init__(images)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return super().generate(request)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.app = create_app(self.storage, backend="local")
        self.state, self.identity = self.app.state.workbench, self.app.identity
        self.image = self.root / "fixture.png"
        Image.new("RGB", (32, 24), "green").save(self.image)
        self.backend = CountingBackend([self.image])
        self.a = self.user("alice")
        self.b = self.user("bobby")
        self.a_info = self.a.get("/api/auth/me").json()
        self.b_info = self.b.get("/api/auth/me").json()

    def client(self, app=None, url="http://localhost"):
        client = TestClient(app or self.app, base_url=url, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def user(self, username, app=None, url="http://localhost"):
        client = self.client(app, url)
        token = client.get("/api/auth/config").json()["csrf_token"]
        headers = {"X-Workbench-CSRF": token}
        credentials = {"username": username, "password": "test-password-123"}
        self.assertEqual(client.post("/api/auth/register", json=credentials, headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/auth/login", json=credentials, headers=headers).status_code, 200)
        client.headers["X-Workbench-CSRF"] = client.get("/api/config").json()["csrf_token"]
        return client

    def project(self, client=None):
        response = (client or self.a).post("/api/main-projects", json={"name": "测试项目", "request_id": uuid4().hex})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def task(self, project=None, client=None, *, reference=False, base=None):
        client = client or self.a
        project = project or self.project(client)
        sid = project["subprojects"][0]["subproject_id"]
        data = {"request_id": uuid4().hex, "operation": "edit" if base else "generate", "prompt": "一张测试图"}
        if base:
            data["base_artifact_id"] = base
        kwargs = {"files": {"files": ("reference.png", self.image.read_bytes(), "image/png")}} if reference else {}
        response = client.post(f"/api/subprojects/{sid}/tasks", data=data, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit(self, task, client=None, **extra):
        return (client or self.a).post(f"/api/tasks/{task['task_id']}/run", json={"request_id": uuid4().hex, "confirm_generation": True, **extra})

    def wallet(self, client=None):
        return (client or self.a).get("/api/credits").json()["credits"]

    def execute(self, task, backend=None):
        response = self.submit(task)
        self.assertEqual(response.status_code, 200, response.text)
        LocalWorker(self.state, backend or self.backend).serve(once=True)
        return self.a.get(f"/api/tasks/{task['task_id']}").json()

    def test_auth_password_hash_cookie_logout_disabled_and_restricted_payload(self):
        anon = self.client()
        self.assertEqual(anon.get("/api/main-projects").status_code, 401)
        self.assertEqual(anon.get("/login").status_code, 200)
        self.assertEqual(anon.get("/.identity/accounts.sqlite3").status_code, 401)
        with self.identity.connect() as db:
            rows = db.execute("SELECT password_hash FROM accounts WHERE password_hash IS NOT NULL").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertTrue(all(row[0].startswith("pbkdf2_sha256$600000$") for row in rows))
        self.assertTrue(all("test-password" not in row[0] for row in rows))
        token = self.a.cookies.get("iw_session")
        self.assertEqual(self.a.post("/api/auth/logout").status_code, 200)
        self.assertIsNone(self.identity.session(token))
        self.assertEqual(self.a.get("/api/credits").status_code, 401)
        with self.identity.transaction() as db:
            db.execute("UPDATE accounts SET status='disabled' WHERE account_id=?", (self.b_info["account_id"],))
        self.assertEqual(self.b.get("/api/main-projects").status_code, 401)
        config = anon.get("/api/auth/config").json()
        response = anon.post("/api/auth/register", headers={"X-Workbench-CSRF": config["csrf_token"]}, json={"username": "evil", "password": "test-password-123", "is_admin": True})
        self.assertEqual(response.status_code, 422)

    def test_workspace_project_task_artifact_and_credit_isolation(self):
        project = self.project()
        task = self.execute(self.task(project))
        artifact = task["outputs"][0]
        self.assertEqual(project["workspace_id"], self.a_info["workspace_id"])
        self.assertEqual(self.b.get("/api/main-projects").json()["main_projects"], [])
        for path in (f"/api/main-projects/{project['main_project_id']}", f"/api/tasks/{task['task_id']}", artifact["url"], artifact["download_url"], f"/api/projects/{task['project_id']}/tasks"):
            self.assertEqual(self.b.get(path).status_code, 404, path)
        self.assertEqual(self.submit(task, self.b).status_code, 404)
        self.assertEqual(self.b.post(f"/api/artifacts/{artifact['artifact_id']}/approve", json={}).status_code, 404)
        self.assertEqual(self.b.get("/api/credits", headers={"X-Workspace-ID": self.a_info["workspace_id"]}).status_code, 404)
        self.assertEqual(self.b.get(f"/api/artifacts/{artifact['artifact_id']}?workspace_id=" + self.a_info["workspace_id"]).status_code, 404)
        self.assertEqual(self.wallet(self.b)["remaining_credit"], 200)
        self.assertEqual(self.b.post("/api/main-projects", json={"name": "窃取", "request_id": project["main_project_id"]}).status_code, 404)
        self.assertEqual(self.identity.ownership(project["main_project_id"])["account_id"], self.a_info["account_id"])

    def test_activation_validation_explains_fields_without_echoing_credentials(self):
        anon = self.client()
        headers = {"X-Workbench-CSRF": anon.get("/api/auth/config").json()["csrf_token"]}
        response = anon.post("/api/auth/activate", headers=headers,
                             json={"activation_token": "incomplete-token", "password": "short-pw"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("完整的本机启用凭据", response.json()["detail"])
        self.assertIn("新密码需为 10–256 个字符", response.json()["detail"])
        self.assertNotIn("incomplete-token", response.text)
        self.assertNotIn("short-pw", response.text)

    def test_parallel_login_attempts_cannot_bypass_remaining_budget(self):
        with self.identity.transaction() as db:
            db.execute("INSERT INTO login_attempts VALUES (?,7,?)",
                       (hashlib.sha256(b"alice").hexdigest(), time.time() + 900))
        start = threading.Barrier(12)
        entered, release = threading.Event(), threading.Event()

        def slow_wrong_password(*args):
            entered.set()
            release.wait(5)
            return False

        def attempt():
            start.wait(timeout=5)
            try:
                self.identity.login("alice", "wrong-password")
            except ValueError:
                return "denied"
            return "unexpected_success"

        with patch("image_workbench.accounts.password_matches", side_effect=slow_wrong_password) as verify:
            with ThreadPoolExecutor(max_workers=12) as pool:
                futures = [pool.submit(attempt) for _ in range(12)]
                try:
                    self.assertTrue(entered.wait(5))
                    wait(futures, timeout=1)
                finally:
                    release.set()
                self.assertTrue(all(f.result() == "denied" for f in futures))
            self.assertEqual(verify.call_count, 1, "只剩一次预算时，并发请求只能有一次进入密码哈希校验")

    def test_auth_requests_are_small_and_do_not_echo_oversized_input(self):
        anon = self.client()
        headers = {"X-Workbench-CSRF": anon.get("/api/auth/config").json()["csrf_token"]}
        with patch("image_workbench.accounts.password_matches") as verify:
            response = anon.post("/api/auth/login", headers=headers,
                                 json={"username": "alice", "password": "sensitive-marker" * 2000})
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("sensitive-marker", response.text)
        verify.assert_not_called()
        streamed = anon.post("/api/auth/login", headers=headers,
                             content=iter([b"sensitive-marker" * 1500]))
        self.assertEqual(streamed.status_code, 413)
        self.assertNotIn("sensitive-marker", streamed.text)

    def test_registration_and_activation_budgets_precede_password_hash(self):
        anon = self.client()
        headers = {"X-Workbench-CSRF": anon.get("/api/auth/config").json()["csrf_token"]}
        with patch.dict("image_workbench.accounts.AUTH_RATE_LIMITS", {"register": (3, 60), "activate": (2, 60)}):
            with patch("image_workbench.accounts.password_hash", wraps=password_hash) as hashing:
                # setUp 注册了两个账号，只剩一个注册预算。
                created = anon.post("/api/auth/register", headers=headers,
                                    json={"username": "charlie", "password": "test-password-123"})
                self.assertEqual(created.status_code, 200, created.text)
                denied = anon.post("/api/auth/register", headers=headers,
                                   json={"username": "delta", "password": "test-password-123"})
                self.assertEqual(denied.status_code, 429)
                self.assertTrue(1 <= int(denied.headers["Retry-After"]) <= 60)
                self.assertEqual(hashing.call_count, 1)
                with self.identity.transaction() as db:
                    db.execute("UPDATE login_attempts SET reset_at=0 WHERE key='auth-global:register'")
                renewed = anon.post("/api/auth/register", headers=headers,
                                    json={"username": "delta", "password": "test-password-123"})
                self.assertEqual(renewed.status_code, 200, renewed.text)
                self.assertEqual(hashing.call_count, 2)
            with patch("image_workbench.accounts.password_hash") as hashing:
                for expected in (400, 400, 429):
                    response = anon.post("/api/auth/activate", headers=headers,
                                         json={"activation_token": "invalid" * 8, "password": "test-password-123"})
                    self.assertEqual(response.status_code, expected, response.text)
                hashing.assert_not_called()

    def test_disabling_account_during_password_check_does_not_create_session(self):
        aid = self.a_info["account_id"]
        with self.identity.connect() as db:
            before = db.execute("SELECT count(*) FROM sessions WHERE account_id=?", (aid,)).fetchone()[0]

        def disable_during_hash(password, encoded):
            valid = password_matches(password, encoded)
            with self.identity.transaction() as db:
                db.execute("UPDATE accounts SET status='disabled' WHERE account_id=?", (aid,))
            return valid

        with patch("image_workbench.accounts.password_matches", side_effect=disable_during_hash):
            with self.assertRaisesRegex(ValueError, "用户名或密码错误"):
                self.identity.login("alice", "test-password-123")
        with self.identity.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE account_id=?", (aid,)).fetchone()[0], before)

    def test_costs_freeze_consume_and_edit_rule(self):
        self.assertEqual(self.wallet()["remaining_credit"], 200)
        first = self.task()
        response = self.submit(first)
        self.assertEqual(response.status_code, 200, response.text)
        wallet = self.wallet()
        self.assertEqual((wallet["remaining_credit"], wallet["frozen_credit"], wallet["used_credit"]), (160, 40, 0))
        LocalWorker(self.state, self.backend).serve(once=True)
        self.assertEqual((self.wallet()["remaining_credit"], self.wallet()["used_credit"]), (160, 40))
        second = self.execute(self.task(reference=True))
        self.assertEqual(self.wallet()["remaining_credit"], 100)
        self.assertEqual(second["credit_status"], "completed")
        self.assertEqual(second["execution_backend"], "local")
        # 即使以后改用真实后端，旧结果仍必须标明它来自演练。
        production = self.client(create_app(self.storage, backend="codex"))
        production.cookies.update(self.a.cookies)
        self.assertEqual(production.get(f"/api/tasks/{second['task_id']}").json()["execution_backend"], "local")
        self.assertEqual(production.get("/api/auth/config").json()["backend"], "codex")
        self.assertEqual(self.a.get("/api/auth/config").json()["backend"], "local")
        base = second["outputs"][0]["artifact_id"]
        project = self.a.get(f"/api/main-projects/{second['main_project_id']}").json()["main_project"]
        edited = self.execute(self.task(project, base=base))
        self.assertEqual(edited["estimated_credit"], 60)
        self.assertEqual(self.wallet()["remaining_credit"], 40)
        entries = self.a.get("/api/credits").json()["transactions"]
        self.assertEqual([entry["type"] for entry in entries].count("consume"), 3)

    def test_reservation_display_stays_at_charged_price_after_policy_change(self):
        task = self.task()
        self.assertEqual(self.submit(task).status_code, 200)
        with patch.dict(CreditCostCalculator.policies, {"local": (80, 100)}):
            detail = self.a.get(f"/api/tasks/{task['task_id']}").json()
            self.assertEqual(detail["estimated_credit"], 80)
            self.assertEqual(detail.get("reserved_credit"), 40)
            self.assertEqual(self.wallet()["frozen_credit"], 40)
            LocalWorker(self.state, self.backend).serve(once=True)
            completed = self.a.get(f"/api/tasks/{task['task_id']}").json()
            self.assertEqual(completed["credit_status"], "completed")
            self.assertEqual(completed["reserved_credit"], 40)
            self.assertEqual(self.wallet()["used_credit"], 40)

    def test_refunded_amount_and_next_retry_quote_remain_separate(self):
        task = self.task()
        failed = self.execute(task, CountingBackend([self.root / "absent.png"]))
        old_job = failed["current_job"]["job_id"]
        with patch.dict(CreditCostCalculator.policies, {"local": (80, 100)}):
            detail = self.a.get(f"/api/tasks/{task['task_id']}").json()
            self.assertEqual((detail.get("reserved_credit"), detail["estimated_credit"]), (40, 80))
            self.assertEqual(self.submit(task, retry_of=old_job).status_code, 200)
            retry = self.a.get(f"/api/tasks/{task['task_id']}").json()
            self.assertEqual(retry["reserved_credit"], 80)
            self.assertEqual(self.wallet()["frozen_credit"], 80)

    def test_capacity_rejection_is_clear_and_never_freezes_or_creates_job(self):
        scoped = self.app.scoped_app(self.a_info["account_id"], self.a_info["workspace_id"])
        scoped.state.workbench.identity.capacity_policy = CapacityPolicy(max_pending_per_account=1)
        first, second = self.task(), self.task()
        self.assertEqual(self.submit(first).status_code, 200)
        denied = self.submit(second)
        self.assertEqual(denied.status_code, 429, denied.text)
        self.assertEqual(denied.json()["status"], "capacity_exceeded")
        self.assertIn("本次未冻结积分", denied.json()["detail"])
        self.assertEqual(self.wallet()["frozen_credit"], 40)
        self.assertIsNone(self.state.get_job(second["project_id"], second["task_id"]))
        self.assertEqual(self.backend.calls, 0)

    def test_insufficient_20_never_creates_job_or_starts_backend(self):
        for _ in range(3):
            self.execute(self.task(reference=True))
        self.assertEqual(self.wallet()["remaining_credit"], 20)
        count = len(self.state.list_jobs())
        task = self.task()
        response = self.submit(task)
        self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(response.json()["status"], "credit_insufficient")
        self.assertEqual((response.json()["expected_credit"], response.json()["remaining_credit"]), (40, 20))
        self.assertEqual(self.a.get(f"/api/tasks/{task['task_id']}").json()["status"], "credit_insufficient")
        self.assertEqual(len(self.state.list_jobs()), count)
        self.assertIsNone(self.state.runner.get_execution(task["project_id"], task["task_id"]))
        self.assertFalse(LocalWorker(self.state, self.backend).run_next())
        self.assertEqual(self.backend.calls, 3)

    def test_five_generations_then_sixth_rejected(self):
        for _ in range(5):
            self.execute(self.task())
        self.assertEqual(self.wallet()["remaining_credit"], 0)
        self.assertEqual(self.submit(self.task()).status_code, 402)
        self.assertEqual(self.backend.calls, 5)

    def test_failure_refund_is_exactly_once(self):
        task = self.task(reference=True)
        failed = self.execute(task, CountingBackend([self.root / "absent.png"]))
        self.assertEqual(failed["credit_status"], "refunded")
        self.assertEqual(failed["execution_status"], "failed")
        self.assertEqual(self.wallet()["remaining_credit"], 200)
        job = self.state.get_job(task["project_id"], task["task_id"])
        self.state.save_job(job)
        LocalWorker(self.state, self.backend).reconcile()
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        ledger = self.a.get("/api/credits").json()["transactions"]
        self.assertEqual(sum(row["type"] == "refund" for row in ledger), 1)

    def test_concurrent_clicks_and_workers_execute_once(self):
        task = self.task(reference=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.submit(task), range(8)))
        self.assertTrue(all(item.status_code == 200 for item in responses), [item.text for item in responses])
        self.assertEqual(len({item.json()["job_id"] for item in responses}), 1)
        self.assertEqual(self.wallet()["frozen_credit"], 60)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: LocalWorker(self.state, self.backend).run_next(), range(4)))
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(self.wallet()["remaining_credit"], 140)
        self.assertEqual(self.wallet()["used_credit"], 60)

    def test_outbox_recovers_interrupted_publish_without_refreezing(self):
        task = self.task(reference=True)
        with patch.object(WorkbenchState, "save_job", side_effect=OSError("simulated publication failure")):
            response = self.submit(task)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.wallet()["frozen_credit"], 60)
        self.assertEqual(len(self.state.list_jobs()), 1)
        LocalWorker(self.state, self.backend).serve(once=True)
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(self.wallet()["remaining_credit"], 140)

    def test_replayed_request_does_not_replace_latest_retry(self):
        task = self.task(reference=True)
        original_request = uuid4().hex
        first = self.submit(task, request_id=original_request).json()
        failing = CountingBackend([self.root / "absent.png"])
        LocalWorker(self.state, failing).serve(once=True)
        second = self.submit(task, retry_of=first["job_id"]).json()
        LocalWorker(self.state, failing).serve(once=True)
        self.assertNotEqual(first["job_id"], second["job_id"])
        replay = self.submit(task, request_id=original_request, retry_of=second["job_id"])
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["job_id"], first["job_id"])
        # 网络重放应确认原请求，不能覆盖较新的执行和重试状态。
        current = self.state.get_job(task["project_id"], task["task_id"])
        self.assertEqual(current["job_id"], second["job_id"])
        detail = self.a.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(detail["current_job"]["job_id"], detail["last_execution_id"])
        self.assertEqual(failing.calls, 2)
        self.assertEqual((self.wallet()["remaining_credit"], self.wallet()["frozen_credit"]), (200, 0))
        entries = self.a.get("/api/credits").json()["transactions"]
        self.assertEqual(sum(entry["type"] == "freeze" for entry in entries), 2)
        self.assertEqual(sum(entry["type"] == "refund" for entry in entries), 2)
        other = self.task()
        self.assertEqual(self.submit(other, request_id=original_request).status_code, 400)
        self.assertIsNone(self.state.get_job(other["project_id"], other["task_id"]))

    def test_late_job_updates_cannot_reopen_settled_job_or_replace_new_retry(self):
        completed_task = self.task()
        self.assertEqual(self.submit(completed_task).status_code, 200)
        submitted = self.state.get_job(completed_task["project_id"], completed_task["task_id"])
        LocalWorker(self.state, self.backend).serve(once=True)
        stale = {**submitted, "status": "running"}
        self.identity.record_job(stale)
        replay = self.identity.request_job(self.a_info["account_id"], submitted["request_id"], completed_task["project_id"], completed_task["task_id"])
        self.assertEqual((replay["status"], replay["credit_status"]), ("completed", "completed"))
        self.state.save_job({**submitted, "status": "running"})
        self.assertEqual(self.state.get_job(completed_task["project_id"], completed_task["task_id"])["status"], "completed")
        self.assertFalse(LocalWorker(self.state, self.backend).run_next())
        self.assertEqual(self.wallet()["used_credit"], 40)
        failed_task = self.task()
        self.assertEqual(self.submit(failed_task).status_code, 200)
        failed_job = self.state.get_job(failed_task["project_id"], failed_task["task_id"])
        LocalWorker(self.state, CountingBackend([self.root / "absent.png"])).serve(once=True)
        retry = self.submit(failed_task, retry_of=failed_job["job_id"]).json()
        self.state.save_job({**failed_job, "status": "running"})
        self.assertEqual(self.state.get_job(failed_task["project_id"], failed_task["task_id"])["job_id"], retry["job_id"])
        self.assertEqual(self.wallet()["frozen_credit"], 40)

    def test_uncertain_keeps_frozen_until_explicit_reconciliation(self):
        task = self.task(reference=True)
        self.assertEqual(self.submit(task).status_code, 200)
        job = self.state.get_job(task["project_id"], task["task_id"])
        job["status"] = "uncertain"
        self.state.save_job(job)
        LocalWorker(self.state, self.backend).reconcile()
        self.assertEqual(self.wallet()["frozen_credit"], 60)
        confirm_stopped(self.state, task["project_id"], task["task_id"])
        self.assertEqual(self.wallet()["remaining_credit"], 200)

    def test_taskrunner_refuses_unreserved_direct_generation(self):
        task = self.task()
        with self.assertRaisesRegex(ValueError, "冻结"):
            TaskRunner(self.storage).run(task["project_id"], task["task_id"], self.backend)
        self.assertEqual(self.backend.calls, 0)

    def test_project_rename_archive_move_delete_restore_preserve_files_and_ids(self):
        project = self.project()
        task = self.execute(self.task(project))
        artifact = task["outputs"][0]
        before_path = self.storage.get_artifact_path(artifact["artifact_id"])
        before_bytes = before_path.read_bytes()
        mid = project["main_project_id"]
        self.assertEqual(self.a.patch(f"/api/main-projects/{mid}", json={"name": "新名称", "status": "archived"}).status_code, 200)
        target = self.a.post("/api/workspaces", json={"name": "第二空间"}).json()["workspace_id"]
        self.assertEqual(self.a.post(f"/api/project-actions/{mid}/move", json={"workspace_id": self.b_info["workspace_id"]}).status_code, 404)
        self.assertEqual(self.a.post(f"/api/project-actions/{mid}/move", json={"workspace_id": target}).status_code, 200)
        self.assertEqual(self.a.get(f"/api/tasks/{task['task_id']}").status_code, 404)
        self.assertEqual(self.a.post("/api/workspaces/select", json={"workspace_id": target}).status_code, 200)
        moved = self.a.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(moved["task_id"], task["task_id"])
        self.assertEqual(moved["workspace_id"], target)
        self.assertEqual(moved["outputs"][0]["artifact_id"], artifact["artifact_id"])
        self.assertEqual(self.a.delete(f"/api/project-actions/{mid}").status_code, 200)
        self.assertEqual(self.a.get(moved["outputs"][0]["url"]).status_code, 404)
        self.assertEqual(len(self.a.get("/api/project-actions/deleted").json()["projects"]), 1)
        self.assertEqual(self.a.post(f"/api/project-actions/{mid}/restore").status_code, 200)
        self.assertEqual(self.a.get(moved["outputs"][0]["url"]).status_code, 200)
        self.assertEqual(self.storage.get_artifact_path(artifact["artifact_id"]), before_path)
        self.assertEqual(before_path.read_bytes(), before_bytes)

    def test_moving_or_deleting_busy_project_is_rejected(self):
        project = self.project()
        self.submit(self.task(project))
        target = self.a.post("/api/workspaces", json={"name": "第二空间"}).json()["workspace_id"]
        mid = project["main_project_id"]
        self.assertEqual(self.a.delete(f"/api/project-actions/{mid}").status_code, 409)
        self.assertEqual(self.a.post(f"/api/project-actions/{mid}/move", json={"workspace_id": target}).status_code, 409)

    def test_admin_reserved_activation_unlimited_and_no_historical_charges(self):
        anon = self.client()
        headers = {"X-Workbench-CSRF": anon.get("/api/auth/config").json()["csrf_token"]}
        self.assertEqual(anon.post("/api/auth/register", headers=headers, json={"username": "moy", "password": "test-password-123"}).status_code, 400)
        activation = self.storage._read_json(self.storage._path(".identity", "moy-activation.json"))["activation_token"]
        self.assertEqual(anon.post("/api/auth/activate", headers=headers, json={"activation_token": activation, "password": "admin-password-123"}).status_code, 200)
        self.assertEqual(anon.post("/api/auth/activate", headers=headers, json={"activation_token": activation, "password": "admin-password-123"}).status_code, 400)
        self.assertEqual(anon.post("/api/auth/login", headers=headers, json={"username": "moy", "password": "admin-password-123"}).status_code, 200)
        wallet = self.wallet(anon)
        self.assertTrue(wallet["is_unlimited"])
        self.assertIsNone(wallet["remaining_credit"])
        self.assertEqual(wallet["used_credit"], 0)
        anon.headers["X-Workbench-CSRF"] = anon.get("/api/config").json()["csrf_token"]
        for _ in range(6):
            task = self.task(client=anon, reference=True)
            self.assertEqual(self.submit(task, anon).status_code, 200)
            LocalWorker(self.state, self.backend).serve(once=True)
        self.assertEqual(self.wallet(anon)["used_credit"], 360)
        self.assertIsNone(self.wallet(anon)["remaining_credit"])

    def test_multiple_tasks_compete_for_balance_without_overspending(self):
        tasks = [self.task(reference=True) for _ in range(5)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(self.submit, tasks))
        self.assertEqual(sorted(item.status_code for item in results), [200, 200, 200, 402, 402])
        self.assertEqual(self.wallet()["frozen_credit"], 180)
        self.assertEqual(self.wallet()["remaining_credit"], 20)

    def test_remote_worker_uses_account_reservation_and_separate_bearer_auth(self):
        access = RemoteAccess("https://images.example", "worker-" + "r" * 40, "owner", "legacy-" + "b" * 32)
        storage = StorageManager(StorageConfig(data_root=self.root / "remote"))
        app = create_app(storage, backend="local", remote_access=access)
        browser = self.user("remoteuser", app, access.origin)
        task = self.task(client=browser, reference=True)
        self.assertEqual(self.submit(task, browser).status_code, 200)
        self.assertEqual(self.wallet(browser)["frozen_credit"], 60)
        self.assertEqual(browser.post("/api/worker/claim", json={"worker_id": uuid4().hex, "backend": "local"}).status_code, 401)
        service_client = self.client(app, access.origin)
        self.assertEqual(service_client.get("/api/credits", headers={"Authorization": access.worker_authorization.decode()}).status_code, 401)
        backend = CountingBackend([self.image])
        worker = RemoteWorker(access.origin, access.worker_token, self.root / "remote-cache", backend, client=service_client)
        worker.serve(once=True)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.wallet(browser)["remaining_credit"], 140)
        detail = browser.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(detail["credit_status"], "completed")
        self.assertEqual(browser.get(detail["outputs"][0]["url"]).status_code, 200)
        worker.serve(once=True)
        self.assertEqual(backend.calls, 1)

    def test_session_csrf_origin_and_account_choice_cannot_be_forged(self):
        task = self.task()
        body = {"request_id": uuid4().hex, "confirm_generation": True}
        self.assertEqual(self.a.post(f"/api/tasks/{task['task_id']}/run", json=body, headers={"X-Workbench-CSRF": self.b.headers["X-Workbench-CSRF"]}).status_code, 403)
        self.assertEqual(self.a.post(f"/api/tasks/{task['task_id']}/run", json=body, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.a.post("/api/workspaces/select", json={"workspace_id": self.b_info["workspace_id"]}).status_code, 404)
        self.assertEqual(self.backend.calls, 0)
        self.assertEqual(self.wallet()["frozen_credit"], 0)

    def test_unassigned_cli_project_cannot_be_claimed_with_client_supplied_id(self):
        project = self.storage.create_project("未认领的 CLI 项目")
        response = self.a.post("/api/main-projects", json={"name": "认领", "request_id": project.project_id})
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(FileNotFoundError):
            self.identity.ownership(project.project_id)
        self.assertEqual(self.a.get("/api/main-projects").json()["main_projects"], [])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.project = self.storage.create_project("历史项目")
        self.task = self.storage.create_task(self.project.project_id, "generate", "保留图片")
        self.image = self.root / "fixture.png"
        Image.new("RGB", (32, 24), "navy").save(self.image)
        self.artifact = self.storage.register_output_image(self.project.project_id, self.task.task_id, self.image)

    def test_preview_is_readonly_apply_idempotent_original_metadata_unchanged(self):
        original = {path.relative_to(self.storage.data_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in self.storage.data_root.rglob("*") if path.is_file()}
        preview = migrate_accounts(self.storage)
        self.assertFalse(self.storage._path(".identity").exists())
        report = migrate_accounts(self.storage, apply=True, expected_fingerprint=preview["fingerprint"], backup_directory=self.root / "backups")
        self.assertTrue(report["integrity_verified"])
        self.assertTrue((Path(report["backup_directory"]) / "backup.json").exists())
        identity = IdentityStore(self.storage)
        self.assertEqual(identity.ownership(self.project.project_id)["workspace_id"], report["workspace_id"])
        self.assertEqual(identity.wallet(report["account_id"])["used_credit"], 0)
        again = migrate_accounts(self.storage, apply=True, expected_fingerprint=preview["fingerprint"], backup_directory=self.root / "backups")
        self.assertEqual(again["workspace_id"], report["workspace_id"])
        for relative, before in original.items():
            self.assertEqual(hashlib.sha256((self.storage.data_root / relative).read_bytes()).hexdigest(), before)
        self.assertEqual(self.storage.get_artifact(self.artifact.artifact_id), self.artifact)
        app = create_app(self.storage, backend="local")
        with TestClient(app, base_url="http://localhost") as client:
            token = client.get("/api/auth/config").json()["csrf_token"]
            headers = {"X-Workbench-CSRF": token}
            activation = self.storage._read_json(self.storage._path(".identity", "moy-activation.json"))["activation_token"]
            self.assertEqual(client.post("/api/auth/activate", headers=headers, json={"activation_token": activation, "password": "admin-password-123"}).status_code, 200)
            self.assertEqual(client.post("/api/auth/login", headers=headers, json={"username": "moy", "password": "admin-password-123"}).status_code, 200)
            projects = client.get("/api/main-projects").json()["main_projects"]
            self.assertEqual(projects[0]["main_project_id"], self.project.project_id)
            self.assertEqual(client.get(f"/api/artifacts/{self.artifact.artifact_id}").status_code, 200)

    def test_stale_preview_does_not_create_identity(self):
        preview = migrate_accounts(self.storage)
        self.storage.create_project("后来新增")
        with self.assertRaisesRegex(ValueError, "变化"):
            migrate_accounts(self.storage, apply=True, expected_fingerprint=preview["fingerprint"], backup_directory=self.root / "backups")
        self.assertFalse(self.storage._path(".identity").exists())

    def test_activation_file_failure_rolls_back_account_and_ownership(self):
        identity = IdentityStore(self.storage)
        with patch.object(self.storage, "_write_json", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                identity.bootstrap([self.project.project_id])
        with identity.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_ownership").fetchone()[0], 0)
        report = identity.bootstrap([self.project.project_id])
        self.assertEqual(identity.ownership(self.project.project_id)["workspace_id"], report["workspace_id"])


class CalculatorTests(unittest.TestCase):
    def test_server_policy_and_strict_inputs(self):
        self.assertEqual(CreditCostCalculator.calculate("generate", "codex", 3, 0), 120)
        self.assertEqual(CreditCostCalculator.calculate("generate", "codex", 2, 8), 120)
        self.assertEqual(CreditCostCalculator.calculate("edit", "codex", 1, 0), 60)
        for args in (("generate", "unknown", 1, 0), ("generate", "codex", True, 0), ("generate", "codex", 0, 0)):
            with self.assertRaises(ValueError):
                CreditCostCalculator.calculate(*args)
