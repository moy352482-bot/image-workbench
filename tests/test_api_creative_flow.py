"""Exercise scripts -> storyboard -> SD2 prompt -> video queue using mock APIs."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx

from image_workbench.deepseek_text import DeepSeekTextAdapter, MODEL
from image_workbench.h3_worker import LocalVideoTransport
from image_workbench.online_video_planner import OnlineVideoPlanner
from image_workbench.online_video_worker import OnlineVideoWorker
from image_workbench.script_api_models import DeepSeekModelRegistry
from image_workbench.script_remote import RemoteScriptRegistry
from image_workbench.seedance_skill import SeedanceSkillPlanner, TASK_FIELDS
from tests import test_storyboard_http as board_fixtures
from tests import test_script_remote as remote_fixtures
from tests.test_script_service import TEXT


class ApiCreativeFlowTests(unittest.TestCase):
    def test_full_creative_flow_keeps_skills_zero_text_charges_and_video_reservation(self):
        fixture = board_fixtures.StoryboardHTTPTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        app = fixture.app
        app.video_store.enabled_backends = ("online-2.0",)
        registry = DeepSeekModelRegistry(available=True)
        app.script_service.registry = registry
        app.script_service.router.registry = registry
        app.storyboard_service.registry = registry
        app.storyboard_service.pricing_path = registry.root / "storyboard_pricing.json"
        calls = []
        rewritten = "海边夜景，中景缓慢推进。守灯人抬手挥向归船，轻声说：回家吧。海浪声伴随灯光亮起。"
        def text_api(request):
            payload = json.loads(request.content)
            calls.append(payload)
            prompt = payload["messages"][0]["content"]
            if "Seedance 2.0 镜头提示词" in prompt:
                task = json.loads(prompt.split("已确认的镜头数据：\n")[-1])
                output = json.dumps({"schema_version": 1, **{k: task[k] for k in TASK_FIELDS}, "prompt": rewritten}, ensure_ascii=False)
            elif "# Storyboard Planning" in prompt:
                output = json.dumps({"shots": [{"title": "海边挥手", "scene_id": "海边", "narrative_duration_ms": 5000,
                    "generation_duration_ms": 5000, "duration_reason": "挥手、对白与短暂停顿需要五秒", "visual_description": "守灯人朝归船挥手",
                    "dialogue": "回家吧。", "video_prompt": "守灯人挥手，说：回家吧。", "reference_artifact_ids": [],
                    "video_model_id": "online-2.0", "aspect_ratio": "16:9"}], "duration_conflict": None}, ensure_ascii=False)
            else:
                output = TEXT
            return httpx.Response(200, json={"id": "mock", "choices": [{"finish_reason": "stop", "message": {"content": output}}]})
        client = httpx.Client(transport=httpx.MockTransport(text_api))
        self.addCleanup(client.close)
        adapter = DeepSeekTextAdapter("mock-key", client=client)
        app.script_service.adapter = adapter
        app.storyboard_service.planner.adapter = adapter
        task = fixture.generate_script(model_id=MODEL, selection_mode="recommended")
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["model_id"], MODEL)
        self.assertIn("video-script-generation", calls[0]["messages"][0]["content"])
        board = fixture.plan(source={"version_id": task["version_id"]}, mode="api", video_model_id="online-2.0", target_duration_ms=5000)
        self.assertEqual(board["shots"][0]["video_model_id"], "online-2.0")
        self.assertEqual(fixture.identity.wallet(fixture.aid)["used_credit"], 0)
        shot = fixture.draft(board)
        self.assertEqual(shot["backend"], "online-2.0")
        store = app.video_store
        cost = shot["duration"] * store.config()["rates"]["720p"]
        job = store.submit(fixture.aid, fixture.wid, fixture.pid, shot["shot_id"], request_id=uuid4().hex,
                           confirm_generation=True, expected_credit=cost)
        self.assertIsNone(store.claim(uuid4().hex, "online-2.0"))  # Must finish prompt planning first.

        class Transport(LocalVideoTransport):
            def _path(self, manifest):
                return "/jobs/" + manifest["job_id"]
            def _request(self, method, path, manifest=None, **kwargs):
                if path.endswith("/claim"):
                    return {"job": self.store.claim(kwargs["json"]["worker_id"], "online-2.0", "planning")}
                return self.store.complete_plan(manifest["job_id"], manifest["worker_id"], manifest["claim_id"], kwargs["json"]["plan"])

        with tempfile.TemporaryDirectory() as directory:
            transport = Transport(store)
            planner = OnlineVideoPlanner(transport, SeedanceSkillPlanner(adapter), Path(directory) / "planning")
            planner.run_once()
            planner.run_once()
            self.assertEqual(len(calls), 3)
            manifest = store.claim(uuid4().hex, "online-2.0")
            submitted = []
            def video_api(request):
                submitted.append(json.loads(request.content))
                return httpx.Response(400, json={"error": "mock rejection"})
            with httpx.Client(transport=httpx.MockTransport(video_api)) as video_client:
                worker = OnlineVideoWorker(transport, "mock-video-key", manifest["worker_id"], Path(directory) / "video", client=video_client)
                worker.process(manifest)
            self.assertEqual(submitted, [{"prompt": rewritten, "mode": "2.0", "ratio": "16:9", "durationSeconds": 5}])
        wallet = fixture.identity.wallet(fixture.aid)
        self.assertEqual((wallet["used_credit"], wallet["frozen_credit"]), (0, 0))
        self.assertEqual(fixture.identity.settled_job(job["job_id"])["status"], "failed")
        self.assertEqual([call["model"] for call in calls], [MODEL] * 3)

    def test_cloud_worker_announces_deepseek_and_delivers_once_without_codex(self):
        fixture = remote_fixtures.RemoteScriptTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        registry = DeepSeekModelRegistry(available=True)
        remote = RemoteScriptRegistry(fixture.app.script_queue, config_root=registry.root)
        fixture.app.script_service.registry = remote
        fixture.app.script_service.router.registry = remote
        fixture.worker.registry = registry
        calls = []
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "mock", "choices": [{"finish_reason": "stop", "message": {"content": TEXT}}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            fixture.worker.adapter = DeepSeekTextAdapter("mock-key", client=client)
            task = fixture.submit(model_id=MODEL)
            self.assertEqual(fixture.worker.run_once(), "completed")
            self.assertEqual(fixture.worker.run_once(), "idle")
        detail = fixture.user.get("/api/scripts/tasks/" + task["task_id"]).json()
        self.assertEqual((detail["text"], detail["model_id"], detail["price"]), (TEXT, MODEL, 0))
        self.assertEqual(len(calls), 1)
