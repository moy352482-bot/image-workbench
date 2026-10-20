"""可选的运营容量预算；与用户可用积分分开。"""
from dataclasses import dataclass
from datetime import datetime, timezone
import os


class CapacityExceeded(ValueError):
    pass


@dataclass(frozen=True)
class CapacityPolicy:
    max_pending_per_account: int = 0
    max_pending_total: int = 0
    max_daily_submitted_credit: int = 0

    def __post_init__(self):
        if any(type(value) is not int or value < 0 for value in vars(self).values()):
            raise ValueError("运营容量上限必须为非负整数")

    @classmethod
    def from_environment(cls):
        names = ("IMAGE_WORKBENCH_MAX_PENDING_PER_ACCOUNT", "IMAGE_WORKBENCH_MAX_PENDING_TOTAL", "IMAGE_WORKBENCH_MAX_DAILY_SUBMITTED_CREDIT")
        try:
            return cls(*(int(os.environ.get(name, "0")) for name in names))
        except ValueError as exc:
            raise ValueError("运营容量配置必须为非负整数") from exc

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
