import hashlib
import io
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import IdentityStore, digest
from image_workbench.remote_access import RemoteAccess
from image_workbench.storage import _now
from image_workbench.video_store import VideoStore, inspect_mp4
from image_workbench.web import create_app
from image_workbench.workbench_state import ConflictError, WorkbenchState


def uid():
    return uuid4().hex


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(stream, format="PNG")
    return stream.getvalue()


def mp4():
    # Container fixture: the execution adapter separately verifies actual codecs.
    def box(kind, content):
        return struct.pack(">I4s", len(content) + 8, kind) + content
    return box(b"ftyp", b"isom\0\0\0\0isommp42") + box(b"moov", b"metadata") + box(b"mdat", b"media")


class VideoTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"IMAGE_WORKBENCH_ENABLE_1080P": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = StorageManager(StorageConfig(data_root=Path(self.temp.name) / "data"))
        self.identity = IdentityStore(self.storage)
        admin = self.identity.bootstrap([])
        self.admin = admin["account_id"]
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "alice", None)
            self.bid, self.bwid = self.identity._account(db, "bobby", None)
        self.other_wid = self.identity.create_workspace(self.aid, "其他空间")["workspace_id"]
        self.store = VideoStore(self.storage, self.identity)
        self.pid = uid()
        self.store.create_project(self.aid, self.wid, self.pid, "视频项目", "第一场\n保留原始脚本")

    def shot(self, **extra):
        body = dict(request_id=uid(), name="分镜一", prompt="人物向镜头挥手", mode="T2V", duration=5,
                    aspect_ratio="landscape", reference_ids=[])
        body.update(extra)
        return self.store.create_shot(self.aid, self.wid, self.pid, **body)

    def submit(self, shot, **extra):
        body = dict(request_id=uid(), confirm_generation=True, expected_credit=10)
        body.update(extra)
        return self.store.submit(self.aid, self.wid, self.pid, shot["shot_id"], **body)

    def ready(self):
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        shot = self.shot()
        job = self.submit(shot)
        worker = uid()
        manifest = self.store.claim(worker)
        self.store.start(job["job_id"], worker, manifest["claim_id"], uid())
        return shot, job, worker, manifest["claim_id"]

    def wallet(self):
        return self.identity.wallet(self.aid)

    def test_default_tier_rates_admin_validation_quote_snapshot_and_idempotency(self):
        shot = self.shot()
        self.assertEqual(self.store.config()["rates"], {"720p": 40, "1080p": 80})
        self.assertEqual(shot["resolution"], "720p")
        self.assertEqual(shot["estimated_credit"], 200)
        with self.assertRaises(PermissionError):
            self.store.set_config(self.aid, {"720p": 2, "1080p": 4})
        for rate in (0, True, 10001, 1.5):
            with self.assertRaises(ValueError):
                self.store.set_config(self.admin, {"720p": rate, "1080p": 80})
        for rates in ({}, {"720p": 40}, {"720p": 40, "1080p": 80, "4k": 120}, 2):
            with self.assertRaises(ValueError):
                self.store.set_config(self.admin, rates)
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        with self.assertRaises(ConflictError):
            self.submit(shot, expected_credit=9)
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        rid = uid()
        job = self.submit(shot, request_id=rid)
        self.store.set_config(self.admin, {"720p": 7, "1080p": 14})
        replay = self.submit(shot, request_id=rid)
        self.assertEqual(replay["job_id"], job["job_id"])
        self.assertEqual(replay["credit_cost"], 10)
        self.assertEqual(replay["resolution"], "720p")
        self.assertEqual(replay["credits_per_second"], 2)
        self.assertEqual(self.wallet()["frozen_credit"], 10)
        self.assertEqual(VideoStore(self.storage).config()["rates"], {"720p": 7, "1080p": 14})

    def test_reference_validation_dedup_order_and_space_isolation(self):
        assets = self.store.add_assets(self.aid, self.wid, self.pid, [("a.png", png()), ("b.png", png("blue"))])
        again = self.store.add_assets(self.aid, self.wid, self.pid, [("renamed.png", png())])
        self.assertEqual(again[0]["asset_id"], assets[0]["asset_id"])
        refs = [a["asset_id"] for a in reversed(assets)]
        shot = self.shot(mode="I2V", reference_ids=refs)
        self.assertEqual(shot["reference_ids"], refs)
        for aid, wid in ((self.bid, self.bwid), (self.aid, self.other_wid), (self.bid, self.wid)):
            with self.assertRaises(FileNotFoundError):
                self.store.project(aid, wid, self.pid)
            with self.assertRaises(FileNotFoundError):
                self.store.asset_file(aid, wid, self.pid, refs[0])
        with self.assertRaises(ValueError):
            self.shot(mode="T2V", reference_ids=refs)
        with self.assertRaises(ValueError):
            self.shot(mode="I2V", reference_ids=[])
        with self.assertRaises(ValueError):
            self.store.add_assets(self.aid, self.wid, self.pid, [("evil.png", b"not an image")])
        with self.assertRaises(ValueError):
            self.store.project(self.aid, self.wid, "../outside")

    @patch.dict("os.environ", {"IMAGE_WORKBENCH_ENABLE_1080P": "1"})
    def test_project_and_shot_request_idempotency(self):
        project = self.store.create_project(self.aid, self.wid, self.pid, "视频项目", "第一场\n保留原始脚本")
        self.assertEqual(project["script"], "第一场\n保留原始脚本")
        with self.assertRaises(ConflictError):
            self.store.create_project(self.aid, self.wid, self.pid, "不同内容")
        rid = uid()
        self.shot(request_id=rid)
        self.assertEqual(self.shot(request_id=rid)["shot_id"], rid)
        with self.assertRaises(ConflictError):
            self.shot(request_id=rid, duration=6)
        with self.assertRaises(ConflictError):
            self.shot(request_id=rid, resolution="1080p")

    @patch.dict("os.environ", {"IMAGE_WORKBENCH_ENABLE_1080P": "1"})
    def test_resolution_tiers_deliver_exact_sizes_and_reject_invalid_resolution(self):
        expected = {
            "720p": {"landscape": (1280, 720), "portrait": (720, 1280), "square": (720, 720)},
            "1080p": {"landscape": (1920, 1080), "portrait": (1080, 1920), "square": (1080, 1080)},
        }
        config = self.store.config()
        self.assertEqual(config["enabled_resolutions"], ["720p", "1080p"])
        for resolution, ratios in expected.items():
            for ratio, dimensions in ratios.items():
                shot = self.shot(resolution=resolution, aspect_ratio=ratio)
                self.assertEqual((shot["width"], shot["height"]), dimensions)
                self.assertEqual(config["resolutions"][resolution][ratio], {"width": dimensions[0], "height": dimensions[1]})
                self.assertEqual(shot["estimated_credit"], 5 * config["rates"][resolution])
                self.assertFalse(shot["requires_rebuild"])
        for resolution in ("480p", "4k", "1080P", "", None, 720, []):
            with self.assertRaises(ValueError):
                self.shot(resolution=resolution)

    @patch.dict("os.environ", {"IMAGE_WORKBENCH_ENABLE_1080P": "1"})
    def test_1080p_default_rate_snapshot_failure_and_retry_price(self):
        with self.identity.transaction() as db:
            db.execute("UPDATE credit_accounts SET total_credit=10000 WHERE account_id=?", (self.aid,))
        shot = self.shot(resolution="1080p")
        self.assertEqual(shot["estimated_credit"], 400)
        with self.assertRaises(ConflictError):
            self.submit(shot, expected_credit=200)
        rid = uid()
        job = self.submit(shot, request_id=rid, expected_credit=400)
        self.assertEqual((job["resolution"], job["credits_per_second"], job["credit_cost"]), ("1080p", 80, 400))
        self.store.set_config(self.admin, {"720p": 40, "1080p": 95})
        replay = self.submit(shot, request_id=rid, expected_credit=400)
        self.assertEqual((replay["credits_per_second"], replay["credit_cost"]), (80, 400))
        worker = uid()
        manifest = self.store.claim(worker)
        self.assertEqual((manifest["task"]["width"], manifest["task"]["height"]), (1920, 1080))
        self.assertEqual(manifest["task"]["resolution"], "1080p")
        self.store.complete(job["job_id"], worker, manifest["claim_id"], "failed", error="模型明确拒绝")
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        with self.assertRaises(ConflictError):
            self.submit(shot, expected_credit=400, retry_of=job["job_id"])
        retry = self.submit(shot, expected_credit=475, retry_of=job["job_id"])
        self.assertEqual((retry["resolution"], retry["credits_per_second"], retry["credit_cost"]), ("1080p", 95, 475))
        self.assertEqual(self.wallet()["frozen_credit"], 475)

    def test_default_720p_five_second_submission_freezes_200_credits(self):
        config = self.store.config()
        self.assertEqual(config["enabled_resolutions"], ["720p"])
        self.assertEqual(config["rates"], {"720p": 40, "1080p": 80})
        self.assertIn("1080p", config["resolutions"])
        shot = self.shot()
        job = self.submit(shot, expected_credit=200)
        self.assertEqual((job["resolution"], job["duration"], job["credit_cost"]), ("720p", 5, 200))
        self.assertEqual(self.wallet()["frozen_credit"], 200)

    def test_disabled_1080p_create_submit_and_retry_do_not_freeze_credits(self):
        for flag in ("", "0", "true"):
            with patch.dict("os.environ", {"IMAGE_WORKBENCH_ENABLE_1080P": flag}):
                with self.assertRaisesRegex(ConflictError, "1080p暂不可用，请新建720p分镜"):
                    self.shot(resolution="1080p")
        self.assertEqual(self.store.project(self.aid, self.wid, self.pid)["shots"], [])
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        with patch.dict("os.environ", {"IMAGE_WORKBENCH_ENABLE_1080P": "1"}):
            draft = self.shot(resolution="1080p")
            failed = self.shot(resolution="1080p")
            job = self.submit(failed, expected_credit=20)
            worker = uid()
            manifest = self.store.claim(worker)
            self.store.complete(job["job_id"], worker, manifest["claim_id"], "failed", error="工作流明确失败")
        for shot, extra in ((draft, {}), (failed, {"retry_of": job["job_id"]})):
            with self.subTest(retry=bool(extra)):
                with self.assertRaisesRegex(ConflictError, "1080p暂不可用，请新建720p分镜"):
                    self.submit(shot, expected_credit=20, **extra)
                self.assertEqual(self.wallet()["frozen_credit"], 0)
                view = self.store.shot(self.aid, self.wid, self.pid, shot["shot_id"])
                self.assertFalse(view["requires_rebuild"])
                self.assertEqual(view["run_blocked_reason"], "1080p暂不可用，请新建720p分镜")
        self.assertEqual(self.wallet()["used_credit"], 0)
        with self.identity.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 1)
        self.assertEqual(self.store.config()["rates"], {"720p": 2, "1080p": 4})

    def test_disabled_1080p_running_uncertain_recovery_settlement_and_history_download(self):
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        request_id, worker, attempt = uid(), uid(), uid()
        with patch.dict("os.environ", {"IMAGE_WORKBENCH_ENABLE_1080P": "1"}):
            shot = self.shot(resolution="1080p")
            job = self.submit(shot, request_id=request_id, expected_credit=20)
            manifest = self.store.claim(worker)
            claim = manifest["claim_id"]
            self.store.start(job["job_id"], worker, claim, attempt, "existing-1080p-prompt")
        self.assertEqual(self.store.claim(worker)["job_id"], job["job_id"])
        self.assertEqual(self.submit(shot, request_id=request_id, expected_credit=20)["job_id"], job["job_id"])
        self.store.complete(job["job_id"], worker, claim, "uncertain", error="等待原任务")
        self.assertEqual(self.submit(shot, expected_credit=20)["job_id"], job["job_id"])
        self.assertEqual(self.store.claim(worker)["prompt_id"], "existing-1080p-prompt")
        self.assertEqual(self.wallet()["frozen_credit"], 20)
        self.store.start(job["job_id"], worker, claim, attempt, "existing-1080p-prompt")
        data, sha = mp4(), hashlib.sha256(mp4()).hexdigest()
        self.store.upload_output(job["job_id"], worker, claim, data, sha)
        self.store.complete(job["job_id"], worker, claim, "succeeded", sha)
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        self.assertEqual(self.wallet()["used_credit"], 20)
        view = self.store.shot(self.aid, self.wid, self.pid, shot["shot_id"])
        self.assertEqual(view["status"], "completed")
        self.assertFalse(view["requires_rebuild"])
        self.assertEqual(view["current_job"]["credits_per_second"], 4)
        self.assertEqual(self.store.config()["rates"], {"720p": 2, "1080p": 4})
        self.assertEqual(self.store.output_file(self.aid, self.wid, self.pid, shot["shot_id"]).read_bytes(), data)
        app = create_app(self.storage, backend="local")
        client = self.http_client(app, self.aid, self.wid)
        response = client.get(view["download_url"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, data)
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_legacy_scalar_configuration_and_low_resolution_shots_are_not_repriced_as_hd(self):
        self.store._write(self.store._path("config.json"), {"credits_per_second": 2})
        config = self.store.config()
        self.assertEqual(config["rates"], {"720p": 40, "1080p": 80})
        self.assertEqual(config["legacy_credits_per_second"], 2)
        shot = self.shot()
        path = self.store._path("projects", self.pid, "shots", shot["shot_id"] + ".json")
        old = self.store._read(path)
        old.pop("resolution")
        old.update(width=768, height=448)
        self.store._write(path, old)
        view = self.store.shot(self.aid, self.wid, self.pid, shot["shot_id"])
        self.assertTrue(view["requires_rebuild"])
        self.assertIsNone(view["estimated_credit"])
        self.assertIn("新建高清分镜", view["run_blocked_reason"])
        with self.assertRaisesRegex(ConflictError, "新建高清分镜"):
            self.submit(shot, expected_credit=200)
        # Merely adding a tier label to old low-resolution metadata cannot enable HD billing.
        old["resolution"] = "720p"
        self.store._write(path, old)
        with self.assertRaisesRegex(ConflictError, "新建高清分镜"):
            self.submit(shot, expected_credit=200)
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        self.store.set_config(self.admin, {"720p": 45, "1080p": 90})
        self.assertEqual(self.store.config()["legacy_credits_per_second"], 2)

    def test_legacy_completed_video_still_plays_and_retains_original_settlement(self):
        shot, job, worker, claim = self.ready()
        path = self.store._path("projects", self.pid, "shots", shot["shot_id"] + ".json")
        old = self.store._read(path)
        old.pop("resolution")
        old.update(width=768, height=448)
        self.store._write(path, old)
        old_job = self.store._read(self.store._job_path(job["job_id"]))
        old_job.pop("resolution")
        self.store._save_job(old_job)
        data, sha = mp4(), hashlib.sha256(mp4()).hexdigest()
        self.store.upload_output(job["job_id"], worker, claim, data, sha)
        self.store.complete(job["job_id"], worker, claim, "succeeded", sha)
        self.store.set_config(self.admin, {"720p": 40, "1080p": 80})
        view = self.store.shot(self.aid, self.wid, self.pid, shot["shot_id"])
        self.assertTrue(view["requires_rebuild"])
        self.assertEqual(view["status"], "completed")
        self.assertIsNotNone(view["output_url"])
        self.assertEqual(self.store.output_file(self.aid, self.wid, self.pid, shot["shot_id"]).read_bytes(), data)
        self.assertEqual(self.wallet()["used_credit"], 10)
        self.assertEqual(view["current_job"]["credits_per_second"], 2)

    def test_success_output_verification_replay_settlement_and_claim_ownership(self):
        shot, job, worker, claim = self.ready()
        self.assertEqual(self.store.claim(worker)["claim_id"], claim)
        self.assertIsNone(self.store.claim(uid()))
        for operation in (lambda: self.store.heartbeat(job["job_id"], uid(), claim),
                          lambda: self.store.upload_output(job["job_id"], worker, uid(), mp4(), hashlib.sha256(mp4()).hexdigest())):
            with self.assertRaises(ConflictError):
                operation()
        data = mp4()
        sha = hashlib.sha256(data).hexdigest()
        with self.assertRaises(ValueError):
            self.store.upload_output(job["job_id"], worker, claim, data, "0" * 64)
        with self.assertRaises(ValueError):
            self.store.upload_output(job["job_id"], worker, claim, b"fake", hashlib.sha256(b"fake").hexdigest())
        self.store.upload_output(job["job_id"], worker, claim, data, sha)
        self.store.complete(job["job_id"], worker, claim, "succeeded", sha)
        self.store.upload_output(job["job_id"], worker, claim, data, sha)
        self.store.complete(job["job_id"], worker, claim, "succeeded", sha)
        self.assertEqual(self.wallet()["used_credit"], 10)
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        self.assertEqual(self.store.output_file(self.aid, self.wid, self.pid, shot["shot_id"]).read_bytes(), data)
        with self.assertRaises(FileNotFoundError):
            self.store.output_file(self.bid, self.bwid, self.pid, shot["shot_id"])
        with self.assertRaises(ConflictError):
            self.store.complete(job["job_id"], worker, claim, "failed")

    def test_uncertain_preserves_reservation_and_can_resolve_without_new_attempt(self):
        shot, job, worker, claim = self.ready()
        manifest = self.store.claim(worker)
        attempt = manifest["attempt_id"]
        self.store.start(job["job_id"], worker, claim, attempt, "existing-comfy-prompt")
        self.store.complete(job["job_id"], worker, claim, "uncertain", error="连接中断，等待原任务结果")
        self.assertEqual(self.wallet()["frozen_credit"], 10)
        self.assertEqual(self.submit(shot)["job_id"], job["job_id"])
        self.assertIsNone(self.store.claim(uid()))
        with self.assertRaises(ConflictError):
            self.store.start(job["job_id"], worker, claim, uid())
        self.assertEqual(self.store.claim(worker)["prompt_id"], "existing-comfy-prompt")
        self.store.start(job["job_id"], worker, claim, attempt, "existing-comfy-prompt")
        resumed = self.store.shot(self.aid, self.wid, self.pid, shot["shot_id"])
        self.assertEqual(resumed["status"], "running")
        self.assertIsNone(resumed["current_job"]["error"])
        self.assertEqual(self.wallet()["frozen_credit"], 10)
        data = mp4()
        sha = hashlib.sha256(data).hexdigest()
        self.store.upload_output(job["job_id"], worker, claim, data, sha)
        self.store.complete(job["job_id"], worker, claim, "succeeded", sha)
        self.assertEqual(self.wallet()["used_credit"], 10)

    def test_failure_refund_retry_requires_current_id_and_charges_new_rate(self):
        shot, job, worker, claim = self.ready()
        self.store.complete(job["job_id"], worker, claim, "failed", error="工作流明确失败")
        self.store.complete(job["job_id"], worker, claim, "failed")
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        self.assertEqual(self.wallet()["used_credit"], 0)
        with self.assertRaises(ConflictError):
            self.submit(shot)
        self.store.set_config(self.admin, {"720p": 3, "1080p": 6})
        retry = self.submit(shot, expected_credit=15, retry_of=job["job_id"])
        self.assertNotEqual(retry["job_id"], job["job_id"])
        self.assertEqual(self.wallet()["frozen_credit"], 15)

    def test_outbox_and_terminal_recovery_and_image_worker_filter(self):
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        shot = self.shot()
        original = self.store._write
        def fail_job_json(path, value):
            if path.parent.name == "jobs":
                raise OSError("模拟冻结后发布JSON中断")
            return original(path, value)
        with patch.object(self.store, "_write", side_effect=fail_job_json):
            with self.assertRaises(OSError):
                self.submit(shot)
        self.assertEqual(self.wallet()["frozen_credit"], 10)
        state = WorkbenchState(self.storage, backend="local")
        self.assertEqual(state.list_jobs(), [])
        worker = uid()
        manifest = self.store.claim(worker)
        self.assertIsNotNone(manifest)
        with patch.object(self.identity, "record_job", side_effect=OSError("模拟结算中断")):
            with self.assertRaises(OSError):
                self.store.complete(manifest["job_id"], worker, manifest["claim_id"], "failed")
        self.assertEqual(self.wallet()["frozen_credit"], 10)
        self.store.project(self.aid, self.wid, self.pid)
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        self.assertEqual(state.list_jobs(), [])

    def test_mp4_box_bounds(self):
        inspect_mp4(mp4())
        for invalid in (b"", mp4()[:-1], b"\0\0\0\4ftyp" + mp4(), png()):
            with self.assertRaises(ValueError):
                inspect_mp4(invalid)

    def test_shared_ledger_rejects_cross_kind_account_workspace_and_project_collisions(self):
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        shot = self.shot()
        job = self.submit(shot)
        original = self.store._read(self.store._job_path(job["job_id"]))
        for aid, wid, changes in (
            (self.bid, self.bwid, {}),
            (self.aid, self.other_wid, {}),
            (self.aid, self.wid, {"project_id": uid()}),
            (self.aid, self.wid, {"kind": "image"}),
        ):
            collision = {**original, "job_id": uid(), "request_id": uid(), **changes}
            with self.assertRaises(ValueError):
                self.identity.reserve(collision, {"account_id": aid, "workspace_id": wid}, 10)
        collision = {**original, "job_id": uid(), "request_id": uid()}
        collision.pop("kind")
        with self.assertRaises(ValueError):
            self.identity.reserve(collision, {"account_id": self.aid, "workspace_id": self.wid}, 10)
        self.assertEqual(self.wallet()["frozen_credit"], 10)
        self.assertEqual(self.identity.wallet(self.bid)["frozen_credit"], 0)

    def test_reference_assets_are_limited_to_the_claimed_shot(self):
        assets = self.store.add_assets(self.aid, self.wid, self.pid, [("a.png", png()), ("b.png", png("blue"))])
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})
        shot = self.shot(mode="I2V", reference_ids=[assets[0]["asset_id"]])
        job = self.submit(shot)
        worker = uid()
        manifest = self.store.claim(worker)
        self.assertEqual(self.store.worker_asset(job["job_id"], worker, manifest["claim_id"], assets[0]["asset_id"]), png())
        with self.assertRaises(FileNotFoundError):
            self.store.worker_asset(job["job_id"], worker, manifest["claim_id"], assets[1]["asset_id"])

    def test_upload_recovers_identical_bytes_after_metadata_write_interruption(self):
        _, job, worker, claim = self.ready()
        data, sha = mp4(), hashlib.sha256(mp4()).hexdigest()
        with patch.object(self.store, "_save_job", side_effect=OSError("模拟输出元数据发布中断")):
            with self.assertRaises(OSError):
                self.store.upload_output(job["job_id"], worker, claim, data, sha)
        changed = data + struct.pack(">I4s", 8, b"free")
        with self.assertRaises(ValueError):
            self.store.upload_output(job["job_id"], worker, claim, changed, hashlib.sha256(changed).hexdigest())
        self.store.upload_output(job["job_id"], worker, claim, data, sha)
        self.store.complete(job["job_id"], worker, claim, "succeeded", sha)
        self.assertEqual(self.wallet()["used_credit"], 10)

    def http_client(self, app, aid=None, wid=None, *, admin=False):
        client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(client.close)
        if aid:
            token, csrf = uid(), uid()
            with self.identity.transaction() as db:
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (digest(token), aid, csrf, wid, _now(), time.time() + 3600, "video-test"))
            client.cookies.set("iw_session", token)
            client.headers["X-Workbench-CSRF"] = csrf
        return client

    def test_http_auth_price_workspace_and_worker_protocol(self):
        access = RemoteAccess("http://localhost", "v" * 48)
        app = create_app(self.storage, backend="local", remote_access=access)
        anon = self.http_client(app)
        alice = self.http_client(app, self.aid, self.wid)
        bobby = self.http_client(app, self.bid, self.bwid)
        admin_wid = self.identity.workspaces(self.admin)[0]["workspace_id"]
        admin = self.http_client(app, self.admin, admin_wid)
        self.assertEqual(anon.get("/api/video/projects").status_code, 401)
        preset = self.store.config()["rates"]
        self.assertEqual(alice.put("/api/video/config", json={"rates": {"720p": 2, "1080p": 4}}).status_code, 405)
        self.assertEqual(admin.put("/api/video/config", json={"rates": {"720p": 2, "1080p": 4}}).status_code, 405)
        self.assertEqual(self.store.config()["rates"], preset)
        self.store.set_config(self.admin, {"720p": 2, "1080p": 4})  # Operator-side fixture, never a website mutation.
        self.assertEqual(bobby.get(f"/api/video/projects/{self.pid}").status_code, 404)
        self.assertEqual(alice.get(f"/api/video/projects/{self.pid}?workspace_id={self.other_wid}").status_code, 404)
        asset_response = alice.post(f"/api/video/projects/{self.pid}/assets", files={"files": ("test.png", png(), "image/png")})
        self.assertEqual(asset_response.status_code, 200, asset_response.text)
        asset = asset_response.json()["assets"][0]
        self.assertEqual(bobby.get(asset["url"]).status_code, 404)
        body = dict(request_id=uid(), name="镜头", prompt="挥手", mode="I2V", duration=5, aspect_ratio="portrait", reference_ids=[asset["asset_id"]])
        self.assertEqual(alice.post(f"/api/video/projects/{self.pid}/shots", json={**body, "resolution": "4k"}).status_code, 422)
        self.assertEqual(alice.get("/api/video/config").json()["enabled_resolutions"], ["720p"])
        disabled = alice.post(f"/api/video/projects/{self.pid}/shots", json={**body, "resolution": "1080p"})
        self.assertEqual(disabled.status_code, 409, disabled.text)
        self.assertEqual(self.wallet()["frozen_credit"], 0)
        response = alice.post(f"/api/video/projects/{self.pid}/shots", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["resolution"], "720p")
        self.assertEqual((response.json()["width"], response.json()["height"]), (720, 1280))
        sid = response.json()["shot_id"]
        route = f"/api/video/projects/{self.pid}/shots/{sid}/run"
        run = {"request_id": uid(), "confirm_generation": True, "expected_credit": 9}
        self.assertEqual(alice.post(route, json=run).status_code, 409)
        run["expected_credit"] = 10
        response = alice.post(route, json=run)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(alice.post(route, json=run).json()["job_id"], response.json()["job_id"])
        worker_id = uid()
        self.assertEqual(anon.post("/api/worker/video/claim", json={"worker_id": worker_id}).status_code, 401)
        worker = self.http_client(app)
        worker.headers["Authorization"] = "Bearer " + access.worker_token
        manifest = worker.post("/api/worker/video/claim", json={"worker_id": worker_id}).json()["job"]
        worker.headers.update({"X-Worker-ID": worker_id, "X-Claim-ID": manifest["claim_id"]})
        base = f"/api/worker/video/jobs/{manifest['job_id']}"
        self.assertEqual(worker.get(base + f"/assets/{asset['asset_id']}").content, png())
        self.assertEqual(worker.post(base + "/start", json={"attempt_id": uid()}).status_code, 200)
        data, sha = mp4(), hashlib.sha256(mp4()).hexdigest()
        self.assertEqual(worker.put(base + "/output", content=data, headers={"X-Content-SHA256": sha}).status_code, 200)
        self.assertEqual(worker.post(base + "/complete", json={"status": "succeeded", "sha256": sha}).status_code, 200)
        shot = alice.get(f"/api/video/projects/{self.pid}/shots/{sid}").json()
        video = alice.get(shot["output_url"], headers={"Range": "bytes=0-15"})
        self.assertEqual(video.status_code, 206, video.text)
        self.assertEqual(video.content, data[:16])
        self.assertEqual(bobby.get(shot["output_url"]).status_code, 404)
        self.assertEqual(self.wallet()["used_credit"], 10)


if __name__ == "__main__":
    unittest.main()
