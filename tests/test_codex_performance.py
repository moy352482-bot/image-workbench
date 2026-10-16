"""Codex timing is exercised with local Python children; no model calls or quota."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from PIL import Image

from image_workbench import BackendError, BackendRequest, CodexImageBackend, StorageConfig, StorageManager
from image_workbench.codex_observer import CodexStdoutObserver
from image_workbench.performance import PerformanceTrace


class CodexPerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = StorageManager(StorageConfig(data_root=self.root / "data"))
        project = self.storage.create_project("performance-local-test")
        self.task = self.storage.create_task(project.project_id, "generate", "private prompt stays off telemetry")
        self.work = self.root / "run" / "work"
        (self.work / "products").mkdir(parents=True)
        self.cache = self.root / "generated_images"
        self.cache.mkdir()
        self.source = self.cache / "sample.png"
        Image.new("RGB", (18, 12), (24, 48, 72)).save(self.source)
        self.stdout = self.root / "run" / "stdout.log"
        self.stderr = self.root / "run" / "stderr.log"
        self.stdout.touch()
        self.stderr.touch()
        self.trace = PerformanceTrace()
        self.request = BackendRequest(
            uuid4().hex, self.task, self.work, (), None,
            self.stdout, self.stderr, 5, self.storage.config.max_image_bytes,
            performance=self.trace,
        )
        self.backend = CodexImageBackend(executable=sys.executable, generated_root=self.cache)

    def test_local_child_observes_split_json_boundaries_and_preserves_output(self):
        payload = {
            "execution_id": self.request.execution_id, "status": "ok",
            "image_paths": [str(self.source)], "message": "done",
        }
        code = "\n".join([
            "import json, pathlib, sys, time",
            "sys.stdin.buffer.read()",
            "time.sleep(0.15)",
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'sample-thread'}), flush=True)",
            "time.sleep(0.1)",
            "sys.stdout.write('{\"type\":\"turn.st'); sys.stdout.flush()",
            "time.sleep(0.12)",
            "sys.stdout.write('arted\"}\\n'); sys.stdout.flush()",
            "time.sleep(0.15)",
            "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'id': 'item-1', 'text': 'private payload must not enter telemetry'}}), flush=True)",
            "time.sleep(0.12)",
            f"pathlib.Path('response.json').write_text({json.dumps(json.dumps(payload))}, encoding='utf-8')",
            "print(json.dumps({'type': 'turn.completed'}), flush=True)",
            "time.sleep(0.1)",
        ])
        command = [sys.executable, "-c", code]
        with mock.patch.object(self.backend, "_build_command", return_value=command):
            result = self.backend.generate(self.request)

        self.assertEqual(result.image_paths[0].read_bytes(), self.source.read_bytes())
        points = self.trace.points
        self.assertLess(points["codex_launch_start"]["offset_seconds"], points["subprocess_spawn_end"]["offset_seconds"])
        self.assertEqual(points["codex_launch_end"], points["codex_execution_start"])
        self.assertGreater(self.trace._duration("codex_launch_start", "codex_launch_end"), 0.25)
        self.assertGreater(self.trace._duration("codex_execution_start", "codex_execution_end"), 0.2)
        event_types = [row["event"] for row in self.trace.cli_events]
        self.assertEqual(event_types, ["thread.started", "turn.started", "item.completed", "turn.completed"])
        self.assertNotIn("private payload", json.dumps(self.trace.cli_events))
        raw_events = [json.loads(line) for line in self.stdout.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(raw_events[2]["item"]["text"], "private payload must not enter telemetry")
        self.assertEqual(json.loads((self.work / "command.json").read_text(encoding="utf-8"))["argv"], command)
        self.assertEqual([event["phase"] for event in result.timing_events], ["codex"])
        phases = {event["phase"] for event in self.trace.segments}
        self.assertTrue({"backend_prepare", "codex_env_prepare", "codex_prompt_prepare", "artifact_collect", "staging_copy"}.issubset(phases))

    def test_optional_trace_does_not_change_cli_argv_or_prompt(self):
        no_trace = replace(self.request, performance=None)
        self.assertEqual(self.backend._build_command(self.request), self.backend._build_command(no_trace))
        self.assertEqual(self.backend._prompt_bytes(self.request), self.backend._prompt(no_trace).encode("utf-8"))
        observer = CodexStdoutObserver(self.stdout, None)
        observer.start()
        observer.stop()
        self.assertIsNone(observer._thread)

    def test_timeout_keeps_observations_and_stops_observer_without_retry(self):
        request = replace(self.request, timeout_seconds=0.35)
        code = (
            "import json,sys,time;sys.stdin.buffer.read();"
            "print(json.dumps({'type':'turn.started'}),flush=True);time.sleep(30)"
        )
        command = [sys.executable, "-c", code]
        real_popen = subprocess.Popen
        children = []
        observers = []

        def popen(argv, **kwargs):
            if argv and argv[0] == sys.executable:
                children.append(argv)
            return real_popen(argv, **kwargs)

        def observer_factory(*args):
            observer = CodexStdoutObserver(*args)
            observers.append(observer)
            return observer

        with mock.patch.object(self.backend, "_build_command", return_value=command), \
             mock.patch("image_workbench.codex_backend.subprocess.Popen", side_effect=popen), \
             mock.patch("image_workbench.codex_backend.CodexStdoutObserver", side_effect=observer_factory):
            with self.assertRaises(BackendError) as raised:
                self.backend.generate(request)
        self.assertEqual(raised.exception.status, "timed_out")
        self.assertEqual(len(children), 1)
        self.assertIn("codex_execution_end", self.trace.points)
        self.assertIn("codex_execution_start", self.trace.points)
        self.assertFalse(observers[0]._thread.is_alive())

    def test_launch_failure_closes_observer_and_does_not_invent_execution_boundary(self):
        with mock.patch.object(self.backend, "_build_command", return_value=[str(self.root / "missing-executable")]):
            with self.assertRaises(BackendError) as raised:
                self.backend.generate(self.request)
        self.assertEqual(raised.exception.failure_type, "codex_launch_failed")
        self.assertIn("codex_launch_start", self.trace.points)
        self.assertIn("codex_execution_end", self.trace.points)
        self.assertNotIn("subprocess_spawn_end", self.trace.points)
        self.assertNotIn("codex_execution_start", self.trace.points)

    def test_post_exit_or_malformed_events_do_not_invent_live_start(self):
        observer = CodexStdoutObserver(self.stdout, self.trace)
        observer._event(b'not json', "live")
        observer._event(b'[]', "live")
        observer._event(b'{"type":"turn.started"}', "post_exit")
        self.assertEqual(len(self.trace.cli_events), 1)
        self.assertNotIn("codex_execution_start", self.trace.points)
        observer.max_line_bytes = 80
        self.stdout.write_bytes(b'{"type":"item.completed","text":"' + b'x' * 200 + b'"}\n{"type":"turn.completed"}\n')
        observer._read("post_exit")
        self.assertEqual([e["event"] for e in self.trace.cli_events], ["turn.started", "turn.completed"])


if __name__ == "__main__":
    unittest.main()
