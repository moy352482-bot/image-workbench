"""账号、空间归属和积分账本。SQLite 事务是账号与额度的唯一事实源。"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time
from uuid import uuid4

from .storage import _now, _validate_id
from .registration import RegistrationDenied, RegistrationPolicy
from .capacity import CapacityPolicy


AUTH_RATE_LIMITS = {"login": (120, 60), "register": (20, 60), "activate": (10, 60)}
LOGIN_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 900


class AuthRateLimited(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__(f"操作过于频繁，请 {self.retry_after} 秒后重试。")


class CreditInsufficient(ValueError):
    def __init__(self, expected, remaining):
        self.expected, self.remaining = expected, remaining
        super().__init__(f"当前积分不足。本次任务预计消耗 {expected} 积分，当前剩余 {remaining} 积分。")


class CreditCostCalculator:
    # 政策由服务端维护；模型名来自服务端配置，不能由客户端提交价格。
    policies = {"codex": (40, 60), "local": (40, 60), "image-api": (40, 60)}

    @classmethod
    def calculate(cls, operation, model, image_count=1, reference_count=0):
        if operation not in {"generate", "edit"} or model not in cls.policies:
            raise ValueError("未配置此操作或模型的积分规则")
        if type(image_count) is not int or image_count < 1 or type(reference_count) is not int or reference_count < 0:
            raise ValueError("图片数量无效")
        ordinary, reference = cls.policies[model]
        return image_count * (reference if reference_count or operation == "edit" else ordinary)


def password_hash(password):
    if not isinstance(password, str) or not 10 <= len(password) <= 256:
        raise ValueError("密码需要 10–256 个字符")
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${value}"


def password_matches(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not isinstance(password, str) or len(password) > 256:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class IdentityStore:
    def __init__(self, storage, *, registration_policy=None, capacity_policy=None):
        self.storage = storage
        self.registration_policy = registration_policy or RegistrationPolicy.from_environment()
        self.capacity_policy = capacity_policy or CapacityPolicy.from_environment()
        self.root = storage.files.path(".identity")
        self.root.mkdir(exist_ok=True)
        self.path = storage.files.path(".identity", "accounts.sqlite3")
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT, password_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','disabled')), is_admin INTEGER NOT NULL DEFAULT 0,
                activation_hash TEXT);
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts,
                name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, storage_root TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS project_ownership (
                main_project_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces,
                deleted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS credit_accounts (
                account_id TEXT PRIMARY KEY REFERENCES accounts, total_credit INTEGER NOT NULL,
                used_credit INTEGER NOT NULL DEFAULT 0, frozen_credit INTEGER NOT NULL DEFAULT 0,
                is_unlimited INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                CHECK(total_credit >= 0 AND used_credit >= 0 AND frozen_credit >= 0),
                CHECK(is_unlimited = 1 OR total_credit >= used_credit + frozen_credit));
            CREATE TABLE IF NOT EXISTS credit_transactions (
                transaction_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts,
                workspace_id TEXT, task_id TEXT, job_id TEXT, type TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK(amount >= 0), created_at TEXT NOT NULL,
                UNIQUE(job_id, type));
            CREATE TABLE IF NOT EXISTS reservations (
                job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts,
                workspace_id TEXT NOT NULL REFERENCES workspaces, project_id TEXT NOT NULL,
                task_id TEXT NOT NULL, request_id TEXT NOT NULL, amount INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('credit_frozen','running','completed','refunded')),
                payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(account_id, request_id));
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON reservations(task_id)
                WHERE status IN ('credit_frozen','running');
            CREATE INDEX IF NOT EXISTS reservation_budget_date ON reservations(created_at);
            CREATE INDEX IF NOT EXISTS reservation_account_status ON reservations(account_id,status);
            CREATE INDEX IF NOT EXISTS reservation_account_created ON reservations(account_id,created_at);
            CREATE TABLE IF NOT EXISTS credit_rejections (
                task_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, expected INTEGER NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts,
                csrf TEXT NOT NULL, workspace_id TEXT NOT NULL REFERENCES workspaces,
                created_at TEXT NOT NULL, expires REAL NOT NULL, device TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS login_attempts (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, reset_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS auth_attempt_expiry ON login_attempts(reset_at);
            CREATE TABLE IF NOT EXISTS registration_invites (
                token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires REAL NOT NULL,
                used_by TEXT UNIQUE REFERENCES accounts, used_at TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def public_account(row):
        return {key: row[key] for key in ("account_id", "username", "email", "created_at", "updated_at", "status", "is_admin")}

    def _account(self, db, username, encoded, *, admin=False, activation=None):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{3,40}", username):
            raise ValueError("用户名需要 3–40 个英文字母、数字、下划线或连字符")
        aid, wid, stamp = uuid4().hex, uuid4().hex, _now()
        db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,'active',?,?)", (aid, username, None, encoded, stamp, stamp, int(admin), activation))
        # storage_root 是逻辑空间根；稳定图片仍通过 project_ownership + StorageManager 解析。
        db.execute("INSERT INTO workspaces VALUES (?,?,?,?,?,?)", (wid, aid, username + " 的工作空间", stamp, stamp, "."))
        db.execute("INSERT INTO credit_accounts VALUES (?,?,0,0,?,?)", (aid, 0 if admin else 200, int(admin), stamp))
        self._ledger(db, aid, wid, None, None, "initial_grant", 0 if admin else 200)
        return aid, wid

    def bootstrap(self, project_ids):
        """本机受控迁移入口。预留 moy；普通注册绝不能认领历史项目。"""
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            owner = db.execute("SELECT * FROM accounts WHERE username='moy'").fetchone()
            if owner:
                if not owner["is_admin"]:
                    raise ValueError("moy 已存在且不是管理员，迁移拒绝覆盖")
                aid = owner["account_id"]
                wid = db.execute("SELECT workspace_id FROM workspaces WHERE account_id=? ORDER BY created_at LIMIT 1", (aid,)).fetchone()[0]
                if owner["password_hash"]:
                    token = None
                else:
                    try:
                        saved = self.storage.files.read_json(self.storage.files.path(".identity", "moy-activation.json"))["activation_token"]
                    except (OSError, ValueError, KeyError):
                        saved = ""
                    if saved and hmac.compare_digest(digest(saved), owner["activation_hash"] or ""):
                        token = None
                    else:
                        db.execute("UPDATE accounts SET activation_hash=? WHERE account_id=?", (digest(token), aid))
            else:
                aid, wid = self._account(db, "moy", None, admin=True, activation=digest(token))
            for pid in project_ids:
                db.execute("INSERT OR IGNORE INTO project_ownership VALUES (?,?,0,?)", (_validate_id(pid), wid, _now()))
            # 写入启用凭据失败则整个账号/归属事务回滚；重启可重新准备。
            if token:
                self.storage.files.write_json(self.storage.files.path(".identity", "moy-activation.json"), {"schema_version": 1, "username": "moy", "activation_token": token})
        return {"account_id": aid, "workspace_id": wid, "activation_required": not bool(owner and owner["password_hash"])}

    def create_invitation(self, *, expires_hours=72):
        """仅供可信本机 CLI；原始凭据仅返回一次，数据库只保存哈希。"""
        if type(expires_hours) is not int or not 1 <= expires_hours <= 720:
            raise ValueError("邀请码有效期必须为 1–720 小时")
        token = secrets.token_urlsafe(32)
        expires = time.time() + expires_hours * 3600
        with self.transaction() as db:
            db.execute("INSERT INTO registration_invites VALUES (?,?,?,NULL,NULL)", (digest(token), _now(), expires))
        return {"invitation_code": token, "expires_at": expires, "single_use": True}

    def _check_registration(self, db, invitation_code):
        policy = self.registration_policy
        if policy.mode == "closed":
            raise RegistrationDenied("当前暂停新账号注册，请使用已有账号登录。")
        if policy.max_accounts and db.execute("SELECT COUNT(*) FROM accounts WHERE is_admin=0").fetchone()[0] >= policy.max_accounts:
            raise RegistrationDenied("当前注册名额已满，请联系管理员。")
        if policy.mode == "invite":
            row = db.execute("SELECT 1 FROM registration_invites WHERE token_hash=? AND used_by IS NULL AND expires>?",
                             (digest(invitation_code or ""), time.time())).fetchone()
            if not row:
                raise RegistrationDenied("需要有效且未使用的邀请码。")

    def register(self, username, password, email=None, invitation_code=None):
        if username.lower() == "moy":
            raise ValueError("该用户名已保留")
        if email is not None and (not isinstance(email, str) or len(email) > 254 or "@" not in email):
            raise ValueError("邮箱格式不正确")
        self._consume_auth_budget("register")
        with self.connect() as db:
            self._check_registration(db, invitation_code)
        encoded = password_hash(password)
        try:
            with self.transaction() as db:
                # 哈希期间名额或邀请可能已被使用，必须在写事务中重新核对。
                self._check_registration(db, invitation_code)
                aid, _ = self._account(db, username, encoded)
                db.execute("UPDATE accounts SET email=? WHERE account_id=?", (email, aid))
                if self.registration_policy.mode == "invite":
                    db.execute("UPDATE registration_invites SET used_by=?,used_at=? WHERE token_hash=?", (aid, _now(), digest(invitation_code)))
                return self.public_account(db.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名不可用") from exc

    def activate(self, token, password):
        self._consume_auth_budget("activate")
        with self.connect() as db:
            pending = db.execute("SELECT 1 FROM accounts WHERE activation_hash=? AND password_hash IS NULL", (digest(token),)).fetchone()
        if not pending:
            raise ValueError("启用凭据无效或已经使用")
        encoded = password_hash(password)
        with self.transaction() as db:
            row = db.execute("SELECT * FROM accounts WHERE activation_hash=? AND password_hash IS NULL", (digest(token),)).fetchone()
            if not row:
                raise ValueError("启用凭据无效或已经使用")
            db.execute("UPDATE accounts SET password_hash=?,activation_hash=NULL,updated_at=? WHERE account_id=?", (encoded, _now(), row["account_id"]))
            return self.public_account(row)

    def _consume_auth_budget(self, action, account_key=None):
        """在昂贵哈希前原子预占预算；不信任客户端可伪造的来源地址。"""
        limit, window = AUTH_RATE_LIMITS[action]
        budgets = [("auth-global:" + action, limit, window)]
        if account_key is not None:
            budgets.append((account_key, LOGIN_ATTEMPTS, LOGIN_WINDOW_SECONDS))
        with self.transaction() as db:
            now = time.time()
            db.execute("DELETE FROM login_attempts WHERE reset_at<=?", (now,))
            for key, maximum, seconds in budgets:
                row = db.execute("SELECT failures,reset_at FROM login_attempts WHERE key=?", (key,)).fetchone()
                if row and row["failures"] >= maximum:
                    raise AuthRateLimited(row["reset_at"] - now)
            for key, maximum, seconds in budgets:
                db.execute("INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET failures=failures+1",
                           (key, now + seconds))

    def login(self, username, password, device=""):
        key = digest(username.lower())
        self._consume_auth_budget("login", key)
        with self.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
        # 未知用户也进行相同成本的验证，避免直接泄露账号存在性。
        encoded = row["password_hash"] if row and row["password_hash"] else "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32
        if not password_matches(password, encoded) or not row or row["status"] != "active":
            raise ValueError("用户名或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            current = db.execute("SELECT status,password_hash FROM accounts WHERE account_id=?", (row["account_id"],)).fetchone()
            if not current or current["status"] != "active" or current["password_hash"] != encoded:
                raise ValueError("用户名或密码错误")
            db.execute("DELETE FROM login_attempts WHERE key=?", (key,))
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            wid = db.execute("SELECT workspace_id FROM workspaces WHERE account_id=? ORDER BY created_at LIMIT 1", (row["account_id"],)).fetchone()[0]
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (digest(token), row["account_id"], csrf, wid, _now(), time.time()+7*86400, device[:500]))
        return token

    def session(self, token):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute("SELECT s.*,a.username,a.status,a.is_admin FROM sessions s JOIN accounts a USING(account_id) WHERE token_hash=? AND expires>? AND a.status='active'", (digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def workspaces(self, account_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT workspace_id,account_id,name,created_at,updated_at FROM workspaces WHERE account_id=? ORDER BY created_at", (account_id,))]

    def require_workspace(self, account_id, workspace_id):
        _validate_id(workspace_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM workspaces WHERE account_id=? AND workspace_id=?", (account_id, workspace_id)).fetchone()
        if not row:
            raise FileNotFoundError("工作空间不存在")
        return dict(row)

    def create_workspace(self, account_id, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("工作空间名称需要 1–120 个字符")
        wid, stamp = uuid4().hex, _now()
        with self.transaction() as db:
            db.execute("INSERT INTO workspaces VALUES (?,?,?,?,?,?)", (wid, account_id, name.strip(), stamp, stamp, "."))
        return self.require_workspace(account_id, wid)

    def select_workspace(self, token, wid):
        session = self.session(token)
        self.require_workspace(session["account_id"], wid)
        with self.transaction() as db:
            db.execute("UPDATE sessions SET workspace_id=? WHERE token_hash=?", (wid, digest(token)))

    def ownership(self, main_id):
        with self.connect() as db:
            row = db.execute("SELECT p.*,w.account_id FROM project_ownership p JOIN workspaces w USING(workspace_id) WHERE main_project_id=?", (main_id,)).fetchone()
        if not row:
            raise FileNotFoundError("项目没有工作空间归属")
        return dict(row)

    def project_ids(self, workspace_id, *, deleted=False):
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT main_project_id FROM project_ownership WHERE workspace_id=? AND deleted=?", (workspace_id, int(deleted)))}

    def assign_project(self, main_id, workspace_id):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM project_ownership WHERE main_project_id=?", (main_id,)).fetchone()
            if row and (row["workspace_id"] != workspace_id or row["deleted"]):
                raise FileNotFoundError("项目 ID 不可用")
            db.execute("INSERT OR IGNORE INTO project_ownership VALUES (?,?,0,?)", (main_id, workspace_id, _now()))

    def change_project(self, account_id, source_workspace_id, main_id, *, target_workspace_id=None, deleted=None):
        self.require_workspace(account_id, source_workspace_id)
        if target_workspace_id:
            self.require_workspace(account_id, target_workspace_id)
        with self.transaction() as db:
            owner = db.execute("SELECT * FROM project_ownership WHERE main_project_id=? AND workspace_id=?", (main_id, source_workspace_id)).fetchone()
            if not owner:
                raise FileNotFoundError("项目不存在")
            if target_workspace_id and owner["deleted"]:
                raise ValueError("请先恢复已删除项目")
            db.execute("UPDATE project_ownership SET workspace_id=?,deleted=?,updated_at=? WHERE main_project_id=?", (target_workspace_id or source_workspace_id, owner["deleted"] if deleted is None else int(deleted), _now(), main_id))

    def wallet(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM credit_accounts WHERE account_id=?", (account_id,)).fetchone()
        if not row:
            raise FileNotFoundError("额度账户不存在")
        value = dict(row)
        value["remaining_credit"] = None if value["is_unlimited"] else value["total_credit"] - value["used_credit"] - value["frozen_credit"]
        if value["is_unlimited"]:
            value["total_credit"] = None
        return value

    def ledger(self, account_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM credit_transactions WHERE account_id=? ORDER BY created_at DESC LIMIT 200", (account_id,))]

    @staticmethod
    def _ledger(db, aid, wid, tid, jid, kind, amount):
        db.execute("INSERT INTO credit_transactions VALUES (?,?,?,?,?,?,?,?)", (uuid4().hex, aid, wid, tid, jid, kind, amount, _now()))

    def request_job(self, account_id, request_id, project_id, task_id):
        """确认旧请求的结果；读取账本不改变当前任务的最新作业。"""
        with self.connect() as db:
            row = db.execute("SELECT project_id,task_id,payload,status FROM reservations WHERE account_id=? AND request_id=?",
                             (account_id, request_id)).fetchone()
        if not row:
            return None
        if (row["project_id"], row["task_id"]) != (project_id, task_id):
            raise ValueError("该请求标识已用于另一项任务")
        return self._job_payload(row)

    @staticmethod
    def _job_payload(row):
        payload = json.loads(row["payload"])
        if row["status"] in {"completed", "refunded"}:
            payload["status"] = "completed" if row["status"] == "completed" else "failed"
            payload["credit_status"] = row["status"]
        return payload

    def settled_job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT payload,status FROM reservations WHERE job_id=? AND status IN ('completed','refunded')", (job_id,)).fetchone()
        return self._job_payload(row) if row else None

    def preflight_script_capacity(self, account_id):
        """创建任务前只读检查；reserve 仍在事务内重查，避免并发穿透。"""
        with self.connect() as db:
            self.capacity_policy.check_script(db, account_id)

    def reserve(self, job, owner, amount):
        aid, wid = owner["account_id"], owner["workspace_id"]
        insufficient = None
        with self.transaction() as db:
            old = db.execute("SELECT * FROM reservations WHERE (account_id=? AND request_id=?) OR (task_id=? AND status IN ('credit_frozen','running')) ORDER BY created_at LIMIT 1", (aid, job["request_id"], job["task_id"])).fetchone()
            if old:
                previous_payload = json.loads(old["payload"])
                if (old["account_id"], old["workspace_id"], old["project_id"], old["task_id"], previous_payload.get("kind", "image")) != (aid, wid, job["project_id"], job["task_id"], job.get("kind", "image")):
                    raise ValueError("该请求标识已用于另一项任务")
                return self._job_payload(old)
            wallet = db.execute("SELECT * FROM credit_accounts WHERE account_id=?", (aid,)).fetchone()
            remaining = wallet["total_credit"] - wallet["used_credit"] - wallet["frozen_credit"]
            if not wallet["is_unlimited"] and remaining < amount:
                db.execute("INSERT INTO credit_rejections VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET expected=excluded.expected,updated_at=excluded.updated_at", (job["task_id"], aid, amount, _now()))
                insufficient = CreditInsufficient(amount, remaining)
            else:
                if job.get("kind") == "script":
                    self.capacity_policy.check_script(db, aid)
                self.capacity_policy.check(db, aid, amount)
                job.update(account_id=aid, workspace_id=wid, credit_cost=amount, credit_status="credit_frozen")
                stamp = _now()
                db.execute("UPDATE credit_accounts SET frozen_credit=frozen_credit+?,updated_at=? WHERE account_id=?", (amount, stamp, aid))
                db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,'credit_frozen',?,?,?)", (job["job_id"], aid, wid, job["project_id"], job["task_id"], job["request_id"], amount, json.dumps(job), stamp, stamp))
                self._ledger(db, aid, wid, job["task_id"], job["job_id"], "freeze", amount)
                db.execute("DELETE FROM credit_rejections WHERE task_id=?", (job["task_id"],))
        if insufficient:
            raise insufficient
        return job

    def pending_jobs(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM reservations WHERE status IN ('credit_frozen','running')")]

    def require_reservation(self, job_id, project_id, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM reservations WHERE job_id=? AND project_id=? AND task_id=? AND status IN ('credit_frozen','running')", (job_id, project_id, task_id)).fetchone()
        if not row:
            raise ValueError("此任务尚未冻结积分，不能启动图片模型")
        return dict(row)

    def record_job(self, job):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM reservations WHERE job_id=?", (job["job_id"],)).fetchone()
            if not row:
                raise ValueError("生成任务缺少积分冻结记录")
            status = row["status"]
            if status in {"completed", "refunded"}:
                # 迟到的领取/执行通知不能重开已结算作业或改变终态载荷。
                settled = self._job_payload(row)
                job.clear()
                job.update(settled)
                return
            if status in {"credit_frozen", "running"}:
                if job["status"] in {"completed", "failed"}:
                    status = "completed" if job["status"] == "completed" else "refunded"
                    db.execute("UPDATE credit_accounts SET frozen_credit=frozen_credit-?,used_credit=used_credit+?,updated_at=? WHERE account_id=?", (row["amount"], row["amount"] if status == "completed" else 0, _now(), row["account_id"]))
                    self._ledger(db, row["account_id"], row["workspace_id"], row["task_id"], row["job_id"], "consume" if status == "completed" else "refund", row["amount"])
                elif job["status"] in {"running", "uncertain"}:
                    status = "running"
            job["credit_status"] = status
            db.execute("UPDATE reservations SET status=?,payload=?,updated_at=? WHERE job_id=?", (status, json.dumps(job), _now(), job["job_id"]))

    def task_credit(self, task_id):
        with self.connect() as db:
            # 已结算的预约优先于后续重试被拒记录，退款事实不能被一次余额不足覆盖。
            row = db.execute("SELECT status,amount FROM reservations WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
            if row:
                return {"credit_status": row[0], "reserved_credit": row[1], "execution_status": "failed" if row[0] == "refunded" else row[0]}
            row = db.execute("SELECT expected FROM credit_rejections WHERE task_id=?", (task_id,)).fetchone()
        if row:
            return {"credit_status": "credit_insufficient", "estimated_credit": row[0], "execution_status": "credit_insufficient"}
        return {"credit_status": "waiting_credit", "execution_status": "waiting_credit"}
