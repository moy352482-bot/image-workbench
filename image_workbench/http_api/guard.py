"""Origin, authentication envelope, CSRF and body-size guard."""
import re
import secrets
from fastapi.responses import JSONResponse
from ..contracts.images import MAX_AUTH_BYTES, MAX_UPLOAD_BYTES

class LocalRequestGuard:
    """拒绝非本机 Host、跨站写请求；读取完整请求前限制实际字节数。"""

    def __init__(self, app, *, csrf_token, remote_access=None, account_mode=False):
        self.app, self.csrf_token, self.remote_access = app, csrf_token, remote_access
        self.account_mode = account_mode

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def guarded_send(event):
            if event["type"] == "http.response.start":
                event["headers"] = [*event.get("headers", []),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")]
                if scope["path"].startswith("/api/"):
                    event["headers"].append((b"cache-control", b"no-store"))
            await send(event)

        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin1").lower()
        origin = headers.get(b"origin", b"").decode("latin1")
        remote = self.remote_access
        worker_request = bool(remote and scope["path"].startswith("/api/worker/"))
        if remote and (worker_request or not self.account_mode):
            expected = remote.worker_authorization if worker_request else remote.browser_authorization
            if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
                challenge = {} if worker_request else {"WWW-Authenticate": 'Basic realm="Image Workbench", charset="UTF-8"'}
                await JSONResponse({"detail": "请先登录。"}, status_code=401, headers=challenge)(scope, receive, guarded_send)
                return
        error, code = None, 403
        if remote and host != remote.host:
            error = "访问地址与配置的源站不一致。"
        elif not remote and not re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?", host):
            error = "仅允许通过本机 localhost 访问。"
        elif origin and origin != (remote.origin if remote else "http://" + host):
            error = "不接受跨站请求，请从本地工作台操作。"
        elif not worker_request and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(headers.get(b"x-workbench-csrf", b""), scope.get("account_csrf", self.csrf_token).encode()):
                error = "页面校验已失效，请刷新后重新操作。"
            elif headers.get(b"sec-fetch-site") == b"cross-site":
                error = "不接受跨站请求，请从本地工作台操作。"
        if error:
            await JSONResponse({"detail": error}, status_code=code)(scope, receive, guarded_send)
            return
        if self.account_mode and not worker_request and not scope.get("account_session") and not scope.get("account_public"):
            # AccountApplication can reject an anonymous request without consuming
            # attacker-controlled bodies. Public auth routes still have size limits.
            await self.app(scope, receive, guarded_send)
            return
        auth_request = scope["path"] in {"/api/auth/login", "/api/auth/register", "/api/auth/activate"}
        if scope["method"] in {"GET", "HEAD", "OPTIONS"} or (self.account_mode and scope.get("account_public") and not auth_request):
            # These routes never consume a request body. Do not allocate an upload
            # buffer for image downloads, static files or other read-only requests.
            await self.app(scope, receive, guarded_send)
            return
        admin_request = scope["path"].startswith("/api/admin/")
        if admin_request and self.account_mode and not scope.get("account_session", {}).get("is_admin"):
            await self.app(scope, receive, guarded_send)
            return
        limit = MAX_AUTH_BYTES if auth_request or admin_request else MAX_UPLOAD_BYTES + 1024 * 1024
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = limit + 1
        chunks, size = [], 0
        if declared <= limit:
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                chunk = event.get("body", b"")
                size += len(chunk)
                if size > limit:
                    break
                chunks.append(chunk)
                if not event.get("more_body", False):
                    break
        if declared > limit or size > limit:
            message = "管理或账号请求不能超过 16 KiB。" if auth_request or admin_request else "上传总大小不能超过40 MB。"
            await JSONResponse({"detail": message}, status_code=413)(scope, receive, guarded_send)
            return
        body = b"".join(chunks)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, guarded_send)
