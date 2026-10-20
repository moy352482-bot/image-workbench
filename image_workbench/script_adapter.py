"""Text-only Codex adapter; shares executable, login environment and process cleanup."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from .codex_runtime import _codex_executable, _child_environment, _stop_process, _DISABLED_FEATURES


class ScriptGenerationError(ValueError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class CodexTextAdapter:
    def __init__(self, executable=None, timeout_seconds=600):
        self.executable = executable or os.environ.get("SCRIPT_CODEX_EXECUTABLE")
        self.timeout_seconds = timeout_seconds

    def command(self, work_dir, model_id, thinking_level):
        command = [_codex_executable(self.executable), "--no-daemon", "--ask-for-approval", "never", "exec",
                   "--ignore-user-config", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
                   "--json", "--color", "never", "--enable", "respect_system_proxy",
                   "--model", model_id, "-c", "model_reasoning_effort=" + json.dumps(thinking_level),
                   "-c", 'forced_login_method="chatgpt"', "-c", "mcp_servers={}",
                   "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0",
                   "--cd", str(work_dir), "--output-last-message", str(work_dir / "response.txt")]
        for feature in (*_DISABLED_FEATURES, "image_generation"):
            command.extend(["--disable", feature])
        return [*command, "-"]

    def generate(self, *, prompt, model_id, thinking_level, work_dir):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        response = work_dir / "response.txt"
        if response.exists():
            raise ScriptGenerationError("已有执行产物，不能重新调用模型", uncertain=True)
        process = None
        try:
            command = self.command(work_dir, model_id, thinking_level)
            (work_dir / "command.json").write_text(json.dumps({"argv": command}, ensure_ascii=False, indent=2), encoding="utf-8")
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            with (work_dir / "stdout.jsonl").open("ab") as stdout, (work_dir / "stderr.log").open("ab") as stderr:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, cwd=work_dir,
                                           env=_child_environment(), shell=False, **flags)
                (work_dir / "process.json").write_text(json.dumps({"pid": process.pid, "started_at_unix": time.time()}), encoding="utf-8")
                process.communicate(prompt.encode("utf-8"), timeout=self.timeout_seconds)
            if process.returncode != 0:
                raise ScriptGenerationError("Codex 未完成生成，冻结积分已释放；可查看任务日志")
            if response.is_symlink() or not response.is_file() or response.stat().st_size > 256 * 1024:
                raise ScriptGenerationError("Codex 未返回有效的 TXT 正文")
            data = response.read_bytes()
            text = data.decode("utf-8")
            receipt = {"model_id": model_id, "thinking_level": thinking_level, "returncode": 0,
                       "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                       "response_sha256": hashlib.sha256(data).hexdigest(), "response_size_bytes": len(data),
                       "completed_at_unix": time.time()}
            temporary = work_dir / (".completed-" + uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="") as stream:
                    json.dump(receipt, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, work_dir / "completed.json")
            finally:
                temporary.unlink(missing_ok=True)
            return text
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            raise ScriptGenerationError("Codex 执行超时；不会自动重试", uncertain=process.poll() is None) from exc
        except ScriptGenerationError:
            raise
        except (OSError, ValueError) as exc:
            if process and process.poll() is None:
                _stop_process(process)
            raise ScriptGenerationError("无法完成 Codex 文本调用，请检查执行机安装、登录和任务日志", uncertain=bool(process and process.poll() is None)) from exc
        except BaseException:
            if process and process.poll() is None:
                _stop_process(process)
            raise

    def recover(self, *, prompt, model_id, thinking_level, work_dir):
        """Recover only a verified successful invocation; never launch a process."""
        work_dir = Path(work_dir)
        receipt_file, response_file = work_dir / "completed.json", work_dir / "response.txt"
        if receipt_file.is_symlink() or response_file.is_symlink():
            raise ScriptGenerationError("执行产物路径无效", uncertain=True)
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
        expected = {"model_id": model_id, "thinking_level": thinking_level, "returncode": 0,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
        if any(receipt.get(k) != v for k, v in expected.items()) or response_file.stat().st_size > 256 * 1024:
            raise ScriptGenerationError("执行完成凭据不匹配", uncertain=True)
        data = response_file.read_bytes()
        if len(data) != receipt.get("response_size_bytes") or hashlib.sha256(data).hexdigest() != receipt.get("response_sha256"):
            raise ScriptGenerationError("执行正文与完成凭据不匹配", uncertain=True)
        return data.decode("utf-8")
