"""本机 H3 单镜执行器；持久化提交意图，网络恢复只对账和回传。"""

import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image

from .locking import FileLock, LockBusyError
from .remote_access import validate_origin
from .storage import _validate_id


MAX_VIDEO_BYTES = 40 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
HD_DIMENSIONS = {
    "720p": {"landscape": (1280, 720), "portrait": (720, 1280), "square": (720, 720)},
    "1080p": {"landscape": (1920, 1080), "portrait": (1080, 1920), "square": (1080, 1080)},
}
DEFAULT_MODELS = {
    "diffusion": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "reference_diffusion": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "encoder": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "turbo_lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
}


class ComfyRejected(ValueError):
    """ComfyUI 明确拒绝了整次提交，没有接受的 prompt。"""


class ArtifactInvalid(ValueError):
    """生成已结束，但产物不满足交付条件。"""


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_write(path, value):
    _write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


def _json_read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def generation_dimensions(task):
    """交付尺寸固定；高清只向上对齐原生画布，旧任务保留原尺寸。"""
    width, height = task.get("width"), task.get("height")
    resolution = task.get("resolution")
    if resolution is None:
        if (any(type(n) is not int or n < 32 or n > 1536 or n % 32 for n in (width, height))
                or width * height > 1024 * 1024):
            raise ValueError("旧 H3 视频尺寸无效")
        return width, height
    sizes = HD_DIMENSIONS.get(resolution)
    if (not sizes or type(width) is not int or type(height) is not int or (width, height) not in sizes.values()
            or "aspect_ratio" in task and sizes.get(task["aspect_ratio"]) != (width, height)):
        raise ValueError("高清档位、画幅和交付尺寸不一致")
    return (width + 31) // 32 * 32, (height + 31) // 32 * 32


class LocalVideoTransport:
    def __init__(self, store):
        self.store = store
        self.identity = "local:" + str(store.storage.data_root.resolve())

    def claim(self, worker_id):
        return self.store.claim(worker_id)

    def start(self, manifest, attempt_id, prompt_id=None):
        return self.store.start(manifest["job_id"], manifest["worker_id"], manifest["claim_id"], attempt_id, prompt_id)

    def heartbeat(self, manifest):
        return self.store.heartbeat(manifest["job_id"], manifest["worker_id"], manifest["claim_id"])

    def asset(self, manifest, asset_id, limit):
        data = self.store.worker_asset(manifest["job_id"], manifest["worker_id"], manifest["claim_id"], asset_id)
        if len(data) > limit:
            raise ValueError("参考图超过下载限制")
        return data

    def upload(self, manifest, data, sha256):
        return self.store.upload_output(manifest["job_id"], manifest["worker_id"], manifest["claim_id"], data, sha256)

    def complete(self, manifest, result):
        return self.store.complete(manifest["job_id"], manifest["worker_id"], manifest["claim_id"], **result)

    def close(self):
        pass


class RemoteVideoTransport:
    def __init__(self, server, token, *, client=None):
        self.server = validate_origin(server)
        if (not isinstance(token, str) or len(token) < 32 or not token.isascii()
                or any(c.isspace() for c in token) or token.startswith("REPLACE_")):
            raise ValueError("执行器令牌至少32个 ASCII 字符且不能含空白")
        self.identity = "remote:" + self.server
        self.client = client or httpx.Client(base_url=self.server, timeout=30, follow_redirects=False,
            trust_env=urlsplit(self.server).hostname not in {"localhost", "127.0.0.1"})
        self.headers = {"Authorization": "Bearer " + token}

    def _headers(self, manifest=None):
        headers = dict(self.headers)
        if manifest:
            headers.update({"X-Worker-ID": _validate_id(manifest["worker_id"]),
                            "X-Claim-ID": _validate_id(manifest["claim_id"])})
        return headers

    def _path(self, manifest):
        return "/api/worker/video/jobs/" + _validate_id(manifest["job_id"])

    def _request(self, method, path, manifest=None, **kwargs):
        headers = {**self._headers(manifest), **kwargs.pop("headers", {})}
        response = self.client.request(method, path, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def claim(self, worker_id):
        return self._request("POST", "/api/worker/video/claim", json={"worker_id": worker_id})["job"]

    def start(self, manifest, attempt_id, prompt_id=None):
        return self._request("POST", self._path(manifest) + "/start", manifest,
                             json={"attempt_id": attempt_id, "prompt_id": prompt_id})

    def heartbeat(self, manifest):
        return self._request("POST", self._path(manifest) + "/heartbeat", manifest)

    def asset(self, manifest, asset_id, limit):
        path = self._path(manifest) + "/assets/" + _validate_id(asset_id)
        with self.client.stream("GET", path, headers=self._headers(manifest)) as response:
            response.raise_for_status()
            data = bytearray()
            for block in response.iter_bytes(65536):
                data.extend(block)
                if len(data) > limit:
                    raise ValueError("参考图超过下载限制")
        return bytes(data)

    def upload(self, manifest, data, sha256):
        return self._request("PUT", self._path(manifest) + "/output", manifest, content=data,
            headers={"Content-Type": "video/mp4", "X-Content-SHA256": sha256}, timeout=180)

    def complete(self, manifest, result):
        return self._request("POST", self._path(manifest) + "/complete", manifest, json=result, timeout=120)

    def close(self):
        self.client.close()


class ComfyH3Adapter:
    """只替换已安装模板的受控参数；不接受上传工作流或任意本地路径。"""

    def __init__(self, comfy_url, workflow_dir, *, models=None, ffmpeg="ffmpeg", ffprobe="ffprobe",
                 timeout=7200, poll_seconds=5, client=None, lock_path=None):
        self.comfy_url = validate_origin(comfy_url)
        if urlsplit(self.comfy_url).hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("H3 执行器只连接本机 ComfyUI")
        self.workflow_dir = Path(workflow_dir).resolve()
        self.templates = {mode: _json_read(self.workflow_dir / (mode.lower() + ".api.json"))
                          for mode in ("T2V", "I2V", "R2V")}
        self.models = {**DEFAULT_MODELS, **(models or {})}
        if set(self.models) != set(DEFAULT_MODELS) or any(
            not isinstance(value, str) or not value or any(c in value for c in ("/", "\\", ":"))
            or value in {".", ".."} for value in self.models.values()
        ):
            raise ValueError("模型配置必须使用受支持的模型键和纯文件名")
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in (timeout, poll_seconds)):
            raise ValueError("执行超时和轮询间隔必须为有限正数")
        self.timeout, self.poll_seconds = float(timeout), float(poll_seconds)
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)
        # 同一用户的本机/云端队列共用此锁，不按缓存目录或服务器地址分割 GPU。
        self.lock_path = Path(lock_path or Path(tempfile.gettempdir()) / "image-workbench-h3-gpu.lock").resolve()
        self.client = client or httpx.Client(base_url=self.comfy_url, timeout=60,
                                            follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def _get(self, path):
        response = self.client.get(path)
        response.raise_for_status()
        return response.json()

    def health(self):
        stats = self._get("/system_stats")
        if not isinstance(stats, dict) or not isinstance(stats.get("devices"), list) or not stats["devices"]:
            raise ValueError("ComfyUI 未返回可用设备")
        if not shutil.which(self.ffmpeg) or not shutil.which(self.ffprobe):
            raise ValueError("缺少用于完整视频校验的 ffmpeg/ffprobe")
        return stats

    def queue(self):
        result = self._get("/queue")
        if not isinstance(result, dict) or any(not isinstance(result.get(k), list)
                                                for k in ("queue_running", "queue_pending")):
            raise ValueError("ComfyUI 队列响应无效")
        return result

    def idle(self):
        queue = self.queue()
        return not (queue["queue_running"] or queue["queue_pending"])

    def check(self):
        """只读预检，不上传资产、不提交 prompt、不载入模型。"""
        report = {"available": False, "comfy_url": self.comfy_url, "workflow_dir": str(self.workflow_dir),
                  "missing_nodes": [], "missing_models": [], "ffmpeg": bool(shutil.which(self.ffmpeg)),
                  "ffprobe": bool(shutil.which(self.ffprobe)), "resolutions": list(HD_DIMENSIONS)}
        try:
            report["devices"] = self.health()["devices"]
            info = self._get("/object_info")
            required = {}
            for mode in self.templates:
                refs = ["preflight/reference.png"] if mode != "T2V" else []
                graph = self.bind({"mode": mode, "prompt": "preflight", "width": 512, "height": 288,
                    "duration": 4}, refs, 1, "image_workbench/preflight/video")
                for node in graph.values():
                    kind = node["class_type"]
                    if kind not in info:
                        report["missing_nodes"].append(kind)
                    for key in ("unet_name", "clip_name", "vae_name", "lora_name"):
                        if key in node["inputs"]:
                            required[(kind, key, node["inputs"][key])] = True
            report["missing_nodes"] = sorted(set(report["missing_nodes"]))
            for kind, key, model in required:
                spec = info.get(kind, {}).get("input", {}).get("required", {}).get(key, [])
                if not spec or not isinstance(spec[0], list) or model not in spec[0]:
                    report["missing_models"].append(model)
            report["missing_models"] = sorted(set(report["missing_models"]))
            queue = self.queue()
            report["queue"] = {"running": len(queue["queue_running"]), "pending": len(queue["queue_pending"])}
            report["available"] = not (report["missing_nodes"] or report["missing_models"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            report["error"] = str(exc)[:400]
        return report

    def bind(self, task, references, seed, prefix):
        mode = task.get("mode")
        prompt, duration = task.get("prompt"), task.get("duration")
        if mode not in self.templates or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 12000:
            raise ValueError("H3 模式或分镜提示词无效")
        width, height = generation_dimensions(task)
        if type(duration) is not int or not 4 <= duration <= 15:
            raise ValueError("H3 单镜时长必须为4到15整秒")
        if ((mode == "T2V" and references) or (mode == "I2V" and len(references) not in (1, 2))
                or (mode == "R2V" and not 1 <= len(references) <= 9)):
            raise ValueError("参考图数量与 H3 模式不符")
        for path in [prefix, *references]:
            if not isinstance(path, str) or not path or path.startswith("/") or any(c in path for c in ("..", "\\", ":")):
                raise ValueError("H3 输入输出路径无效")
        turbo = mode != "R2V"
        frames = duration * 24
        frames += (5 - frames % 17) % 17
        graph = copy.deepcopy(self.templates[mode])
        for node in graph.values():
            kind, inputs = node["class_type"], node["inputs"]
            if kind in {"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"}:
                inputs.update(prompt=prompt, width=width, height=height, length=frames)
                for key in list(inputs):
                    if key in {"first_frame", "last_frame"} or key.startswith("ref_") and key != "ref_image_size":
                        del inputs[key]
                if mode == "I2V":
                    inputs["first_frame"] = ["9000", 0]
                    if len(references) == 2:
                        inputs["last_frame"] = ["9001", 0]
                if mode == "R2V":
                    inputs.update({f"ref_images.ref_image_{i}": [str(9000 + i), 0] for i in range(len(references))})
            elif kind == "RandomNoise":
                inputs["noise_seed"] = seed
            elif kind == "BasicScheduler":
                inputs["steps"] = 8 if turbo else 20
            elif kind == "SaveVideo":
                inputs.update(filename_prefix=prefix, format="mp4")
            elif kind == "CreateVideo":
                inputs["fps"] = 24
            elif kind == "UNETLoader":
                inputs["unet_name"] = self.models["reference_diffusion" if mode == "R2V" else "diffusion"]
            elif kind == "CLIPLoader":
                inputs["clip_name"] = self.models["encoder"]
            elif kind == "LoraLoaderModelOnly" and turbo:
                inputs["lora_name"] = self.models["turbo_lora"]
            elif kind == "VAELoader":
                inputs["vae_name"] = self.models["audio_vae" if "audio" in inputs["vae_name"] else "video_vae"]
        # 本机 MiniMaxH3VideoVAE 自带 256px 空间分块、17 帧时间分块及 CPU 输出流。
        # 保留 VAEDecode 的直接输出缓冲区路径；此版本的 decode_tiled 忽略外部 tile 参数，
        # 换为通用 VAEDecodeTiled 不减少采样显存，反而复制完整 CPU 视频缓冲区。
        for index, reference in enumerate(references):
            graph[str(9000 + index)] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        switches = {key: node["inputs"]["on_true" if turbo else "on_false"]
                    for key, node in graph.items() if node["class_type"] == "ComfySwitchNode"}
        for node in graph.values():
            for key, value in list(node["inputs"].items()):
                if isinstance(value, list) and len(value) == 2 and str(value[0]) in switches:
                    node["inputs"][key] = switches[str(value[0])]
        required = set()
        def visit(key):
            if key in required:
                return
            required.add(key)
            for value in graph[key]["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and str(value[0]) in graph:
                    visit(str(value[0]))
        outputs = [key for key, node in graph.items() if node["class_type"] == "SaveVideo"]
        if len(outputs) != 1:
            raise ValueError("H3 模板必须且只能包含一个 SaveVideo 输出")
        visit(outputs[0])
        return {key: value for key, value in graph.items() if key in required}

    def upload_reference(self, job_id, asset_id, data, extension):
        name = _validate_id(asset_id) + extension
        folder = "image_workbench/" + _validate_id(job_id)
        response = self.client.post("/upload/image", files={"image": (name, data)},
            data={"overwrite": "true", "subfolder": folder, "type": "input"}, timeout=120)
        response.raise_for_status()
        uploaded = response.json()
        if uploaded.get("name") != name or uploaded.get("subfolder") != folder or uploaded.get("type", "input") != "input":
            raise ValueError("ComfyUI 返回了非预期的参考图位置")
        return folder + "/" + name

    def submit(self, graph, attempt_id):
        response = self.client.post("/prompt", json={"prompt": graph, "client_id": attempt_id}, timeout=120)
        if response.status_code in {400, 422}:
            try:
                rejection = response.json()
            except ValueError:
                rejection = {}
            if rejection.get("error") and not rejection.get("prompt_id"):
                raise ComfyRejected("ComfyUI 拒绝工作流，未开始生成")
        response.raise_for_status()
        result = response.json()
        prompt_id = result.get("prompt_id")
        if not isinstance(prompt_id, str) or not 1 <= len(prompt_id) <= 200:
            raise ValueError("ComfyUI 提交结果不明确")
        return prompt_id

    def reconcile(self, graph):
        queue = self.queue()
        history = self._get("/history")
        if not isinstance(history, dict):
            raise ValueError("ComfyUI 历史响应无效")
        entries = queue["queue_running"] + queue["queue_pending"]
        entries += [entry.get("prompt", []) for entry in history.values() if isinstance(entry, dict)]
        matches = set()
        for entry in entries:
            if isinstance(entry, list) and len(entry) >= 3 and entry[2] == graph:
                matches.add(str(entry[1]))
        return sorted(matches)

    def history(self, prompt_id):
        # prompt_id 来自 ComfyUI，但仍不能成为额外路径段或查询参数。
        if not isinstance(prompt_id, str) or not 1 <= len(prompt_id) <= 200 or any(c in prompt_id for c in "/\\?#"):
            raise ValueError("ComfyUI prompt_id 无效")
        return self._get("/history/" + prompt_id).get(prompt_id)

    def download(self, history, destination, prefix, limit):
        candidates = []
        for node in history.get("outputs", {}).values():
            for group in node.values():
                if isinstance(group, list):
                    candidates.extend(item for item in group if isinstance(item, dict)
                                      and str(item.get("filename", "")).lower().endswith(".mp4"))
        if len(candidates) != 1:
            raise ArtifactInvalid("ComfyUI 未返回唯一 MP4 产物")
        descriptor = candidates[0]
        folder, basename = prefix.rsplit("/", 1)
        filename = descriptor.get("filename", "")
        if (descriptor.get("type") != "output" or descriptor.get("subfolder", "").replace("\\", "/") != folder
                or not filename.startswith(basename) or any(c in filename for c in ("..", "/", "\\", ":"))):
            raise ArtifactInvalid("ComfyUI 产物不属于当前作业")
        with self.client.stream("GET", "/view", params={key: descriptor[key] for key in ("filename", "subfolder", "type")},
                                timeout=180) as response:
            response.raise_for_status()
            data = bytearray()
            for block in response.iter_bytes(65536):
                data.extend(block)
                if len(data) > min(limit, MAX_VIDEO_BYTES):
                    raise ArtifactInvalid("视频超过40 MiB交付限制")
        if not data:
            raise ArtifactInvalid("视频产物为空")
        _write(destination, bytes(data))

    def validate_output(self, path, task, limit):
        path = Path(path)
        if not 0 < path.stat().st_size <= min(limit, MAX_VIDEO_BYTES):
            raise ArtifactInvalid("视频大小不满足交付限制")
        options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
                   "timeout": 180, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        try:
            probe = subprocess.run([self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], **options)
            if probe.returncode:
                raise ArtifactInvalid("ffprobe 无法读取生成视频")
            metadata = json.loads(probe.stdout)
            videos = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"]
            if len(videos) != 1:
                raise ArtifactInvalid("产物必须包含一个视频流")
            video = videos[0]
            numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/")
            fps = float(numerator) / float(denominator)
            duration = float(video.get("duration") or metadata.get("format", {}).get("duration", 0))
            frames = task["duration"] * 24
            frames += (5 - frames % 17) % 17
            if (video.get("width") != task["width"] or video.get("height") != task["height"]
                    or not math.isfinite(duration) or not math.isfinite(fps) or abs(fps - 24) > .05
                    or abs(duration - frames / 24) > .2):
                raise ArtifactInvalid("产物尺寸、帧率或时长与分镜不符")
            decode = subprocess.run([self.ffmpeg, "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path),
                                     "-map", "0:v", "-map", "0:a?", "-f", "null", "-"], **options)
            if decode.returncode:
                raise ArtifactInvalid("生成视频完整解码校验失败")
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
            if isinstance(exc, ArtifactInvalid):
                raise
            raise ArtifactInvalid("视频媒体信息无效") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArtifactInvalid("生成视频解码校验超时") from exc
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size,
                "width": video["width"], "height": video["height"], "fps": fps, "duration": duration}

    def finalize_output(self, source, destination, task, limit):
        """验证原生高清帧后仅中心裁边，不允许低清放大或改变时长/音轨。"""
        source, destination = Path(source), Path(destination)
        native_width, native_height = generation_dimensions(task)
        if task.get("resolution") not in HD_DIMENSIONS:
            raise ArtifactInvalid("只有明确高清档位的任务可以进入裁边处理")
        if destination.exists():
            return self.validate_output(destination, task, limit)
        # 原生分辨率必须精确匹配向上对齐的画布；低清视频不会被补成高清交付。
        source_info = self.validate_output(source, {**task, "width": native_width, "height": native_height}, limit)
        width, height = task["width"], task["height"]
        x, y = (native_width - width) // 2, (native_height - height) // 2
        temporary = destination.with_name(destination.name + "." + uuid4().hex + ".tmp.mp4")
        command = [self.ffmpeg, "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-vf", f"crop={width}:{height}:{x}:{y}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", str(temporary)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise ArtifactInvalid("高清原片中心裁边失败，未重复生成")
            info = self.validate_output(temporary, task, limit)
            # 发布最终视频前验证完整解码并同步数据；中断后只重复本地处理，不重发工作流。
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            return {**info, "source_width": source_info["width"], "source_height": source_info["height"],
                    "processing": "center_crop", "resolution": task["resolution"]}
        except subprocess.TimeoutExpired as exc:
            raise ArtifactInvalid("高清原片中心裁边超时，未重复生成") from exc
        finally:
            temporary.unlink(missing_ok=True)


class H3Worker:
    def __init__(self, transport, cache_root, adapter, *, worker_id=None):
        self.transport, self.adapter = transport, adapter
        self.root = Path(cache_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.adapter.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_job_id = None
        identity_path = self.root / "identity.json"
        with FileLock(self.root / "client.lock"):
            identity = {"schema_version": 1, "server": transport.identity, "comfy_url": adapter.comfy_url,
                        "worker_id": _validate_id(worker_id) if worker_id else uuid4().hex}
            if identity_path.exists():
                existing = _json_read(identity_path)
                if any(existing.get(key) != identity[key] for key in ("server", "comfy_url")):
                    raise ValueError("此 H3 缓存属于另一服务器或 ComfyUI，请使用独立目录")
                if worker_id and existing.get("worker_id") != worker_id:
                    raise ValueError("此 H3 缓存属于另一执行器")
                identity = existing
            else:
                _json_write(identity_path, identity)
            self.worker_id = _validate_id(identity["worker_id"])

    def close(self):
        self.transport.close()
        self.adapter.close()

    def _path(self, job_id, name=""):
        path = (self.root / _validate_id(job_id) / name).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("H3 缓存路径越界")
        return path

    def _save(self, journal):
        _json_write(self._path(journal["manifest"]["job_id"], "journal.json"), journal)

    def _result(self, journal, status, error=None, **fields):
        journal.update(phase="result", result={"status": status, **fields})
        if error:
            journal["result"]["error"] = error[:1000]
        self._save(journal)

    def _uncertain(self, journal, error):
        journal.update(phase="uncertain", result={"status": "uncertain", "error": error[:1000]})
        self._save(journal)
        self.transport.complete(journal["manifest"], journal["result"])
        return "uncertain"

    def _prepare(self, journal):
        manifest = journal["manifest"]
        task = manifest["task"]
        assets = {asset["asset_id"]: asset for asset in manifest["assets"]}
        reference_ids = task.get("reference_ids", [])
        if not isinstance(reference_ids, list) or len(reference_ids) != len(set(reference_ids)) or len(reference_ids) > 9:
            raise ValueError("参考图清单无效")
        # 先验证参数再上传文件；user 的 seed/steps/nodes/path 等字段不会进入模板。
        self.adapter.bind(task, ["preflight/" + _validate_id(aid) + ".png" for aid in reference_ids], 1, "preflight/video")
        names = []
        for aid in reference_ids:
            asset = assets.get(aid)
            if not asset or type(asset.get("size_bytes")) is not int or not 0 < asset["size_bytes"] <= MAX_IMAGE_BYTES:
                raise ValueError("参考图不存在或超过20 MiB")
            extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(asset.get("format"))
            if not extension:
                raise ValueError("参考图类型不支持")
            local = self._path(manifest["job_id"], "references/" + _validate_id(aid) + extension)
            if not local.exists():
                data = self.transport.asset(manifest, aid, MAX_IMAGE_BYTES)
                if len(data) != asset["size_bytes"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                    raise ValueError("下载参考图校验失败")
                _write(local, data)
            data = local.read_bytes()
            if len(data) != asset["size_bytes"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError("缓存参考图校验失败")
            with Image.open(io.BytesIO(data)) as image:
                if image.format != asset["format"] or not 0 < image.width * image.height <= 40_000_000:
                    raise ValueError("参考图媒体信息无效")
                image.verify()
            names.append(self.adapter.upload_reference(manifest["job_id"], aid, data, extension))
        attempt_id = journal.get("attempt_id") or uuid4().hex
        prefix = f"image_workbench/{manifest['job_id']}/{attempt_id}/video"
        seed = int(attempt_id[:15], 16)
        graph = self.adapter.bind(task, names, seed, prefix)
        journal.update(phase="prepared", attempt_id=attempt_id, workflow=graph, output_prefix=prefix,
            workflow_sha256=hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest())
        self._save(journal)

    def _deliver(self, journal):
        result, manifest = journal["result"], journal["manifest"]
        if result["status"] == "succeeded":
            output = self._path(manifest["job_id"], "output.mp4")
            data = output.read_bytes()
            if hashlib.sha256(data).hexdigest() != result["sha256"]:
                raise ValueError("待回传视频已改变，保留任务且不重新生成")
            received = self.transport.upload(manifest, data, result["sha256"])
            if received.get("sha256") != result["sha256"]:
                raise ValueError("服务端视频校验不一致")
        response = self.transport.complete(manifest, result)
        if response.get("status") in {"succeeded", "completed", "failed"}:
            journal["phase"] = "done"
            self._save(journal)
        return response.get("status", result["status"])

    def _execute(self, journal):
        manifest = journal["manifest"]
        if journal["phase"] == "result":
            return self._deliver(journal)
        if journal["phase"] == "ready":
            if not manifest["task"].get("resolution"):
                self._result(journal, "failed", "旧镜头没有高清档位，未开始生成；请创建720p或1080p镜头")
                return self._deliver(journal)
            self.adapter.health()
            if not self.adapter.idle():
                return "busy"
            try:
                self._prepare(journal)
            except ValueError:
                self._result(journal, "failed", "参考图或工作流参数校验失败，未开始生成")
                return self._deliver(journal)
        if journal["phase"] == "prepared":
            self.adapter.health()
            if not self.adapter.idle():
                return "busy"
            self.transport.start(manifest, journal["attempt_id"])
            # POST 发出之前先同步落盘；此标记之后绝不自动重发 /prompt。
            journal.update(phase="submitting", submitted_at=time.time())
            self._save(journal)
            try:
                prompt_id = self.adapter.submit(journal["workflow"], journal["attempt_id"])
            except ComfyRejected as exc:
                self._result(journal, "failed", str(exc))
                return self._deliver(journal)
            except (httpx.HTTPError, ValueError):
                prompt_id = None
            if prompt_id:
                journal.update(phase="running", prompt_id=prompt_id)
                self._save(journal)
        if journal["phase"] in {"submitting", "uncertain"}:
            if journal.get("prompt_id"):
                journal["phase"] = "running"
                self._save(journal)
            elif not journal.get("workflow"):
                return self._uncertain(journal, "本地提交检查点缺失，需核对 ComfyUI 历史后处理")
            else:
                matches = self.adapter.reconcile(journal["workflow"])
                if len(matches) != 1:
                    return self._uncertain(journal, "ComfyUI 提交结果暂不能唯一确认；保留积分冻结，不会重复生成")
                journal.update(phase="running", prompt_id=matches[0])
                self._save(journal)
        if journal["phase"] == "running":
            self.transport.start(manifest, journal["attempt_id"], journal["prompt_id"])
            deadline = time.monotonic() + self.adapter.timeout
            while True:
                history = self.adapter.history(journal["prompt_id"])
                if history:
                    status = history.get("status", {})
                    if status.get("status_str") == "error":
                        journal["history"] = history
                        self._result(journal, "failed", "ComfyUI 明确报告生成失败")
                        return self._deliver(journal)
                    if status.get("completed"):
                        journal.update(phase="generated", history=history)
                        self._save(journal)
                        break
                if time.monotonic() >= deadline:
                    return self._uncertain(journal, "ComfyUI 仍未确认完成，保留积分冻结并继续对账")
                time.sleep(self.adapter.poll_seconds)
        if journal["phase"] == "generated":
            output = self._path(manifest["job_id"], "output.mp4")
            limit = min(MAX_VIDEO_BYTES, manifest.get("max_output_bytes", MAX_VIDEO_BYTES))
            try:
                if manifest["task"].get("resolution"):
                    source = self._path(manifest["job_id"], "source.mp4")
                    if not source.exists() and not output.exists():
                        self.adapter.download(journal["history"], source, journal["output_prefix"], limit)
                    info = self.adapter.finalize_output(source, output, manifest["task"], limit)
                else:
                    # 旧 journal/图不转换档位、不重新绑定；按原尺寸验证和回收原结果。
                    if not output.exists():
                        self.adapter.download(journal["history"], output, journal["output_prefix"], limit)
                    info = self.adapter.validate_output(output, manifest["task"], limit)
            except ArtifactInvalid as exc:
                self._result(journal, "failed", str(exc))
                return self._deliver(journal)
            journal["media"] = info
            self._result(journal, "succeeded", sha256=info["sha256"])
            return self._deliver(journal)
        raise ValueError("H3 检查点状态无效")

    def run_once(self):
        with FileLock(self.root / "client.lock"), FileLock(self.adapter.lock_path):
            pending = []
            for path in sorted(self.root.glob("*/journal.json")):
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("H3 检查点路径越界")
                journal = _json_read(path)
                if journal.get("phase") != "done":
                    pending.append(journal)
            if pending:
                journal = pending[0]
            else:
                # GPU 忙时不抢占、不领取新镜头；已领取任务依靠缓存恢复。
                self.adapter.health()
                if not self.adapter.idle():
                    return "busy"
                manifest = self.transport.claim(self.worker_id)
                if manifest is None:
                    return "idle"
                _validate_id(manifest["job_id"])
                _validate_id(manifest["claim_id"])
                if manifest.get("worker_id") != self.worker_id:
                    raise ValueError("服务端领取身份不一致")
                phase = "uncertain" if manifest.get("attempt_id") else "ready"
                journal = {"schema_version": 1, "phase": phase, "manifest": manifest}
                if manifest.get("attempt_id"):
                    attempt = _validate_id(manifest["attempt_id"])
                    journal["attempt_id"] = attempt
                    if manifest.get("prompt_id"):
                        # 云端已持久化回执而本地缓存缺失时，仍可按原 prompt 查询及回收。
                        # output_prefix 按不可变 job/attempt 重建，绝不重建或提交新工作流。
                        journal.update(phase="running", prompt_id=manifest["prompt_id"],
                            output_prefix=f"image_workbench/{manifest['job_id']}/{attempt}/video")
                self._save(journal)
            self.last_job_id = journal["manifest"]["job_id"]
            self.transport.heartbeat(journal["manifest"])
            stop = threading.Event()
            def pulse():
                while not stop.wait(15):
                    try:
                        self.transport.heartbeat(journal["manifest"])
                    except (httpx.HTTPError, OSError, ValueError):
                        pass
            thread = threading.Thread(target=pulse, daemon=True)
            thread.start()
            try:
                return self._execute(journal)
            finally:
                stop.set()
                thread.join(timeout=35)

    def serve(self, *, once=False, poll_seconds=5):
        if not math.isfinite(float(poll_seconds)) or float(poll_seconds) <= 0:
            raise ValueError("轮询间隔必须为有限正数")
        last_report = None
        while True:
            try:
                status = self.run_once()
                report = (self.last_job_id, status)
                if status not in {"idle", "busy"} and report != last_report:
                    print(f"H3 视频任务 {self.last_job_id} 状态：{status}", flush=True)
                last_report = report
            except LockBusyError:
                if once:
                    return "busy"
            except (httpx.HTTPError, OSError) as exc:
                if once:
                    raise
                # 不打印请求头/令牌/媒体内容。
                report = (type(exc).__name__, "连接或存储暂不可用，保留 H3 检查点并等待恢复")
                if report != last_report:
                    print(report[1], flush=True)
                last_report = report
            if once:
                return
            time.sleep(poll_seconds)
