import copy
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import IdentityStore
from image_workbench.h3_worker import (ArtifactInvalid, ComfyH3Adapter, DEFAULT_MODELS, H3Worker,
    LocalVideoTransport, RemoteVideoTransport, MAX_VIDEO_BYTES, HD_DIMENSIONS, generation_dimensions)
from image_workbench.locking import FileLock, LockBusyError
from image_workbench.remote_access import RemoteAccess
from image_workbench.video_store import VideoStore
from image_workbench.web import create_app


def uid():
    return uuid4().hex


def container_fixture():
    def box(kind, data):
        return struct.pack(">I4s", len(data) + 8, kind) + data
    return box(b"ftyp", b"isom\0\0\0\0isommp42") + box(b"moov", b"metadata") + box(b"mdat", b"media")


def png():
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), "blue").save(stream, format="PNG")
    return stream.getvalue()


def template(mode):
    # The graph preserves native loader/sampler/switch/output contracts; no GPU code is imported.
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": DEFAULT_MODELS["reference_diffusion" if mode == "R2V" else "diffusion"]}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": DEFAULT_MODELS["encoder"]}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": DEFAULT_MODELS["video_vae"]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": DEFAULT_MODELS["audio_vae"]}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo" if mode == "R2V" else "MiniMaxH3ImageToVideo",
              "inputs": {"clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0],
                         "prompt": "template example", "width": 768, "height": 448, "length": 124}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
        "7": {"class_type": "BasicScheduler", "inputs": {"steps": 99, "model": ["14", 0]}},
        "8": {"class_type": "SamplerCustomAdvanced", "inputs": {"latent_image": ["5", 1],
              "noise": ["6", 0], "sigmas": ["7", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["4", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "audio": ["10", 0], "fps": 12}},
        "12": {"class_type": "SaveVideo", "inputs": {"video": ["11", 0], "filename_prefix": "template/output", "format": "auto", "format.codec": "auto"}},
        "13": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": "missing_ref2v_lora.safetensors"}},
        "14": {"class_type": "ComfySwitchNode", "inputs": {"switch": True, "on_true": ["13", 0], "on_false": ["1", 0]}},
        "15": {"class_type": "LoadImage", "inputs": {"image": "old_reference.png"}},
    }


class FakeComfy:
    def __init__(self):
        self.graphs = {}
        self.posts = 0
        self.uploads = []
        self.fail_submit = None
        self.external_busy = False
        self.completed = True
        self.error = False
        self.hidden = False
        self.history_reads = 0
        self.downloads = 0
        self.output = container_fixture()
        self.loss_download = False
        self.client = httpx.Client(base_url="http://127.0.0.1:8189", transport=httpx.MockTransport(self.request))

    def entry(self, prompt_id, graph):
        prefix = next(node["inputs"]["filename_prefix"] for node in graph.values() if node["class_type"] == "SaveVideo")
        folder, name = prefix.rsplit("/", 1)
        return {"prompt": [1, prompt_id, graph, {}, ["12"]], "status": {
            "completed": self.completed, "status_str": "error" if self.error else "success"},
            "outputs": {"12": {"images": [{"filename": name + "_00001_.mp4", "subfolder": folder, "type": "output"}]}}}

    def request(self, request):
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "devices": [{"name": "mock GPU", "type": "cuda"}]})
        if path == "/object_info":
            info = {}
            for mode in ("T2V", "I2V", "R2V"):
                for node in template(mode).values():
                    info.setdefault(node["class_type"], {"input": {"required": {}}})
            mappings = {"UNETLoader": ("unet_name", [DEFAULT_MODELS["diffusion"], DEFAULT_MODELS["reference_diffusion"]]),
                        "CLIPLoader": ("clip_name", [DEFAULT_MODELS["encoder"]]),
                        "VAELoader": ("vae_name", [DEFAULT_MODELS["video_vae"], DEFAULT_MODELS["audio_vae"]]),
                        "LoraLoaderModelOnly": ("lora_name", [DEFAULT_MODELS["turbo_lora"]])}
            for kind, (key, names) in mappings.items():
                info[kind]["input"]["required"][key] = [names, {}]
            return httpx.Response(200, json=info)
        if path == "/queue":
            queued = [[1, prompt_id, graph, {}, ["12"]] for prompt_id, graph in self.graphs.items()
                      if not self.completed and not self.hidden]
            if self.external_busy:
                queued.append([0, "other-job", {"other": "workflow"}])
            return httpx.Response(200, json={"queue_running": queued, "queue_pending": []})
        if path == "/upload/image":
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + request.headers["content-type"] + "\r\n\r\n").encode() + request.read())
            parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
            name = parts["image"].get_filename()
            subfolder = parts["subfolder"].get_payload(decode=True).decode()
            self.uploads.append((name, parts["image"].get_payload(decode=True)))
            return httpx.Response(200, json={"name": name, "subfolder": subfolder, "type": "input"})
        if path == "/prompt":
            self.posts += 1
            body = json.loads(request.read())
            if self.fail_submit == "reject":
                return httpx.Response(400, json={"error": {"type": "prompt_outputs_failed_validation"}})
            if self.fail_submit == "before":
                raise httpx.ReadError("lost before acceptance", request=request)
            prompt_id = "prompt-" + str(len(self.graphs) + 1)
            self.graphs[prompt_id] = body["prompt"]
            if self.fail_submit == "after":
                raise httpx.ReadError("lost acknowledgement", request=request)
            if self.fail_submit == "crash":
                raise KeyboardInterrupt()
            return httpx.Response(200, json={"prompt_id": prompt_id, "node_errors": {}})
        if path.startswith("/history"):
            self.history_reads += 1
            entries = {} if self.hidden else {pid: self.entry(pid, graph) for pid, graph in self.graphs.items()}
            if path != "/history":
                pid = path.split("/")[-1]
                entries = {pid: entries[pid]} if pid in entries else {}
            return httpx.Response(200, json=entries)
        if path == "/view":
            self.downloads += 1
            if self.loss_download:
                self.loss_download = False
                raise httpx.ReadError("lost download", request=request)
            return httpx.Response(200, content=self.output)
        raise AssertionError(path)


class H3WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workflow_dir = self.root / "templates"
        self.workflow_dir.mkdir()
        for mode in ("T2V", "I2V", "R2V"):
            (self.workflow_dir / (mode.lower() + ".api.json")).write_text(json.dumps(template(mode)), encoding="utf-8")
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.identity = IdentityStore(self.storage)
        self.admin = self.identity.bootstrap([])["account_id"]
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "alice", None)
        self.store = VideoStore(self.storage, self.identity)
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        self.pid = uid()
        self.store.create_project(self.aid, self.wid, self.pid, self.pid, "保留剧本原文")
        self.comfy = FakeComfy()
        self.addCleanup(self.comfy.client.close)
        self.transport = LocalVideoTransport(self.store)
        self.adapter = ComfyH3Adapter("http://127.0.0.1:8189", self.workflow_dir, client=self.comfy.client,
            lock_path=self.root / "gpu.lock", timeout=.02, poll_seconds=.001)
        # State-machine tests use a container fixture. Full real decoding is verified separately below.
        self.media = mock.patch.object(self.adapter, "validate_output", side_effect=lambda path, task, limit: {
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "size_bytes": Path(path).stat().st_size})
        self.media.start()
        self.addCleanup(self.media.stop)
        def finalize(source, destination, task, limit):
            if not Path(destination).exists():
                Path(destination).write_bytes(Path(source).read_bytes())
            return self.adapter.validate_output(destination, task, limit)
        self.finalizer = mock.patch.object(self.adapter, "finalize_output", side_effect=finalize)
        self.finalizer.start()
        self.addCleanup(self.finalizer.stop)
        self.tools = mock.patch("image_workbench.h3_worker.shutil.which", side_effect=lambda value: value)
        self.tools.start()
        self.addCleanup(self.tools.stop)
        self.cache = self.root / "cache"

    def worker(self, **kwargs):
        return H3Worker(self.transport, self.cache, self.adapter, **kwargs)

    def submit(self, mode="T2V", references=0, resolution="720p"):
        asset_ids = []
        for index in range(references):
            asset_ids.extend(asset["asset_id"] for asset in self.store.add_assets(
                self.aid, self.wid, self.pid, [(str(index) + ".png", png())]))
        shot = self.store.create_shot(self.aid, self.wid, self.pid, request_id=uid(), name="测试镜头",
            prompt="人物向镜头挥手", mode=mode, duration=4, aspect_ratio="square", reference_ids=asset_ids,
            resolution=resolution)
        job = self.store.submit(self.aid, self.wid, self.pid, shot["shot_id"], request_id=uid(),
                                confirm_generation=True, expected_credit=8 if resolution == "720p" else 16)
        return shot, job

    def journal(self, job):
        return json.loads((self.cache / job["job_id"] / "journal.json").read_text(encoding="utf-8"))

    def test_end_to_end_single_submission_and_billed_once(self):
        shot, job = self.submit()
        before = self.identity.wallet(self.aid)
        worker = self.worker()
        self.assertEqual(worker.run_once(), "completed")
        self.assertEqual(worker.run_once(), "idle")
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.journal(job)["phase"], "done")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.store.output_file(self.aid, self.wid, self.pid, shot["shot_id"]).read_bytes(), self.comfy.output)
        self.assertGreater(before["frozen_credit"], 0)

    def test_native_binding_selects_r2v_base_and_preserves_reference_order(self):
        refs = ["refs/first.png", "refs/last.png"]
        task = {"mode": "R2V", "prompt": "use <Picture 1>", "duration": 4, "width": 768, "height": 448,
                "steps": 1, "nodes": {"malicious": {}}, "path": "C:/outside"}
        graph = self.adapter.bind(task, refs, 42, "project/job/video")
        self.assertFalse(any(node["class_type"] in {"LoraLoaderModelOnly", "ComfySwitchNode"} for node in graph.values()))
        self.assertEqual(graph["5"]["inputs"]["ref_images.ref_image_1"], ["9001", 0])
        self.assertEqual(graph["9000"]["inputs"]["image"], refs[0])
        self.assertEqual(graph["7"]["inputs"]["steps"], 20)
        self.assertEqual(graph["5"]["inputs"]["length"], 107)
        self.assertNotIn("15", graph)
        self.assertEqual(graph["12"]["inputs"]["filename_prefix"], "project/job/video")
        i2v = self.adapter.bind({**task, "mode": "I2V"}, refs, 42, "project/job/video")
        self.assertEqual(i2v["5"]["inputs"]["first_frame"], ["9000", 0])
        self.assertEqual(i2v["5"]["inputs"]["last_frame"], ["9001", 0])
        self.assertEqual(i2v["7"]["inputs"]["steps"], 8)
        self.assertEqual(i2v["13"]["inputs"]["lora_name"], DEFAULT_MODELS["turbo_lora"])
        for bad in ({"duration": 3}, {"width": 777}, {"mode": "EXEC"}, {"prompt": ""}):
            with self.assertRaises(ValueError):
                self.adapter.bind({**task, **bad}, refs, 42, "project/job/video")
        with self.assertRaises(ValueError):
            self.adapter.bind(task, ["../other.png"], 42, "project/job/video")

    def test_hd_binding_uses_native_ceil32_canvas_and_rejects_fake_hd(self):
        for resolution, ratios in HD_DIMENSIONS.items():
            for ratio, (width, height) in ratios.items():
                with self.subTest(resolution=resolution, ratio=ratio):
                    task = {"mode": "T2V", "prompt": "native HD", "duration": 4, "width": width,
                            "height": height, "aspect_ratio": ratio, "resolution": resolution}
                    graph = self.adapter.bind(task, [], 42, "project/job/video")
                    inputs = graph["5"]["inputs"]
                    self.assertEqual((inputs["width"], inputs["height"]),
                        ((width + 31) // 32 * 32, (height + 31) // 32 * 32))
                    self.assertEqual(graph["9"]["class_type"], "VAEDecode")
                    self.assertEqual(graph["10"]["class_type"], "VAEDecodeAudio")
                    self.assertGreaterEqual(inputs["width"], width)
                    self.assertGreaterEqual(inputs["height"], height)
        valid = {"resolution": "720p", "width": 1280, "height": 720, "aspect_ratio": "landscape"}
        for change in ({"resolution": "4k"}, {"width": 768, "height": 448}, {"width": 7680},
                       {"aspect_ratio": "portrait"}, {"width": True}):
            with self.assertRaises(ValueError):
                generation_dimensions({**valid, **change})
        self.assertEqual(generation_dimensions({"width": 768, "height": 448}), (768, 448))

    def test_reference_download_hash_and_comfy_upload_are_real(self):
        _, job = self.submit(mode="I2V", references=1)
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(len(self.comfy.uploads), 1)
        self.assertEqual(self.comfy.uploads[0][1], png())
        graph = self.journal(job)["workflow"]
        self.assertIn(job["job_id"], graph["9000"]["inputs"]["image"])

    def test_lost_prompt_response_reconciles_without_second_submission(self):
        _, job = self.submit()
        self.comfy.fail_submit = "after"
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.journal(job)["prompt_id"], "prompt-1")

    def test_crash_after_submit_restart_only_reconciles(self):
        _, job = self.submit()
        self.comfy.fail_submit = "crash"
        with self.assertRaises(KeyboardInterrupt):
            self.worker().run_once()
        self.assertEqual(self.journal(job)["phase"], "submitting")
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)

    def test_unknown_submission_retains_freeze_until_history_appears(self):
        _, job = self.submit()
        self.comfy.fail_submit = "after"
        self.comfy.hidden = True
        self.assertEqual(self.worker().run_once(), "uncertain")
        self.assertEqual(self.worker().run_once(), "uncertain")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 8)
        self.assertEqual(self.comfy.posts, 1)
        self.comfy.hidden = False
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.journal(job)["phase"], "done")

    def test_no_match_does_not_assume_submission_failed_or_retry(self):
        _, job = self.submit()
        self.comfy.fail_submit = "before"
        self.assertEqual(self.worker().run_once(), "uncertain")
        self.assertEqual(self.worker().run_once(), "uncertain")
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.journal(job)["phase"], "uncertain")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 8)

    def test_exact_graph_reconcile_never_adopts_other_work(self):
        _, job = self.submit()
        self.comfy.fail_submit = "before"
        self.assertEqual(self.worker().run_once(), "uncertain")
        graph = copy.deepcopy(self.journal(job)["workflow"])
        graph["5"]["inputs"]["prompt"] = "different prompt"
        self.comfy.graphs["other"] = graph
        self.assertEqual(self.worker().run_once(), "uncertain")
        self.assertEqual(self.comfy.posts, 1)

    def test_explicit_rejection_and_execution_error_refund(self):
        self.submit()
        self.comfy.fail_submit = "reject"
        self.assertEqual(self.worker().run_once(), "failed")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.submit()
        self.comfy.fail_submit = None
        self.comfy.error = True
        self.assertEqual(self.worker().run_once(), "failed")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.comfy.posts, 2)

    def test_lost_start_response_reuses_attempt_without_duplicate_prompt(self):
        _, job = self.submit()
        original = self.transport.start
        def lose(manifest, attempt_id, prompt_id=None):
            original(manifest, attempt_id, prompt_id)
            raise httpx.ReadError("lost start response")
        with mock.patch.object(self.transport, "start", side_effect=lose):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        self.assertEqual(self.comfy.posts, 0)
        attempt = self.journal(job)["attempt_id"]
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.journal(job)["attempt_id"], attempt)
        self.assertEqual(self.comfy.posts, 1)

    def test_lost_download_and_complete_responses_only_redeliver(self):
        _, job = self.submit()
        self.comfy.loss_download = True
        with self.assertRaises(httpx.ReadError):
            self.worker().run_once()
        self.assertEqual(self.journal(job)["phase"], "generated")
        original = self.transport.complete
        def lose(manifest, result):
            original(manifest, result)
            raise httpx.ReadError("lost complete response")
        with mock.patch.object(self.transport, "complete", side_effect=lose):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        self.assertEqual(self.journal(job)["phase"], "result")
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_missing_local_journal_recovers_server_prompt_without_new_submission(self):
        _, job = self.submit()
        self.comfy.loss_download = True
        worker = self.worker()
        with self.assertRaises(httpx.ReadError):
            worker.run_once()
        journal_path = self.cache / job["job_id"] / "journal.json"
        journal_path.unlink()  # Only this test-owned temporary checkpoint is removed.
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)

    def test_server_prompt_recovery_survives_another_timeout_without_workflow(self):
        _, job = self.submit()
        self.comfy.completed = False
        self.assertEqual(self.worker().run_once(), "uncertain")
        (self.cache / job["job_id"] / "journal.json").unlink()
        # A currently running own prompt is in the queue. Restore receipt after it leaves the queue,
        # while history still says not completed, reproducing delayed completion metadata.
        with mock.patch.object(self.adapter, "idle", return_value=True):
            self.assertEqual(self.worker().run_once(), "uncertain")
        self.assertNotIn("workflow", self.journal(job))
        self.comfy.completed = True
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)

    def test_running_timeout_is_uncertain_and_does_not_refund(self):
        self.submit()
        self.comfy.completed = False
        self.assertEqual(self.worker().run_once(), "uncertain")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 8)
        self.comfy.completed = True
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)

    def test_hd_postprocessing_restart_only_reuses_original_source_and_workflow(self):
        _, job = self.submit(resolution="1080p")
        original = self.adapter.finalize_output
        with mock.patch.object(self.adapter, "finalize_output", side_effect=OSError("temporary disk failure")):
            with self.assertRaises(OSError):
                self.worker().run_once()
        first = self.journal(job)
        self.assertEqual(first["phase"], "generated")
        self.assertEqual(first["workflow"]["5"]["inputs"]["width"], 1088)
        self.assertTrue((self.cache / job["job_id"] / "source.mp4").exists())
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.comfy.downloads, 1)
        self.assertEqual(first["workflow"], self.journal(job)["workflow"])
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 16)

    def test_legacy_unstarted_task_refunds_but_submitted_graph_is_recovered_unchanged(self):
        _, job = self.submit()
        original_claim = self.transport.claim
        def legacy_claim(worker_id):
            manifest = original_claim(worker_id)
            if manifest:
                manifest["task"].pop("resolution", None)
                manifest["task"].update(width=512, height=512)
            return manifest
        with mock.patch.object(self.transport, "claim", side_effect=legacy_claim):
            self.assertEqual(self.worker().run_once(), "failed")
        self.assertEqual(self.comfy.posts, 0)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        _, old_job = self.submit()
        worker = self.worker()
        manifest = original_claim(worker.worker_id)
        manifest["task"].pop("resolution", None)
        manifest["task"].update(width=512, height=512)
        attempt = uid()
        prefix = f"image_workbench/{old_job['job_id']}/{attempt}/video"
        graph = self.adapter.bind(manifest["task"], [], 42, prefix)
        self.transport.start(manifest, attempt)
        # Simulate the previously submitted legacy graph and durable local intent.
        self.comfy.graphs["original-legacy-prompt"] = copy.deepcopy(graph)
        worker._save({"schema_version": 1, "phase": "submitting", "manifest": manifest,
                      "attempt_id": attempt, "workflow": graph, "output_prefix": prefix})
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 0)
        self.assertEqual(self.journal(old_job)["workflow"], graph)

    def test_busy_queue_and_shared_gpu_lock_prevent_claim_or_interruption(self):
        _, job = self.submit()
        self.comfy.external_busy = True
        self.assertEqual(self.worker().run_once(), "busy")
        self.assertEqual(self.comfy.posts, 0)
        self.assertFalse((self.cache / job["job_id"] / "journal.json").exists())
        self.comfy.external_busy = False
        worker = self.worker()
        with FileLock(self.adapter.lock_path):
            with self.assertRaises(LockBusyError):
                worker.run_once()
        self.assertEqual(worker.run_once(), "completed")

    def test_oversize_or_foreign_output_fails_without_repeating_generation(self):
        _, job = self.submit()
        original = self.comfy.entry
        def foreign(prompt_id, graph):
            entry = original(prompt_id, graph)
            entry["outputs"]["12"]["images"][0]["subfolder"] = "some_other_job"
            return entry
        with mock.patch.object(self.comfy, "entry", side_effect=foreign):
            self.assertEqual(self.worker().run_once(), "failed")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.comfy.downloads, 0)
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.journal(job)["phase"], "done")

    def test_check_read_only_and_missing_model_report(self):
        report = self.adapter.check()
        self.assertTrue(report["available"], report)
        self.assertEqual(self.comfy.posts, 0)
        self.assertFalse(self.comfy.uploads)
        self.adapter.models["encoder"] = "missing.safetensors"
        report = self.adapter.check()
        self.assertFalse(report["available"])
        self.assertEqual(report["missing_models"], ["missing.safetensors"])
        with mock.patch.object(self.adapter, "health", side_effect=httpx.ConnectError("offline")):
            self.assertFalse(self.adapter.check()["available"])

    def test_cache_identity_and_remote_transport_auth_boundary(self):
        self.worker()
        with self.assertRaises(ValueError):
            self.worker(worker_id=uid())
        calls = []
        def response(request):
            calls.append(request)
            if request.url.path.endswith("/claim"):
                return httpx.Response(200, json={"job": None})
            if "/assets/" in request.url.path:
                return httpx.Response(200, content=b"asset")
            return httpx.Response(200, json={"ok": True})
        client = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(response))
        self.addCleanup(client.close)
        transport = RemoteVideoTransport("https://example.test", "a" * 40, client=client)
        manifest = {"job_id": uid(), "claim_id": uid(), "worker_id": uid()}
        self.assertIsNone(transport.claim(manifest["worker_id"]))
        transport.start(manifest, uid(), "prompt-123")
        self.assertEqual(calls[-1].headers["x-worker-id"], manifest["worker_id"])
        self.assertEqual(calls[-1].headers["x-claim-id"], manifest["claim_id"])
        self.assertEqual(calls[-1].headers["authorization"], "Bearer " + "a" * 40)
        with self.assertRaises(ValueError):
            transport.asset(manifest, uid(), 2)
        with self.assertRaises(ValueError):
            RemoteVideoTransport("http://remote.example", "a" * 40)

    def test_remote_http_end_to_end_and_lost_upload_acknowledgement(self):
        shot, _ = self.submit(mode="I2V", references=1)
        access = RemoteAccess("http://localhost", "x" * 40)
        app = create_app(self.storage, backend="local", remote_access=access)
        client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(client.close)
        self.transport = RemoteVideoTransport("http://localhost", access.worker_token, client=client)
        original = client.request
        lost = False
        def request(method, url, **kwargs):
            nonlocal lost
            response = original(method, url, **kwargs)
            if str(url).endswith("/output") and not lost:
                self.assertEqual(response.status_code, 200, response.text)
                lost = True
                raise httpx.ReadError("lost remote upload acknowledgement")
            return response
        with mock.patch.object(client, "request", side_effect=request):
            with self.assertRaises(httpx.ReadError):
                self.worker().run_once()
        self.assertEqual(self.worker().run_once(), "completed")
        self.assertEqual(self.comfy.posts, 1)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], 8)
        self.assertEqual(self.store.output_file(self.aid, self.wid, self.pid, shot["shot_id"]).read_bytes(), self.comfy.output)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要已安装 ffmpeg/ffprobe")
class H3RealMediaValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.path = cls.root / "sample.mp4"
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24",
            "-frames:v", "107", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.path)],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("ffmpeg fixture creation failed")
        for mode in ("T2V", "I2V", "R2V"):
            (cls.root / (mode.lower() + ".api.json")).write_text(json.dumps(template(mode)), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_mp4_full_decode_and_dimension_duration_validation(self):
        adapter = ComfyH3Adapter("http://127.0.0.1:8189", self.root)
        self.addCleanup(adapter.close)
        task = {"width": 64, "height": 64, "duration": 4}
        result = adapter.validate_output(self.path, task, MAX_VIDEO_BYTES)
        self.assertEqual(result["fps"], 24)
        self.assertAlmostEqual(result["duration"], 107 / 24, places=3)
        with self.assertRaises(ArtifactInvalid):
            adapter.validate_output(self.path, {**task, "width": 96}, MAX_VIDEO_BYTES)
        with self.assertRaises(ArtifactInvalid):
            adapter.validate_output(self.path, task, 4)
        invalid = self.root / "broken.mp4"
        invalid.write_bytes(container_fixture())
        with self.assertRaises(ArtifactInvalid):
            adapter.validate_output(invalid, task, MAX_VIDEO_BYTES)

    def test_native_hd_center_crop_exact_delivery_audio_and_recovery(self):
        adapter = ComfyH3Adapter("http://127.0.0.1:8189", self.root)
        self.addCleanup(adapter.close)
        for resolution in ("720p", "1080p"):
            with self.subTest(resolution=resolution):
                width, height = HD_DIMENSIONS[resolution]["landscape"]
                task = {"width": width, "height": height, "duration": 4,
                        "resolution": resolution, "aspect_ratio": "landscape"}
                native_width, native_height = generation_dimensions(task)
                y = (native_height - height) // 2
                source = self.root / (resolution + "-native.mp4")
                output = self.root / (resolution + "-delivery.mp4")
                # The red native padding must disappear; no scaling is necessary or permitted.
                fixture = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    f"color=c=red:s={native_width}x{native_height}:r=24,drawbox=x=0:y={y}:w={width}:h={height}:color=blue:t=fill",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000", "-frames:v", "107",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(source)], capture_output=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(fixture.returncode, 0, fixture.stderr)
                info = adapter.finalize_output(source, output, task, MAX_VIDEO_BYTES)
                self.assertEqual((info["width"], info["height"]), (width, height))
                self.assertEqual((info["source_width"], info["source_height"]), (native_width, native_height))
                self.assertEqual(info["processing"], "center_crop")
                first = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(first.returncode, 0)
                self.assertLess(first.stdout[0], 25)
                self.assertGreater(first.stdout[2], 220)
                audio_checksums = []
                for path in (source, output):
                    audio = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                        "-f", "md5", "-"], capture_output=True, timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.assertEqual(audio.returncode, 0, audio.stderr)
                    audio_checksums.append(audio.stdout)
                self.assertEqual(*audio_checksums)
                # Atomic final output can be reused after a crash, even if source is unavailable.
                recovered = adapter.finalize_output(self.root / "unavailable-source.mp4", output, task, MAX_VIDEO_BYTES)
                self.assertEqual(recovered["sha256"], info["sha256"])
        invalid_task = {"resolution": "720p", "aspect_ratio": "landscape", "width": 1280, "height": 720, "duration": 4}
        with self.assertRaises(ArtifactInvalid):
            adapter.finalize_output(self.path, self.root / "never-upscale.mp4", invalid_task, MAX_VIDEO_BYTES)
        self.assertFalse((self.root / "never-upscale.mp4").exists())


if __name__ == "__main__":
    unittest.main()
