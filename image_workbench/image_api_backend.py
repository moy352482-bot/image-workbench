"""图片 HTTP Provider；只产生候选文件，队列、积分和归档由原有服务维护。"""

import base64
import binascii
from contextlib import nullcontext
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx

from .image_backend import BackendError, BackendResult


class ImageAPIBackend:
    name = "image-api"

    def __init__(self, base_url, api_key, *, model="gpt-image-2", client=None, max_attempts=3):
        if not isinstance(base_url, str):
            raise ValueError("请配置图片 API 的实际服务地址")
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.hostname == "your-domain.com"):
            raise ValueError("图片 API 地址必须是实际 HTTP/HTTPS 地址，且不能含凭据、查询或片段")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程图片 API 必须使用 HTTPS")
        if not isinstance(api_key, str) or not api_key.strip() or not api_key.isascii() or any(c.isspace() for c in api_key):
            raise ValueError("请配置有效的图片 API Key")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("图片 API model 不能为空")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("图片 API 总尝试次数必须为 1 到 3")
        self.base_url = base_url.rstrip("/")
        # 同时接受站点根地址及常见的 /v1 基础地址。
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.api_key, self.model, self.client = api_key, model, client
        self.max_attempts = max_attempts

    @classmethod
    def from_environment(cls):
        config = {}
        path = os.environ.get("IMAGE_WORKBENCH_IMAGE_API_CONFIG")
        if path:
            try:
                config = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                raise ValueError("图片 API 私密配置文件无法读取或格式错误") from None
            if not isinstance(config, dict) or set(config) - {"base_url", "api_key", "model"}:
                raise ValueError("图片 API 配置只接受 base_url、api_key、model")
        return cls(
            os.environ.get("IMAGE_WORKBENCH_IMAGE_API_BASE_URL", config.get("base_url", "")),
            os.environ.get("IMAGE_WORKBENCH_IMAGE_API_KEY", config.get("api_key", "")),
            model=os.environ.get("IMAGE_WORKBENCH_IMAGE_API_MODEL", config.get("model", "gpt-image-2")),
        )

    def _write_diagnostic(self, request, filename, value):
        text = json.dumps(value, ensure_ascii=False, indent=2).replace(self.api_key, "[REDACTED]")
        (request.work_dir / filename).write_text(text + "\n", encoding="utf-8")

    def _request(self, request, endpoint, content):
        started, attempts = time.monotonic(), []
        client_context = nullcontext(self.client) if self.client is not None else httpx.Client(follow_redirects=False)
        with client_context as client:
            for attempt in range(1, self.max_attempts + 1):
                attempt_started = time.monotonic()
                item = {"attempt": attempt, "status_code": None, "error": None, "usage": None}
                problem, retryable, retry_after, result = None, False, None, None
                try:
                    remaining = request.timeout_seconds - (attempt_started - started)
                    if remaining <= 0:
                        raise httpx.ReadTimeout("image API deadline")
                    timeout = httpx.Timeout(remaining, connect=min(15, remaining))
                    with client.stream("POST", self.base_url + endpoint, content=content,
                                       headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                                       timeout=timeout, follow_redirects=False) as response:
                        item["status_code"] = response.status_code
                        retry_after = response.headers.get("Retry-After")
                        limit = ((request.max_image_bytes + 2) // 3) * 4 + 65536 if response.is_success else 65536
                        body = bytearray()
                        for block in response.iter_bytes(65536):
                            if time.monotonic() - started > request.timeout_seconds:
                                raise httpx.ReadTimeout("image API deadline")
                            body.extend(block)
                            if len(body) > limit:
                                raise BackendError("图片 API 响应超过大小限制", status="invalid_artifact", failure_type="artifact_invalid")
                    try:
                        result = json.loads(body)
                    except (ValueError, UnicodeError):
                        result = None
                    item["usage"] = result.get("usage") if isinstance(result, dict) else None
                    item["error"] = result.get("error") if isinstance(result, dict) else "响应不是 JSON"
                    if not 200 <= item["status_code"] < 300:
                        error = item["error"]
                        rejected = isinstance(error, dict) and error.get("code") == "content_policy_violation"
                        retryable = rejected or item["status_code"] in {429, 500, 502, 503, 504}
                        problem = BackendError("图片供应方的内容审核拒绝了本次生成，请调整需求后新建任务。",
                            failure_type="image_api_content_rejected") if rejected else BackendError(
                            f"图片 API 请求失败（HTTP {item['status_code']}），详见 api-response.json", failure_type="image_api_failed")
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    item["error"] = "未能建立图片 API 连接"
                    problem, retryable = BackendError("图片 API 连接失败", failure_type="image_api_failed"), True
                except httpx.TimeoutException:
                    item["error"] = "图片 API 请求超时；上游完成状态未知，不自动重试"
                    problem = BackendError("图片 API 请求超时；不会自动重新生成", status="timed_out", failure_type="image_api_timeout")
                except httpx.RequestError:
                    item["error"] = "图片 API 传输中断；上游完成状态未知，不自动重试"
                    problem = BackendError("图片 API 传输中断；不会自动重新生成", failure_type="image_api_failed")
                except BackendError as error:
                    item["error"], problem = str(error), error
                item["seconds"] = round(time.monotonic() - attempt_started, 3)
                attempts.append(item)
                delay = 2 if attempt == 1 else 5
                if retry_after is not None:
                    try:
                        supplied_delay = float(retry_after)
                        if math.isfinite(supplied_delay):
                            delay = max(delay, supplied_delay)
                    except ValueError:
                        # 无法识别的服务器等待要求不提前重试。
                        retryable = False
                retry = (problem is not None and retryable and attempt < self.max_attempts
                         and delay < request.timeout_seconds - (time.monotonic() - started))
                if retry:
                    item["retry_delay_seconds"] = delay
                elapsed = round(time.monotonic() - started, 3)
                self._write_diagnostic(request, "api-response.json", {
                    "status_code": item["status_code"], "seconds": elapsed, "usage": item["usage"],
                    "error": item["error"], "attempt_count": len(attempts), "attempts": attempts,
                })
                if problem is None:
                    return result, item["status_code"], elapsed
                if not retry:
                    raise problem from None
                with request.stdout_path.open("a", encoding="utf-8") as log:
                    log.write(f"图片 API 第 {attempt} 次请求未成功，{delay:g} 秒后以相同内容重试。\n")
                time.sleep(delay)

    def generate(self, request):
        references = ([request.edit_image] if request.edit_image else []) + list(request.reference_images)
        if request.task.operation == "edit" and not request.edit_image:
            raise BackendError("图片编辑缺少基底图", failure_type="prepare_failed")
        endpoint = "/v1/images/edits" if references else "/v1/images/generations"
        payload = {"model": self.model, "prompt": request.task.prompt, "n": 1,
                   "response_format": "b64_json", "quality": "auto", "stream": False}
        sizes = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}
        if request.aspect_ratio != "auto":
            if request.aspect_ratio not in sizes:
                raise BackendError("图片比例无效", failure_type="prepare_failed")
            payload["size"] = sizes[request.aspect_ratio]
        if references:
            images = []
            for path in references:
                if not 0 < path.stat().st_size <= min(request.max_image_bytes, 50 * 1024 * 1024):
                    raise BackendError("参考图片大小超出限制", failure_type="prepare_failed")
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                images.append({"image_url": f"data:{mime_type};base64,{encoded}"})
            payload["images"] = images
        else:
            payload["history_disabled"] = True
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._write_diagnostic(request, "api-request.json", {
            "execution_id": request.execution_id, "endpoint": endpoint, "model": self.model,
            "operation": request.task.operation, "reference_count": len(references),
            "size": payload.get("size", "auto"), "response_format": "b64_json", "n": 1,
            "max_attempts": self.max_attempts, "request_sha256": hashlib.sha256(content).hexdigest(),
        })
        result, response_status, elapsed = self._request(request, endpoint, content)
        entries = result.get("data") if isinstance(result, dict) else None
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise BackendError("图片 API 未返回本次请求的一张图片", status="missing_artifact",
                               failure_type="artifact_not_found")
        encoded = entries[0].get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            raise BackendError("图片 API 没有按约定返回 b64_json 图片", status="missing_artifact",
                               failure_type="artifact_not_found")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise BackendError("图片 API 返回无效 Base64", status="invalid_artifact",
                               failure_type="artifact_invalid") from None
        if not data or len(data) > request.max_image_bytes:
            raise BackendError("图片 API 产物大小无效", status="invalid_artifact", failure_type="artifact_invalid")
        suffix = ".jpg" if data.startswith(b"\xff\xd8\xff") else ".webp" if data.startswith(b"RIFF") else ".png"
        path = request.work_dir / "products" / ("image-0001" + suffix)
        with path.open("xb") as stream:
            stream.write(data)
        with request.stdout_path.open("a", encoding="utf-8") as log:
            log.write(f"图片 API {endpoint}: HTTP {response_status}; {elapsed}s; 1 个候选产物\n")
        return BackendResult((path,))
