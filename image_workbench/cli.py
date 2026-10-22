"""本地命令行入口。机器可读的 JSON 输出，真实生图必须显式选择。"""

import argparse
from dataclasses import asdict
import json
import os
import math
from pathlib import Path
import sys

from .codex_backend import CodexImageBackend, check_codex_environment
from .config import StorageConfig
from .image_backend import LocalFileBackend
from .storage import StorageManager
from .task_runner import TaskRunner
from .batch_runner import BatchRunner
from .project_manager import ProjectManager


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Image Workbench 本地任务执行器")
    parser.add_argument("--data-root", type=Path, help="数据目录，优先于环境变量")
    commands = parser.add_subparsers(dest="command", required=True)
    accounts = commands.add_parser("accounts-migrate", help="预览或应用账号与工作空间归属迁移")
    mode = accounts.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    accounts.add_argument("--expected-fingerprint")
    accounts.add_argument("--backup-directory", type=Path)
    invitation = commands.add_parser("accounts-invite", help="为受控注册签发一次性邀请码；仅供可信本机维护")
    invitation.add_argument("--expires-hours", type=int, default=72, help="有效期小时数，1–720，默认72")
    project = commands.add_parser("project-create", help="创建项目")
    project.add_argument("--name", required=True)
    task = commands.add_parser("task-create", help="创建生成/编辑任务并可导入参考图")
    task.add_argument("--project", required=True)
    task.add_argument("--operation", choices=("generate", "edit"), required=True)
    task.add_argument("--prompt", required=True)
    task.add_argument("--base", help="编辑任务基底图片 ID")
    task.add_argument("--input", type=Path, action="append", default=[], help="参考图片，可重复指定")
    for name, help_text in (
        ("run", "执行指定任务"), ("status", "查询最新或指定执行状态"),
        ("outputs", "查询任务已登记输出"), ("retry-archive", "仅重试原产物归档，不重新生图"),
        ("mark-interrupted", "确认进程已停止后将活动记录标记为中断"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--project", required=True)
        command.add_argument("--task", required=True)
        if name == "run":
            command.add_argument("--backend", choices=("local", "codex"), required=True)
            command.add_argument("--image", type=Path, action="append", default=[], help="local 后端的已有测试产物，可重复指定")
            command.add_argument("--timeout", type=float, default=300)
            command.add_argument("--model", help="可选 Codex 模型标识，例如 gpt-6-luna")
            command.add_argument("--allow-image-generation", action="store_true", help="主动启动一次真实 Codex 图片请求，会使用账号生成额度")
        elif name in ("status", "retry-archive", "mark-interrupted"):
            command.add_argument("--execution", required=name != "status")
    commands.add_parser("doctor", help="仅本地环境检查，不调用模型")
    migration = commands.add_parser("projects-migrate", help="项目层级迁移；默认只读预览，不调用生图")
    migration_mode = migration.add_mutually_exclusive_group()
    migration_mode.add_argument("--apply", action="store_true", help="应用已经审阅并批准的迁移")
    migration_mode.add_argument("--backup-only", action="store_true", help="仅准备元数据备份，不应用迁移")
    migration.add_argument("--expected-fingerprint", help="应用时必须传入 dry-run 的完整指纹")
    migration.add_argument("--backup-directory", type=Path, help="数据根目录之外的备份目录")
    batch = commands.add_parser("batch", help="读取 JSON 清单，顺序执行最多 20 项并输出报告")
    batch.add_argument("--manifest", type=Path, required=True)
    batch.add_argument("--reports-root", type=Path, default=Path("reports"))
    batch.add_argument("--backend", choices=("local", "codex"), required=True)
    batch.add_argument("--image", type=Path, action="append", default=[], help="local 后端测试产物")
    batch.add_argument("--timeout", type=float, default=600)
    batch.add_argument("--model", help="可选 Codex 模型标识；恢复时必须与原批次一致")
    batch.add_argument("--resume-directory", type=Path, help="恢复原报告目录；保留已完成项，不重新生成")
    batch.add_argument("--allow-image-generation", action="store_true", help="允许本清单真实生图，会消耗账号额度")
    for name in ("web", "worker"):
        command = commands.add_parser(name, help="本机网页工作台" if name == "web" else "独立顺序执行队列")
        command.add_argument("--backend", choices=("local", "codex"), default="codex")
        command.add_argument("--image", type=Path, action="append", default=[], help="演练后端使用的已有图片")
        command.add_argument("--timeout", type=float, default=600)
        if name == "web":
            command.add_argument("--port", type=int, default=7860)
        else:
            command.add_argument("--once", action="store_true", help="仅处理至多一个已提交作业后退出")
    cloud = commands.add_parser("cloud", help="云端收任务模式；只归档回传图片，不启动本机 Codex")
    cloud.add_argument("--port", type=int, default=7860)
    cloud.add_argument("--backend", choices=("codex", "local"), default="codex")
    remote = commands.add_parser("remote-worker", help="本机主动领取云端任务并回传图片")
    remote.add_argument("--server", required=True, help="HTTPS 源站地址；本机联调/SSH 隧道可用 HTTP")
    remote.add_argument("--cache-root", type=Path, required=True, help="独立持久缓存，不要使用原项目 data 目录")
    remote.add_argument("--backend", choices=("codex", "local"), default="codex")
    remote.add_argument("--image", type=Path, action="append", default=[], help="local 演练输出")
    remote.add_argument("--timeout", type=float, default=600)
    remote.add_argument("--poll-seconds", type=float, default=5)
    remote.add_argument("--once", action="store_true")
    remote.add_argument("--confirm-stopped", metavar="JOB_ID", help="确认原生图进程已停止后解除本机不确定检查点，不会重新生成")
    h3 = commands.add_parser("h3-worker", help="本机 ComfyUI MiniMax H3 视频执行器；只处理用户已确认并冻结积分的镜头")
    h3.add_argument("--config", type=Path, required=True, help="本机视频执行器 JSON 配置")
    h3.add_argument("--once", action="store_true", help="仅轮询一次并处理至多一个已提交镜头")
    h3.add_argument("--check", action="store_true", help="仅只读检查 ComfyUI 节点、模板和工具，不领取或生成任务")
    confirm = commands.add_parser("worker-confirm-stopped", help="确认原 Codex 子进程已停止后，解除状态待确认作业；本命令不结束进程")
    confirm.add_argument("--project", required=True)
    confirm.add_argument("--task", required=True)
    confirm.add_argument("--confirm-stopped", action="store_true", help="明确确认原执行进程已停止；之后仍需网页手动重试")
    script_confirm = commands.add_parser("script-confirm-stopped", help="确认原文本 Codex 已停止后，解除待确认脚本任务并释放积分；不会结束进程或重新生成")
    script_confirm.add_argument("--task", required=True)
    script_confirm.add_argument("--confirm-stopped", action="store_true")
    return parser


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command in {"run", "batch", "web", "worker", "remote-worker"}:
        if args.command in {"run", "batch"} and args.backend == "codex" and not args.allow_image_generation:
            parser.error("真实生图必须由你主动添加 --allow-image-generation")
        if args.backend == "codex" and args.image:
            parser.error("--image 只用于 local；真实生图的参考图来自任务的 --input 和 --base")
        if args.backend == "local" and not args.image:
            parser.error("local 后端至少需要一项 --image 已有图片路径")
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            parser.error("--timeout 必须大于0")
    if args.command == "remote-worker" and (not math.isfinite(args.poll_seconds) or args.poll_seconds < 1):
        parser.error("--poll-seconds 必须为至少1秒的有限数")
    if args.command in {"worker-confirm-stopped", "script-confirm-stopped"} and not args.confirm_stopped:
        parser.error("请核实原执行进程已经停止，再添加 --confirm-stopped")
    if args.command in {"web", "cloud"} and not 1 <= args.port <= 65535:
        parser.error("端口必须在1到65535之间")
    if args.command == "projects-migrate":
        if (args.apply or args.backup_only) and args.backup_directory is None:
            parser.error("应用或准备备份必须指定 --backup-directory")
        if args.apply and not args.expected_fingerprint:
            parser.error("应用迁移必须提供 --expected-fingerprint")
    try:
        if args.command == "h3-worker":
            from .h3_worker import ComfyH3Adapter, H3Worker, LocalVideoTransport, RemoteVideoTransport
            from .video_store import VideoStore
            config_path = args.config.expanduser().resolve()
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            allowed = {"comfy_url", "workflow_dir", "models", "ffmpeg", "ffprobe", "timeout", "poll_seconds", "cache_root", "server", "gpu_lock_path"}
            if not isinstance(config, dict) or set(config) - allowed:
                raise ValueError("视频执行器配置包含不支持的字段")
            if not all(config.get(key) for key in ("comfy_url", "workflow_dir", "cache_root")):
                raise ValueError("视频执行器配置需要 comfy_url、workflow_dir 和 cache_root")
            timeout = float(config.get("timeout", 7200))
            poll_seconds = float(config.get("poll_seconds", 5))
            if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(poll_seconds) or poll_seconds < 1:
                raise ValueError("视频执行器 timeout 必须大于0，poll_seconds 必须至少1秒")
            def configured_path(key):
                path = Path(config[key]).expanduser()
                return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()
            options = {"lock_path": configured_path("gpu_lock_path")} if config.get("gpu_lock_path") else {}
            adapter = ComfyH3Adapter(config["comfy_url"], configured_path("workflow_dir"),
                models=config.get("models"), ffmpeg=config.get("ffmpeg", "ffmpeg"), ffprobe=config.get("ffprobe", "ffprobe"),
                timeout=timeout, poll_seconds=poll_seconds, **options)
            if args.check:
                try:
                    report = adapter.check()
                    _print(report)
                    return 0 if report.get("available") else 1
                finally:
                    adapter.close()
            if config.get("server"):
                transport = RemoteVideoTransport(config["server"], os.environ.get("IMAGE_WORKBENCH_WORKER_TOKEN", ""))
            else:
                transport = LocalVideoTransport(VideoStore(StorageManager(StorageConfig(data_root=args.data_root))))
            worker = H3Worker(transport, configured_path("cache_root"), adapter)
            try:
                worker.serve(once=args.once, poll_seconds=poll_seconds)
            finally:
                worker.close()
            return 0
        if args.command == "remote-worker":
            from .remote_worker import RemoteWorker
            token = os.environ.get("IMAGE_WORKBENCH_WORKER_TOKEN", "")
            backend = LocalFileBackend(args.image) if args.backend == "local" else CodexImageBackend()
            worker = RemoteWorker(args.server, token, args.cache_root, backend, timeout=args.timeout)
            try:
                if args.confirm_stopped:
                    worker.confirm_stopped(args.confirm_stopped)
                worker.serve(once=args.once, poll_seconds=args.poll_seconds)
            finally:
                worker.close()
            return 0
        if args.command == "doctor":
            report = check_codex_environment()
            _print(report)
            return 0 if report["cli_path"] and not report["errors"] else 1
        storage = StorageManager(StorageConfig(data_root=args.data_root))
        if args.command == "accounts-migrate":
            from .account_migration import migrate_accounts
            _print(migrate_accounts(storage, apply=args.apply, expected_fingerprint=args.expected_fingerprint, backup_directory=args.backup_directory))
            return 0
        if args.command == "accounts-invite":
            from .accounts import IdentityStore
            _print(IdentityStore(storage).create_invitation(expires_hours=args.expires_hours))
            return 0
        if args.command == "cloud":
            import uvicorn
            from .remote_access import RemoteAccess
            from .web import create_app
            access = RemoteAccess(os.environ.get("IMAGE_WORKBENCH_PUBLIC_ORIGIN", ""),
                                  os.environ.get("IMAGE_WORKBENCH_WORKER_TOKEN", ""))
            app = create_app(storage, backend=args.backend, remote_access=access)
            # 与旧 LocalWorker 共用互斥锁，防止同一队列被两种模式同时消费。
            with app.state.workbench.worker_lock():
                uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, reload=False,
                            access_log=False, proxy_headers=False)
            return 0
        if args.command == "projects-migrate":
            projects = ProjectManager(storage)
            if args.backup_only:
                result = projects.backup_metadata(args.backup_directory)
            else:
                result = projects.migrate_legacy(dry_run=not args.apply,
                    expected_fingerprint=args.expected_fingerprint, backup_directory=args.backup_directory)
            _print(result)
            return 0
        runner = TaskRunner(storage)
        if args.command == "script-confirm-stopped":
            from .accounts import IdentityStore
            from .script_service import ScriptService
            service = ScriptService(storage, IdentityStore(storage))
            job = service._load(args.task)
            _print(service.confirm_stopped(job["account_id"], job["workspace_id"], args.task, confirmed=True))
            return 0
        if args.command == "worker-confirm-stopped":
            from .workbench_state import WorkbenchState
            from .worker import confirm_stopped
            _print(confirm_stopped(WorkbenchState(storage), args.project, args.task))
            return 0
        if args.command in {"web", "worker"}:
            from .workbench_state import WorkbenchState
            from .worker import LocalWorker, ensure_worker
            state = WorkbenchState(storage, backend=args.backend)
            for path in args.image:
                storage.inspect_image(path)
            if args.command == "worker":
                backend = LocalFileBackend(args.image) if args.backend == "local" else CodexImageBackend()
                LocalWorker(state, backend, timeout_seconds=args.timeout).serve(once=args.once)
            else:
                import uvicorn
                from .web import create_app
                app = create_app(storage, backend=args.backend)
                ensure_worker(app.state.workbench, local_images=args.image, timeout_seconds=args.timeout)
                uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, reload=False, access_log=False)
            return 0
        if args.command == "batch":
            backend = LocalFileBackend(args.image) if args.backend == "local" else CodexImageBackend(model=args.model)
            report = BatchRunner(storage).run(
                args.manifest, backend, reports_root=args.reports_root, timeout_seconds=args.timeout,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
                resume_directory=args.resume_directory,
            )
            _print(report)
            return 0 if report["acceptance_passed"] else 1
        if args.command == "project-create":
            _print(asdict(storage.create_project(args.name)))
            return 0
        if args.command == "task-create":
            for path in args.input:
                if not path.is_file():
                    raise FileNotFoundError(f"参考图不存在或不是普通文件: {path}")
            task = storage.create_task(args.project, args.operation, args.prompt, args.base)
            for path in args.input:
                storage.add_input_image(task.project_id, task.task_id, path)
            _print(asdict(task))
            return 0
        if args.command == "outputs":
            outputs = [
                {**asdict(item), "path": str(storage.get_artifact_path(item.artifact_id))}
                for item in storage.list_task_artifacts(args.project, args.task) if item.kind == "output"
            ]
            _print({"project_id": args.project, "task_id": args.task, "outputs": outputs})
            return 0
        if args.command == "run":
            backend = LocalFileBackend(args.image) if args.backend == "local" else CodexImageBackend(model=args.model)
            record = runner.run(args.project, args.task, backend, timeout_seconds=args.timeout)
        elif args.command == "status":
            record = runner.get_execution(args.project, args.task, args.execution)
        elif args.command == "retry-archive":
            record = runner.retry_archive(args.project, args.task, args.execution)
        else:
            record = runner.mark_interrupted(args.project, args.task, args.execution)
        if record is None:
            _print({"project_id": args.project, "task_id": args.task, "status": "not_run", "execution": None})
        else:
            _print({**asdict(record), "execution_directory": str(runner.get_execution_directory(args.project, args.task, record.execution_id))})
        return 0 if args.command in {"status", "mark-interrupted"} or (record and record.status == "succeeded") else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
