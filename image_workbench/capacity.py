"""运营容量预算和不可关闭的脚本提交保护；与用户可用积分分开。"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os


class CapacityExceeded(ValueError):
    pass


@dataclass(frozen=True)
class CapacityPolicy:
    max_pending_per_account: int = 0
    max_pending_total: int = 0
    max_daily_submitted_credit: int = 0
    script_window_seconds: int = 60
    max_script_submissions_per_window: int = 6
    max_pending_scripts_per_account: int = 3

    def __post_init__(self):
        optional = (self.max_pending_per_account, self.max_pending_total, self.max_daily_submitted_credit)
        if any(type(value) is not int or value < 0 for value in optional):
            raise ValueError("运营容量上限必须为非负整数")
        required = (self.script_window_seconds, self.max_script_submissions_per_window, self.max_pending_scripts_per_account)
        if any(type(value) is not int or value <= 0 for value in required):
            raise ValueError("脚本提交窗口、次数和在途上限必须为正整数，不能关闭免费生成保护")

    @classmethod
    def from_environment(cls):
        names = {
            "max_pending_per_account": ("IMAGE_WORKBENCH_MAX_PENDING_PER_ACCOUNT", 0),
            "max_pending_total": ("IMAGE_WORKBENCH_MAX_PENDING_TOTAL", 0),
            "max_daily_submitted_credit": ("IMAGE_WORKBENCH_MAX_DAILY_SUBMITTED_CREDIT", 0),
            "script_window_seconds": ("IMAGE_WORKBENCH_SCRIPT_WINDOW_SECONDS", 60),
            "max_script_submissions_per_window": ("IMAGE_WORKBENCH_MAX_SCRIPT_SUBMISSIONS_PER_WINDOW", 6),
            "max_pending_scripts_per_account": ("IMAGE_WORKBENCH_MAX_PENDING_SCRIPTS_PER_ACCOUNT", 3),
        }
        try:
            return cls(**{field: int(os.environ.get(name, str(default))) for field, (name, default) in names.items()})
        except ValueError as exc:
            raise ValueError("运营容量配置必须为非负整数；脚本提交窗口、次数和在途上限必须为正整数") from exc

    def check_script(self, db, account_id):
        """只读预检与事务内检查共用；免费、已完成和退款提交都占窗口次数。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.script_window_seconds)).isoformat()
        rows = db.execute(
            "SELECT status,created_at,payload FROM reservations "
            "WHERE account_id=? AND (created_at>? OR status IN ('credit_frozen','running'))",
            (account_id, cutoff),
        )
        recent = pending = 0
        for row in rows:
            if json.loads(row["payload"]).get("kind", "image") != "script":
                continue
            recent += row["created_at"] > cutoff
            pending += row["status"] in {"credit_frozen", "running"}
        if pending >= self.max_pending_scripts_per_account:
            raise CapacityExceeded(
                f"该账号已有 {self.max_pending_scripts_per_account} 个脚本任务排队或执行中，请等待完成后再提交。"
            )
        if recent >= self.max_script_submissions_per_window:
            raise CapacityExceeded(
                f"脚本提交过于频繁：每 {self.script_window_seconds} 秒最多 {self.max_script_submissions_per_window} 次，请稍后重试。"
            )

    def check(self, db, account_id, amount):
        # 与 freeze/outbox 同一个 BEGIN IMMEDIATE，避免最后一个名额并发穿透。
        if self.max_pending_per_account:
            count = db.execute("SELECT COUNT(*) FROM reservations WHERE account_id=? AND status IN ('credit_frozen','running')", (account_id,)).fetchone()[0]
            if count >= self.max_pending_per_account:
                raise CapacityExceeded("你的待处理任务已达上限，请等待已有任务结束后再提交；本次未冻结积分。")
        if self.max_pending_total:
            count = db.execute("SELECT COUNT(*) FROM reservations WHERE status IN ('credit_frozen','running')").fetchone()[0]
            if count >= self.max_pending_total:
                raise CapacityExceeded("当前执行队列已满，请稍后再提交；本次未冻结积分。")
        if self.max_daily_submitted_credit:
            today = datetime.now(timezone.utc).date().isoformat()
            # 失败或退款仍可能消耗上游资源，因此不返还当日运营预算。
            used = db.execute("SELECT COALESCE(SUM(amount),0) FROM reservations WHERE created_at>=?", (today,)).fetchone()[0]
            if used + amount > self.max_daily_submitted_credit:
                raise CapacityExceeded("今日提交预算已用完，请在 UTC 次日或管理员调整预算后重试；本次未冻结积分。")
