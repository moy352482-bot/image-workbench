import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy_release", ROOT / "scripts" / "deploy_release.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "image_workbench").mkdir()
        (self.root / "image_workbench" / "__init__.py").write_text("# release\n")
        for name in ("requirements.txt", "requirements-web.txt"):
            (self.root / name).write_text("# requirements\n")
        (self.root / "key").write_text("test key placeholder")
        self.config = {"host": "192.0.2.10", "user": "ubuntu", "port": 22, "private_key": str(self.root / "key")}

    def test_package_uses_allowlist_and_has_content_manifest(self):
        for relative in ("data/private.png", ".env", ".ssh/id_rsa", ".venv/token", "deploy/server.json",
                         "image_workbench/__pycache__/secret.pyc"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must not upload")
        archive, manifest = deploy.package(self.root, self.root, "20261013T000000Z-12345678")
        with tarfile.open(archive) as bundle:
            self.assertEqual(set(bundle.getnames()), {"image_workbench/__init__.py", "requirements.txt", "requirements-web.txt", "RELEASE.json"})
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(bundle.extractfile(name).read()).hexdigest(), digest)
        self.assertEqual(manifest["files"], deploy.fingerprint(self.root))

    def test_hidden_credentials_inside_program_directory_block_packaging(self):
        (self.root / "image_workbench" / ".env").write_text("secret")
        with self.assertRaises(ValueError):
            deploy.runtime_files(self.root)

    def test_script_runtime_packages_required_config_and_templates_without_unrelated_files(self):
        (self.root / "image_workbench/script_service.py").write_text("# script runtime")
        with self.assertRaises(ValueError):
            deploy.runtime_files(self.root)
        for relative in ("config/script_models.json", "config/script_pricing.json", "config/script_routing.json",
                         "skills/video-script-generation/registry/templates.json", "skills/video-script-generation/examples/screenplays/sample.txt"):
            path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("{}")
        (self.root / "config/private.json").write_text("must not upload")
        for name in ("SKILL.md", "references/script-craft.md", "references/screenplay-craft.md", "references/SOURCES.json",
                     "NOTICE.txt", "licenses/drama-skills.txt", "licenses/vimax.txt", "licenses/toonflow.txt"):
            path = self.root / "skills/video-script-generation" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("creative skill fixture", encoding="utf-8")
        archive, manifest = deploy.package(self.root, self.root, "20261016T000000Z-12345678")
        self.assertIn("config/script_pricing.json", manifest["files"])
        self.assertIn("skills/video-script-generation/registry/templates.json", manifest["files"])
        self.assertIn("skills/video-script-generation/references/screenplay-craft.md", manifest["files"])
        self.assertNotIn("config/private.json", manifest["files"])
        (self.root / "config/script_models.json").unlink()
        with self.assertRaises(ValueError):
            deploy.runtime_files(self.root)

    def test_storyboard_runtime_requires_and_packages_its_prompt(self):
        (self.root / "image_workbench/storyboard_planner.py").write_text("# planner")
        with self.assertRaises(ValueError):
            deploy.runtime_files(self.root)
        skill = self.root / "skills/storyboard-planning/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Text-only storyboard instructions", encoding="utf-8")
        (skill.parent / "private.json").write_text("must not upload")
        with self.assertRaises(ValueError):
            deploy.runtime_files(self.root)
        pricing = self.root / "config/storyboard_pricing.json"
        pricing.parent.mkdir()
        pricing.write_text('{"models": {}}')
        with self.assertRaises(ValueError):
            deploy.runtime_files(self.root)
        for name in ("references/shot-design.md", "references/continuity.md", "references/SOURCES.json",
                     "NOTICE.txt", "licenses/drama-skills.txt", "licenses/vimax.txt", "licenses/toonflow.txt", "licenses/minimax-h3.txt"):
            path = skill.parent / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("creative skill fixture", encoding="utf-8")
        archive, manifest = deploy.package(self.root, self.root, "20261020T000000Z-12345678")
        with tarfile.open(archive) as bundle:
            self.assertEqual(bundle.extractfile("skills/storyboard-planning/SKILL.md").read(),
                             skill.read_bytes())
        self.assertIn("skills/storyboard-planning/SKILL.md", manifest["files"])
        self.assertIn("skills/storyboard-planning/references/continuity.md", manifest["files"])
        self.assertIn("skills/storyboard-planning/licenses/minimax-h3.txt", manifest["files"])
        self.assertIn("config/storyboard_pricing.json", manifest["files"])
        self.assertNotIn("skills/storyboard-planning/private.json", manifest["files"])

    def test_host_user_port_and_key_validation(self):
        path = self.root / "server.json"
        path.write_text(json.dumps(self.config))
        self.assertEqual(deploy.read_config(path)["host"], "192.0.2.10")
        for changes in ({"host": "host; touch /tmp/unsafe"}, {"host": "-oProxyCommand=bad"},
                        {"host": "YOUR_SERVER_IP"}, {"user": "root;bad"}, {"port": True}, {"port": 70000}):
            path.write_text(json.dumps({**self.config, **changes}))
            with self.assertRaises(ValueError):
                deploy.read_config(path)
        args = deploy.ssh_arguments(self.config)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)

    def test_h3_runtime_requires_and_packages_skill_and_luna_fast_config(self):
        (self.root / "image_workbench/h3_skill.py").write_text("# H3 skill")
        files = ["skills/h3-video-workflow/" + name for name in (
            "SKILL.md", "references/base-en.txt", "references/ref-en.txt", "references/SOURCES.json", "LICENSE", "NOTICE")]
        files.append("config/h3_workflow.json")
        for relative in files:
            with self.assertRaises(ValueError):
                deploy.runtime_files(self.root)
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        archive, manifest = deploy.package(self.root, self.root, "20261022T000000Z-12345678")
        with tarfile.open(archive) as bundle:
            config = json.load(bundle.extractfile("config/h3_workflow.json"))
            self.assertEqual(config["model_id"], "gpt-6-luna")
            self.assertEqual(config["service_tier"], "fast")
            for relative in files:
                self.assertEqual(bundle.extractfile(relative).read(), (ROOT / relative).read_bytes())
            sources = json.load(bundle.extractfile("skills/h3-video-workflow/references/SOURCES.json"))
            for name, source in sources["files"].items():
                self.assertEqual(hashlib.sha256(bundle.extractfile("skills/h3-video-workflow/" + name).read()).hexdigest(),
                                 source["sha256"])
        self.assertIn("skills/h3-video-workflow/SKILL.md", manifest["files"])

    def test_failed_checks_prevent_deployment(self):
        path = self.root / "server.json"
        path.write_text(json.dumps(self.config))
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy, "validate_local", side_effect=RuntimeError("failed checks")), mock.patch.object(deploy, "deploy") as remote, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(deploy.main(["--config", str(path)]), 1)
            remote.assert_not_called()

    def test_code_changed_during_checks_prevents_deployment(self):
        path = self.root / "server.json"
        path.write_text(json.dumps(self.config))
        def change(*args):
            (self.root / "image_workbench" / "__init__.py").write_text("# changed during validation")
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy, "validate_local", side_effect=change), mock.patch.object(deploy, "deploy") as remote, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(deploy.main(["--config", str(path)]), 1)
            remote.assert_not_called()

    def test_package_only_never_connects_to_server(self):
        with mock.patch.object(deploy, "ROOT", self.root), mock.patch.object(deploy, "validate_local"), mock.patch.object(deploy, "deploy") as remote, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(deploy.main(["--package-only"]), 0)
            remote.assert_not_called()

    def test_changed_prepared_archive_is_rejected_before_network_access(self):
        archive = self.root / "release.tar.gz"
        archive.write_bytes(b"original approved archive")
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.write_bytes(b"changed after confirmation")
        with mock.patch.object(deploy.subprocess, "run") as network:
            with self.assertRaises(RuntimeError):
                deploy.deploy(self.config, self.root, archive, "20261013T000000Z-12345678", script_bytes=b"#!/bin/bash\n", expected_archive_sha=expected)
        network.assert_not_called()

    def test_uploaded_script_keeps_approved_digest_if_file_changes(self):
        archive = self.root / "release.tar.gz"
        archive.write_bytes(b"approved archive")
        approved = b"#!/bin/bash\necho approved\n"
        replacement = b"#!/bin/bash\necho replaced\n"
        original_write = Path.write_bytes
        def change_after_write(path, content):
            result = original_write(path, content)
            if path.name == "apply_release.sh":
                original_write(path, replacement)
            return result
        with mock.patch.object(Path, "write_bytes", change_after_write), mock.patch.object(deploy.subprocess, "run") as network, contextlib.redirect_stdout(io.StringIO()):
            deploy.deploy(self.config, self.root, archive, "20261013T000000Z-12345678", script_bytes=approved)
        command = network.call_args_list[-1].args[0][-1]
        self.assertIn(hashlib.sha256(approved).hexdigest(), command)
        self.assertNotIn(hashlib.sha256(replacement).hexdigest(), command)


@unittest.skipUnless(os.name == "posix", "Linux Bash 发布模拟在 WSL 单独执行")
class LinuxReleaseTests(unittest.TestCase):
    def simulate(self, fail_health):
        with tempfile.TemporaryDirectory(prefix="iw-deploy-test-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "image_workbench").mkdir()
            (source / "image_workbench" / "__init__.py").write_text("# fixture")
            for name in ("requirements.txt", "requirements-web.txt"):
                (source / name).write_text("")
            rid = "20261013T000000Z-12345678"
            archive, _ = deploy.package(source, root, rid)
            server = root / "app"
            previous = server / "releases" / "old"
            previous.mkdir(parents=True)
            (server / "current").symlink_to(previous)
            envfile = root / "server.env"
            envfile.write_text("fixture")
            (root / "business-data").write_text("keep original images and database")
            commands = root / "bin"
            commands.mkdir()
            fake_python = '''#!/bin/bash
if [[ $1 == -m || $1 == -c ]]; then exit 0; fi
if [[ $FAIL_HEALTH == 1 && $0 == *20261013T000000Z-12345678* ]]; then exit 1; fi
exit 0
'''
            (previous / ".venv" / "bin").mkdir(parents=True)
            (previous / ".venv" / "bin" / "python").write_text(fake_python)
            (previous / ".venv" / "bin" / "python").chmod(0o755)
            (root / "fake_python").write_text(fake_python)
            scripts = {
                "python3": '''#!/bin/bash
if [[ $1 == -m && $2 == venv ]]; then
 mkdir -p "$3/bin"
 cp "$TEST_ROOT/fake_python" "$3/bin/python"
 chmod 755 "$3/bin/python"
 exit 0
fi
exec /usr/bin/python3 "$@"
''',
                "systemctl": '''#!/bin/bash
if [[ $1 == show ]]; then
 case $4 in
 WorkingDirectory) echo "$TEST_ROOT/app/current" ;;
 ExecStart) echo "$TEST_ROOT/app/current/.venv/bin/python" ;;
 MainPID) echo 99999 ;;
 esac
fi
if [[ $1 == restart ]]; then echo restart >> "$TEST_ROOT/restarts"; fi
exit 0
''',
                "readlink": '''#!/bin/bash
if [[ $2 == /proc/99999/cwd ]]; then exec /usr/bin/readlink -f "$TEST_ROOT/app/current"; fi
exec /usr/bin/readlink "$@"
''',
                "sleep": "#!/bin/bash\nexit 0\n",
            }
            for name, body in scripts.items():
                path = commands / name
                path.write_text(body)
                path.chmod(0o755)
            script = (ROOT / "scripts" / "apply_release.sh").read_text()
            script = script.replace("root=/opt/image-workbench", "root=" + str(server))
            script = script.replace("/etc/image-workbench.env", str(envfile))
            # 模拟服务而非真实操作系统权限；真实脚本的 root 门槛保持不变。
            script = script.replace('[[ $EUID -eq 0 ]]', '[[ 1 -eq 1 ]]')
            target = root / "apply.sh"
            target.write_text(script)
            result = subprocess.run(["bash", str(target), str(archive), hashlib.sha256(archive.read_bytes()).hexdigest(), rid],
                env={**os.environ, "PATH": str(commands) + ":" + os.environ["PATH"], "TEST_ROOT": str(root),
                     "FAIL_HEALTH": "1" if fail_health else "0"}, capture_output=True, text=True, timeout=20)
            self.assertEqual((root / "business-data").read_text(), "keep original images and database")
            self.assertTrue(previous.is_dir())
            expected = previous if fail_health else server / "releases" / rid
            self.assertEqual((server / "current").resolve(), expected, result.stdout + result.stderr)
            self.assertEqual(result.returncode != 0, fail_health, result.stdout + result.stderr)
            self.assertEqual(len((root / "restarts").read_text().splitlines()), 2 if fail_health else 1)

    def test_success_switches_release_without_changing_data(self):
        self.simulate(False)

    def test_failed_health_rolls_back_and_reports_failure(self):
        self.simulate(True)


if __name__ == "__main__":
    unittest.main()
