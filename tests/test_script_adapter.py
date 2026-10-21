"""文本 Codex Adapter 合同测试；所有进程启动均替换为本地假进程。"""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from image_workbench.script_adapter import CodexTextAdapter, ScriptGenerationError


class FakeProcess:
    def __init__(self, argv, kwargs, *, body="第一场　清晨\n小林推开门。", returncode=0, timeout=False, unkillable=False):
        self.args = self.argv = argv
        self.kwargs = kwargs
        self.pid = 2147483647  # 虚构 PID；绝不指向测试机上的真实进程。
        self.returncode = None
        self.body, self.final_returncode = body, returncode
        self.timeout, self.unkillable = timeout, unkillable
        self.stdin = io.BytesIO()
        self.stdin_bytes = None
        self.kill_calls = self.wait_calls = 0

    def communicate(self, input=None, timeout=None):
        self.stdin_bytes, self.timeout_seconds = input, timeout
        if self.timeout:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        for key, data in (("stdout", b'{"type":"debug","internal":"never publish this log"}\n'),
                          ("stderr", b"diagnostic only\n")):
            stream = self.kwargs.get(key)
            if hasattr(stream, "write"):
                stream.write(data)
                stream.flush()
        if self.body is not None:
            path = Path(self.argv[self.argv.index("--output-last-message") + 1])
            path.write_bytes(self.body.encode("utf-8") if isinstance(self.body, str) else self.body)
        self.returncode = self.final_returncode
        return b"stdout is not the final answer", b"stderr is not the final answer"

    def poll(self):
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        if not self.unkillable:
            self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.unkillable:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode


class FakeStopProcess:
    """捕获 Windows taskkill 的 Popen，禁止测试启动任何外部命令。"""
    def __init__(self, argv):
        self.args = argv
        self.returncode = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def communicate(self, input=None, timeout=None):
        return None, None

    def poll(self):
        return self.returncode


class ScriptAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "task" / "execution"
        self.executable = self.root / ("codex.exe" if os.name == "nt" else "codex")
        self.executable.write_bytes(b"this file is never executed")
        self.adapter = CodexTextAdapter(self.executable, timeout_seconds=17.5)
        self.processes = []
        self.stop_commands = []

    def popen(self, **behavior):
        def create(argv, **kwargs):
            if Path(argv[0]).name.lower() == "taskkill.exe":
                self.stop_commands.append(argv)
                return FakeStopProcess(argv)
            self.assertEqual(Path(argv[0]), self.executable)
            process = FakeProcess(argv, kwargs, **behavior)
            self.processes.append(process)
            return process
        # subprocess 是共享模块，停止进程树时的 taskkill 也被此替身截获。
        return patch("image_workbench.script_adapter.subprocess.Popen", side_effect=create)

    def generate(self, **kwargs):
        options = {"prompt": "请生成纯文本剧本，包含中文对白。", "model_id": "gpt-6-astra",
                   "thinking_level": "xhigh", "work_dir": self.work}
        options.update(kwargs)
        return self.adapter.generate(**options)

    def test_exact_model_effort_readonly_tool_disabling_and_final_response(self):
        prompt = "场景要求：夜晚，中文对白。\n不输出内部调试记录。"
        final = "场景一　室内　夜\n阿宁（轻声）：现在可以走了。\n"
        with self.popen(body=final), patch.dict(os.environ, {"OPENAI_API_KEY": "test-api-key",
                                                           "CODEX_API_KEY": "test-codex-key",
                                                           "OPENAI_BASE_URL": "https://invalid.example"}):
            result = self.generate(prompt=prompt, model_id="gpt-6.1-sol", thinking_level="max")
        self.assertEqual(result, final)
        self.assertEqual(len(self.processes), 1)
        process = self.processes[0]
        argv, options = process.argv, process.kwargs
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.1-sol")
        configs = [argv[index + 1] for index, part in enumerate(argv[:-1]) if part == "-c"]
        self.assertIn('model_reasoning_effort="max"', configs)
        self.assertIn('forced_login_method="chatgpt"', configs)
        self.assertIn("mcp_servers={}", configs)
        self.assertIn('web_search="disabled"', configs)
        self.assertIn("project_doc_max_bytes=0", configs)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "never")
        for flag in ("--no-daemon", "--ignore-user-config", "--ephemeral", "--json"):
            self.assertIn(flag, argv)
        disabled = {argv[index + 1] for index, part in enumerate(argv[:-1]) if part == "--disable"}
        self.assertTrue({"shell_tool", "unified_exec", "hooks", "plugins", "apps", "multi_agent",
                         "computer_use", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
                         "image_generation"}.issubset(disabled))
        self.assertEqual(argv[-1], "-")
        self.assertNotIn(prompt, argv)
        self.assertEqual(process.stdin_bytes, prompt.encode("utf-8"))
        self.assertEqual(process.timeout_seconds, 17.5)
        self.assertFalse(options["shell"])
        self.assertEqual(options["stdin"], subprocess.PIPE)
        self.assertEqual(options["cwd"], self.work)
        for secret in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
            self.assertNotIn(secret, options["env"])
        self.assertIn("internal", (self.work / "stdout.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("internal", result)
        self.assertEqual(json.loads((self.work / "command.json").read_text(encoding="utf-8"))["argv"], argv)
        self.assertEqual(json.loads((self.work / "process.json").read_text(encoding="utf-8"))["pid"], process.pid)

    def test_each_model_and_effort_is_passed_without_replacement(self):
        for index, (model, effort) in enumerate((("gpt-6-astra", "high"), ("gpt-6.1-sol", "xhigh"), ("gpt-6-luna", "low"))):
            with self.subTest(model=model), self.popen():
                self.generate(model_id=model, thinking_level=effort, work_dir=self.root / str(index))
                argv = self.processes[-1].argv
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertIn("model_reasoning_effort=" + json.dumps(effort), argv)
        self.assertEqual(len(self.processes), 3)

    def test_missing_final_response_does_not_use_stdout(self):
        with self.popen(body=None), self.assertRaisesRegex(ScriptGenerationError, "有效的 TXT"):
            self.generate()
        self.assertEqual(len(self.processes), 1)
        self.assertTrue((self.work / "stdout.jsonl").read_bytes())

    def test_nonzero_exit_is_failed_and_never_retried(self):
        with self.popen(returncode=1), self.assertRaises(ScriptGenerationError) as caught:
            self.generate()
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.processes), 1)

    def test_invalid_utf8_or_oversized_final_response_is_rejected(self):
        for index, body in enumerate((b"\xff", b"x" * (256 * 1024 + 1))):
            with self.subTest(index=index), self.popen(body=body), self.assertRaises(ScriptGenerationError):
                self.generate(work_dir=self.root / str(index))
        self.assertEqual(len(self.processes), 2)

    def test_existing_response_blocks_reexecution(self):
        self.work.mkdir(parents=True)
        response = self.work / "response.txt"
        response.write_text("原始成品", encoding="utf-8")
        with self.popen() as popen, self.assertRaises(ScriptGenerationError) as caught:
            self.generate()
        popen.assert_not_called()
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(response.read_text(encoding="utf-8"), "原始成品")

    @unittest.skipUnless(os.name == "nt", "Windows 测试以 Popen 替身截获 taskkill，其他平台不发送真实进程组信号")
    def test_timeout_stops_owned_process_and_does_not_retry(self):
        with self.popen(timeout=True), self.assertRaisesRegex(ScriptGenerationError, "超时") as caught:
            self.generate()
        self.assertEqual(len(self.processes), 1)
        process = self.processes[0]
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_calls, 1)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(self.stop_commands), 1)
        self.assertIn(str(process.pid), self.stop_commands[0])
        self.assertTrue(process.stdin.closed)

    @unittest.skipUnless(os.name == "nt", "Windows 测试以 Popen 替身截获 taskkill，其他平台不发送真实进程组信号")
    def test_timeout_with_unconfirmed_stop_stays_uncertain(self):
        with self.popen(timeout=True, unkillable=True), self.assertRaises(ScriptGenerationError) as caught:
            self.generate()
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.processes[0].kill_calls, 1)
        self.assertTrue(self.processes[0].stdin.closed)


if __name__ == "__main__":
    unittest.main()
