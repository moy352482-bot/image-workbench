"""一键检查、打包、SSH 上传和版本切换；不上传业务数据或登录凭据。"""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
REMOTE_ROOT = "/opt/image-workbench"


def runtime_files(root):
    paths = []
    for path in sorted((root / "image_workbench").rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ValueError(f"发布内容不允许符号链接：{path}")
        if path.is_file():
            if path.name.startswith(".") or path.suffix.lower() in {".pem", ".key", ".env"}:
                raise ValueError(f"程序目录中存在不应发布的隐藏文件或密钥：{path.name}")
            paths.append(path)
    if not paths:
        raise ValueError("没有找到 image_workbench 程序文件")
    if (root / "image_workbench" / "script_service.py").is_file():
        paths.extend(root / "config" / name for name in ("script_models.json", "script_pricing.json", "script_routing.json"))
        skill = root / "skills" / "video-script-generation"
        required = ("registry/templates.json", "SKILL.md", "references/script-craft.md",
                    "references/screenplay-craft.md", "references/SOURCES.json", "NOTICE.txt",
                    "licenses/drama-skills.txt", "licenses/vimax.txt", "licenses/toonflow.txt")
        if any(not (skill / name).is_file() for name in required):
            raise ValueError("脚本生成模板或 Skill 资源缺失")
        for path in sorted(skill.rglob("*")):
            if path.is_symlink():
                raise ValueError("脚本模板目录不允许符号链接")
            if path.is_file():
                if path.name.startswith(".") or path.suffix not in {".json", ".txt", ".md"}:
                    raise ValueError("脚本模板目录含非发布文件")
                paths.append(path)
    if (root / "image_workbench" / "storyboard_planner.py").is_file():
        paths.extend(root / "skills" / "storyboard-planning" / name for name in (
            "SKILL.md", "references/shot-design.md", "references/continuity.md", "references/SOURCES.json",
            "NOTICE.txt", "licenses/drama-skills.txt", "licenses/vimax.txt", "licenses/toonflow.txt", "licenses/minimax-h3.txt"))
        paths.append(root / "config" / "storyboard_pricing.json")
    if (root / "image_workbench" / "h3_skill.py").is_file():
        paths.extend(root / "skills" / "h3-video-workflow" / name for name in (
            "SKILL.md", "references/base-en.txt", "references/ref-en.txt", "references/SOURCES.json", "LICENSE", "NOTICE"))
        paths.append(root / "config" / "h3_workflow.json")
    paths.extend(root / name for name in ("requirements.txt", "requirements-web.txt"))
    for path in paths:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("发布文件缺失或路径越界")
    return paths


def fingerprint(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in runtime_files(root)}


def package(root, directory, release_id):
    manifest = {"release_id": release_id, "files": {}}
    archive = directory / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for path in runtime_files(root):
            relative, data = path.relative_to(root).as_posix(), path.read_bytes()
            manifest["files"][relative] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(relative)
            info.size, info.mode, info.mtime = len(data), 0o644, int(path.stat().st_mtime)
            bundle.addfile(info, io.BytesIO(data))
        data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        info = tarfile.TarInfo("RELEASE.json")
        info.size, info.mode = len(data), 0o644
        bundle.addfile(info, io.BytesIO(data))
    return archive, manifest


def read_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if set(config) != {"host", "user", "port", "private_key"}:
        raise ValueError("server.json 必须包含 host、user、port、private_key 四项")
    host, user = config["host"], config["user"]
    if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host)
            or host.startswith("YOUR_") or ".." in host):
        raise ValueError("请填写真实服务器 IP 或主机名，不要包含协议、端口或命令")
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("SSH 用户名格式不正确")
    if type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("SSH 端口必须在1到65535之间")
    key = Path(config["private_key"]).expanduser().resolve()
    if not key.is_file() or key.suffix == ".pub":
        raise ValueError("private_key 必须指向已有私钥文件，而不是 .pub 公钥")
    config["private_key"] = str(key)
    return config


def ssh_arguments(config, *, scp=False):
    return ["scp" if scp else "ssh", "-i", config["private_key"], "-P" if scp else "-p", str(config["port"]),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=15"]


def validate_local(root, log_path):
    print("运行本地回归检查……", flush=True)
    with log_path.open("wb") as stream:
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"],
                                cwd=root, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"本地检查未通过，未上传任何文件。检查结果：{log_path}")
    node = shutil.which("node")
    if node:
        for path in sorted((root / "image_workbench" / "web_static").glob("*.js")):
            subprocess.run([node, "--check", str(path)], check=True, cwd=root)


def deploy(config, directory, archive, release_id, *, script_bytes=None, expected_archive_sha=None):
    destination = config["user"] + "@" + config["host"]
    remote = "/tmp/image-workbench-deploy-" + uuid4().hex
    # 固定脚本快照与上传文件的校验值，避免校验后文件被本地编辑替换。
    script = directory / "apply_release.sh"
    script_data = script_bytes if script_bytes is not None else (ROOT / "scripts" / "apply_release.sh").read_bytes().replace(b"\r\n", b"\n")
    script.write_bytes(script_data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if expected_archive_sha is not None:
        if digest != expected_archive_sha:
            raise RuntimeError("发布包与已确认摘要不符，未开始上传。")
        # 即使上传期间文件再次变化，远端也必须验证原先批准的摘要。
        digest = expected_archive_sha
    script_digest = hashlib.sha256(script_data).hexdigest()
    print(f"上传版本 {release_id} 到 {destination}……", flush=True)
    subprocess.run([*ssh_arguments(config), destination, "umask 077 && mkdir " + shlex.quote(remote)], check=True, timeout=30)
    subprocess.run([*ssh_arguments(config, scp=True), archive.name, script.name, destination + ":" + remote + "/"],
                   cwd=directory, check=True, timeout=600)
    checked_script = shlex.quote(remote + "/apply_release.sh")
    verify = f"printf '%s  %s\\n' {shlex.quote(script_digest)} {checked_script} | sha256sum -c -"
    invocation = ["/bin/bash", remote + "/apply_release.sh", remote + "/release.tar.gz", digest, release_id]
    if config["user"] != "root":
        invocation = ["sudo", "-n", *invocation]
    command = verify + " && " + shlex.join(invocation)
    try:
        subprocess.run([*ssh_arguments(config), destination, command], check=True, timeout=1800)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError("远程执行未返回成功，请核实服务器当前版本和服务状态。") from None
    print("服务器版本更新成功。刷新网页即可查看；本机执行器代码如有更新，空闲时重新启动执行器。", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "deploy" / "server.json")
    parser.add_argument("--package-only", action="store_true", help="只检查和打包，不连接服务器")
    args = parser.parse_args(argv)
    try:
        if not args.package_only and not args.config.is_file():
            raise ValueError("尚未配置服务器：先把 deploy/server.example.json 复制为 deploy/server.json，再填写服务器信息。")
        config = None if args.package_only else read_config(args.config)
        release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
        directory = ROOT / ".deploy" / release_id
        directory.mkdir(parents=True)
        archive, manifest = package(ROOT, directory, release_id)
        validate_local(ROOT, directory / "checks.txt")
        if fingerprint(ROOT) != manifest["files"]:
            raise RuntimeError("检查过程中代码发生变化，本次发布已停止；保存完成后重新运行。")
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"检查通过，发布包：{archive}", flush=True)
        if config:
            deploy(config, directory, archive, release_id)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"发布未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
