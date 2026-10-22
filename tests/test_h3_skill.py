"""Offline H3 Skill integration; Codex processes and ComfyUI are always mocked."""

import copy
import hashlib
import io
import json
import os
import unittest
from unittest import mock

from PIL import Image

from image_workbench.cli import main
from image_workbench.domain_errors import TextGenerationError
from image_workbench.h3_skill import H3SkillPlanner, load_h3_skill_config
from image_workbench.script_adapter import CodexTextAdapter
from tests import test_h3_worker as fixtures
from tests.test_script_adapter import FakeProcess


def official_prompt(inputs):
    body = ("[Shot 1] A person faces the camera, slowly raises one hand and gives a gentle wave. "
            "The camera remains still. The person (S1) says: <d>[Chinese] 你好，世界！</d>")
    sound = "\n\noverall_soundscape: Quiet room ambience and a faint rustle of clothing.\n\nnon_diegetic_music: N/A"
    if inputs["mode"] == "R2V":
        return ("subject_definitions:\n<Subject 1> is the person referenced by <Picture 1>.\n\n"
                "summary: [reference generation] <Subject 1> waves at the camera.\n\n"
                "retention_analysis: <Subject 1> (appears in [Shot 1]): fully_preserved - retain the person's identity.\n\n"
                "detailed_description: The scene follows the user's requested style. " + body + sound)
    prefix = ""
    if inputs["mode"] == "I2V":
        if len(inputs["reference_ids"]) == 1:
            prefix = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        else:
            prefix = ("How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the "
                      "0.00-second mark of the target video; Picture 2 (from Shot 1) aligns with the "
                      f"{inputs['duration']:.2f}-second mark of the target video.\n\n")
    return prefix + "integrated_multimodal_description: " + body + sound


class H3SkillTests(unittest.TestCase):
    setUp = fixtures.H3WorkerTests.setUp
    worker = fixtures.H3WorkerTests.worker
    journal = fixtures.H3WorkerTests.journal

    def submit(self, mode="T2V", references=0, resolution="720p"):
        # Distinct images are needed: production correctly deduplicates equal bytes.
        images = []
        for index in range(references):
            stream = io.BytesIO()
            Image.new("RGB", (24, 16), (index, 20, 40)).save(stream, format="PNG")
            images.append(stream.getvalue())
        with mock.patch.object(fixtures, "png", side_effect=images):
            return fixtures.H3WorkerTests.submit(self, mode, references, resolution)

    def planner(self):
        config = load_h3_skill_config()
        executable = self.root / ("codex.exe" if os.name == "nt" else "codex")
        executable.write_bytes(b"never executed")
        adapter = CodexTextAdapter(executable, timeout_seconds=config["timeout_seconds"], service_tier=config["service_tier"])
        return H3SkillPlanner(adapter, config=config)

    def process(self, changes=None, *, returncode=0):
        class H3Process(FakeProcess):
            def communicate(self, input=None, timeout=None):
                inputs = json.loads(input.decode().rsplit("\n", 1)[-1])
                self.body = json.dumps({**inputs, "schema_version": 1,
                    "prompt": official_prompt(inputs), **(changes or {})}, ensure_ascii=False)
                return super().communicate(input, timeout)
        return mock.patch("image_workbench.script_adapter.subprocess.Popen",
            side_effect=lambda argv, **kwargs: H3Process(argv, kwargs, returncode=returncode))

    def test_luna_fast_skill_drives_all_three_workflows_without_changing_task_or_billing(self):
        for mode, count in (("T2V", 0), ("I2V", 1), ("I2V", 2), ("R2V", 3)):
            with self.subTest(mode=mode):
                shot, job = self.submit(mode=mode, references=count)
                before = self.identity.wallet(self.aid)
                planner = self.planner()
                with self.process() as process:
                    self.assertEqual(self.worker(planner=planner).run_once(), "completed")
                self.assertEqual(process.call_count, 1)
                journal = self.journal(job)
                request = journal["skill_request"]
                self.assertIn(planner.skill, request["prompt"])
                self.assertEqual(journal["manifest"]["task"]["prompt"], "人物向镜头挥手")
                graph = journal["workflow"]
                self.assertEqual(graph["5"]["inputs"]["prompt"], journal["skill_plan"]["prompt"])
                self.assertIn("<d>[Chinese] 你好，世界！</d>", graph["5"]["inputs"]["prompt"])
                self.assertEqual(journal["skill_plan"]["reference_ids"], shot["reference_ids"])
                execution = self.cache / job["job_id"] / "skill"
                argv = json.loads((execution / "command.json").read_text(encoding="utf-8"))["argv"]
                self.assertEqual(argv[argv.index("--model") + 1], "gpt-6-luna")
                self.assertIn('service_tier="fast"', argv)
                self.assertIn('model_reasoning_effort="low"', argv)
                receipt = json.loads((execution / "completed.json").read_text())
                self.assertEqual(receipt["requested_service_tier"], "fast")
                after = self.identity.wallet(self.aid)
                self.assertEqual(after["frozen_credit"], 0)
                self.assertEqual(before["frozen_credit"], 8)
                self.assertEqual(self.store.shot(self.aid, self.wid, self.pid, shot["shot_id"])["prompt"], shot["prompt"])
        self.assertEqual(self.comfy.posts, 4)

    def test_correct_official_guides_and_mode_are_in_actual_request(self):
        planner = self.planner()
        for mode, count, prompt_mode in (("T2V", 0, "T2VA"), ("I2V", 1, "I2VA"),
                                         ("I2V", 2, "FL2VA"), ("R2V", 3, "Ref2VA")):
            with self.subTest(mode=prompt_mode):
                shot, _ = self.submit(mode=mode, references=count)
                request = planner.request(shot)
                self.assertEqual(request["prompt_mode"], prompt_mode)
                self.assertEqual(request["inputs"]["mode"], mode)
                self.assertIn(planner.guides["base-en.txt"], request["prompt"])
                if mode == "R2V":
                    self.assertIn(planner.guides["ref-en.txt"], request["prompt"])
                    self.assertEqual(len(request["guide_sha256"]), 2)
                else:
                    self.assertNotIn(planner.guides["ref-en.txt"], request["prompt"])
                    self.assertEqual(len(request["guide_sha256"]), 1)
                for name, digest in request["guide_sha256"].items():
                    self.assertEqual(digest, hashlib.sha256(planner.guides[name].encode()).hexdigest())

    def test_wrong_missing_empty_or_reordered_official_sections_block_video(self):
        base = official_prompt({"mode": "T2V"})
        ref = official_prompt({"mode": "R2V"})
        invalid = ("Plain text without official sections", ref,
                   base.replace("overall_soundscape:", "other_field:"),
                   base.replace("integrated_multimodal_description:", "non_diegetic_music:"),
                   base.replace("non_diegetic_music: N/A", "non_diegetic_music:"))
        for body in invalid:
            with self.subTest(body=body[:50]):
                self.submit()
                with self.process({"prompt": body}):
                    self.assertEqual(self.worker(planner=self.planner()).run_once(), "failed")
                self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.submit(mode="R2V", references=1)
        with self.process({"prompt": base}):
            self.assertEqual(self.worker(planner=self.planner()).run_once(), "failed")
        self.assertEqual(self.comfy.posts, 0)

    def test_invalid_plan_never_submits_video_and_releases_reservation(self):
        cases = ({"duration": 15}, {"mode": "R2V"}, {"resolution": "1080p"}, {"width": 1920},
                 {"aspect_ratio": "portrait"}, {"reference_ids": ["other"]},
                 {"nodes": {}}, {"prompt": " "}, {"schema_version": True})
        for changes in cases:
            with self.subTest(changes=changes):
                _, job = self.submit()
                with self.process(changes):
                    self.assertEqual(self.worker(planner=self.planner()).run_once(), "failed")
                self.assertEqual(self.journal(job)["phase"], "done")
                self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)
        self.assertEqual(self.comfy.posts, 0)

    def test_reference_order_is_immutable(self):
        shot, job = self.submit(mode="I2V", references=2)
        with self.process({"reference_ids": list(reversed(shot["reference_ids"]))}):
            self.assertEqual(self.worker(planner=self.planner()).run_once(), "failed")
        self.assertEqual(self.comfy.posts, 0)
        self.assertEqual(self.journal(job)["phase"], "done")

    def test_completed_text_recovers_frozen_skill_without_second_call(self):
        _, job = self.submit()
        planner = self.planner()
        run = planner.run

        def crash_after_text(*args):
            run(*args)
            raise KeyboardInterrupt()

        with self.process() as process, mock.patch.object(planner, "run", side_effect=crash_after_text):
            with self.assertRaises(KeyboardInterrupt):
                self.worker(planner=planner).run_once()
            self.assertEqual(process.call_count, 1)
        self.assertNotIn("skill_plan", self.journal(job))
        frozen = self.journal(job)["skill_request"]
        planner.skill = "A newly installed skill must not change an existing invocation."
        planner.guides["base-en.txt"] = "Changed upstream guide must not alter frozen requests."
        with self.process() as process:
            self.assertEqual(self.worker(planner=planner).run_once(), "completed")
            process.assert_not_called()
        self.assertEqual(self.journal(job)["skill_request"], frozen)
        self.assertEqual(self.comfy.posts, 1)

    def test_missing_receipt_holds_claim_without_reinvocation_or_video(self):
        _, job = self.submit()
        planner = self.planner()
        with mock.patch("image_workbench.script_adapter.subprocess.Popen", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.worker(planner=planner).run_once()
        with self.process() as process:
            self.assertEqual(self.worker(planner=planner).run_once(), "uncertain")
            self.assertEqual(self.worker(planner=planner).run_once(), "uncertain")
            process.assert_not_called()
        self.assertEqual(self.comfy.posts, 0)
        self.assertEqual(self.journal(job)["phase"], "ready")
        self.assertIn("完成凭据", self.journal(job)["skill_error"])
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 8)

    def test_legacy_skill_receipt_recovers_without_new_format_requirements(self):
        shot, _ = self.submit()
        planner = self.planner()
        inputs = {key: shot[key] for key in ("mode", "duration", "resolution", "aspect_ratio",
                                            "width", "height", "reference_ids", "prompt")}
        request = {"config": planner.config, "skill_sha256": "legacy-skill-digest", "inputs": inputs,
                   "prompt": "Legacy H3 instructions\n" + json.dumps(inputs, ensure_ascii=False)}
        execution = self.root / "legacy-skill"
        with self.process({"prompt": "旧版已完成的普通提示词"}) as process:
            original = planner.run(request, execution)
            self.assertEqual(process.call_count, 1)
        with self.process() as process:
            self.assertEqual(self.planner().run(request, execution), original)
            process.assert_not_called()

    def test_receipt_tier_or_response_tampering_prevents_rendering(self):
        _, job = self.submit()
        planner = self.planner()
        run = planner.run

        def crash(*args):
            run(*args)
            raise KeyboardInterrupt()

        with self.process(), mock.patch.object(planner, "run", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.worker(planner=planner).run_once()
        receipt_path = self.cache / job["job_id"] / "skill/completed.json"
        original = json.loads(receipt_path.read_text())
        for change in ({"requested_service_tier": "default"}, {"response_sha256": "wrong"}):
            receipt_path.write_text(json.dumps({**original, **change}))
            with self.process() as process:
                self.assertEqual(self.worker(planner=planner).run_once(), "uncertain")
                process.assert_not_called()
            self.assertEqual(self.comfy.posts, 0)
        receipt_path.write_text(json.dumps(original))
        with self.process() as process:
            self.assertEqual(self.worker(planner=planner).run_once(), "completed")
            process.assert_not_called()

    def test_known_provider_failure_refunds_without_fallback(self):
        self.submit()
        with self.process(returncode=1) as process:
            self.assertEqual(self.worker(planner=self.planner()).run_once(), "failed")
            self.assertEqual(process.call_count, 1)
        self.assertEqual(self.comfy.posts, 0)
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_comfy_restart_uses_frozen_graph_without_luna(self):
        _, job = self.submit()
        self.comfy.fail_submit = "crash"
        with self.process() as process:
            with self.assertRaises(KeyboardInterrupt):
                self.worker(planner=self.planner()).run_once()
            self.assertEqual(process.call_count, 1)
        self.assertEqual(self.journal(job)["phase"], "submitting")
        with self.process() as process:
            self.assertEqual(self.worker(planner=self.planner()).run_once(), "completed")
            process.assert_not_called()
        self.assertEqual(self.comfy.posts, 1)

    def test_legacy_ready_journal_is_not_replanned(self):
        _, job = self.submit()
        with mock.patch.object(self.adapter, "upload_reference", side_effect=KeyboardInterrupt), \
                mock.patch.object(self.adapter, "bind", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.worker().run_once()
        self.assertNotIn("skill_request", self.journal(job))
        with self.process() as process:
            self.assertEqual(self.worker(planner=self.planner()).run_once(), "completed")
            process.assert_not_called()

    def test_legacy_unstarted_task_is_refunded_before_skill_invocation(self):
        _, job = self.submit()
        claim = self.transport.claim

        def legacy_claim(worker_id):
            manifest = claim(worker_id)
            manifest["task"].pop("resolution")
            return manifest

        with mock.patch.object(self.transport, "claim", side_effect=legacy_claim), self.process() as process:
            self.assertEqual(self.worker(planner=self.planner()).run_once(), "failed")
            process.assert_not_called()
        self.assertEqual(self.comfy.posts, 0)
        self.assertNotIn("skill_request", self.journal(job))
        self.assertEqual(self.identity.wallet(self.aid)["frozen_credit"], 0)

    def test_config_change_does_not_relaunch_pending_invocation(self):
        planner = self.planner()
        shot, _ = self.submit()
        request = planner.request(shot)
        planner.config = {**planner.config, "thinking_level": "high"}
        with self.process() as process, self.assertRaises(TextGenerationError) as caught:
            planner.run(request, self.root / "execution")
        self.assertTrue(caught.exception.uncertain)
        process.assert_not_called()

    def test_cli_installs_skill_by_default_and_check_does_not_invoke_model(self):
        path = self.root / "worker.json"
        path.write_text(json.dumps({"comfy_url": self.adapter.comfy_url,
            "workflow_dir": str(self.workflow_dir), "cache_root": str(self.cache)}))
        args = ["--data-root", str(self.root / "data"), "h3-worker", "--config", str(path)]
        with mock.patch("image_workbench.h3_worker.ComfyH3Adapter", return_value=self.adapter), \
                mock.patch("image_workbench.h3_worker.H3Worker") as worker, self.process() as process:
            self.assertEqual(main([*args, "--once"]), 0)
            planner = worker.call_args.kwargs["planner"]
            self.assertEqual(planner.config["model_id"], "gpt-6-luna")
            self.assertEqual(planner.adapter.service_tier, "fast")
            worker.return_value.serve.assert_called_once()
            process.assert_not_called()
        with mock.patch("image_workbench.h3_worker.ComfyH3Adapter", return_value=self.adapter), \
                mock.patch.object(self.adapter, "check", return_value={"available": True}), \
                mock.patch("image_workbench.cli._print") as output, self.process() as process:
            self.assertEqual(main([*args, "--check"]), 0)
            self.assertFalse(output.call_args.args[0]["workflow_skill"]["model_invocation_tested"])
            process.assert_not_called()

    def test_config_rejects_model_or_tier_substitution(self):
        folder = self.root / "config"
        folder.mkdir()
        baseline = load_h3_skill_config()
        for change in ({"model_id": "gpt-6-astra"}, {"service_tier": "default"}, {"thinking_level": "fast"}):
            (folder / "h3_workflow.json").write_text(json.dumps({**baseline, **change}))
            with self.assertRaises(ValueError):
                load_h3_skill_config(self.root)

    def test_invalid_json_is_rejected(self):
        planner = self.planner()
        shot, _ = self.submit()
        request = planner.request(shot)
        for body in ("not JSON", "[]", "```json\n{}\n```", json.dumps({**copy.deepcopy(shot), "schema_version": 1})):
            with self.assertRaises(ValueError):
                planner.validate(body, request)
