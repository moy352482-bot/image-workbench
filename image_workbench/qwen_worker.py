"""将网站测试任务交给本机 Qwen ComfyUI；提交只发生一次，回传可重试。"""
import argparse
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .domain_errors import ComfyRejected, ArtifactInvalid
from .atomic_files import write_bytes as _write, read_json as _json_read, write_json as _json_write
from .locking import FileLock, LockBusyError
from .qwen_lab import MAX_IMAGE_BYTES, MODELS, MODES, image_info, validate_task
from .remote_access import validate_origin
from .storage import _validate_id

SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"


def workflow(task, references, prefix):
    task = validate_task(task)
    if len(references) != MODES[task["mode"]]:
        raise ValueError("工作流参考图数量不匹配")
    def node(kind, **inputs):
        return {"class_type": kind, "inputs": inputs}
    graph = {
        "1": node("UNETLoader", unet_name=MODELS["fp8"], weight_dtype="default"),
        "2": node("CLIPLoader", clip_name="qwen3vl_8b_w4a8.safetensors", type="qwen_image", device="default"),
        "3": node("VAELoader", vae_name="qwen_image_2.1_vae_bf16.safetensors"),
        "4": node("TextEncodeQwenImage21", clip=["2", 0], prompt=task["prompt"], negative_prompt="", resolution=task["resolution"]),
        "5": node("EmptyLatentImage", width=task["resolution"], height=task["resolution"], batch_size=1),
        "6": node("QwenImage21Cache", model=["1", 0], device="auto", dtype="default"),
        "7": node("CFGGuider", model=["6", 0], positive=["4", 0], negative=["4", 1], cfg=1.0),
        "8": node("RandomNoise", noise_seed=task["seed"]),
        "9": node("KSamplerSelect", sampler_name="euler"),
        "10": node("ManualSigmas", sigmas=SIGMAS),
        "11": node("SamplerCustomAdvanced", noise=["8", 0], guider=["7", 0], sampler=["9", 0], sigmas=["10", 0], latent_image=["5", 0]),
        "12": node("VAEDecode", samples=["11", 0], vae=["3", 0]),
        "13": node("SaveImage", images=["12", 0], filename_prefix=prefix),
    }
    if task["model"] == "q5":
        graph["1"] = node("UnetLoaderGGUF", unet_name=MODELS["q5"])
    if references:
        del graph["5"]
        graph["4"]["inputs"]["vae"] = ["3", 0]
        graph["11"]["inputs"]["latent_image"] = ["4", 2]
        for index, name in enumerate(references, 1):
            nid = str(13 + index)
            graph[nid] = node("LoadImage", image=name)
            graph["4"]["inputs"][f"images.image_{index}"] = [nid, 0]
    return graph


class QwenTransport:
    def __init__(self, server, token, *, client=None):
        self.server = validate_origin(server)
        if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("执行器令牌无效")
        self.client = client or httpx.Client(base_url=self.server, timeout=30, follow_redirects=False,
                                             trust_env=urlsplit(self.server).hostname not in {"localhost", "127.0.0.1"})
        self.headers = {"Authorization": "Bearer " + token}

    def _headers(self, job=None):
        headers = dict(self.headers)
        if job:
            headers.update({"X-Worker-ID": _validate_id(job["worker_id"]), "X-Claim-ID": _validate_id(job["claim_id"])})
        return headers

    def request(self, method, suffix, job=None, **kwargs):
        prefix = "/api/worker/qwen"
        if job:
            prefix += "/jobs/" + _validate_id(job["job_id"])
        headers = {**self._headers(job), **kwargs.pop("headers", {})}
        response = self.client.request(method, prefix + suffix, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def input(self, job, index):
        with self.client.stream("GET", f"/api/worker/qwen/jobs/{_validate_id(job['job_id'])}/inputs/{int(index)}",
                                headers=self._headers(job)) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(65536):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("参考图下载超过限制")
        return bytes(data)

    def beat(self, wid, ready=True, message=""):
        return self.request("POST", "/heartbeat", json={"worker_id": wid, "ready": ready, "message": message})


class ComfyQwenAdapter:
    def __init__(self, url, lock_path, *, peer_urls=(), timeout=900, poll_seconds=2, client=None):
        self.url = validate_origin(url)
        self.peer_urls = [validate_origin(p) for p in peer_urls]
        if any(urlsplit(p).hostname not in {"localhost", "127.0.0.1"} for p in [self.url, *self.peer_urls]):
            raise ValueError("执行器只可连接本机 ComfyUI")
        self.lock_path = Path(lock_path).resolve()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout, self.poll_seconds = float(timeout), float(poll_seconds)
        if not 10 <= self.timeout <= 3600 or not .1 <= self.poll_seconds <= 30:
            raise ValueError("执行器超时或轮询间隔无效")
        self.client = client or httpx.Client(base_url=self.url, timeout=60, trust_env=False, follow_redirects=False)

    def get(self, path):
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()

    def health(self):
        value = self.get("/system_stats")
        if not value.get("devices"):
            raise ValueError("ComfyUI 未提供设备")
        return value

    def check(self):
        self.health()
        info = self.get("/object_info")
        needed = {n["class_type"] for model in MODELS for n in workflow({"mode": "generate", "model": model, "prompt": "check", "seed": 42, "resolution": 1024}, [], "check").values()}
        missing = needed - info.keys()
        if missing:
            raise ValueError("ComfyUI 缺少节点：" + ", ".join(sorted(missing)))
        for kind, field, filename in (("UNETLoader", "unet_name", MODELS["fp8"]), ("UnetLoaderGGUF", "unet_name", MODELS["q5"]),
                                     ("CLIPLoader", "clip_name", "qwen3vl_8b_w4a8.safetensors"), ("VAELoader", "vae_name", "qwen_image_2.1_vae_bf16.safetensors")):
            if filename not in info[kind]["input"]["required"][field][0]:
                raise ValueError("ComfyUI 未找到模型：" + filename)
        return {"ok": True, "models": list(MODELS), "comfy_url": self.url}

    def idle(self):
        queue = self.get("/queue")
        return not queue["queue_running"] and not queue["queue_pending"]

    def prepare_gpu(self):
        if not self.idle():
            return False
        for url in self.peer_urls:
            try:
                response = self.client.get(url + "/queue", timeout=3)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue
            response.raise_for_status()
            queue = response.json()
            if queue["queue_running"] or queue["queue_pending"]:
                return False
            response = self.client.post(url + "/free", json={"unload_models": True, "free_memory": True}, timeout=30)
            response.raise_for_status()
        return True

    def release_gpu(self):
        try:
            if self.idle():
                self.client.post("/free", json={"unload_models": True, "free_memory": True}).raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError):
            pass

    def upload(self, jid, index, data):
        filename, folder = f"input-{index}.png", "image_workbench/qwen/" + _validate_id(jid)
        response = self.client.post("/upload/image", files={"image": (filename, data, "image/png")},
                                    data={"type": "input", "subfolder": folder, "overwrite": "true"})
        response.raise_for_status()
        result = response.json()
        if (result.get("name"), result.get("subfolder"), result.get("type")) != (filename, folder, "input"):
            raise ValueError("ComfyUI 图片上传回执不匹配")
        return folder + "/" + filename

    def submit(self, graph, attempt):
        response = self.client.post("/prompt", json={"prompt": graph, "client_id": attempt})
        if response.status_code == 400:
            try:
                result = response.json()
            except ValueError:
                result = {}
            if result.get("error") and not result.get("prompt_id"):
                raise ComfyRejected("模型拒绝此工作流，未开始生成")
        response.raise_for_status()
        pid = response.json().get("prompt_id")
        if not isinstance(pid, str) or not 1 <= len(pid) <= 200 or any(c in pid for c in "/\\?#"):
            raise ValueError("模型提交回执无效")
        return pid

    def reconcile(self, graph):
        queue, history = self.get("/queue"), self.get("/history")
        entries = queue["queue_running"] + queue["queue_pending"] + [h.get("prompt", []) for h in history.values()]
        return sorted({str(x[1]) for x in entries if len(x) >= 3 and x[2] == graph})

    def history(self, pid):
        if not isinstance(pid, str) or not 1 <= len(pid) <= 200 or any(c in pid for c in "/\\?#"):
            raise ValueError("模型执行标识无效")
        return self.get("/history/" + pid).get(pid)

    def download(self, history, prefix):
        images = history.get("outputs", {}).get("13", {}).get("images", [])
        if len(images) != 1:
            raise ArtifactInvalid("模型未返回唯一成图")
        descriptor = images[0]
        folder, basename = prefix.rsplit("/", 1)
        filename = descriptor.get("filename", "")
        if (descriptor.get("type") != "output" or descriptor.get("subfolder", "").replace("\\", "/") != folder
                or not filename.startswith(basename) or any(c in filename for c in ("..", "/", "\\", ":"))):
            raise ArtifactInvalid("模型输出不属于当前任务")
        with self.client.stream("GET", "/view", params={k: descriptor[k] for k in ("filename", "subfolder", "type")}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(65536):
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    raise ArtifactInvalid("模型输出超过 20 MB")
        return image_info(bytes(data))[0]


class QwenWorker:
    def __init__(self, transport, adapter, cache_root):
        self.transport, self.adapter = transport, adapter
        self.root = Path(cache_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.root / "client.lock"):
            path = self.root / "identity.json"
            if path.exists():
                identity = _json_read(path)
                if (identity["server"], identity["comfy_url"]) != (transport.server, adapter.url):
                    raise ValueError("缓存属于其他服务器或 ComfyUI")
            else:
                identity = {"server": transport.server, "comfy_url": adapter.url, "worker_id": uuid4().hex}
                _json_write(path, identity)
        self.worker_id = _validate_id(identity["worker_id"])

    def directory(self, jid):
        path = (self.root / _validate_id(jid)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("缓存路径越界")
        path.mkdir(exist_ok=True)
        return path

    def save(self, journal):
        _json_write(self.directory(journal["job"]["job_id"]) / "journal.json", journal)

    def finish(self, journal, status, error=None, **values):
        journal.update(phase="result", result={"status": status, "error": error, **values})
        self.save(journal)

    def uncertain(self, journal, message):
        journal.update(phase="uncertain")
        self.save(journal)
        self.transport.request("POST", "/complete", journal["job"], json={"status": "uncertain", "error": message})
        return "uncertain"

    def deliver(self, journal):
        job, result = journal["job"], journal["result"]
        if result["status"] == "succeeded":
            data = (self.directory(job["job_id"]) / "output.png").read_bytes()
            if hashlib.sha256(data).hexdigest() != result["sha256"]:
                raise ValueError("缓存图片已经改变，不能回传")
            received = self.transport.request("PUT", "/output", job, content=data,
                headers={"Content-Type": "image/png", "X-Content-SHA256": result["sha256"]}, timeout=120)
            if received.get("sha256") != result["sha256"]:
                raise ValueError("云端成图校验不一致")
        response = self.transport.request("POST", "/complete", job, json=result)
        if response["status"] in {"completed", "failed"}:
            journal["phase"] = "done"
            self.save(journal)
        return response["status"]

    def execute(self, journal):
        job = journal["job"]
        folder = self.directory(job["job_id"])
        if journal["phase"] == "result":
            return self.deliver(journal)
        if journal["phase"] == "ready":
            if not self.adapter.prepare_gpu():
                return "busy"
            try:
                validate_task(job["task"])
                references = []
                for item in job["inputs"]:
                    data = self.transport.input(job, item["index"])
                    if hashlib.sha256(data).hexdigest() != item["sha256"] or len(data) != item["size_bytes"]:
                        raise ValueError("下载原图校验失败")
                    image_info(data)
                    _write(folder / f"input-{item['index']}.png", data)
                    references.append(self.adapter.upload(job["job_id"], item["index"], data))
                attempt = uuid4().hex
                prefix = f"image_workbench/qwen/{job['job_id']}/{attempt}/image"
                journal.update(phase="prepared", attempt_id=attempt, prefix=prefix,
                               workflow=workflow(job["task"], references, prefix))
                self.save(journal)
            except ValueError:
                self.finish(journal, "failed", "原图或工作流校验失败，未提交模型")
                return self.deliver(journal)
        if journal["phase"] == "prepared":
            if not self.adapter.prepare_gpu():
                return "busy"
            self.transport.request("POST", "/start", job, json={"attempt_id": journal["attempt_id"]})
            journal.update(phase="submitting", submitted_at=time.time())
            self.save(journal)  # 之后的任何断线恢复都只查询原提交，绝不自动重发。
            try:
                pid = self.adapter.submit(journal["workflow"], journal["attempt_id"])
            except ComfyRejected as exc:
                self.finish(journal, "failed", str(exc))
                return self.deliver(journal)
            except (httpx.HTTPError, ValueError):
                pid = None
            if pid:
                journal.update(phase="running", prompt_id=pid)
                self.save(journal)
        if journal["phase"] in {"submitting", "uncertain"}:
            pid = journal.get("prompt_id")
            if not pid:
                matches = self.adapter.reconcile(journal["workflow"]) if journal.get("workflow") else []
                if len(matches) != 1:
                    return self.uncertain(journal, "模型执行状态待确认；正在核对原任务，不会重复生成")
                pid = matches[0]
            journal.update(phase="running", prompt_id=pid)
            self.save(journal)
        if journal["phase"] == "running":
            self.transport.request("POST", "/start", job, json={"attempt_id": journal["attempt_id"], "prompt_id": journal["prompt_id"]})
            deadline = time.monotonic() + self.adapter.timeout
            while True:
                history = self.adapter.history(journal["prompt_id"])
                if history:
                    status = history.get("status", {})
                    if status.get("status_str") == "error":
                        journal["history"] = history
                        self.finish(journal, "failed", "本机模型执行失败，请检查原图或稍后新建测试")
                        return self.deliver(journal)
                    if status.get("completed"):
                        journal.update(phase="generated", history=history)
                        self.save(journal)
                        break
                if time.monotonic() >= deadline:
                    return self.uncertain(journal, "本机尚未确认完成，恢复连接后继续取回原结果")
                time.sleep(self.adapter.poll_seconds)
        if journal["phase"] == "generated":
            try:
                path = folder / "output.png"
                if not path.exists():
                    _write(path, self.adapter.download(journal["history"], journal["prefix"]))
                _, info = image_info(path.read_bytes())
            except (ArtifactInvalid, ValueError):
                self.finish(journal, "failed", "模型成图校验失败，未重复生成")
                return self.deliver(journal)
            elapsed = max(0, time.time() - journal["submitted_at"]) if journal.get("submitted_at") else None
            self.finish(journal, "succeeded", sha256=info["sha256"], elapsed_seconds=elapsed)
            return self.deliver(journal)
        raise ValueError("执行检查点无效")

    def run_once(self):
        with FileLock(self.root / "client.lock"), FileLock(self.adapter.lock_path):
            journals = [_json_read(p) for p in sorted(self.root.glob("*/journal.json"))]
            journal = next((j for j in journals if j["phase"] != "done"), None)
            if not journal:
                self.adapter.health()
                if not self.adapter.idle():
                    self.transport.beat(self.worker_id, True, "显卡正在处理其他任务")
                    return "busy"
                job = self.transport.request("POST", "/claim", json={"worker_id": self.worker_id})["job"]
                if not job:
                    return "idle"
                if job["worker_id"] != self.worker_id:
                    raise ValueError("领取的任务身份不一致")
                _validate_id(job["claim_id"])
                journal = {"phase": "ready", "job": job}
                if job.get("attempt_id"):
                    attempt = _validate_id(job["attempt_id"])
                    journal.update(phase="uncertain", attempt_id=attempt,
                                   prefix=f"image_workbench/qwen/{job['job_id']}/{attempt}/image")
                    if job.get("prompt_id"):
                        journal["prompt_id"] = job["prompt_id"]
                self.save(journal)
            job = journal["job"]
            self.transport.request("POST", "/heartbeat", job)
            stop = threading.Event()
            def pulse():
                while not stop.wait(15):
                    try:
                        self.transport.request("POST", "/heartbeat", job)
                    except (httpx.HTTPError, OSError, ValueError):
                        pass
            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            try:
                return self.execute(journal)
            finally:
                if journal["phase"] in {"done", "result", "generated"}:
                    self.adapter.release_gpu()
                stop.set()
                thread.join(timeout=35)

    def serve(self, once=False):
        previous = None
        while True:
            try:
                status = self.run_once()
            except LockBusyError:
                status = "busy"
                try:
                    self.transport.beat(self.worker_id, True, "显卡正在处理其他任务")
                except (httpx.HTTPError, OSError, ValueError):
                    pass
            except (httpx.HTTPError, OSError, ValueError) as exc:
                status = "reconnecting"
                print("连接暂不可用，保留任务检查点：" + type(exc).__name__, flush=True)
                try:
                    self.transport.beat(self.worker_id, False, "本机模型连接暂不可用，任务已保留")
                except (httpx.HTTPError, OSError, ValueError):
                    pass
            if status != previous:
                print(time.strftime("%Y-%m-%d %H:%M:%S"), status, flush=True)
                previous = status
            if once:
                return status
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--worker-config", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = _json_read(args.config)
    adapter = ComfyQwenAdapter(config["comfy_url"], config["gpu_lock_path"], peer_urls=config.get("peer_comfy_urls", []))
    try:
        if args.check:
            print(json.dumps(adapter.check(), ensure_ascii=False))
            return
        if not args.worker_config:
            parser.error("需要 --worker-config 指向已有受保护执行器配置")
        credentials = _json_read(args.worker_config)
        transport = QwenTransport(credentials["server"], credentials["worker_token"])
        try:
            worker = QwenWorker(transport, adapter, config["cache_root"])
            worker.serve(once=args.once)
        finally:
            transport.client.close()
    finally:
        adapter.client.close()


if __name__ == "__main__":
    main()
