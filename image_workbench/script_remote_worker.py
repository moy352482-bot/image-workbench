"""Run cloud script jobs using this computer's Codex login, with durable delivery receipts."""
import argparse
import hashlib
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import httpx

from .config import StorageConfig
from .locking import FileLock
from .remote_access import validate_origin
from .script_adapter import CodexTextAdapter, ScriptGenerationError
from .script_models import ScriptModelRegistry
from .script_templates import validate_generated_text
from .storage import StorageManager, _validate_id


class ScriptRemoteWorker:
    def __init__(self, server, token, cache_root, *, adapter=None, registry=None, client=None):
        self.server = validate_origin(server)
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("执行器令牌无效")
        self.storage = StorageManager(StorageConfig(data_root=Path(cache_root)))
        self.files = self.storage.files.scope(".script-worker")
        self.root = self.files.root
        self.root.mkdir(exist_ok=True)
        self.adapter = adapter or CodexTextAdapter()
        self.registry = registry or ScriptModelRegistry()
        self.client = client or httpx.Client(base_url=self.server, timeout=30, follow_redirects=False)
        self.headers = {"Authorization": "Bearer " + token}
        with self.lock():
            path = self.root / "identity.json"
            identity = self.files.read_json(path) if path.exists() else {"schema_version": 1, "server": self.server, "worker_id": uuid4().hex}
            if identity["server"] != self.server:
                raise ValueError("缓存属于其他服务器")
            self.files.write_json(path, identity)
        self.worker_id = _validate_id(identity["worker_id"])

    def lock(self):
        return FileLock(self.root / "worker.lock")

    def close(self):
        self.client.close()

    def request(self, method, path, manifest=None, **kwargs):
        headers = dict(self.headers)
        if manifest:
            headers.update({"X-Worker-ID": self.worker_id, "X-Claim-ID": manifest["claim_id"]})
        response = self.client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def _directory(self, tid):
        path = self.files.check(self.root / _validate_id(tid))
        path.mkdir(exist_ok=True)
        return path

    def _save(self, journal):
        journal["schema_version"] = 1
        self.files.write_json(self._directory(journal["manifest"]["task_id"]) / "journal.json", journal)

    def _result(self, journal, text):
        text = validate_generated_text(text)
        journal.update(phase="result", result={"status": "succeeded", "text": text,
                       "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "attempt_id": journal["attempt_id"]})
        self._save(journal)

    def _execute(self, journal):
        manifest = journal["manifest"]
        base = f"/api/worker/scripts/tasks/{manifest['task_id']}"
        work = self._directory(manifest["task_id"]) / "codex"
        arguments = {"prompt": manifest["generation_prompt"], "model_id": manifest["model_id"],
                     "thinking_level": manifest["thinking_level"], "work_dir": work}
        if journal["phase"] in {"started", "uncertain"}:
            if (work / "completed.json").is_file():
                try:
                    raw = self.adapter.recover(**arguments)
                except ValueError:
                    journal.update(phase="uncertain", result={"status": "uncertain", "attempt_id": journal["attempt_id"],
                                   "error": "执行完成凭据不能校验，请核实原任务"})
                    self._save(journal)
                else:
                    try:
                        text = validate_generated_text(raw)
                    except ValueError:
                        journal.update(phase="result", result={"status": "failed", "attempt_id": journal["attempt_id"],
                                       "error": "Codex 返回的正文格式无效，积分已退回"})
                        self._save(journal)
                    else:
                        self._result(journal, text)
            else:
                journal.update(phase="uncertain", result={"status": "uncertain", "attempt_id": journal["attempt_id"],
                               "error": "执行进程中断，请核实原任务；不会自动重新调用 Codex"})
                self._save(journal)
            return
        if journal["phase"] == "ready":
            journal.update(phase="starting", attempt_id=uuid4().hex)
            self._save(journal)
        if journal["phase"] != "starting":
            return
        self.request("POST", base + "/start", manifest, json={"attempt_id": journal["attempt_id"]})
        journal["phase"] = "started"
        self._save(journal)  # Any crash after this point may recover a receipt, never invoke again.
        phase = "invoke"
        try:
            self.registry.resolve(manifest["model_id"], manifest["thinking_level"], manifest["generation_type"])
            raw = self.adapter.generate(**arguments)
            phase = "validate"
            text = validate_generated_text(raw)
            phase = "persist"
            self._result(journal, text)
        except Exception as exc:
            uncertain = bool(getattr(exc, "uncertain", False) or (phase == "persist" and (work / "completed.json").exists()))
            journal.update(phase="uncertain" if uncertain else "result", result={
                "status": "uncertain" if uncertain else "failed", "attempt_id": journal["attempt_id"],
                "error": "正文已保留或执行状态待确认，请恢复原任务" if uncertain else "Codex 脚本生成未完成，积分已退回"})
            self._save(journal)

    def run_once(self):
        with self.lock():
            pending = []
            for path in sorted(self.root.glob("*/journal.json")):
                journal = self.files.read_json(self.files.check(path))
                if journal["phase"] != "done":
                    pending.append(journal)
            models = [{"model_id": m["model_id"], "efforts": [e["id"] for e in m["thinking_levels"]]} for m in self.registry.models() if m["available"]]
            claimed = None if pending else self.request("POST", "/api/worker/scripts/claim", json={"worker_id": self.worker_id, "models": models})["job"]
            if pending:
                journal = pending[0]
                if claimed and claimed["task_id"] != journal["manifest"]["task_id"]:
                    raise ValueError("云端与本机待处理任务不一致")
            elif claimed:
                _validate_id(claimed["claim_id"])
                if claimed.get("attempt_id"):
                    journal = {"manifest": claimed, "phase": "uncertain", "attempt_id": claimed["attempt_id"]}
                else:
                    journal = {"manifest": claimed, "phase": "ready"}
                self._save(journal)
            else:
                return "idle"
            manifest = journal["manifest"]
            base = f"/api/worker/scripts/tasks/{manifest['task_id']}"
            self.request("POST", base + "/heartbeat", manifest)
            stop = threading.Event()

            def pulse():
                while not stop.wait(15):
                    try:
                        self.request("POST", base + "/heartbeat", manifest)
                    except (httpx.HTTPError, OSError):
                        pass

            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            try:
                if journal["phase"] != "result":
                    self._execute(journal)
                response = self.request("POST", base + "/complete", manifest, json=journal["result"])
                if response["status"] in {"completed", "failed"}:
                    journal["phase"] = "done"
                    self._save(journal)
                return response["status"]
            finally:
                stop.set()
                thread.join(timeout=35)

    def confirm_stopped(self, task_id):
        with self.lock():
            path = self._directory(_validate_id(task_id)) / "journal.json"
            journal = self.files.read_json(path)
            if journal["phase"] not in {"started", "uncertain", "starting"}:
                raise ValueError("仅可解除已人工核实停止的待确认任务")
            journal.update(phase="result", result={"status": "failed", "attempt_id": journal["attempt_id"],
                           "error": "管理员已确认原执行停止，冻结积分已退回"})
            self._save(journal)

    def serve(self, once=False):
        previous = None
        while True:
            try:
                status = self.run_once()
                if status != previous:
                    print("脚本执行器：" + status, flush=True)
                    previous = status
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if once or (code < 500 and code not in {408, 429}):
                    raise RuntimeError(f"脚本服务器返回 HTTP {code}，检查点已保留") from None
            except (httpx.RequestError, OSError):
                if once:
                    raise
            if once:
                return
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--confirm-stopped", help="仅在人工核实原执行已停止后，解除指定任务的冻结")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    worker = ScriptRemoteWorker(config["server"], config["worker_token"], Path(config["cache_root"]) / "script-worker")
    try:
        if args.confirm_stopped:
            worker.confirm_stopped(args.confirm_stopped)
        worker.serve(args.once)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
