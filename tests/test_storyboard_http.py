"""Storyboard acceptance contract; all files and media workers stay local.

Run: .venv/Scripts/python -m unittest tests.test_storyboard_http -v
"""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from image_workbench import StorageConfig, StorageManager
from image_workbench.accounts import digest
from image_workbench.script_models import ScriptModelRegistry
from image_workbench.storage import _now
from image_workbench.web import create_app
from tests.test_script_service import TEXT
from tests.test_video import png, mp4


class StoryboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        self.app = create_app(self.storage, backend="local")
        self.app.video_store.enabled_backends = ("h3", "online-2.0")  # Legacy queue compatibility fixture.
        self.identity = self.app.identity
        with self.identity.transaction() as db:
            self.aid, self.wid = self.identity._account(db, "storyboard_alice", None)
            self.bid, self.bwid = self.identity._account(db, "storyboard_bobby", None)
        self.other_wid = self.identity.create_workspace(self.aid, "其他空间")["workspace_id"]
        self.alice = self.client(self.aid, self.wid)
        self.bobby = self.client(self.bid, self.bwid)
        self.anon = self.client()
        self.pid = uuid4().hex
        self.prefix = f"/api/video/projects/{self.pid}"
        self.post("/api/video/projects", {"request_id": self.pid, "name": "分镜验收"})
        self.link = self.post(self.prefix + "/pipeline", {})
        self.script_adapter = Mock()
        self.script_adapter.generate.return_value = TEXT
        self.app.script_service.adapter = self.script_adapter
        registry = ScriptModelRegistry(catalog={"models": [{"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": "high"}]}]})
        self.app.script_service.registry = registry
        self.app.script_service.router.registry = registry
        if hasattr(self.app, "storyboard_service"):
            self.app.storyboard_service.registry = registry
            self.planner_adapter = Mock()
            self.planner_adapter.generate.side_effect = AssertionError("验收测试禁止调用真实模型")
            self.app.storyboard_service.planner.adapter = self.planner_adapter

    def client(self, aid=None, wid=None):
        client = TestClient(self.app, base_url="http://localhost", raise_server_exceptions=False)
        self.addCleanup(client.close)
        if aid:
            token, csrf = uuid4().hex, uuid4().hex
            with self.identity.transaction() as db:
                db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (digest(token), aid, csrf, wid, _now(), time.time() + 3600, "storyboard-test"))
            client.cookies.set("iw_session", token)
            client.headers["X-Workbench-CSRF"] = csrf
        return client

    def post(self, path, body, status=200):
        response = self.alice.post(path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def get(self, path):
        response = self.alice.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def upload(self, text=TEXT):
        response = self.alice.post(self.prefix + "/script/upload", data={"request_id": uuid4().hex}, files={"file": ("原创脚本.txt", text.encode(), "text/plain")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def options(self, **changes):
        value = {"mode": "mock", "selection_mode": "recommended", "target_duration_ms": 12000, "aspect_ratio": "16:9", "video_model_id": "h3", "video_project_id": self.pid}
        value.update(changes)
        return value

    def script_body(self, **changes):
        value = {"subproject_id": self.link["subproject_id"], "generation_type": "script", "input_request": "写海边守灯人的原创视频脚本", "selection_mode": "manual", "model_id": "gpt-6-luna", "thinking_level": "high"}
        value.update(changes)
        return value

    def generate_script(self, **changes):
        body = self.script_body(**changes)
        quote = self.post("/api/scripts/quote", body)
        task = self.post("/api/scripts/tasks", {**body, "request_id": uuid4().hex, "quote_signature": quote["quote_signature"], "storyboard_confirmed": bool(body.get("storyboard_options"))})
        return self.get(f"/api/scripts/tasks/{task['task_id']}")

    def plan(self, source=None, **changes):
        source = source or self.upload()
        body = self.options(source_script_version=source["version_id"], **changes)
        quote = self.post("/api/storyboards/quote", body)
        self.assertTrue(quote["can_generate"], quote)
        task = self.post("/api/storyboards/tasks", {**body, "request_id": uuid4().hex, "quote_signature": quote["quote_signature"], "confirmed": True})
        task = self.get(f"/api/storyboards/tasks/{task['task_id']}")
        self.assertEqual(task["status"], "completed", task)
        return self.detail(task["storyboard_id"])

    def detail(self, sid, version=None):
        return self.get(f"/api/storyboards/{sid}" + (f"?version={version}" if version else ""))

    def edit(self, board, operation, **changes):
        return self.post(f"/api/storyboards/{board['storyboard_id']}/edit", {"request_id": uuid4().hex, "expected_version": board["version"], "operation": operation, **changes})

    def draft(self, board, shot=None, **changes):
        shot = shot or board["shots"][0]
        return self.post(f"/api/storyboards/{board['storyboard_id']}/shots/{shot['shot_id']}/video-draft", {"request_id": uuid4().hex, "expected_version": board["version"], "resolution": "720p", "allow_draft_test": True, **changes})

    def test_01_script_without_storyboard_keeps_original_txt_workflow(self):
        task = self.generate_script()
        self.assertEqual(task["status"], "completed")
        response = self.alice.get(f"/api/scripts/tasks/{task['task_id']}/download")
        self.assertEqual(response.content.decode(), TEXT)
        self.assertEqual(self.get(f"/api/storyboards?subproject_id={self.link['subproject_id']}")["storyboards"], [])
        self.script_adapter.generate.assert_called_once()

    def test_02_script_linkage_completes_without_browser_second_submit(self):
        task = self.generate_script(storyboard_options=self.options())
        self.assertEqual(task["status"], "completed")
        boards = self.get(f"/api/storyboards?video_project_id={self.pid}")["storyboards"]
        self.assertEqual(len(boards), 1)
        board = self.detail(boards[0]["storyboard_id"])
        self.assertEqual(board["source_script_version"], task["version_id"])
        self.assertEqual(board["source_script_artifact_id"], task["output_artifact_id"])

    def test_03_script_failure_never_starts_storyboard(self):
        self.script_adapter.generate.side_effect = RuntimeError("测试脚本生成失败")
        task = self.generate_script(storyboard_options=self.options())
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.get(f"/api/storyboards?video_project_id={self.pid}")["storyboards"], [])

    def test_04_planner_failure_preserves_completed_script_and_txt(self):
        with patch.object(self.app.storyboard_service.planner, "plan", side_effect=RuntimeError("测试分镜规划失败")):
            task = self.generate_script(storyboard_options=self.options())
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.alice.get(f"/api/scripts/tasks/{task['task_id']}/download").content.decode(), TEXT)
        self.assertEqual(task["storyboard_task"]["status"], "failed")
        failed = self.get(f"/api/storyboards/tasks/{task['storyboard_task_id']}")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("测试分镜规划失败", failed["error"])
        self.assertEqual(self.get(f"/api/storyboards?video_project_id={self.pid}")["storyboards"], [])

    def test_05_historical_script_version_is_selected_explicitly(self):
        old = self.upload()
        self.upload(TEXT.replace("回家吧", "新的结尾"))
        board = self.plan(old)
        self.assertEqual(board["source_script_version"], old["version_id"])
        self.assertEqual(board["source_script_artifact_id"], old["output_artifact_id"])

    def test_06_narrative_and_generation_duration_are_separate_with_conflict(self):
        board = self.plan(target_duration_ms=1000)
        self.assertIsNotNone(board["duration_conflict"])
        self.assertEqual(board["duration_conflict"]["target_duration_ms"], 1000)
        self.assertTrue(board["duration_conflict"]["suggestions"])
        for shot in board["shots"]:
            self.assertGreater(shot["narrative_duration_ms"], 0)
            self.assertGreater(shot["generation_duration_ms"], 0)
            self.assertTrue(shot["duration_reason"])

    def test_07_update_retains_stable_id_and_old_version(self):
        board = self.plan()
        old = board["shots"][0]
        edited = self.edit(board, "update", shot_id=old["shot_id"], changes={"title": "手动修改标题"})
        self.assertEqual(edited["version"], board["version"] + 1)
        self.assertEqual(edited["shots"][0]["shot_id"], old["shot_id"])
        self.assertEqual(self.detail(board["storyboard_id"], board["version"])["shots"], board["shots"])

    def test_08_add_delete_preserve_remaining_ids(self):
        board = self.plan()
        ids = [s["shot_id"] for s in board["shots"]]
        added = self.edit(board, "add", changes={"title": "新增镜头", "narrative_duration_ms": 1000, "generation_duration_ms": 4000})
        new_ids = [s["shot_id"] for s in added["shots"] if s["shot_id"] not in ids]
        self.assertEqual(len(new_ids), 1)
        removed = self.edit(added, "delete", shot_id=new_ids[0])
        self.assertEqual([s["shot_id"] for s in removed["shots"]], ids)
        self.assertIn(new_ids[0], [s["shot_id"] for s in removed["retired_shots"]])

    def test_09_split_allocates_new_ids_and_records_lineage(self):
        board = self.plan()
        old = board["shots"][0]["shot_id"]
        split = self.edit(board, "split", shot_id=old, parts=[{"title": "前半", "narrative_duration_ms": 1000}, {"title": "后半", "narrative_duration_ms": 1000}])
        children = [s for s in split["shots"] if old in s["source_shot_ids"]]
        self.assertEqual(len(children), 2)
        self.assertNotIn(old, [s["shot_id"] for s in split["shots"]])
        self.assertIn(old, [s["shot_id"] for s in split["retired_shots"]])

    def test_10_merge_allocates_new_id_and_retires_sources(self):
        board = self.plan()
        board = self.edit(board, "split", shot_id=board["shots"][0]["shot_id"], parts=[{"title": "甲"}, {"title": "乙"}])
        ids = [s["shot_id"] for s in board["shots"][:2]]
        merged = self.edit(board, "merge", shot_ids=ids)
        children = [s for s in merged["shots"] if set(ids).issubset(s["source_shot_ids"])]
        self.assertEqual(len(children), 1)
        self.assertNotIn(children[0]["shot_id"], ids)
        self.assertTrue(set(ids).issubset(s["shot_id"] for s in merged["retired_shots"]))

    def test_11_reorder_changes_order_only_and_preserves_ids(self):
        board = self.plan()
        if len(board["shots"]) < 2:
            board = self.edit(board, "add", changes={"title": "第二镜"})
        ids = [s["shot_id"] for s in board["shots"]][::-1]
        edited = self.edit(board, "reorder", shot_ids=ids)
        self.assertEqual([s["shot_id"] for s in edited["shots"]], ids)
        self.assertEqual(len(set(s["order"] for s in edited["shots"])), len(ids))

    def test_12_local_replan_is_preview_until_explicit_accept(self):
        board = self.plan()
        selected = board["shots"][0]["shot_id"]
        body = self.options(expected_version=board["version"], shot_ids=[selected], reason="调整选中镜头的情绪")
        base = f"/api/storyboards/{board['storyboard_id']}"
        quote = self.post(base + "/replan/quote", body)
        queued = self.post(base + "/replan", {**body, "request_id": uuid4().hex, "quote_signature": quote["quote_signature"], "confirmed": True})
        task = self.get(f"/api/storyboards/tasks/{queued['task_id']}")
        self.assertEqual(task["status"], "completed", task)
        self.assertTrue(task["preview"]["shots"])
        self.assertEqual(self.detail(board["storyboard_id"])["version"], board["version"])
        untouched = {s["shot_id"]: s for s in board["shots"] if s["shot_id"] != selected}
        for shot in task["preview"]["shots"]:
            if shot["shot_id"] in untouched:
                self.assertEqual({k: v for k, v in shot.items() if k != "order"},
                                 {k: v for k, v in untouched[shot["shot_id"]].items() if k != "order"})
        accepted = self.post(base + "/accept", {"request_id": uuid4().hex, "task_id": task["task_id"], "expected_version": board["version"]})
        self.assertEqual(accepted["version"], board["version"] + 1)

    def test_13_stale_expected_version_cannot_overwrite(self):
        board = self.plan()
        edited = self.edit(board, "save", target_duration_ms=15000)
        self.post(f"/api/storyboards/{board['storyboard_id']}/edit", {"request_id": uuid4().hex, "expected_version": board["version"], "operation": "save", "target_duration_ms": 16000}, 409)
        self.assertEqual(self.detail(board["storyboard_id"])["version"], edited["version"])

    def test_14_version_history_remains_readable_after_edits(self):
        board = self.plan()
        edited = self.edit(board, "save", target_duration_ms=15000)
        history = self.detail(board["storyboard_id"])
        self.assertEqual(sorted(v["version"] for v in history["versions"]), [board["version"], edited["version"]])
        self.assertEqual(self.detail(board["storyboard_id"], board["version"])["target_duration_ms"], 12000)

    def test_15_missing_image_artifact_blocks_draft_without_substitution(self):
        board = self.plan()
        response = self.alice.post(f"/api/storyboards/{board['storyboard_id']}/edit", json={"request_id": uuid4().hex, "expected_version": board["version"], "operation": "update", "shot_id": board["shots"][0]["shot_id"], "changes": {"reference_artifact_ids": [uuid4().hex]}})
        # Missing bindings may be rejected on edit or on materializing the draft.
        if response.status_code == 200:
            board = response.json()
            response = self.alice.post(f"/api/storyboards/{board['storyboard_id']}/shots/{board['shots'][0]['shot_id']}/video-draft", json={"request_id": uuid4().hex, "expected_version": board["version"], "allow_draft_test": True})
        self.assertIn(response.status_code, (400, 404, 409), response.text)
        self.assertEqual(self.get(self.prefix)["shots"], [])

    def test_16_project_reference_image_maps_to_artifact_and_i2v(self):
        uploaded = self.alice.post(self.prefix + "/assets", files=[("files", ("hero.png", png(), "image/png"))])
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        board = self.plan()
        assets = self.get(f"/api/storyboards/{board['storyboard_id']}/assets")["assets"]
        self.assertEqual(len(assets), 1)
        artifact_id = assets[0]["artifact_id"]
        self.assertEqual(self.storage.get_artifact(artifact_id).format, "PNG")
        edited = self.edit(board, "update", shot_id=board["shots"][0]["shot_id"], changes={"start_frame_artifact_id": artifact_id})
        draft = self.draft(edited)
        self.assertEqual(draft["mode"], "I2V")
        self.assertEqual(len(draft["reference_ids"]), 1)

    def test_17_video_draft_binds_exact_storyboard_version_without_running(self):
        board = self.plan()
        draft = self.draft(board)
        for key, value in (("storyboard_id", board["storyboard_id"]), ("storyboard_version", board["version"]), ("storyboard_shot_id", board["shots"][0]["shot_id"])):
            self.assertEqual(draft[key], value)
        self.assertEqual(draft["status"], "draft")
        self.assertIsNone(self.app.video_store.claim(uuid4().hex))
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_18_successful_video_registers_mp4_and_edit_preserves_outdated_result(self):
        board = self.plan()
        original_shot_id = board["shots"][0]["shot_id"]
        board = self.edit(board, "update", shot_id=original_shot_id, changes={"narrative_duration_ms": 4000, "generation_duration_ms": 4000})
        draft = self.draft(board)
        self.post(self.prefix + f"/shots/{draft['shot_id']}/run", {"request_id": uuid4().hex, "confirm_generation": True, "expected_credit": draft["estimated_credit"]})
        worker = uuid4().hex
        manifest = self.app.video_store.claim(worker)
        job, claim = manifest["job_id"], manifest["claim_id"]
        self.app.video_store.start(job, worker, claim, uuid4().hex)
        data = mp4()
        sha = hashlib.sha256(data).hexdigest()
        self.app.video_store.upload_output(job, worker, claim, data, sha)
        self.app.video_store.complete(job, worker, claim, "succeeded", sha256=sha)
        videos = self.detail(board["storyboard_id"])["videos"]
        self.assertEqual(len(videos), 1)
        self.assertFalse(videos[0]["outdated"])
        self.assertEqual(self.storage.get_artifact(videos[0]["artifact_id"]).format, "MP4")
        self.assertEqual(self.alice.get(videos[0]["output_url"]).content, data)
        board = self.edit(board, "reorder", shot_ids=[s["shot_id"] for s in board["shots"]][::-1])
        self.assertFalse(self.detail(board["storyboard_id"])["videos"][0]["outdated"])
        board = self.edit(board, "update", shot_id=original_shot_id, changes={"video_prompt": "不同的动作"})
        old = self.detail(board["storyboard_id"])["videos"][0]
        self.assertTrue(old["outdated"])
        self.assertEqual(self.alice.get(old["output_url"]).content, data)

    def test_19_model_duration_limit_blocks_video_draft(self):
        board = self.plan()
        board = self.edit(board, "update", shot_id=board["shots"][0]["shot_id"], changes={"generation_duration_ms": 999000})
        response = self.alice.post(f"/api/storyboards/{board['storyboard_id']}/shots/{board['shots'][0]['shot_id']}/video-draft", json={"request_id": uuid4().hex, "expected_version": board["version"], "allow_draft_test": True})
        self.assertIn(response.status_code, (400, 409), response.text)
        self.assertEqual(self.get(self.prefix)["shots"], [])

    def test_20_review_required_unless_explicit_draft_test(self):
        board = self.plan()
        base = f"/api/storyboards/{board['storyboard_id']}/shots/{board['shots'][0]['shot_id']}/video-draft"
        self.post(base, {"request_id": uuid4().hex, "expected_version": board["version"], "allow_draft_test": False}, 409)
        approved = self.edit(board, "approve")
        self.assertEqual(approved["status"], "approved")
        self.draft(approved, allow_draft_test=False)

    def test_21_real_mode_disabled_never_starts_model_or_reserves_credit(self):
        source = self.upload()
        body = self.options(mode="codex", source_script_version=source["version_id"])
        quote = self.post("/api/storyboards/quote", body)
        self.assertFalse(quote["can_generate"])
        response = self.alice.post("/api/storyboards/tasks", json={**body, "request_id": uuid4().hex, "confirmed": True, "quote_signature": quote["quote_signature"]})
        self.assertIn(response.status_code, (400, 409), response.text)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_22_request_replay_and_refresh_create_one_board(self):
        source = self.upload()
        body = self.options(source_script_version=source["version_id"])
        quote = self.post("/api/storyboards/quote", body)
        submitted = {**body, "request_id": uuid4().hex, "quote_signature": quote["quote_signature"], "confirmed": True}
        first = self.post("/api/storyboards/tasks", submitted)
        self.assertEqual(self.post("/api/storyboards/tasks", submitted)["task_id"], first["task_id"])
        self.post(f"/api/storyboards/tasks/{first['task_id']}/run", {})
        self.get(f"/api/storyboards/tasks/{first['task_id']}")
        self.assertEqual(len(self.get(f"/api/storyboards?video_project_id={self.pid}")["storyboards"]), 1)

    def test_23_account_workspace_and_csrf_boundaries(self):
        board = self.plan()
        path = f"/api/storyboards/{board['storyboard_id']}"
        self.assertEqual(self.bobby.get(path).status_code, 404)
        self.assertEqual(self.alice.get(path, headers={"X-Workspace-ID": self.other_wid}).status_code, 404)
        self.assertEqual(self.anon.get(path).status_code, 401)
        self.assertEqual(self.alice.post(path + "/edit", json={"request_id": uuid4().hex, "expected_version": board["version"], "operation": "approve"}, headers={"X-Workbench-CSRF": ""}).status_code, 403)

    def test_24_original_video_pipeline_still_uses_script_reference_and_pricing(self):
        self.upload()
        uploaded = self.alice.post(self.prefix + "/assets", files=[("files", ("hero.png", png(), "image/png"))]).json()["assets"][0]
        shot = self.post(self.prefix + "/shots", {"request_id": uuid4().hex, "name": "原视频回归", "prompt": "主角挥手", "mode": "I2V", "duration": 4, "aspect_ratio": "landscape", "reference_ids": [uploaded["asset_id"]]})
        self.assertEqual(shot["estimated_credit"], 160)
        self.post(self.prefix + f"/shots/{shot['shot_id']}/run", {"request_id": uuid4().hex, "confirm_generation": True, "expected_credit": 160})
        manifest = self.app.video_store.claim(uuid4().hex)
        self.assertEqual(manifest["project"]["script"], TEXT)
        self.assertEqual(manifest["task"]["reference_ids"], [uploaded["asset_id"]])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 160)

    def set_storyboard_pricing(self, models):
        service = self.app.storyboard_service
        service.registry = self.app.script_service.registry
        service.pricing_path = self.root / "storyboard_pricing.json"
        service.pricing_path.write_text(json.dumps({"mock_enabled": True, "real_enabled": True, "free_test_enabled": False, "models": models}), encoding="utf-8")
        adapter = Mock()
        adapter.generate.side_effect = AssertionError("拒绝条件下不得调用真实模型")
        service.planner.adapter = adapter
        return adapter

    def test_25_unpriced_storyboard_tier_cannot_submit_or_inherit_script_price(self):
        adapter = self.set_storyboard_pricing({})
        source = self.upload()
        body = self.options(mode="codex", selection_mode="manual", model_id="gpt-6-luna", thinking_level="high", source_script_version=source["version_id"])
        quote = self.post("/api/storyboards/quote", body)
        self.assertIsNone(quote["price"])
        self.assertFalse(quote["can_generate"])
        response = self.alice.post("/api/storyboards/tasks", json={**body, "request_id": uuid4().hex, "confirmed": True, "quote_signature": quote["quote_signature"]})
        self.assertIn(response.status_code, (400, 409), response.text)
        adapter.generate.assert_not_called()
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_26_insufficient_storyboard_credit_blocks_before_model_or_reservation(self):
        adapter = self.set_storyboard_pricing({"gpt-6-luna": {"high": 201}})
        source = self.upload()
        body = self.options(mode="codex", selection_mode="manual", model_id="gpt-6-luna", thinking_level="high", source_script_version=source["version_id"])
        quote = self.post("/api/storyboards/quote", body)
        self.assertEqual(quote["price"], 201)
        self.assertEqual(quote["shortfall"], 1)
        self.assertFalse(quote["can_generate"])
        response = self.alice.post("/api/storyboards/tasks", json={**body, "request_id": uuid4().hex, "confirmed": True, "quote_signature": quote["quote_signature"]})
        self.assertEqual(response.status_code, 402, response.text)
        adapter.generate.assert_not_called()
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.identity.wallet(self.aid)["remaining_credit"], 200)

    def test_27_edit_replay_is_idempotent_but_changed_payload_is_rejected(self):
        board = self.plan()
        path = f"/api/storyboards/{board['storyboard_id']}/edit"
        body = {"request_id": uuid4().hex, "expected_version": board["version"], "operation": "save", "target_duration_ms": 15000}
        first = self.post(path, body)
        self.assertEqual(self.post(path, body)["version"], first["version"])
        self.post(path, {**body, "target_duration_ms": 16000}, 409)
        self.assertEqual(len(self.detail(board["storyboard_id"])["versions"]), 2)

    def test_28_nonadjacent_replan_keeps_middle_shots_and_relative_story_order(self):
        board = self.plan()
        for title in ("中段桥接", "最后收束"):
            board = self.edit(board, "add", changes={"title": title, "visual_description": title})
        first, last = board["shots"][0], board["shots"][-1]
        middle = board["shots"][1:-1]
        body = self.options(expected_version=board["version"], shot_ids=[first["shot_id"], last["shot_id"]], reason="分别改善开场和收束，保留中间剧情")
        base = f"/api/storyboards/{board['storyboard_id']}"
        quote = self.post(base + "/replan/quote", body)
        queued = self.post(base + "/replan", {**body, "request_id": uuid4().hex, "quote_signature": quote["quote_signature"], "confirmed": True})
        task = self.get(f"/api/storyboards/tasks/{queued['task_id']}")
        self.assertEqual(task["status"], "completed", task)
        shots = task["preview"]["shots"]
        remaining = [s for s in shots if s["shot_id"] in {m["shot_id"] for m in middle}]
        self.assertEqual([{k: v for k, v in s.items() if k != "order"} for s in remaining],
                         [{k: v for k, v in s.items() if k != "order"} for s in middle])
        first_group = [i for i, s in enumerate(shots) if first["shot_id"] in s["source_shot_ids"]]
        last_group = [i for i, s in enumerate(shots) if last["shot_id"] in s["source_shot_ids"]]
        self.assertTrue(first_group)
        self.assertTrue(last_group)
        middle_positions = [i for i, s in enumerate(shots) if s["shot_id"] in {m["shot_id"] for m in middle}]
        self.assertLess(max(first_group), min(middle_positions))
        self.assertGreater(min(last_group), max(middle_positions))
        self.assertEqual(self.detail(board["storyboard_id"])["version"], board["version"])


if __name__ == "__main__":
    unittest.main()
