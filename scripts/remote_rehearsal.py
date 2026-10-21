"""真实回环 HTTP + 独立 CLI 进程演练；不启动 Codex、不读取真实 data。"""

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="remote-http-", dir=ROOT / "tests") as temporary:
        directory = Path(temporary)
        fixture = directory / "fixture.png"
        Image.new("RGB", (64, 48), "teal").save(fixture)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        password, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        env = {**os.environ, "IMAGE_WORKBENCH_PUBLIC_ORIGIN": origin,
               "IMAGE_WORKBENCH_WORKER_TOKEN": token, "PYTHONIOENCODING": "utf-8"}
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        command = [sys.executable, "-X", "utf8", "-m", "image_workbench", "--data-root", str(directory / "server"),
                   "cloud", "--backend", "local", "--port", str(port)]
        client = httpx.Client(base_url=origin, timeout=15, trust_env=False)
        process = None
        try:
            with (directory / "server.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log, **options)
                for attempt in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("cloud CLI exited: " + (directory / "server.log").read_text())
                    try:
                        config = client.get("/api/auth/config")
                        config.raise_for_status()
                        break
                    except httpx.HTTPError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("server did not become ready")
                client.headers["X-Workbench-CSRF"] = config.json()["csrf_token"]
                credentials = {"username": "http_rehearsal", "password": password}
                client.post("/api/auth/register", json=credentials).raise_for_status()
                client.post("/api/auth/login", json=credentials).raise_for_status()
                config = client.get("/api/config")
                config.raise_for_status()
                assert config.json()["execution_mode"] == "remote"
                assert not config.json()["worker_running"]
                assert httpx.get(origin + "/api/config", trust_env=False).status_code == 401
                client.headers["X-Workbench-CSRF"] = config.json()["csrf_token"]
                response = client.post("/api/projects", json={"request_id": uuid4().hex, "name": "真实HTTP传递演练"})
                response.raise_for_status()
                pid = response.json()["project_id"]
                outcomes = []
                base_id = None
                for operation in ("generate", "edit"):
                    data = {"request_id": uuid4().hex, "operation": operation, "prompt": "完整HTTP任务传递演练"}
                    if base_id:
                        data["base_artifact_id"] = base_id
                    response = client.post(f"/api/projects/{pid}/tasks", data=data,
                                           files={"files": ("reference.png", fixture.read_bytes(), "image/png")})
                    response.raise_for_status()
                    tid = response.json()["task_id"]
                    path = f"/api/tasks/{tid}"
                    response = client.post(path + "/run", json={"request_id": uuid4().hex, "confirm_generation": True})
                    response.raise_for_status()
                    assert client.get(path).json()["status"] == "queued"
                    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "image_workbench", "remote-worker",
                        "--server", origin, "--cache-root", str(directory / "client"), "--backend", "local",
                        "--image", str(fixture), "--once"], cwd=ROOT, env=env, capture_output=True, timeout=45, **options)
                    if result.returncode:
                        raise RuntimeError(result.stderr.decode("utf-8"))
                    detail = client.get(path).json()
                    assert detail["status"] == "awaiting_review", detail
                    assert len(detail["outputs"]) == 1
                    aid = detail["outputs"][0]["artifact_id"]
                    downloaded = client.get(f"/api/artifacts/{aid}")
                    downloaded.raise_for_status()
                    digest = hashlib.sha256(downloaded.content).hexdigest()
                    assert digest == hashlib.sha256(fixture.read_bytes()).hexdigest()
                    client.post(f"/api/artifacts/{aid}/approve", json={}).raise_for_status()
                    assert client.get(path).json()["status"] == "approved"
                    if base_id:
                        assert detail["outputs"][0]["source_artifact_id"] == base_id
                    outcomes.append({"operation": operation, "status": "approved", "sha256": digest,
                                     "reference_count": detail["input_count"], "lineage_verified": bool(base_id)})
                    base_id = aid
                assert client.get("/api/config").json()["worker_running"]
                wallet = client.get("/api/credits").json()["credits"]
                assert wallet["remaining_credit"] == 80 and wallet["used_credit"] == 120 and wallet["frozen_credit"] == 0
                report = {"transport": "real loopback HTTP, separate cloud and worker CLI processes",
                          "backend": "local fixture; no Codex generation; isolated test credits used",
                          "remaining_credit": wallet["remaining_credit"], "used_credit": wallet["used_credit"],
                          "authentication_enforced": True, "outcomes": outcomes, "passed": True}
                target = ROOT / "reports" / "remote_http_rehearsal.json"
                target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            client.close()
            if process is not None and process.poll() is None:
                # 只结束本脚本启动的云端演练进程。
                if os.name == "nt":
                    subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                                    "/PID", str(process.pid), "/T", "/F"], capture_output=True, **options)
                else:
                    process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
