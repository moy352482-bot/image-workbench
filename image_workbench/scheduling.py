"""领取时的执行名额策略；与排队容量、积分和上游账号额度分开。"""
from collections import Counter
from dataclasses import dataclass
import os


ACTIVE_STATUSES = frozenset({"running", "uncertain"})


@dataclass(frozen=True)
class SchedulingPolicy:
    max_active_total: int = 1
    max_active_per_account: int = 1

    def __post_init__(self):
        if any(type(value) is not int or not 1 <= value <= 32 for value in vars(self).values()):
            raise ValueError("执行并发上限必须为 1 到 32 的整数")

    @classmethod
    def from_environment(cls):
        names = ("IMAGE_WORKBENCH_MAX_ACTIVE_TOTAL", "IMAGE_WORKBENCH_MAX_ACTIVE_PER_ACCOUNT")
        try:
            return cls(*(int(os.environ.get(name, "1")) for name in names))
        except ValueError as exc:
            raise ValueError("执行并发配置必须为 1 到 32 的整数") from exc

    def effective_total(self, execution_mode):
        # 本地 Worker 与 TaskRunner 仍是单执行链路；不能只改配置就声称支持并行。
        return self.max_active_total if execution_mode == "remote" else 1

    @staticmethod
    def account(job):
        # 旧作业没有账号信息时共享同一桶，不允许靠缺失身份绕过每账号上限。
        return job.get("account_id") or None

    def _eligible(self, jobs, execution_mode):
        active = [job for job in jobs if job["status"] in ACTIVE_STATUSES]
        if len(active) >= self.effective_total(execution_mode):
            return []
        counts = Counter(self.account(job) for job in active)
        return [job for job in jobs if job["status"] == "queued"
                and counts[self.account(job)] < self.max_active_per_account]

    def choose(self, jobs, *, execution_mode="remote"):
        """调用方须持有 control_lock，完成选择和 running 写入后才能释放。"""
        return next(iter(self._eligible(jobs, execution_mode)), None)

    def snapshot(self, jobs, *, execution_mode="remote"):
        counts = Counter(job["status"] for job in jobs)
        active = sum(counts[status] for status in ACTIVE_STATUSES)
        total = self.effective_total(execution_mode)
        return {"configured_max_active_total": self.max_active_total,
                "max_active_total": total, "max_active_per_account": self.max_active_per_account,
                "queued": counts["queued"], "running": counts["running"], "uncertain": counts["uncertain"],
                "active": active, "available_slots": max(0, total - active),
                # 候选数按当前占用计算，不代表可同时启动的数量或在线执行器数。
                "eligible_queued": len(self._eligible(jobs, execution_mode))}
