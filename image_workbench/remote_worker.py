"""本机主动领取云端任务；持久检查点确保网络重试只重传，不重跑模型。"""

import hashlib
from pathlib import Path
import threading
import time
from uuid import uuid4
from urllib.parse import urlsplit

import httpx

from .config import StorageConfig
from .image_backend import BackendError, BackendRequest
from .locking import FileLock
from .remote_access import validate_origin
from .atomic_files import atomic_bytes
from .storage import StorageManager, _validate_id


class RemoteWorker:
    def __init__(self, server, token, cache_root, backend, *, timeout=600, client=None):
        self.server = validate_origin(server)
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("执行器令牌至少32字符")
        if token.startswith("REPLACE_"):
            raise ValueError("请先替换示例配置中的执行器令牌")
        self.storage = StorageManager(StorageConfig(data_root=cache_root))
        self.files = self.storage.files.scope(".remote-worker")
        self.root = self.files.root
        self.root.mkdir(exist_ok=True)
        self.backend, self.timeout = backend, timeout
        self.client = client or httpx.Client(base_url=self.server, timeout=30, follow_redirects=False,
            trust_env=urlsplit(self.server).hostname not in {"localhost", "127.0.0.1"})
        self.headers = {"Authorization": "Bearer " + token}
        identity_path = self.root / "identity.json"
        with FileLock(self.root / "client.lock"):
            if identity_path.exists():
                identity = self.files.read_json(identity_path)
                if identity["server"] != self.server or identity["backend"] != backend.name:
                    raise ValueError("此缓存属于其他服务器或后端，请使用独立缓存目录")
            else:
                identity = {"schema_version": 1, "server": self.server, "backend": backend.name, "worker_id": uuid4().hex}
                self.files.write_json(identity_path, identity)
        self.worker_id = _validate_id(identity["worker_id"])
        self.last_job_id = None

    def close(self):
        self.client.close()

    def _request(self, method, path, manifest=None, **kwargs):
        headers = dict(self.headers)
        if manifest:
            headers.update({"X-Worker-ID": self.worker_id, "X-Claim-ID": manifest["claim_id"]})
        response = self.client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    def _path(self, job_id):
        return self.files.check(self.root / _validate_id(job_id))

    def _save(self, journal):
        self.files.write_json(self._path(journal["manifest"]["job_id"]) / "journal.json", journal)

    def _download(self, manifest, asset, directory):
        aid = _validate_id(asset["artifact_id"])
        extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[asset["format"]]
        path = self.files.check(directory / (aid + extension))
        limit = min(manifest["max_image_bytes"], self.storage.config.max_image_bytes)
        if not 0 < asset["size_bytes"] <= limit:
            raise ValueError("参考图片超过本机限制")
        if not path.exists():
            headers = {**self.headers, "X-Worker-ID": self.worker_id, "X-Claim-ID": manifest["claim_id"]}
            # 固定相对路径，不采用服务端提供的任意下载 URL；不跟随重定向泄漏令牌。
            with self.client.stream("GET", f"/api/worker/jobs/{manifest['job_id']}/assets/{aid}", headers=headers) as response:
                response.raise_for_status()
                data = bytearray()
                for block in response.iter_bytes(65536):
                    data.extend(block)
                    if len(data) > limit:
                        raise ValueError("下载图片超过限制")
            if len(data) != asset["size_bytes"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError("参考图片校验失败")
            atomic_bytes(self.storage, path, data)
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            raise ValueError("本机参考图片校验失败")
        self.storage.inspect_image(path)
        return path

    def _execute(self, journal):
        manifest = journal["manifest"]
        if manifest["action"] == "restore":
            journal.update(phase="result", result={"status": "succeeded", "hashes": []}, outputs=[])
            self._save(journal)
            return
        directory = self._path(manifest["job_id"])
        references = self.files.check(directory / "references")
        references.mkdir(exist_ok=True)
        self.files.check(directory / "products").mkdir(exist_ok=True)
        task = self.storage.decode_task(manifest["task"])
        inputs = tuple(self._download(manifest, asset, references) for asset in manifest["inputs"])
        base = self._download(manifest, manifest["base"], references) if manifest["base"] else None
        if (task.operation == "edit") != (base is not None):
            raise ValueError("编辑基底不完整")
        request = BackendRequest(manifest["job_id"], task, directory, inputs, base,
            directory / "stdout.log", directory / "stderr.log", self.timeout,
            min(manifest["max_image_bytes"], self.storage.config.max_image_bytes))
        if journal["phase"] == "ready":
            journal.update(phase="starting", attempt_id=uuid4().hex)
            self._save(journal)
        self._request("POST", f"/api/worker/jobs/{manifest['job_id']}/start", manifest,
                      json={"attempt_id": journal["attempt_id"]})
        # 必须先落盘再执行；这里之后的崩溃一律不自动重新生成。
        journal["phase"] = "started"
        self._save(journal)
        try:
            result = self.backend.generate(request)
            if result.returncode != 0 or not 1 <= len(result.image_paths) <= 32:
                raise BackendError("没有合法图片产物", status="invalid_artifact")
            paths, hashes = [], []
            for raw in result.image_paths:
                path = self.files.check(Path(raw))
                if not path.is_relative_to(directory / "products") or path in paths:
                    raise BackendError("产物不属于当前作业", status="invalid_artifact")
                self.storage.inspect_image(path)
                paths.append(path)
                hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
            journal.update(phase="result", result={"status": "succeeded", "hashes": hashes},
                           outputs=[p.relative_to(self.root).as_posix() for p in paths])
        except BackendError as exc:
            failure = exc.failure_type or {"timed_out": "codex_timeout", "missing_artifact": "artifact_not_found",
                "invalid_artifact": "artifact_invalid", "archive_failed": "register_failed",
                "interrupted": "interrupted"}.get(exc.status, "codex_execution_failed")
            journal.update(phase="result", result={"status": "failed", "failure_type": failure}, outputs=[])
        except (Exception, KeyboardInterrupt):
            journal.update(phase="uncertain", result={"status": "uncertain", "failure_type": "interrupted"}, outputs=[])
        self._save(journal)

    def _deliver(self, journal):
        manifest = journal["manifest"]
        base = f"/api/worker/jobs/{manifest['job_id']}"
        for index, relative in enumerate(journal.get("outputs", [])):
            path = self.files.check(self.root / relative)
            if not path.is_relative_to(self._path(manifest["job_id"]) / "products"):
                raise ValueError("回传路径越界")
            data = path.read_bytes()
            digest = journal["result"]["hashes"][index]
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("待回传图片已改变，不会重新生成")
            received = self._request("PUT", f"{base}/outputs/{index}", manifest,
                                     content=data, timeout=120).json()
            if received["sha256"] != digest:
                raise ValueError("云端产物校验失败")
        response = self._request("POST", base + "/complete", manifest, json=journal["result"], timeout=120).json()
        if response["status"] in {"completed", "failed"}:
            journal["phase"] = "done"
            self._save(journal)
        return response["status"]

    def run_once(self):
        with FileLock(self.root / "client.lock"):
            pending = []
            for directory in sorted(self.root.iterdir()):
                self.files.check(directory)
                if directory.is_dir() and (directory / "journal.json").exists():
                    journal = self.files.read_json(directory / "journal.json")
                    if journal["phase"] != "done":
                        pending.append(journal)
            if pending:
                journal = pending[0]
            else:
                manifest = self._request("POST", "/api/worker/claim",
                    json={"worker_id": self.worker_id, "backend": self.backend.name}).json()["job"]
                if manifest is None:
                    return "idle"
                _validate_id(manifest["claim_id"])
                directory = self._path(manifest["job_id"])
                directory.mkdir(exist_ok=True)
                journal = {"schema_version": 1, "phase": "ready", "manifest": manifest}
                self._save(journal)
            manifest = journal["manifest"]
            self.last_job_id = manifest["job_id"]
            base = f"/api/worker/jobs/{manifest['job_id']}"
            self._request("POST", base + "/heartbeat", manifest)
            stop = threading.Event()
            def pulse():
                while not stop.wait(15):
                    try:
                        self._request("POST", base + "/heartbeat", manifest)
                    except (httpx.HTTPError, OSError):
                        pass  # 断网仍保存已经产生的结果，稍后重传。
            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            try:
                if journal["phase"] == "started":
                    journal.update(phase="uncertain", result={"status": "uncertain", "failure_type": "interrupted"}, outputs=[])
                    self._save(journal)
                if journal["phase"] in {"ready", "starting"}:
                    try:
                        self._execute(journal)
                    except ValueError:
                        if journal["phase"] not in {"ready", "starting"}:
                            raise
                        journal.update(phase="result", result={"status": "failed", "failure_type": "prepare_failed"}, outputs=[])
                        self._save(journal)
                return self._deliver(journal)
            finally:
                stop.set()
                thread.join(timeout=35)

    def confirm_stopped(self, job_id):
        with FileLock(self.root / "client.lock"):
            journal = self.files.read_json(self._path(job_id) / "journal.json")
            if journal["phase"] not in {"started", "uncertain"}:
                raise ValueError("只能解除状态待确认任务")
            journal.update(phase="result", result={"status": "failed", "failure_type": "interrupted"}, outputs=[])
            self._save(journal)

    def serve(self, *, once=False, poll_seconds=5):
        last_report = None
        while True:
            try:
                status = self.run_once()
                report = (self.last_job_id, status)
                if status != "idle" and report != last_report:
                    print(f"云端任务 {self.last_job_id} 状态：{status}", flush=True)
                last_report = report
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code < 500 and code not in {408, 429}:
                    raise RuntimeError(f"服务器拒绝请求（HTTP {code}）；检查令牌、后端模式或任务状态。缓存已保留。") from None
                if once:
                    raise RuntimeError(f"服务器暂不可用（HTTP {code}）；任务已保留") from None
                print(f"服务器暂不可用（HTTP {code}），稍后重连。", flush=True)
            except (httpx.RequestError, OSError):
                if once:
                    raise RuntimeError("网络或存储暂不可用，任务检查点已保留") from None
                print("网络或存储暂不可用，保留检查点并稍后重连。", flush=True)
            if once:
                return
            time.sleep(poll_seconds)
