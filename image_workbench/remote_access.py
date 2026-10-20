"""云端个人工作台的访问配置；浏览器密码和执行器令牌分开使用。"""

import base64
from dataclasses import dataclass
from urllib.parse import urlsplit


def validate_origin(value):
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("服务器地址必须是完整的 http(s) 源站地址，不能带路径或账号")
    if url.scheme != "https" and url.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("远程连接必须使用 HTTPS；HTTP 仅允许本机联调或 SSH 隧道")
    return value.rstrip("/")


@dataclass(frozen=True)
class RemoteAccess:
    origin: str
    worker_token: str
    username: str = ""
    password: str = ""

    def __post_init__(self):
        object.__setattr__(self, "origin", validate_origin(self.origin))
        if len(self.worker_token) < 32 or not self.worker_token.isascii() or any(c.isspace() for c in self.worker_token):
            raise ValueError("执行器令牌至少32个 ASCII 字符，不能含空白")
        if (self.username or self.password) and (len(self.password) < 16 or not self.username or ":" in self.username):
            raise ValueError("网站密码至少16个字符，用户名不能为空或包含冒号")
        if self.password == self.worker_token:
            raise ValueError("网站密码和执行器令牌必须不同")
        if self.password.startswith("REPLACE_") or self.worker_token.startswith("REPLACE_"):
            raise ValueError("请先替换示例配置中的密码和令牌")

    @property
    def browser_authorization(self):
        return b"Basic " + base64.b64encode(f"{self.username}:{self.password}".encode())

    @property
    def worker_authorization(self):
        return b"Bearer " + self.worker_token.encode("ascii")

    @property
    def host(self):
        return urlsplit(self.origin).netloc.lower()
