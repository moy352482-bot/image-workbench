"""DeepSeek Flash text provider with durable, verifiable completion receipts."""
import hashlib
import os
from pathlib import Path
import time

import httpx

from .atomic_files import read_json, write_bytes, write_json
from .domain_errors import TextGenerationError


MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
EFFORTS = ("low", "high", "max")
MAX_RESPONSE_BYTES = 256 * 1024


def api_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key and os.environ.get("IMAGE_WORKBENCH_TEXT_API_CONFIG"):
        config = read_json(Path(os.environ["IMAGE_WORKBENCH_TEXT_API_CONFIG"]).expanduser())
        key = str(config.get("api_key", "")).strip()
    return key


class DeepSeekTextAdapter:
    def __init__(self, key=None, *, client=None, timeout_seconds=600):
        self.key = api_key() if key is None else key
        self.client = client
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _intent(prompt, model_id, thinking_level):
        if model_id != MODEL or thinking_level not in EFFORTS:
            raise TextGenerationError("DeepSeek Flash 模型或思考强度无效")
        return {"provider": "deepseek", "model_id": model_id, "thinking_level": thinking_level,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}

    def generate(self, *, prompt, model_id, thinking_level, work_dir):
        intent = self._intent(prompt, model_id, thinking_level)
        if not self.key:
            raise TextGenerationError("DeepSeek API Key 尚未配置")
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        marker = work / "request.json"
        if marker.exists() or (work / "completed.json").exists() or (work / "response.txt").exists():
            raise TextGenerationError("已有文本调用记录，请恢复原任务；不会重复调用", uncertain=True)
        write_json(marker, intent)
        payload = {"model": model_id, "reasoning_effort": thinking_level, "max_tokens": 32768,
                   "stream": False, "messages": [{"role": "user", "content": prompt}]}
        client = self.client or httpx.Client(timeout=self.timeout_seconds, follow_redirects=False)
        try:
            response = client.post(ENDPOINT, headers={"Authorization": "Bearer " + self.key}, json=payload)
            if response.status_code != 200:
                uncertain = response.status_code >= 500 or response.status_code in {408, 409}
                raise TextGenerationError(f"DeepSeek 文本调用未完成（HTTP {response.status_code}）", uncertain=uncertain)
            try:
                body = response.json()
                choice = body["choices"][0]
                text = choice["message"]["content"]
                if choice.get("finish_reason") != "stop" or not isinstance(text, str) or not text.strip():
                    raise ValueError("incomplete response")
                data = text.encode("utf-8")
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("oversized response")
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                raise TextGenerationError("DeepSeek 未返回完整有效的文本，不会自动重试") from exc
            write_bytes(work / "response.txt", data)
            write_json(work / "completed.json", {**intent, "returncode": 0,
                       "response_sha256": hashlib.sha256(data).hexdigest(), "response_size_bytes": len(data),
                       "provider_request_id": body.get("id"), "usage": body.get("usage"),
                       "completed_at_unix": time.time()})
            return text
        except httpx.HTTPError as exc:
            raise TextGenerationError("DeepSeek 连接中断，调用结果待确认；不会自动重试", uncertain=True) from exc
        except OSError as exc:
            raise TextGenerationError("文本回执保存未完成，请恢复原任务", uncertain=True) from exc
        finally:
            if self.client is None:
                client.close()

    def recover(self, *, prompt, model_id, thinking_level, work_dir):
        expected = self._intent(prompt, model_id, thinking_level)
        work = Path(work_dir)
        receipt_file, response_file = work / "completed.json", work / "response.txt"
        if receipt_file.is_symlink() or response_file.is_symlink():
            raise TextGenerationError("文本回执路径无效", uncertain=True)
        try:
            receipt = read_json(receipt_file)
            if any(receipt.get(k) != v for k, v in expected.items()) or receipt.get("returncode") != 0:
                raise ValueError("receipt mismatch")
            if response_file.stat().st_size > MAX_RESPONSE_BYTES:
                raise ValueError("oversized response")
            data = response_file.read_bytes()
            if (len(data) != receipt.get("response_size_bytes")
                    or hashlib.sha256(data).hexdigest() != receipt.get("response_sha256")):
                raise ValueError("response mismatch")
            return data.decode("utf-8")
        except (OSError, ValueError, TypeError) as exc:
            raise TextGenerationError("文本完成凭据尚未确认；不会重新调用 DeepSeek", uncertain=True) from exc
