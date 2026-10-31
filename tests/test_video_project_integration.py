"""Shared main/subproject ownership across image, script, storyboard and video work."""
import hashlib
import unittest
from uuid import uuid4

from image_workbench.project_manager import ProjectManager
from tests import test_video_pipeline
from tests.test_script_service import TEXT
from tests.test_video import mp4, png


class VideoProjectIntegrationTests(unittest.TestCase):
    client = test_video_pipeline.PipelineTests.client

    def setUp(self):
        test_video_pipeline.PipelineTests.setUp(self)
        self.store = self.app.video_store
        self.projects = ProjectManager(self.storage)
        with self.identity.transaction() as db:
            self.admin_id, _ = self.identity._account(db, "video_archive_admin", None, admin=True)
        self.store.set_config(self.admin_id, {"720p": 2, "1080p": 4})

    def json(self, method, path, body=None, **kwargs):
        if body is not None:
            kwargs["json"] = body
        response = getattr(self.alice, method)(path, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def make_main(self, name):
        return self.json("post", "/api/main-projects", {"request_id": uuid4().hex, "name": name})

    def make_sub(self, main_id, name):
        return self.json("post", f"/api/main-projects/{main_id}/subprojects",
                         {"request_id": uuid4().hex, "name": name})

    def mapped(self, subproject_id, client=None):
        response = (client or self.alice).post("/api/video/projects/from-subproject",
                                               json={"subproject_id": subproject_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create_shot(self, pid, *, duration=4, reference_ids=None, mode="T2V"):
        return self.json("post", f"/api/video/projects/{pid}/shots", {
            "request_id": uuid4().hex, "name": "项目分镜", "prompt": "镜头缓慢推进",
            "mode": mode, "duration": duration, "aspect_ratio": "landscape",
            "reference_ids": reference_ids or [],
        })

    def complete(self, pid, shot):
        rate = self.store.config()["rates"]["720p"]
        job = self.json("post", f"/api/video/projects/{pid}/shots/{shot['shot_id']}/run", {
            "request_id": uuid4().hex, "confirm_generation": True,
            "expected_credit": shot["duration"] * rate,
        })
        worker = uuid4().hex
        manifest = self.store.claim(worker)
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["job_id"], job["job_id"])
        self.store.start(job["job_id"], worker, manifest["claim_id"], uuid4().hex)
        data = mp4()
        digest = hashlib.sha256(data).hexdigest()
        self.store.upload_output(job["job_id"], worker, manifest["claim_id"], data, digest)
        done = self.store.complete(job["job_id"], worker, manifest["claim_id"], "succeeded", sha256=digest)
        self.assertEqual(done["status"], "completed")
        return done

    def media_snapshot(self):
        return {str(path.relative_to(self.storage.data_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.storage.data_root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".txt"}}

    def test_main_and_subprojects_are_shared_renamable_and_workspace_isolated(self):
        main = self.make_main("共享品牌项目")
        default = next(sub for sub in main["subprojects"] if sub["name"] == "未分类任务")
        autumn = self.make_sub(main["main_project_id"], "秋季宣传片")

        overview = self.json("get", "/api/main-projects")["main_projects"]
        shared = next(item for item in overview if item["main_project_id"] == main["main_project_id"])
        self.assertEqual({sub["subproject_id"] for sub in shared["subprojects"]},
                         {default["subproject_id"], autumn["subproject_id"]})
        image_view = self.json("get", "/api/projects")["projects"]
        self.assertIn(main["main_project_id"], {item["project_id"] for item in image_view})

        before = self.media_snapshot()
        first = self.mapped(default["subproject_id"])
        again = self.mapped(default["subproject_id"])
        self.assertEqual(first["project_id"], again["project_id"])
        self.assertEqual(first["main_project_id"], main["main_project_id"])
        self.assertEqual(first["subproject_name"], "未分类任务")
        second = self.mapped(autumn["subproject_id"])
        self.assertNotEqual(first["project_id"], second["project_id"])
        self.assertEqual(second["main_project_name"], "共享品牌项目")
        self.assertEqual(before, self.media_snapshot())

        self.assertEqual(self.bobby.post("/api/video/projects/from-subproject",
                                         json={"subproject_id": autumn["subproject_id"]}).status_code, 404)
        self.assertEqual(self.alice.post("/api/video/projects/from-subproject",
                                         json={"subproject_id": autumn["subproject_id"]},
                                         headers={"X-Workspace-ID": self.other_wid}).status_code, 404)
        self.assertEqual(self.bobby.get(f"/api/main-projects/{main['main_project_id']}").status_code, 404)

        self.json("patch", f"/api/main-projects/{main['main_project_id']}", {"name": "共享品牌 2026"})
        self.json("patch", f"/api/subprojects/{autumn['subproject_id']}", {"name": "秋季成片"})
        renamed = self.mapped(autumn["subproject_id"])
        self.assertEqual(renamed["project_id"], second["project_id"])
        self.assertEqual(renamed["main_project_name"], "共享品牌 2026")
        self.assertEqual(renamed["subproject_name"], "秋季成片")
        self.assertEqual(before, self.media_snapshot())

    def test_scripts_and_image_artifacts_stay_in_the_selected_subproject(self):
        main = self.make_main("素材共享项目")
        default = main["subprojects"][0]
        other = self.make_sub(main["main_project_id"], "另一子项目")
        project = self.mapped(default["subproject_id"])
        other_project = self.mapped(other["subproject_id"])

        uploaded_script = self.json("post", f"/api/video/projects/{project['project_id']}/script/upload",
                                    None, data={"request_id": uuid4().hex},
                                    files={"file": ("脚本.txt", TEXT.encode(), "text/plain")})
        script_tasks = self.json("get", f"/api/scripts/tasks?subproject_id={default['subproject_id']}")["tasks"]
        self.assertIn(uploaded_script["task_id"], {task["task_id"] for task in script_tasks})
        pipeline = self.json("get", f"/api/video/projects/{project['project_id']}/pipeline")
        self.assertIn(uploaded_script["task_id"], {task["task_id"] for task in pipeline["script_tasks"]})
        self.assertEqual(pipeline["main_project_name"], "素材共享项目")
        self.assertEqual(pipeline["subproject_name"], "未分类任务")

        uploaded = self.json("post", f"/api/subprojects/{default['subproject_id']}/assets",
                             None, files={"files": ("角色.png", png(), "image/png")})["assets"][0]
        image_id = uploaded["artifact_id"]
        linked = self.json("post", f"/api/video/projects/{project['project_id']}/images/from-artifact",
                           {"artifact_id": image_id})
        self.assertEqual(linked["assets"][0]["artifact_id"], image_id)
        self.assertEqual(self.storage.get_artifact(image_id).artifact_id, image_id)

        shot = self.create_shot(project["project_id"], reference_ids=[image_id], mode="I2V")
        self.assertEqual(shot["reference_ids"], [image_id])
        self.assertEqual(shot["main_project_id"], main["main_project_id"])
        self.assertEqual(shot["subproject_id"], default["subproject_id"])
        self.assertEqual(shot["reference_ids"][0], image_id)
        self.assertEqual(self.storage.get_artifact(image_id).artifact_id, image_id)

        rejected = self.alice.post(f"/api/video/projects/{other_project['project_id']}/images/from-artifact",
                                   json={"artifact_id": image_id})
        self.assertEqual(rejected.status_code, 400, rejected.text)

    def test_successful_video_is_archived_once_under_the_selected_subproject(self):
        main = self.make_main("视频归档项目")
        first_sub = main["subprojects"][0]
        second_sub = self.make_sub(main["main_project_id"], "第二子项目")
        first = self.mapped(first_sub["subproject_id"])
        second = self.mapped(second_sub["subproject_id"])

        shot_a = self.create_shot(first["project_id"])
        shot_b = self.create_shot(second["project_id"])
        job_a = self.complete(first["project_id"], shot_a)
        job_b = self.complete(second["project_id"], shot_b)

        self.assertEqual(job_a["subproject_id"], first_sub["subproject_id"])
        self.assertEqual(job_b["subproject_id"], second_sub["subproject_id"])
        self.assertEqual(job_a["main_project_id"], main["main_project_id"])
        self.assertNotEqual(job_a["output_artifact_id"], job_b["output_artifact_id"])
        for job, sub_id in ((job_a, first_sub["subproject_id"]), (job_b, second_sub["subproject_id"])):
            artifact = self.storage.get_artifact(job["output_artifact_id"])
            self.assertEqual(artifact.format, "MP4")
            self.assertEqual(self.projects.get_task_placement(job["output_task_id"]).subproject_id, sub_id)
            video_tasks = [item for _, item in self.projects.list_tasks(subproject_id=sub_id)
                           if item.task_id == job["output_task_id"]]
            self.assertEqual(len(video_tasks), 1)

        first_view = self.json("get", f"/api/video/projects/{first['project_id']}")
        second_view = self.json("get", f"/api/video/projects/{second['project_id']}")
        self.assertEqual(first_view["shots"][0]["artifact_id"], job_a["output_artifact_id"])
        self.assertEqual(second_view["shots"][0]["artifact_id"], job_b["output_artifact_id"])
        self.assertNotIn(job_a["output_artifact_id"],
                         {item["artifact_id"] for item in self.json("get", f"/api/video/projects/{second['project_id']}/tray")["assets"]})

        jobs, wallet = len(self.store._jobs()), self.identity.wallet(self.aid)["used_credit"]
        self.mapped(first_sub["subproject_id"])
        self.mapped(second_sub["subproject_id"])
        self.json("get", f"/api/video/projects/{first['project_id']}/pipeline")
        self.json("get", f"/api/video/projects/{second['project_id']}/pipeline")
        self.assertEqual(len(self.store._jobs()), jobs)
        self.assertEqual(self.identity.wallet(self.aid)["used_credit"], wallet)

    def test_upload_and_unbound_history_are_archived_after_explicit_binding(self):
        main = self.make_main("兼容项目")
        sub = main["subprojects"][0]
        legacy_sub = self.make_sub(main["main_project_id"], "旧版归档")
        project = self.mapped(sub["subproject_id"])
        request_id = uuid4().hex
        uploaded = self.json("post", f"/api/video/projects/{project['project_id']}/videos/upload", None,
                             data={"request_id": request_id, "name": "已有片段"},
                             files={"file": ("片段.mp4", mp4(), "video/mp4")})
        artifact_id = uploaded["output"]["artifact_id"]
        self.assertEqual(uploaded["subproject_id"], sub["subproject_id"])
        self.assertEqual(uploaded["main_project_id"], main["main_project_id"])
        self.assertEqual(self.storage.get_artifact(artifact_id).format, "MP4")
        self.assertEqual(self.projects.get_task_placement(
            self.storage.get_artifact(artifact_id).task_id).subproject_id, sub["subproject_id"])
        replay = self.json("post", f"/api/video/projects/{project['project_id']}/videos/upload", None,
                           data={"request_id": request_id, "name": "已有片段"},
                           files={"file": ("片段.mp4", mp4(), "video/mp4")})
        self.assertEqual(replay["output"]["artifact_id"], artifact_id)

        legacy_pid = uuid4().hex
        self.json("post", "/api/video/projects", {"request_id": legacy_pid, "name": "旧版视频项目"})
        legacy_shot = self.create_shot(legacy_pid)
        legacy_job = self.complete(legacy_pid, legacy_shot)
        self.assertNotIn("subproject_id", legacy_job)
        self.assertNotIn("output_artifact_id", legacy_job)
        self.assertEqual(self.json("get", f"/api/video/projects/{legacy_pid}")["shots"][0]["name"], "项目分镜")
        self.assertEqual(self.json("get", f"/api/video/projects/{legacy_pid}")["pipeline"], {})

        bound = self.json("post", f"/api/video/projects/{legacy_pid}/bind-subproject",
                          {"subproject_id": legacy_sub["subproject_id"]})
        self.assertEqual(bound["subproject_id"], legacy_sub["subproject_id"])
        self.assertEqual(bound["main_project_id"], main["main_project_id"])
        self.assertEqual(bound["shots"][0]["shot_id"], legacy_shot["shot_id"])
        self.assertEqual(self.json("post", f"/api/video/projects/{legacy_pid}/bind-subproject",
                                   {"subproject_id": legacy_sub["subproject_id"]})["project_id"], legacy_pid)
        self.assertEqual(self.alice.post(
            f"/api/video/projects/{legacy_pid}/bind-subproject",
            json={"subproject_id": self.link["subproject_id"]}).status_code, 409)

        new_shot = self.create_shot(legacy_pid)
        self.assertEqual(new_shot["subproject_id"], legacy_sub["subproject_id"])
        new_job = self.complete(legacy_pid, new_shot)
        self.assertEqual(new_job["subproject_id"], legacy_sub["subproject_id"])
        self.assertEqual(self.storage.get_artifact(new_job["output_artifact_id"]).format, "MP4")


if __name__ == "__main__":
    unittest.main()
