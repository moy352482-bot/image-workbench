import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from unittest.mock import patch

from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import IdentityStore
from image_workbench.cli import main
from image_workbench.registration import RegistrationDenied, RegistrationPolicy
from image_workbench.web import create_app


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.storage = StorageManager(StorageConfig(data_root=Path(temp.name) / "data"))
        self.identity = IdentityStore(self.storage, registration_policy=RegistrationPolicy("invite", 2))

    def test_closed_invalid_expired_used_invite_rejected_before_hash_without_disclosure(self):
        app = create_app(self.storage, backend="local", registration_policy=RegistrationPolicy("invite"))
        with TestClient(app, base_url="http://localhost") as client:
            config = client.get("/api/auth/config").json()
            self.assertEqual(config["registration"]["mode"], "invite")
            headers = {"X-Workbench-CSRF": config["csrf_token"]}
            body = {"username": "alice", "password": "test-password-123", "invitation_code": "secret-invalid-marker"}
            with patch("image_workbench.accounts.password_hash") as hashing:
                response = client.post("/api/auth/register", headers=headers, json=body)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(body["invitation_code"], response.text)
                hashing.assert_not_called()
            invitation = self.identity.create_invitation()
            with self.identity.transaction() as db:
                db.execute("UPDATE registration_invites SET expires=0")
            with patch("image_workbench.accounts.password_hash") as hashing:
                with self.assertRaises(RegistrationDenied):
                    self.identity.register("alice", "test-password-123", invitation_code=invitation["invitation_code"])
                hashing.assert_not_called()
            self.identity.registration_policy = RegistrationPolicy("closed")
            with patch("image_workbench.accounts.password_hash") as hashing:
                with self.assertRaises(RegistrationDenied):
                    self.identity.register("alice", "test-password-123")
                hashing.assert_not_called()

    def test_parallel_single_use_invitation_has_one_account_and_one_grant(self):
        token = self.identity.create_invitation()["invitation_code"]
        barrier = threading.Barrier(2)
        def hashing(password):
            barrier.wait(timeout=5)
            return "isolated-test-hash"
        def attempt(username):
            try:
                return self.identity.register(username, "test-password-123", invitation_code=token)
            except RegistrationDenied:
                return None
        with patch("image_workbench.accounts.password_hash", side_effect=hashing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, ["alice", "bobby"]))
        self.assertEqual(sum(item is not None for item in results), 1)
        with self.identity.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 1)
            self.assertEqual(tuple(db.execute("SELECT COUNT(*),SUM(amount) FROM credit_transactions WHERE type='initial_grant'").fetchone()), (1, 200))
            row = db.execute("SELECT * FROM registration_invites").fetchone()
            self.assertNotEqual(row["token_hash"], token)
            self.assertTrue(row["used_by"])
        with self.assertRaises(RegistrationDenied):
            self.identity.register("carol", "test-password-123", invitation_code=token)

    def test_account_limit_is_atomic_and_disabled_accounts_still_count(self):
        self.identity.registration_policy = RegistrationPolicy("open", 1)
        barrier = threading.Barrier(2)
        def hashing(password):
            barrier.wait(timeout=5)
            return "isolated-test-hash"
        def attempt(username):
            try:
                return self.identity.register(username, "test-password-123")
            except RegistrationDenied:
                return None
        with patch("image_workbench.accounts.password_hash", side_effect=hashing):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(attempt, ["alice", "bobby"]))
        self.assertEqual(sum(item is not None for item in results), 1)
        with self.identity.transaction() as db:
            db.execute("UPDATE accounts SET status='disabled'")
        with self.assertRaises(RegistrationDenied):
            self.identity.register("carol", "test-password-123")

    def test_failed_account_insert_does_not_consume_invite_or_grant(self):
        token = self.identity.create_invitation()["invitation_code"]
        self.identity.register("alice", "test-password-123", invitation_code=token)
        second = self.identity.create_invitation()["invitation_code"]
        with self.assertRaises(ValueError):
            self.identity.register("alice", "test-password-123", invitation_code=second)
        account = self.identity.register("bobby", "test-password-123", invitation_code=second)
        self.assertEqual(self.identity.wallet(account["account_id"])["remaining_credit"], 200)
        with self.identity.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM credit_transactions").fetchone()[0], 2)

    def test_existing_login_survives_closed_registration_and_cli_invite_is_usable(self):
        with patch.dict("os.environ", {"IMAGE_WORKBENCH_REGISTRATION_MODE": "invite", "IMAGE_WORKBENCH_MAX_ACCOUNTS": "2"}):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--data-root", str(self.storage.config.data_root), "accounts-invite", "--expires-hours", "1"]), 0)
            token = json.loads(output.getvalue())["invitation_code"]
            self.identity.register("alice", "test-password-123", invitation_code=token)
            app = create_app(self.storage, backend="local", registration_policy=RegistrationPolicy("closed"))
            with TestClient(app, base_url="http://localhost") as client:
                config = client.get("/api/auth/config").json()
                headers = {"X-Workbench-CSRF": config["csrf_token"]}
                body = {"username": "alice", "password": "test-password-123"}
                self.assertEqual(client.post("/api/auth/login", headers=headers, json=body).status_code, 200)
                self.assertEqual(client.get("/api/auth/me").json()["credits"]["remaining_credit"], 200)

    def test_invalid_policy_does_not_fall_back_to_open_registration(self):
        for env in ({"IMAGE_WORKBENCH_REGISTRATION_MODE": "typo"}, {"IMAGE_WORKBENCH_MAX_ACCOUNTS": "bad"}, {"IMAGE_WORKBENCH_MAX_ACCOUNTS": "-1"}):
            with patch.dict("os.environ", env):
                with self.assertRaises(ValueError):
                    RegistrationPolicy.from_environment()
