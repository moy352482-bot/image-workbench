"""Shared DeepSeek Flash chat adapter.

The adapter reads the existing ``deepseek_flash`` block from
``config/script_routing.json`` and its environment-variable indirection.  Script
classification and prompt enhancement use the same client; neither may choose a
task model, deduct credits or touch storage.
"""
import json
import os
from pathlib import Path
import socket
import urllib.error
import urllib.request

from .deepseek_text import ENDPOINT, MODEL, api_key

CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"
MAX_RESPONSE_BYTES = 256 * 1024


class FlashConfigError(ValueError):
    """The configured adapter cannot be used; ``reason`` is disabled/incomplete."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class FlashRequestError(ValueError):
    """The request failed; ``reason`` is timeout/empty/request_failed."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class DeepSeekFlashClient:
    def __init__(self, config_root=None):
        self.root = Path(config_root or CONFIG_ROOT)

    def settings(self):
        try:
            config = json.loads((self.root / "script_routing.json").read_text(encoding="utf-8"))["deepseek_flash"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FlashConfigError("incomplete") from exc
        if not config.get("enabled"):
            raise FlashConfigError("disabled")
        endpoint = os.environ.get(config.get("endpoint_env", ""), "").strip() or ENDPOINT
        model = os.environ.get(config.get("model_env", ""), "").strip() or MODEL
        try:
            key = os.environ.get(config.get("api_key_env", ""), "").strip() or api_key()
        except (OSError, ValueError, TypeError) as exc:
            raise FlashConfigError("incomplete") from exc
        if not endpoint.startswith("https://") or not model or not key:
            raise FlashConfigError("incomplete")
        return {"endpoint": endpoint, "model": model, "api_key": key,
                "timeout_seconds": float(config.get("timeout_seconds", 20))}

    def complete(self, *, system, user, max_tokens=300, temperature=0, timeout=None):
        settings = self.settings()
        payload = {"model": settings["model"], "temperature": temperature, "max_tokens": max_tokens,
                   "thinking": {"type": "disabled"}, "stream": False,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        request = urllib.request.Request(
            settings["endpoint"], data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + settings["api_key"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout or settings["timeout_seconds"]) as response:
                data = json.loads(response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
                raise FlashRequestError("timeout") from exc
            raise FlashRequestError("request_failed") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise FlashRequestError("timeout") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise FlashRequestError("request_failed") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FlashRequestError("request_failed") from exc
        if not isinstance(content, str) or not content.strip():
            raise FlashRequestError("empty")
        usage = data.get("usage")
        return {"content": content, "model": settings["model"], "usage": usage if isinstance(usage, dict) else {}}
