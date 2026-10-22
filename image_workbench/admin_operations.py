"""管理员固定运维动作；后台进程执行，HTTP 不接收命令、路径或连接凭据。"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

from .locking import FileLock, LockBusyError
from .storage import StorageManager, _now, _validate_id
from .config import StorageConfig
from .domain_errors import ConflictError


SOURCE_ROOT = Path(__file__).resolve().parents[1]
RELEASE_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
ACTIVE = {"queued", "running"}
PUBLIC_OPERATION_FIELDS = {
    "id", "kind", "status", "created_at", "finished_at", "summary", "report_id",
    "release_id", "target", "source_fingerprint", "heartbeat_at",
}


def file_sha(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def source_fingerprint(root):
    """报告同时绑定运行代码与验收输入；不读取业务数据或部署凭据。"""
    root = Path(root).resolve()
    paths = []
    for directory, pattern in (("image_workbench", "*"), ("tests", "*.py"), ("scripts", "*.py"), ("scripts", "*.sh")):
        candidates = (root / directory).rglob(pattern) if directory == "image_workbench" else (root / directory).glob(pattern)
        for path in candidates:
            if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith(".") and path.suffix not in {".pyc", ".pyo"}:
                paths.append(path)
    paths.extend(path for path in (root / name for name in (
        "requirements.txt", "requirements-web.txt", "requirements-dev.txt", "ruff.toml",
        ".github/workflows/verify.yml")) if path.is_file())
    for directory in ("config", "skills"):
        paths.extend(path for path in (root / directory).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    paths.extend((root / "scripts").glob("*.ps1"))
    # Architecture rules, frozen HTTP contracts and JS behavior tests also
    # determine whether a report certifies this source revision.
    paths.extend(path for path in (root / "architecture.json",) if path.is_file())
    for folder, pattern in (("tests/contracts", "*.json"), ("tests/frontend", "*.js")):
        paths.extend((root / folder).glob(pattern))
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("程序目录包含不支持的链接")
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(file_sha(path).encode())
    return digest.hexdigest()


def parse_test_report(text, returncode):
    match = re.search(r"^Ran (\d+) tests? in ([0-9.]+)s", text, re.MULTILINE)
    total, duration = (int(match[1]), float(match[2])) if match else (0, 0.0)
    skipped_match = re.search(r"(?:^OK|^FAILED).*?skipped=(\d+)", text, re.MULTILINE)
    skipped = int(skipped_match[1]) if skipped_match else 0
    failures = sum(int(value) for value in re.findall(r"(?:failures|errors)=(\d+)", "\n".join(text.splitlines()[-5:])))
    valid = bool(match and total > 0 and re.search(r"^OK(?:\s|$)", text, re.MULTILINE) and returncode == 0)
    return {"total": total, "passed": max(0, total - failures - skipped), "failed": failures,
            "skipped": skipped, "duration_seconds": duration, "status": "passed" if valid else "failed"}


class AdminOperations:
    def __init__(self, storage, *, source_root=None):
        self.storage = storage
        self.source_root = Path(source_root or SOURCE_ROOT).resolve()
        self.root = storage._path(".workbench", "admin")
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("operations", "reports", "previews", "logs"):
            storage._path(".workbench", "admin", name).mkdir(exist_ok=True)
        self.loaded_at = _now()

    def path(self, category, identifier):
        _validate_id(identifier)
        return self.storage._path(".workbench", "admin", category, identifier + ".json")

    def write(self, category, value):
        self.storage._write_json(self.path(category, value["id"]), {"schema_version": 1, **value})

    def read(self, category, identifier):
        return self.storage._read_json(self.path(category, identifier))

    def control(self):
        return FileLock(self.storage._path(".workbench", "admin", "control.lock"), timeout=5)

    def records(self, category):
        values = [self.storage._read_json(path) for path in self.storage._path(".workbench", "admin", category).glob("*.json")]
        return sorted(values, key=lambda value: value["created_at"], reverse=True)

    @staticmethod
    def public_operation(value):
        result = {key: value.get(key) for key in PUBLIC_OPERATION_FIELDS}
        heartbeat = value.get("heartbeat_at", value.get("started_epoch", 0))
        if value["status"] in ACTIVE and heartbeat and time.time() - heartbeat > 90:
            result["status"] = "needs_attention"
            result["summary"] = "操作进程未更新状态，请先核实进程及目标环境；系统不会自动重放。"
        result["can_acknowledge"] = result["status"] == "needs_attention"
        return result

    def operations(self):
        return [self.public_operation(value) for value in self.records("operations")[:100]]

    def reports(self):
        current = source_fingerprint(self.source_root)
        return [{**value, "current_source": value["source_fingerprint"] == current} for value in self.records("reports")[:100]]

    def local_available(self):
        return all((self.source_root / name).exists() for name in (
            "scripts/deploy_release.py", "scripts/apply_release.sh", "scripts/verify.py", "tests/test_web.py"))

    def deploy_module(self):
        path = self.source_root / "scripts" / "deploy_release.py"
        spec = importlib.util.spec_from_file_location("_workbench_deploy", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def config_snapshot(self):
        path = self.source_root / "deploy" / "server.json"
        # 只接受服务器本机文件；浏览器没有写入配置或私钥的接口。
        if path.is_symlink() or not path.resolve().is_relative_to(self.source_root):
            raise ValueError("部署配置路径不受支持")
        raw = path.read_bytes()
        config = self.deploy_module().read_config(path)
        if path.read_bytes() != raw:
            raise ConflictError("部署配置发生变化，请刷新后重新确认")
        return config, hashlib.sha256(raw).hexdigest()

    def deployment_status(self):
        available = self.local_available()
        value = {"available": False, "package_available": available, "target": None,
                 "reason": "此安装不含测试与发布工具，请在开发主机管理部署。"}
        if not available:
            return value
        try:
            config, _ = self.config_snapshot()
            if not shutil.which("ssh") or not shutil.which("scp"):
                value["reason"] = "本机缺少 SSH / SCP 工具。"
            else:
                value.update(available=True, target=f"{config['user']}@{config['host']}:{config['port']}", reason="先准备已通过检查的发布包，再确认部署。")
        except (OSError, ValueError, json.JSONDecodeError):
            value["reason"] = "服务器连接配置尚未就绪，请在本机配置 deploy/server.json 与 SSH 密钥。"
        prepared = next((op for op in self.records("operations") if op["kind"] == "prepare" and op["status"] == "succeeded"), None)
        if prepared:
            value["release_id"] = prepared.get("release_id")
        return value

    def release_path(self, release_id):
        if not isinstance(release_id, str) or not RELEASE_PATTERN.fullmatch(release_id):
            raise ValueError("发布版本格式无效")
        parent = self.source_root / ".deploy"
        path = parent / release_id
        if parent.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(self.source_root):
            raise ValueError("发布目录不受支持")
        return path

    def verified_release(self, release_id):
        directory = self.release_path(release_id)
        metadata = json.loads((directory / "admin-prepared.json").read_text(encoding="utf-8"))
        if metadata["release_id"] != release_id or metadata["source_fingerprint"] != source_fingerprint(self.source_root):
            raise ConflictError("代码已更新，请重新检查并准备发布包")
        report = self.read("reports", metadata["report_id"])
        if report["status"] != "passed" or report["source_fingerprint"] != metadata["source_fingerprint"]:
            raise ConflictError("发布包没有对应的通过报告")
        for name, field in (("release.tar.gz", "archive_sha"), ("apply_release.sh", "script_sha"), ("manifest.json", "manifest_sha")):
            path = directory / name
            if path.is_symlink() or file_sha(path) != metadata[field]:
                raise ConflictError("发布包或清单已变化，请重新准备")
        return metadata

    def preview(self, release_id, account_id, session_key):
        if not self.deployment_status()["available"]:
            raise ConflictError("部署配置或本机发布工具尚未就绪")
        metadata = self.verified_release(release_id)
        config, config_sha = self.config_snapshot()
        token = secrets.token_urlsafe(32)
        target = f"{config['user']}@{config['host']}:{config['port']}"
        value = {"id": hashlib.sha256(token.encode()).hexdigest()[:32], "created_at": _now(), "expires_at": time.time() + 300,
                 "account_id": account_id, "session_key": session_key, "release_id": release_id, "target": target,
                 "config_sha": config_sha, "source_fingerprint": metadata["source_fingerprint"], "archive_sha": metadata["archive_sha"],
                 "script_sha": metadata["script_sha"], "manifest_sha": metadata["manifest_sha"], "used": False}
        with self.control():
            self.write("previews", value)
        return {"token": token, "release_id": release_id, "target": target, "expires_at": value["expires_at"],
                "summary": "部署此已检查版本并重启目标网站。业务图片与账号库不上传；启动失败会尝试代码回退。连接中断时需人工核实远端结果。"}

    def start(self, kind, account_id, *, session_key=None, token=None):
        if kind not in {"checks", "prepare", "deploy"} or not self.local_available():
            raise ConflictError("当前安装不支持此维护操作")
        with self.control():
            if any(op["status"] in ACTIVE | {"needs_attention"} for op in self.records("operations")):
                raise ConflictError("已有维护操作未结束，请等待或先核实其状态")
            preview = None
            if kind == "deploy":
                if not isinstance(token, str) or not 32 <= len(token) <= 128:
                    raise ValueError("请先预览并确认部署")
                preview = self.read("previews", hashlib.sha256(token.encode()).hexdigest()[:32])
                if preview["used"] or preview["expires_at"] < time.time() or (preview["account_id"], preview["session_key"]) != (account_id, session_key):
                    raise ConflictError("部署确认已失效，请重新预览")
                release = self.verified_release(preview["release_id"])
                _, config_sha = self.config_snapshot()
                if config_sha != preview["config_sha"] or any(release[key] != preview[key] for key in ("source_fingerprint", "archive_sha", "script_sha", "manifest_sha")):
                    raise ConflictError("代码、发布包或服务器配置已变化，请重新预览")
                preview["used"] = True
                self.write("previews", preview)
            op = {"id": uuid4().hex, "kind": kind, "account_id": account_id, "status": "queued", "created_at": _now(),
                  "started_epoch": time.time(), "heartbeat_at": time.time(), "summary": "等待后台维护进程启动", "finished_at": None,
                  "source_fingerprint": source_fingerprint(self.source_root)}
            if preview:
                op["approval"] = {key: preview[key] for key in ("release_id", "target", "config_sha", "source_fingerprint", "archive_sha", "script_sha", "manifest_sha")}
                op.update(release_id=preview["release_id"], target=preview["target"])
            self.write("operations", op)
            try:
                kwargs = {"cwd": self.source_root, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                else:
                    kwargs["start_new_session"] = True
                child = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "image_workbench.admin_operations", "--run", op["id"],
                                          "--data-root", str(self.storage.data_root), "--source-root", str(self.source_root)], **kwargs)
                op["runner_pid"] = child.pid
                self.write("operations", op)
            except OSError:
                op.update(status="failed", finished_at=_now(), summary="无法启动维护进程，请检查本机运行环境")
                self.write("operations", op)
                raise ConflictError(op["summary"]) from None
            return self.public_operation(op)

    def acknowledge(self, operation_id, account_id):
        """仅记载管理员主动核实；不推断远端结果，也不重放原操作。"""
        with self.control():
            op = self.read("operations", operation_id)
            if not self.public_operation(op)["can_acknowledge"]:
                raise ConflictError("该操作仍在执行或已经结束，不能解除记录")
            directory = self.source_root / ".deploy"
            directory.mkdir(exist_ok=True)
            try:
                with FileLock(directory / "admin-operation.lock"):
                    op.update(status="acknowledged", acknowledged_by=account_id, acknowledged_at=_now(), finished_at=_now(),
                              summary="管理员已核实并结束待确认记录；未重新执行原操作。")
                    self.write("operations", op)
            except LockBusyError:
                raise ConflictError("维护进程仍持有执行锁，请等待其结束后再核实") from None
            return self.public_operation(op)

    def execute_checks(self, op, *, prepare=False):
        baseline = source_fingerprint(self.source_root)
        if baseline != op["source_fingerprint"]:
            raise ConflictError("启动前代码已变化，请重新运行检查")
        module, directory, manifest = None, None, None
        if prepare:
            module = self.deploy_module()
            release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
            directory = self.release_path(release_id)
            directory.mkdir(parents=True)
            archive, manifest = module.package(self.source_root, directory, release_id)
            (directory / "apply_release.sh").write_bytes((self.source_root / "scripts" / "apply_release.sh").read_bytes().replace(b"\r\n", b"\n"))
        log_path = self.storage._path(".workbench", "admin", "logs", op["id"] + "-checks.log")
        with log_path.open("wb") as log:
            result = subprocess.run([sys.executable, "-X", "utf8", str(self.source_root / "scripts" / "verify.py")],
                                    cwd=self.source_root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=1200, check=False)
        output = log_path.read_text(encoding="utf-8", errors="replace")
        report = parse_test_report(output, result.returncode)
        failed_cases = [f"{kind}: {name}" for kind, name in re.findall(r"^(FAIL|ERROR): ([A-Za-z0-9_.]+)", output, re.MULTILINE)][:100]
        report.update(id=uuid4().hex, title="统一质量、架构与功能回归检查", created_at=_now(), source_fingerprint=baseline,
                      checks=[{"name": "Ruff、架构与功能/前端回归", "status": report["status"],
                               "summary": "；".join(failed_cases) or ("用例结果见统计" if report["total"] else "前置检查未通过，回归尚未完成")}])
        unchanged = source_fingerprint(self.source_root) == baseline
        if not unchanged:
            report["status"] = "stale"
        report["summary"] = (f"{report['total']} 项测试：{report['passed']} 通过、{report['failed']} 失败、{report['skipped']} 跳过。"
                             + ("检查期间代码发生变化，请重新检查。" if not unchanged else ""))
        self.write("reports", report)
        op["report_id"] = report["id"]
        if report["status"] != "passed":
            raise ConflictError("质量或回归检查未通过，或代码已变化；未部署任何版本")
        if prepare:
            if module.fingerprint(self.source_root) != manifest["files"]:
                raise ConflictError("发布程序已变化，请重新准备")
            (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            metadata = {"release_id": release_id, "report_id": report["id"], "source_fingerprint": baseline,
                        "archive_sha": file_sha(archive), "script_sha": file_sha(directory / "apply_release.sh"),
                        "manifest_sha": file_sha(directory / "manifest.json")}
            (directory / "admin-prepared.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            op["release_id"] = release_id
        op["summary"] = "检查通过，发布包已准备；尚未连接服务器。" if prepare else report["summary"]

    def execute_deploy(self, op):
        approval = op["approval"]
        release = self.verified_release(approval["release_id"])
        config, config_sha = self.config_snapshot()
        if config_sha != approval["config_sha"] or any(release[key] != approval[key] for key in ("source_fingerprint", "archive_sha", "script_sha", "manifest_sha")):
            raise ConflictError("确认后代码、发布包或目标配置变化，未开始部署")
        directory = self.release_path(approval["release_id"])
        # 已验证脚本的字节直接交给发布器，不能临时读取另一个脚本版本。
        script_bytes = (directory / "apply_release.sh").read_bytes()
        if hashlib.sha256(script_bytes).hexdigest() != approval["script_sha"]:
            raise ConflictError("发布脚本已变化，未开始部署")
        op["deployment_started"] = True
        self.write("operations", op)
        self.deploy_module().deploy(config, directory, directory / "release.tar.gz", approval["release_id"],
                                    script_bytes=script_bytes, expected_archive_sha=approval["archive_sha"])
        op["summary"] = "目标服务器已返回部署成功及健康检查通过。"

    def run(self, operation_id):
        # 先原子领取这一条记录；重复 runner 退出时不能写回旧 queued 载荷。
        with self.control():
            op = self.read("operations", operation_id)
            if op["status"] != "queued":
                return
            op.update(status="running", runner_pid=os.getpid(), heartbeat_at=time.time(), summary="正在执行维护检查" if op["kind"] != "deploy" else "正在更新目标服务器")
            self.write("operations", op)
        lock_directory = self.source_root / ".deploy"
        lock_directory.mkdir(exist_ok=True)
        stop_heartbeat = threading.Event()
        try:
            with FileLock(lock_directory / "admin-operation.lock"):
                def heartbeat():
                    while not stop_heartbeat.wait(10):
                        with self.control():
                            current = self.read("operations", operation_id)
                            current["heartbeat_at"] = time.time()
                            self.write("operations", current)

                thread = threading.Thread(target=heartbeat, daemon=True)
                thread.start()
                log_path = self.storage._path(".workbench", "admin", "logs", operation_id + ".log")
                with log_path.open("w", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
                    if op["kind"] == "deploy":
                        self.execute_deploy(op)
                    else:
                        self.execute_checks(op, prepare=op["kind"] == "prepare")
                op["status"] = "succeeded"
        except Exception as exc:
            op["status"] = "needs_attention" if op.get("deployment_started") else "failed"
            op["summary"] = ("部署未返回可确认结果，请核实远端当前版本和服务状态；不会自动重试。" if op.get("deployment_started")
                             else str(exc) if isinstance(exc, ConflictError) else "维护操作未完成，请检查本机维护日志。")
        finally:
            stop_heartbeat.set()
            if 'thread' in locals():
                thread.join(timeout=5)
            op.update(finished_at=_now(), heartbeat_at=time.time())
            with self.control():
                self.write("operations", op)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    operations = AdminOperations(StorageManager(StorageConfig(data_root=args.data_root)), source_root=args.source_root)
    operations.run(_validate_id(args.run))


if __name__ == "__main__":
    main()
