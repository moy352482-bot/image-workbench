"""Codex executable, environment and process lifecycle shared by text and image adapters."""
import os
from pathlib import Path
import shutil
import signal
import subprocess

_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "hooks", "plugins", "apps", "multi_agent",
    "computer_use", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
)


def _codex_executable(value: str | Path | None = None) -> str:
    requested = str(value) if value is not None else ("codex.exe" if os.name == "nt" else "codex")
    found = shutil.which(requested)
    path = Path(found or requested).expanduser().absolute()
    if not path.is_file():
        raise FileNotFoundError("找不到本地 Codex CLI，请确认 codex.exe/codex 位于 PATH")
    if path.suffix.lower() in {".bat", ".cmd", ".ps1"}:
        raise ValueError("请使用实际 Codex 可执行文件，不能使用 shell 脚本包装器")
    return str(path)


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
        env.pop(key, None)
    return env


def _stop_process(process: subprocess.Popen) -> str:
    """只终止本次启动的进程树/进程组，不操作其他 Codex 会话。"""
    if process.poll() is not None:
        return "进程已退出"
    detail = "已请求终止本次进程树"
    try:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            result = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, shell=False, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                detail = "进程树终止命令失败，已尝试终止直接子进程"
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = f"进程树终止遇到 {type(exc).__name__}，已尝试终止直接子进程"
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail += f"；无法确认进程退出（{type(exc).__name__}），请检查 process.json 中的 PID"
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
    return detail


