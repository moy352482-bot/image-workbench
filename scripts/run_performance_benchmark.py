"""Ten explicitly approved, serial real calls through the unchanged Workbench backend."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from image_workbench import CodexImageBackend, StorageConfig, StorageManager, TaskRunner


PLAIN_PROMPTS = [
    "生成一张正方形静物摄影：一个蓝色陶瓷马克杯放在浅米色木桌上，柔和自然侧光，浅灰背景，主体完整清晰，无文字、无标志。",
    "生成一张正方形插画：森林边的一间小木屋，门前有短石板路，清晨温暖阳光，普通细节程度，画面整洁，无文字。",
    "生成一张正方形静物摄影：一本合上的绿色书与一盆小绿植放在白色桌面，柔和日光，浅色墙壁背景，无可读文字、无标志。",
    "生成一张正方形摄影：一辆橙色城市自行车靠在浅米色墙边，地上有柔和树影，主体完整，普通细节，无人物、无文字、无标志。",
    "生成一张正方形静物摄影：透明玻璃花瓶中的黄色和白色春季小花，摆在简洁木桌上，浅灰背景，柔和自然光，无文字。",
]
REFERENCE_PROMPTS = [
    "编辑这张参考图片，保留主体的形状和构图，把背景改为干净的淡蓝色，并保留自然柔和阴影，无文字。",
    "编辑这张参考图片，保持主体及其位置，将光照改为温暖的傍晚侧光，背景简洁，保留清晰的主体轮廓，无文字。",
    "编辑这张参考图片，保留主体形状与大致构图，将整体画面改成细腻的水彩插画风格，背景清爽，无文字。",
    "编辑这张参考图片，主体保持原样，把承托主体的表面改成浅灰色石材质感，柔和自然光，无文字。",
    "编辑这张参考图片，保留主体形状，将背景改为淡奶油色并在背景加入柔和的窗格光影，主体完整清楚，无文字。",
]


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def snapshot_projects(source_root, destination_root):
    """Match the current artifact-index scale without accounts, jobs or historical CLI logs."""
    source = StorageManager(StorageConfig(data_root=source_root))
    target = StorageManager(StorageConfig(data_root=destination_root))
    counts = {"projects": 0, "tasks": 0, "artifacts": 0, "copied_bytes": 0}
    for project in source.list_projects():
        pid = project.project_id
        project_dir = target._path("projects", pid)
        (project_dir / "tasks").mkdir(parents=True)
        shutil.copyfile(source._path("projects", pid, "project.json"), project_dir / "project.json")
        counts["projects"] += 1
        for task in source.list_tasks(pid):
            tid = task.task_id
            task_dir = target._path("projects", pid, "tasks", tid)
            for folder in ("inputs", "outputs", "logs"):
                (task_dir / folder).mkdir(parents=True, exist_ok=True)
            for filename in ("task.json", "artifacts.json"):
                shutil.copyfile(source._path("projects", pid, "tasks", tid, filename), task_dir / filename)
            counts["tasks"] += 1
            for artifact in source.list_task_artifacts(pid, tid):
                src = source.get_artifact_path(artifact.artifact_id)
                dst = target._check_path(target.data_root / artifact.relative_path)
                shutil.copyfile(src, dst)
                counts["artifacts"] += 1
                counts["copied_bytes"] += dst.stat().st_size
    return target, counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--confirmed-count", type=int, choices=[10], required=True,
                        help="Only use after the human explicitly approves 5 plain + 5 reference image calls.")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)  # Never replay a partial run on relaunch.
    data_root = run_dir / "data"
    storage, counts = snapshot_projects(args.source_data_root.resolve(), data_root)
    project = storage.create_project("性能分析：已确认的10次真实调用")
    manifest = {
        "schema_version": 1, "records": [], "attempts": [],
        "sample_metadata": {
            "started_at": now(), "planned_count": 10, "plain_count": 5, "reference_count": 5,
            "human_confirmed_count": args.confirmed_count, "serial": True, "automatic_retries": 0,
            "backend": "CodexImageBackend() unchanged default model and CLI options",
            "python": sys.version.split()[0], "source_data_root": str(args.source_data_root.resolve()),
            "measurement_data_root": str(data_root), "project_id": project.project_id,
            "snapshot_counts": counts,
            "scope": "Current TaskRunner/StorageManager/Codex backend on same host and disk; artifact metadata and images copied at current dataset scale. No production accounts/jobs copied or modified. No HTTP, billing, remote-worker transfer or UI delivery timing.",
            "reference_design": "All five edits use one output of this batch (first successful plain task). Each has exactly one base image and no supporting reference.",
        },
    }
    manifest_path = run_dir / "manifest.json"
    write(manifest_path, manifest)
    runner, backend = TaskRunner(storage), CodexImageBackend()
    base_id = None
    prompts = [("A_plain", prompt) for prompt in PLAIN_PROMPTS] + [("B_reference", prompt) for prompt in REFERENCE_PROMPTS]
    for index, (group, prompt) in enumerate(prompts, start=1):
        if group == "B_reference" and base_id is None:
            manifest["sample_metadata"]["stop_reason"] = "No successful plain image available as reference; no extra generation or retry."
            break
        task = storage.create_task(project.project_id, "generate" if group == "A_plain" else "edit", prompt,
                                   base_artifact_id=base_id if group == "B_reference" else None)
        attempt = {"index": index, "group": group, "task_id": task.task_id, "prompt": prompt,
                   "started_at": now(), "status": "starting", "automatic_retries": 0}
        manifest["attempts"].append(attempt)
        write(manifest_path, manifest)
        print(json.dumps({"event": "task_started", "index": index, "group": group, "task_id": task.task_id}, ensure_ascii=False), flush=True)
        try:
            record = runner.run(project.project_id, task.task_id, backend, timeout_seconds=600)
        except BaseException as exc:
            attempt.update(status="interrupted_or_infrastructure_error", error_type=type(exc).__name__, finished_at=now())
            manifest["sample_metadata"]["stop_reason"] = "Runner raised; do not replay uncertain attempts."
            write(manifest_path, manifest)
            raise
        path = data_root / "logs" / "performance" / f"{task.task_id}--{record.execution_id}.json"
        attempt.update(status=record.status, execution_id=record.execution_id, failure_type=record.failure_type,
                       finished_at=now(), output_artifact_ids=record.output_artifact_ids,
                       performance_record=str(path))
        if path.is_file():
            manifest["records"].append(str(path))
            duration = json.loads(path.read_text(encoding="utf-8"))["durations_seconds"]
        else:
            duration = {}
            attempt["performance_missing"] = True
        if base_id is None and group == "A_plain" and record.status == "succeeded" and record.output_artifact_ids:
            base_id = record.output_artifact_ids[0]
            manifest["sample_metadata"]["reference_artifact_id"] = base_id
        write(manifest_path, manifest)
        print(json.dumps({"event": "task_finished", "index": index, "group": group, "status": record.status,
                          "task_id": task.task_id, "durations_seconds": duration}, ensure_ascii=False), flush=True)
    manifest["sample_metadata"].update(finished_at=now(), attempted_count=len(manifest["attempts"]),
                                      observed_record_count=len(manifest["records"]))
    write(manifest_path, manifest)
    print(json.dumps({"event": "benchmark_finished", "manifest": str(manifest_path),
                      "attempts": len(manifest["attempts"]), "records": len(manifest["records"])}, ensure_ascii=False), flush=True)
    return 0 if len(manifest["attempts"]) == 10 else 1


if __name__ == "__main__":
    raise SystemExit(main())
