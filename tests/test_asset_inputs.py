"""Asset registration, logical references and legacy image execution contracts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from image_workbench import LocalFileBackend
from image_workbench.accounts import CreditCostCalculator
from image_workbench.remote_queue import RemoteQueue
from image_workbench.worker import LocalWorker
from tests import test_video_pipeline
from tests.test_video import png


class RecordingLocalBackend(LocalFileBackend):
    def __init__(self, sources):
        super().__init__(sources)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return super().generate(request)


class AssetInputTests(unittest.TestCase):
    """Reuse the authenticated two-account fixture without inheriting its tests."""
    client = test_video_pipeline.PipelineTests.client

    def setUp(self):
        test_video_pipeline.PipelineTests.setUp(self)
        self.sid = self.link["subproject_id"]
        self.mid = self.link["main_project_id"]
        self.assets_url = f"/api/subprojects/{self.sid}/assets"
        self.tasks_url = f"/api/subprojects/{self.sid}/tasks"
        self.state = self.app.scoped_app(self.aid, self.wid).state.workbench
        self.fixture = self.root / "fixture.png"
        self.fixture.write_bytes(png("purple"))

    def upload(self, data=None, *, client=None, url=None, filename="reference.png"):
        return (client or self.alice).post(
            url or self.assets_url,
            files=[("files", (filename, png() if data is None else data, "image/png"))],
        )

    def asset(self, data=None):
        response = self.upload(data)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["assets"][0]

    def draft(self, *, client=None, sid=None, refs=None, **changes):
        body = {"request_id": uuid4().hex, "operation": "generate", "prompt": "根据参考图创建新画面"}
        if refs is not None:
            body["reference_artifact_ids"] = json.dumps(refs)
        body.update(changes)
        return (client or self.alice).post(f"/api/subprojects/{sid or self.sid}/tasks", data=body)

    def all_artifacts(self):
        return [
            artifact
            for project in self.storage.list_projects()
            for task in self.storage.list_tasks(project.project_id)
            for artifact in self.storage.list_task_artifacts(project.project_id, task.task_id)
        ]

    def image_files(self):
        return {
            path for path in self.storage.data_root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        }

    def other_subproject(self):
        response = self.alice.post(
            f"/api/main-projects/{self.mid}/subprojects",
            json={"request_id": uuid4().hex, "name": "其他子项目"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["subproject_id"]

    def test_asset_library_cannot_be_moved_through_task_api(self):
        asset = self.asset()
        other = self.other_subproject()
        response = self.alice.post(f"/api/tasks/{asset['task_id']}/move", json={
            "target_subproject_id": other, "expected_subproject_id": self.sid})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("素材库不能移动", response.text)
        self.assertEqual(self.state.projects.get_task_placement(asset["task_id"]).subproject_id, self.sid)
        second = self.asset(png("blue"))
        self.assertEqual(second["subproject_id"], self.sid)

    def test_misplaced_legacy_library_fails_before_new_artifact(self):
        asset = self.asset()
        other = self.other_subproject()
        self.state.projects.move_task(asset["task_id"], other, expected_subproject_id=self.sid)
        before = {a.artifact_id for a in self.all_artifacts()}
        response = self.upload(png("blue"))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("归属记录不一致", response.text)
        self.assertEqual({a.artifact_id for a in self.all_artifacts()}, before)

    def submit(self, task):
        response = self.alice.post(
            f"/api/tasks/{task['task_id']}/run",
            json={"request_id": uuid4().hex, "confirm_generation": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_actual_png_jpeg_webp_contents_are_registered_and_retrievable(self):
        files, expected = [], {}
        for image_format in ("PNG", "JPEG", "WEBP"):
            stream = io.BytesIO()
            Image.new("RGB", (32, 24), "coral").save(stream, image_format)
            expected[image_format] = stream.getvalue()
            files.append(("files", ("../../misleading.txt", stream.getvalue(), "application/octet-stream")))
        result = self.alice.post(self.assets_url, files=files)
        self.assertEqual(result.status_code, 200, result.text)
        assets = result.json()["assets"]
        self.assertEqual({asset["format"] for asset in assets}, set(expected))
        self.assertEqual(len(self.all_artifacts()), 3)
        self.assertEqual(len(self.image_files()), 3)
        for asset in assets:
            self.assertEqual(self.alice.get(asset["url"]).content, expected[asset["format"]])
            self.assertEqual(self.alice.get(asset["download_url"]).content, expected[asset["format"]])
            self.assertNotIn("relative_path", asset)
            self.assertNotIn(str(self.storage.data_root), json.dumps(asset))
        listed = self.alice.get(self.assets_url)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            {asset["artifact_id"]: asset["filename"] for asset in listed.json()["assets"]},
            {asset["artifact_id"]: asset["filename"] for asset in assets},
        )
        self.assertEqual({asset["filename"] for asset in assets}, {"misleading.txt"})
        self.assertCountEqual(
            [asset["artifact_id"] for asset in listed.json()["assets"]],
            [asset["artifact_id"] for asset in assets],
        )
        self.assertFalse((self.root / "misleading.txt").exists())
        self.assertEqual(list((self.storage.data_root / ".workbench/uploads").iterdir()), [])

    def test_reupload_same_batch_and_concurrent_upload_have_one_artifact_and_file(self):
        # Race the initial registration, not merely reads of an existing image.
        clients = [self.client(self.aid, self.wid) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda client: self.upload(client=client), clients))
        self.assertEqual(results[0].status_code, 200, results[0].text)
        aid = results[0].json()["assets"][0]["artifact_id"]
        for result in results:
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["assets"][0]["artifact_id"], aid)
        copies = [("files", ("same.png", png(), "image/png"))] * 3
        first = self.alice.post(self.assets_url, files=copies)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual({item["artifact_id"] for item in first.json()["assets"]}, {aid})
        self.assertEqual(self.asset()["artifact_id"], aid)
        self.assertEqual(len(self.alice.get(self.assets_url).json()["assets"]), 1)
        self.assertEqual(len(self.all_artifacts()), 1)
        self.assertEqual(len(self.image_files()), 1)

    def test_batch_is_validated_before_registration_and_count_is_one_to_nine(self):
        empty = self.alice.post(self.assets_url, data={})
        self.assertEqual(empty.status_code, 400, empty.text)
        for invalid in (b"not a PNG", png()[:35]):
            with self.subTest(invalid=invalid[:12]):
                result = self.alice.post(self.assets_url, files=[
                    ("files", ("good.png", png(), "image/png")),
                    ("files", ("bad.png", invalid, "image/png")),
                ])
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(self.all_artifacts(), [])
        stream = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(stream, "GIF")
        self.assertEqual(self.upload(stream.getvalue()).status_code, 400)
        ten = [("files", ("x.png", png(), "image/png"))] * 10
        self.assertEqual(self.alice.post(self.assets_url, files=ten).status_code, 400)
        self.assertEqual(self.all_artifacts(), [])
        nine = [
            ("files", (f"{index}.png", png((index * 20, 50, 90)), "image/png"))
            for index in range(9)
        ]
        result = self.alice.post(self.assets_url, files=nine)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()["assets"]), 9)

    def test_single_file_uses_configured_limit_and_total_upload_is_bounded(self):
        limit = replace(self.storage.config, max_image_bytes=len(png()) - 1)
        with patch.object(self.storage, "config", limit):
            rejected = self.upload()
        self.assertIn(rejected.status_code, (400, 413), rejected.text)
        self.assertEqual(self.all_artifacts(), [])
        # Valid PNG data with padding keeps each file below 20 MiB but exceeds
        # the 40 MiB request limit; no large bitmap or model is needed.
        padded = png() + b"\0" * (14 * 1024 * 1024)
        result = self.alice.post(self.assets_url, files=[
            ("files", (f"{i}.png", padded, "image/png")) for i in range(3)
        ])
        self.assertIn(result.status_code, (400, 413), result.text)
        self.assertEqual(self.all_artifacts(), [])
        self.assertEqual(list((self.storage.data_root / ".workbench/uploads").iterdir()), [])

    def test_workspace_account_and_subproject_cannot_take_each_others_assets(self):
        asset = self.asset()
        for client, headers in (
            (self.bobby, {}),
            (self.alice, {"X-Workspace-ID": self.other_wid}),
        ):
            self.assertEqual(client.get(self.assets_url, headers=headers).status_code, 404)
            self.assertEqual(client.post(self.assets_url, headers=headers,
                files={"files": ("x.png", png(), "image/png")}).status_code, 404)
            # Do not retain the explicit original-workspace query when testing
            # a deliberately different active workspace.
            self.assertEqual(client.get(f"/api/artifacts/{asset['artifact_id']}", headers=headers).status_code, 404)
        self.assertEqual(self.anon.get(self.assets_url).status_code, 401)
        self.assertEqual(self.anon.post(self.assets_url, files={"files": ("x.png", png())}).status_code, 403)
        other_sid = self.other_subproject()
        self.assertEqual(self.alice.get(f"/api/subprojects/{other_sid}/assets").json()["assets"], [])
        denied = self.draft(sid=other_sid, refs=[asset["artifact_id"]])
        self.assertIn(denied.status_code, (400, 404), denied.text)
        denied_edit = self.draft(sid=other_sid, operation="edit", base_artifact_id=asset["artifact_id"])
        self.assertIn(denied_edit.status_code, (400, 404), denied_edit.text)
        own = self.upload(url=f"/api/subprojects/{other_sid}/assets")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertNotEqual(own.json()["assets"][0]["artifact_id"], asset["artifact_id"])

    def test_foreign_reference_cannot_enter_an_owned_task_from_another_account_or_workspace(self):
        foreign = self.asset()
        for client in (self.bobby, self.client(self.aid, self.other_wid)):
            pid = uuid4().hex
            project = client.post("/api/video/projects",
                                  json={"request_id": pid, "name": "隔离参考图的项目"})
            self.assertEqual(project.status_code, 200, project.text)
            link = client.post(f"/api/video/projects/{pid}/pipeline", json={})
            self.assertEqual(link.status_code, 200, link.text)
            sid = link.json()["subproject_id"]
            rejected = self.draft(client=client, sid=sid, refs=[foreign["artifact_id"]])
            self.assertEqual(rejected.status_code, 404, rejected.text)
            rejected_base = self.draft(client=client, sid=sid, operation="edit",
                                       base_artifact_id=foreign["artifact_id"])
            self.assertEqual(rejected_base.status_code, 404, rejected_base.text)
            self.assertEqual(client.get(f"/api/subprojects/{sid}/assets").json()["assets"], [])

    def test_video_upload_and_image_library_share_id_and_only_json_video_link(self):
        original = self.asset()
        files_before = self.image_files()
        response = self.upload(url=self.prefix + "/assets")
        self.assertEqual(response.status_code, 200, response.text)
        linked = response.json()["assets"][0]
        self.assertEqual(linked["asset_id"], original["artifact_id"])
        self.assertEqual(linked["artifact_id"], original["artifact_id"])
        self.assertEqual(self.image_files(), files_before)
        self.assertEqual(len(self.all_artifacts()), 1)
        self.assertEqual(self.alice.get(linked["url"]).content, png())
        video_directory = self.app.video_store._path("projects", self.pid, "assets")
        self.assertEqual({path.suffix for path in video_directory.iterdir()}, {".json"})
        imported = self.alice.post(self.prefix + "/images/from-artifact",
                                   json={"artifact_id": original["artifact_id"]})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(self.image_files(), files_before)
        self.assertEqual(len(self.alice.get(self.prefix).json()["assets"]), 1)

    def test_reference_ids_create_idempotent_draft_without_copy_and_charge_reference_rate(self):
        asset = self.asset()
        before = self.image_files()
        rid = uuid4().hex
        response = self.draft(refs=[asset["artifact_id"]], request_id=rid)
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual(task["input_count"], 1)
        self.assertEqual(task["inputs"][0]["artifact_id"], asset["artifact_id"])
        self.assertEqual(task["estimated_credit"],
                         CreditCostCalculator.calculate("generate", self.state.backend, 1, 1))
        self.assertEqual(self.storage.list_task_artifacts(task["project_id"], task["task_id"]), [])
        self.assertEqual(self.image_files(), before)
        again = self.draft(refs=[asset["artifact_id"]], request_id=rid)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["task_id"], task["task_id"])
        self.assertEqual(self.draft(refs=[], request_id=rid).status_code, 409)
        job = self.submit(task)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], task["estimated_credit"])
        self.assertEqual(self.state.runner.list_executions(task["project_id"], task["task_id"]), [])

    def test_bad_reference_shapes_duplicates_unknown_id_and_mixed_count_are_rejected(self):
        asset = self.asset()
        for value in ("{", "{}", '"text"', "[1]", json.dumps([asset["artifact_id"]] * 2)):
            with self.subTest(value=value):
                response = self.draft(reference_artifact_ids=value)
                self.assertEqual(response.status_code, 400, response.text)
        unknown = self.draft(refs=[uuid4().hex])
        self.assertIn(unknown.status_code, (400, 404), unknown.text)
        refs = [self.asset(png((i * 20, 50, 100)))["artifact_id"] for i in range(8)]
        response = self.alice.post(self.tasks_url,
            data={"request_id": uuid4().hex, "operation": "generate", "prompt": "总数不可超过8",
                  "reference_artifact_ids": json.dumps(refs)},
            files={"files": ("extra.png", png("blue"), "image/png")})
        self.assertEqual(response.status_code, 400, response.text)

    def test_local_worker_receives_referenced_input_and_uploaded_edit_base(self):
        base = self.asset()
        reference = self.asset(png("blue"))
        draft = self.draft(operation="edit", base_artifact_id=base["artifact_id"],
                           refs=[reference["artifact_id"]])
        self.assertEqual(draft.status_code, 200, draft.text)
        task = draft.json()
        self.assertEqual(task["base_image"]["artifact_id"], base["artifact_id"])
        self.assertEqual(self.storage.list_task_artifacts(task["project_id"], task["task_id"]), [])
        self.submit(task)
        backend = RecordingLocalBackend([self.fixture])
        LocalWorker(self.app.state.workbench, backend).serve(once=True)
        self.assertEqual(len(backend.requests), 1)
        request = backend.requests[0]
        self.assertEqual(request.edit_image.read_bytes(), png())
        self.assertEqual([path.read_bytes() for path in request.reference_images], [png("blue")])
        result = self.alice.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["outputs"][0]["source_artifact_id"], base["artifact_id"])
        self.assertEqual(self.alice.get(base["url"]).content, png())

    def test_remote_queue_manifest_and_asset_transport_include_references_and_edit_base(self):
        base = self.asset()
        reference = self.asset(png("blue"))
        unrelated = self.asset(png("yellow"))
        response = self.draft(operation="edit", base_artifact_id=base["artifact_id"],
                              refs=[reference["artifact_id"]])
        self.assertEqual(response.status_code, 200, response.text)
        self.submit(response.json())
        queue = RemoteQueue(self.app.state.workbench)
        worker_id = uuid4().hex
        manifest = queue.claim(worker_id, "local")
        self.assertEqual([item["artifact_id"] for item in manifest["inputs"]], [reference["artifact_id"]])
        self.assertEqual(manifest["base"]["artifact_id"], base["artifact_id"])
        arguments = (manifest["job_id"], worker_id, manifest["claim_id"])
        self.assertEqual(queue.asset(*arguments, reference["artifact_id"]), png("blue"))
        self.assertEqual(queue.asset(*arguments, base["artifact_id"]), png())
        with self.assertRaises(FileNotFoundError):
            queue.asset(*arguments, unrelated["artifact_id"])

    def test_legacy_file_inputs_and_historical_outputs_remain_usable_with_new_references(self):
        reference = self.asset()
        body = {"request_id": uuid4().hex, "operation": "generate", "prompt": "旧上传与素材组合",
                "reference_artifact_ids": json.dumps([reference["artifact_id"]])}
        response = self.alice.post(self.tasks_url, data=body,
                                  files={"files": ("legacy.png", png("green"), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        self.assertEqual(task["input_count"], 2)
        self.assertEqual(len(self.storage.list_task_artifacts(task["project_id"], task["task_id"])), 1)
        self.submit(task)
        backend = RecordingLocalBackend([self.fixture])
        LocalWorker(self.app.state.workbench, backend).serve(once=True)
        self.assertCountEqual([path.read_bytes() for path in backend.requests[0].reference_images],
                              [png(), png("green")])
        old = self.alice.get(f"/api/tasks/{task['task_id']}").json()["outputs"][0]
        branch = self.draft(operation="edit", base_artifact_id=old["artifact_id"])
        self.assertEqual(branch.status_code, 200, branch.text)
        self.assertEqual(branch.json()["base_image"]["artifact_id"], old["artifact_id"])
        self.assertEqual(self.alice.get(old["url"]).content, self.fixture.read_bytes())
        assets = self.alice.get(self.assets_url).json()["assets"]
        self.assertIn(old["artifact_id"], [item["artifact_id"] for item in assets])

    def test_legacy_task_options_without_reference_field_preserve_local_inputs(self):
        response = self.alice.post(self.tasks_url,
            data={"request_id": uuid4().hex, "operation": "generate", "prompt": "旧版本任务"},
            files={"files": ("legacy.png", png("green"), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        task = response.json()
        options = self.state.task_options(task["project_id"], task["task_id"])
        options.pop("reference_artifact_ids", None)
        options_path = self.state._task_file("tasks", task["project_id"], task["task_id"])
        self.storage._write_json(options_path, options)
        detail = self.alice.get(f"/api/tasks/{task['task_id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["input_count"], 1)
        self.submit(task)
        backend = RecordingLocalBackend([self.fixture])
        LocalWorker(self.app.state.workbench, backend).serve(once=True)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.requests[0].reference_images[0].read_bytes(), png("green"))
        self.assertEqual(self.alice.get(f"/api/tasks/{task['task_id']}").json()["status"], "awaiting_review")


if __name__ == "__main__":
    unittest.main()
