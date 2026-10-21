"""服务端注册准入配置；默认兼容本机注册，部署可显式收紧。"""
from dataclasses import dataclass
import os


class RegistrationDenied(ValueError):
    pass


@dataclass(frozen=True)
class RegistrationPolicy:
    mode: str = "open"
    max_accounts: int = 0  # 0 表示未配置总量上限，已禁用账号仍占用名额。

    def __post_init__(self):
        if self.mode not in {"open", "invite", "closed"}:
            raise ValueError("注册模式必须为 open、invite 或 closed")
        if type(self.max_accounts) is not int or self.max_accounts < 0:
            raise ValueError("普通账号总量上限必须为非负整数")

    @classmethod
    def from_environment(cls):
        try:
            maximum = int(os.environ.get("IMAGE_WORKBENCH_MAX_ACCOUNTS", "0"))
        except ValueError as exc:
            raise ValueError("IMAGE_WORKBENCH_MAX_ACCOUNTS 必须为非负整数") from exc
        return cls(os.environ.get("IMAGE_WORKBENCH_REGISTRATION_MODE", "open"), maximum)

    def public(self):
        return {"mode": self.mode, "initial_credit": 200, "max_accounts": self.max_accounts or None}
