"""Stage B contract tests: persistent media selection and non-destructive clips."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from image_workbench.web import create_app
from tests import test_video_pipeline
from tests.test_video import mp4, png


class AssetTrayTests(unittest.TestCase):
    client = test_video_pipeline.PipelineTests.client

    def setUp(self):
        test_video_pipeline.PipelineTests.setUp(self)
        self.sid = self.link["subproject_id"]
        self.mid = self.link["main_project_id"]
        self.tray_url = self.prefix + "/tray"

    def upload_image(self, color="red", *, sid=None, client=None):
        response = (client or self.alice).post(
            f"/api/subprojects/{sid or self.sid}/assets",
            files={"files": ("原始参考.png", png(color), "image/png")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["assets"][0]

    def upload_video(self, *, pid=None, client=None):
        response = (client or self.alice).post(
            f"/api/video/projects/{pid or self.pid}/videos/upload",
            data={"request_id": uuid4().hex},
            files={"file": ("原始视频.mp4", mp4(), "video/mp4")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tray(self, *, client=None, pid=None):
        response = (client or self.alice).get(f"/api/video/projects/{pid or self.pid}/tray")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["assets"]

    def change(self, aid, media_type="image", action="add", *, client=None, pid=None, shot_id=None):
        body = {"artifact_id": aid, "media_type": media_type, "action": action}
        if shot_id is not None:
            body["shot_id"] = shot_id
        return (client or self.alice).post(
            f"/api/video/projects/{pid or self.pid}/tray", json=body,
        )

    def mapped(self, sid, *, client=None):
        return (client or self.alice).post(
            "/api/video/projects/from-subproject", json={"subproject_id": sid},
        )

    def shot(self, *, pid=None, client=None, **changes):
        body = {
            "request_id": uuid4().hex, "name": "待绑定镜头", "prompt": "镜头缓慢推进",
            "mode": "T2V", "duration": 4, "aspect_ratio": "landscape", "reference_ids": [],
        }
        body.update(changes)
        response = (client or self.alice).post(f"/api/video/projects/{pid or self.pid}/shots", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def bind(self, target, source, aid, *, pid=None, client=None):
        return (client or self.alice).post(
            f"/api/video/projects/{pid or self.pid}/shots/{target['shot_id']}/clip",
            json={"source_shot_id": source["shot_id"], "artifact_id": aid},
        )

    def media_snapshot(self):
        return {
            str(path.relative_to(self.storage.data_root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.storage.data_root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4"}
        }

    def new_project(self, client=None):
        client = client or self.alice
        pid = uuid4().hex
        response = client.post("/api/video/projects", json={"request_id": pid, "name": "其他项目"})
        self.assertEqual(response.status_code, 200, response.text)
        response = client.post(f"/api/video/projects/{pid}/pipeline", json={})
        self.assertEqual(response.status_code, 200, response.text)
        return pid, response.json()

    def video_asset(self, shot):
        assets = [
            item for item in self.tray()
            if item["media_type"] == "video"
            and item.get("shot_id", item.get("source_shot_id")) == shot["shot_id"]
        ]
        self.assertEqual(len(assets), 1)
        return assets[0]

    def test_image_tray_membership_is_idempotent_persistent_and_remove_preserves_original(self):
        image = self.upload_image()
        before = self.media_snapshot()
        wallet = self.identity.wallet(self.aid)
        for _ in range(2):
            response = self.change(image["artifact_id"])
            self.assertEqual(response.status_code, 200, response.text)
        assets = self.tray()
        matching = [item for item in assets if item["artifact_id"] == image["artifact_id"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["media_type"], "image")
        self.assertEqual(self.alice.get(matching[0]["url"]).content, png())
        self.assertEqual(self.media_snapshot(), before)
        with TestClient(create_app(self.storage, backend="local"), base_url="http://localhost") as reopened:
            reopened.cookies.update(self.alice.cookies)
            reopened.headers["X-Workbench-CSRF"] = self.alice.headers["X-Workbench-CSRF"]
            self.assertIn(image["artifact_id"], [item["artifact_id"] for item in self.tray(client=reopened)])
            removed = self.change(image["artifact_id"], action="remove", client=reopened)
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertNotIn(image["artifact_id"], [item["artifact_id"] for item in self.tray(client=reopened)])
        self.assertNotIn(image["artifact_id"], [item["artifact_id"] for item in self.tray()])
        self.assertEqual(self.alice.get(image["url"]).content, png())
        self.assertIn(image["artifact_id"], [
            item["artifact_id"] for item in self.alice.get(f"/api/subprojects/{self.sid}/assets").json()["assets"]
        ])
        self.assertEqual(self.media_snapshot(), before)
        restored = self.change(image["artifact_id"])
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.identity.wallet(self.aid), wallet)

    def test_tray_image_keeps_artifact_id_when_used_by_image_and_video_tasks(self):
        image = self.upload_image()
        before = self.media_snapshot()
        added = self.change(image["artifact_id"])
        self.assertEqual(added.status_code, 200, added.text)
        video = self.shot(mode="I2V", reference_ids=[image["artifact_id"]])
        self.assertEqual(video["reference_ids"], [image["artifact_id"]])
        response = self.alice.post(f"/api/subprojects/{self.sid}/tasks",
            data={"request_id": uuid4().hex, "operation": "generate", "prompt": "复用同一图片",
                  "reference_artifact_ids": json.dumps([image["artifact_id"]])})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["inputs"][0]["artifact_id"], image["artifact_id"])
        self.assertEqual(self.media_snapshot(), before)
        linked = self.alice.get(self.prefix).json()["assets"]
        self.assertIn(image["artifact_id"], [item["asset_id"] for item in linked])

    def test_uploaded_video_has_canonical_artifact_without_copy_and_remove_preserves_mp4(self):
        source = self.upload_video()
        item = self.video_asset(source)
        artifact = self.storage.get_artifact(item["artifact_id"])
        self.assertEqual(artifact.format, "MP4")
        original = self.app.video_store.output_file(self.aid,self.wid,self.pid,source["shot_id"])
        self.assertTrue(original.samefile(self.storage.get_artifact_path(artifact.artifact_id)))
        before = self.media_snapshot()
        response = self.change(item["artifact_id"], "video", "remove", shot_id=source["shot_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(item["artifact_id"], [asset["artifact_id"] for asset in self.tray()])
        self.assertEqual(self.alice.get(source["output_url"]).content, mp4())
        response = self.change(item["artifact_id"], "video", "add", shot_id=source["shot_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.video_asset(source)["artifact_id"], item["artifact_id"])
        self.assertEqual(self.media_snapshot(), before)

    def test_generated_video_references_real_artifact_with_one_physical_payload(self):
        source = self.shot()
        response = self.alice.post(self.prefix + f"/shots/{source['shot_id']}/run",
            json={"request_id": uuid4().hex, "confirm_generation": True, "expected_credit": source["estimated_credit"]})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        store = self.app.video_store
        worker_id = uuid4().hex
        manifest = store.claim(worker_id)
        claim_id = manifest["claim_id"]
        store.start(job["job_id"], worker_id, claim_id, uuid4().hex)
        digest = hashlib.sha256(mp4()).hexdigest()
        store.upload_output(job["job_id"], worker_id, claim_id, mp4(), digest)
        store.complete(job["job_id"], worker_id, claim_id, "succeeded", digest)
        item = self.video_asset(source)
        artifact = self.storage.get_artifact(item["artifact_id"])
        self.assertEqual(artifact.format, "MP4")
        self.assertEqual(self.storage.get_task(artifact.project_id, artifact.task_id).operation, "video")
        original = store.output_file(self.aid,self.wid,self.pid,source["shot_id"])
        self.assertTrue(original.samefile(self.storage.get_artifact_path(artifact.artifact_id)))
        self.assertEqual(self.video_asset(source)["artifact_id"], item["artifact_id"])
        before = self.media_snapshot()
        target = self.shot()
        response = self.bind(target, source, item["artifact_id"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.media_snapshot(), before)
        other_source = self.upload_video()
        other_item = self.video_asset(other_source)
        before_overwrite = self.media_snapshot()
        rejected = self.bind(source, other_source, other_item["artifact_id"])
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(self.media_snapshot(), before_overwrite)
        completed = self.alice.get(self.prefix + f"/shots/{source['shot_id']}").json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.alice.get(completed["output_url"]).content, mp4())

    def test_existing_subproject_mapping_is_reused_without_extra_main_projects(self):
        before = self.alice.get("/api/main-projects").json()["main_projects"]
        first = self.mapped(self.sid)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["project_id"], self.pid)
        self.assertEqual(self.mapped(self.sid).json()["project_id"], self.pid)
        self.assertEqual(len(self.alice.get("/api/main-projects").json()["main_projects"]), len(before))
        self.assertEqual(len(self.alice.get("/api/video/projects").json()["projects"]), 1)

    def test_new_subproject_mapping_preserves_main_and_subproject_and_converges_concurrently(self):
        response = self.alice.post(f"/api/main-projects/{self.mid}/subprojects",
            json={"request_id": uuid4().hex, "name": "复用主项目的子项目"})
        self.assertEqual(response.status_code, 200, response.text)
        sid = response.json()["subproject_id"]
        clients = [self.client(self.aid, self.wid) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda client: self.mapped(sid, client=client), clients))
        for result in results:
            self.assertEqual(result.status_code, 200, result.text)
        pids = {result.json()["project_id"] for result in results}
        self.assertEqual(len(pids), 1)
        pid = pids.pop()
        self.assertNotEqual(pid, self.pid)
        link = self.alice.get(f"/api/video/projects/{pid}/pipeline")
        self.assertEqual(link.status_code, 200, link.text)
        self.assertEqual((link.json()["main_project_id"], link.json()["subproject_id"]), (self.mid, sid))
        self.assertEqual(len(self.alice.get("/api/main-projects").json()["main_projects"]), 1)
        self.assertEqual(len(self.alice.get("/api/video/projects").json()["projects"]), 2)

    def test_tray_and_mapping_enforce_account_workspace_and_project_boundaries(self):
        image = self.upload_image()
        own_other_pid, _ = self.new_project()
        for client, headers in (
            (self.bobby, {}),
            (self.alice, {"X-Workspace-ID": self.other_wid}),
        ):
            self.assertEqual(client.get(self.tray_url, headers=headers).status_code, 404)
            self.assertEqual(client.post(self.tray_url, headers=headers,
                json={"artifact_id": image["artifact_id"], "media_type": "image", "action": "add"}).status_code, 404)
            self.assertEqual(client.post("/api/video/projects/from-subproject", headers=headers,
                json={"subproject_id": self.sid}).status_code, 404)
        rejected = self.change(image["artifact_id"], pid=own_other_pid)
        self.assertIn(rejected.status_code, (400, 404), rejected.text)
        self.assertNotIn(image["artifact_id"], [item["artifact_id"] for item in self.tray(pid=own_other_pid)])
        self.assertEqual(self.anon.get(self.tray_url).status_code, 401)
        self.assertEqual(self.anon.post(self.tray_url,
            json={"artifact_id": image["artifact_id"], "media_type": "image", "action": "remove"}).status_code, 403)

    def test_unknown_artifact_and_wrong_media_type_cannot_create_tray_entries(self):
        image = self.upload_image()
        for body in (
            {"artifact_id": uuid4().hex, "media_type": "image", "action": "add"},
            {"artifact_id": image["artifact_id"], "media_type": "video", "action": "add"},
            {"artifact_id": image["artifact_id"], "media_type": "image", "action": "delete"},
            {"artifact_id": "../outside", "media_type": "image", "action": "add"},
        ):
            result = self.alice.post(self.tray_url, json=body)
            self.assertIn(result.status_code, (400, 404, 422), result.text)
        self.assertEqual(self.alice.get(image["url"]).content, png())

    def test_bind_uploaded_clip_to_draft_persists_preview_without_copy_or_charge(self):
        source = self.upload_video()
        item = self.video_asset(source)
        target = self.shot()
        before = self.media_snapshot()
        wallet = self.identity.wallet(self.aid)
        for _ in range(2):
            response = self.bind(target, source, item["artifact_id"])
            self.assertEqual(response.status_code, 200, response.text)
        detail = self.alice.get(self.prefix + f"/shots/{target['shot_id']}").json()
        self.assertEqual(detail["status"], "draft")
        bound = detail["bound_clip"]
        self.assertEqual((bound["artifact_id"], bound["source_shot_id"]), (item["artifact_id"], source["shot_id"]))
        preview = bound.get("url") or bound.get("output_url")
        self.assertTrue(preview)
        self.assertEqual(self.alice.get(preview).content, mp4())
        self.assertEqual(self.media_snapshot(), before)
        self.assertEqual(self.identity.wallet(self.aid), wallet)
        removed = self.change(item["artifact_id"], "video", "remove", shot_id=source["shot_id"])
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.alice.get(preview).content, mp4())
        self.assertEqual(self.alice.get(self.prefix + f"/shots/{target['shot_id']}").json()["bound_clip"]["artifact_id"],
                         item["artifact_id"])

    def test_clip_binding_rejects_incomplete_foreign_or_mismatched_sources(self):
        target = self.shot()
        incomplete = self.shot(name="尚未完成的源镜头")
        rejected = self.bind(target, incomplete, incomplete["shot_id"])
        self.assertIn(rejected.status_code, (400, 404, 409), rejected.text)
        other_pid, _ = self.new_project()
        foreign = self.upload_video(pid=other_pid)
        rejected = self.bind(target, foreign, foreign["shot_id"])
        self.assertIn(rejected.status_code, (400, 404), rejected.text)
        source = self.upload_video()
        rejected = self.bind(target, source, uuid4().hex)
        self.assertIn(rejected.status_code, (400, 404), rejected.text)
        for client, headers in ((self.bobby, {}), (self.alice, {"X-Workspace-ID": self.other_wid})):
            response = client.post(self.prefix + f"/shots/{target['shot_id']}/clip", headers=headers,
                json={"source_shot_id": source["shot_id"], "artifact_id": source["shot_id"]})
            self.assertEqual(response.status_code, 404, response.text)
        detail = self.alice.get(self.prefix + f"/shots/{target['shot_id']}").json()
        self.assertFalse(detail.get("bound_clip"))

    def test_clip_binding_cannot_replace_uploaded_or_already_submitted_target(self):
        source = self.upload_video()
        source_item = self.video_asset(source)
        uploaded_target = self.upload_video()
        rejected = self.bind(uploaded_target, source, source_item["artifact_id"])
        self.assertEqual(rejected.status_code, 409, rejected.text)
        target = self.shot()
        response = self.alice.post(self.prefix + f"/shots/{target['shot_id']}/run",
            json={"request_id": uuid4().hex, "confirm_generation": True, "expected_credit": target["estimated_credit"]})
        self.assertEqual(response.status_code, 200, response.text)
        before = self.media_snapshot()
        rejected = self.bind(target, source, source_item["artifact_id"])
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(self.media_snapshot(), before)
        self.assertEqual(self.alice.get(uploaded_target["output_url"]).content, mp4())


if __name__ == "__main__":
    unittest.main()
