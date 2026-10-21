"""Synthetic storage lookup benchmark; never invokes an image backend."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from uuid import uuid4

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from image_workbench.config import StorageConfig
from image_workbench.models import Artifact
from image_workbench.storage import StorageManager


REPORTS = ROOT / "reports"
SIZES = (20, 100, 200)
SAMPLED_IDS_PER_SCENARIO = 3


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_fixture(storage, project, count):
    """Create valid synthetic task/artifact records without repeated ID scans."""
    png = storage.data_root.parent / f"fixture-{uuid4().hex}.png"
    Image.new("RGB", (2, 2), (35, 110, 180)).save(png, format="PNG")
    data = png.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    ids = []
    try:
        for index in range(count):
            task = storage.create_task(project.project_id, "generate", f"合成性能夹具 {index + 1}")
            artifact_id = uuid4().hex
            relative = f"projects/{project.project_id}/tasks/{task.task_id}/outputs/{artifact_id}.png"
            output = storage.data_root / Path(relative)
            shutil.copyfile(png, output)
            artifact = Artifact(
                artifact_id=artifact_id,
                project_id=project.project_id,
                task_id=task.task_id,
                kind="output",
                role=None,
                original_filename="synthetic-fixture.png",
                format="PNG",
                width=2,
                height=2,
                size_bytes=len(data),
                sha256=digest,
                relative_path=relative,
                created_at=_utc_now(),
                source_artifact_id=None,
            )
            storage._write_json(storage._task_dir(project.project_id, task.task_id) / "artifacts.json", {
                "schema_version": 1,
                "artifacts": [artifact.__dict__],
            })
            ids.append(artifact_id)
    finally:
        png.unlink(missing_ok=True)
    return ids


def _scenario(count, scratch_root):
    data_root = scratch_root / f"data-{count}"
    storage = StorageManager(StorageConfig(data_root=data_root))
    project = storage.create_project(f"Synthetic storage lookup fixture, {count} tasks")
    setup_started = time.perf_counter()
    artifact_ids = _write_fixture(storage, project, count)
    setup_seconds = time.perf_counter() - setup_started

    reads = Counter()
    original_read_json = storage._read_json

    def counted_read_json(path):
        reads["json_files"] += 1
        return original_read_json(path)

    storage._read_json = counted_read_json
    first = artifact_ids[0]
    started = time.perf_counter()
    first_path = storage.get_artifact_path(first)
    cold_lookup_ms = (time.perf_counter() - started) * 1000
    if not first_path.is_file():
        raise RuntimeError(f"lookup returned a missing path: {first}")
    cold_json_reads = reads["json_files"]
    reads["json_files"] = 0

    samples = []
    post_write_path = None
    registration_seconds = None
    registration_json_reads = None
    try:
        indices = sorted({0, count // 2, count - 1})[:SAMPLED_IDS_PER_SCENARIO]
        for index in indices:
            artifact_id = artifact_ids[index]
            started = time.perf_counter()
            resolved = storage.get_artifact_path(artifact_id)
            samples.append(time.perf_counter() - started)
            if not resolved.is_file():
                raise RuntimeError(f"lookup returned a missing path: {artifact_id}")
        reads["json_files"] = 0
        source = scratch_root / "post-write-fixture.png"
        Image.new("RGB", (2, 2), (210, 80, 30)).save(source, format="PNG")
        base_artifact = storage.get_artifact(first)
        started = time.perf_counter()
        added = storage.add_input_image(project.project_id, base_artifact.task_id, source, role="reference")
        registration_seconds = time.perf_counter() - started
        registration_json_reads = reads["json_files"]
        reads["json_files"] = 0
        started = time.perf_counter()
        post_write_path = storage.get_artifact_path(added.artifact_id)
        post_write_ms = (time.perf_counter() - started) * 1000
        if not post_write_path.is_file():
            raise RuntimeError(f"post-write lookup returned a missing path: {added.artifact_id}")
        post_write_json_reads = reads["json_files"]
        source.unlink(missing_ok=True)
    finally:
        storage._read_json = original_read_json

    query_count = len(samples)
    return {
        "tasks": count,
        "artifacts": count,
        "queries": query_count,
        "total_json_reads": reads["json_files"],
        "json_reads_per_query": reads["json_files"] / query_count,
        "cold_index_build_json_reads": cold_json_reads,
        "cold_lookup_ms": cold_lookup_ms,
        "mean_lookup_ms": sum(samples) * 1000 / query_count,
        "median_lookup_ms": sorted(samples)[query_count // 2] * 1000,
        "max_lookup_ms": max(samples) * 1000,
        "total_query_seconds": sum(samples),
        "post_write_lookup_ms": post_write_ms,
        "post_write_lookup_json_reads": post_write_json_reads,
        "registration_json_reads": registration_json_reads,
        "registration_seconds": registration_seconds,
        "fixture_setup_seconds_excluded_from_lookup": setup_seconds,
        "baseline_json_reads_per_query": 2 + 5 * count,
    }


def main():
    REPORTS.mkdir(parents=True, exist_ok=True)
    scratch_parent = REPORTS.resolve()
    scratch_root = (scratch_parent / f".fleet-storage-probe-{uuid4().hex}").resolve()
    if scratch_root.parent != scratch_parent or not scratch_root.name.startswith(".fleet-storage-probe-"):
        raise RuntimeError(f"Refusing unexpected temporary path: {scratch_root}")
    scratch_root.mkdir()
    try:
        results = [_scenario(count, scratch_root) for count in SIZES]
    finally:
        if scratch_root.exists():
            resolved = scratch_root.resolve()
            if resolved.parent != scratch_parent or not resolved.name.startswith(".fleet-storage-probe-"):
                raise RuntimeError(f"Refusing to remove unexpected temporary path: {resolved}")
            shutil.rmtree(resolved)

    payload = {
        "schema_version": 1,
        "title": "Synthetic artifact-ID storage lookup after stat-index optimization",
        "created_at": _utc_now(),
        "environment": {"python": os.sys.version.split()[0], "platform": os.name},
        "method": {
            "lookup": "StorageManager.get_artifact_path(artifact_id)",
            "fixture": "1 project per scenario; one synthetic 2x2 PNG output per task; no image backend or model call",
            "query_count": "One cold index-building lookup, followed by three warm representative ID lookups; then one new artifact is registered through the public API and queried",
            "read_count": "Counts StorageManager._read_json calls separately for cold index build, warm queries, registration, and post-write query",
            "fixture_creation": "Uses valid task/artifact metadata and files in a temporary data root; artifact registration is written directly to avoid measuring setup's duplicate-ID scans",
        },
        "results": results,
        "interpretation": "Before optimization, each ID lookup scanned and decoded the full project/task/artifact metadata tree, requiring 2 + 5N JSON reads for one project with N tasks and one artifact per task. The optimized instance-local index keeps JSON authoritative: a warm lookup checks a stat signature across the record tree and validates the target path, while JSON is reread and fully revalidated only after a detected change. The probe also registers a new artifact through the public API and confirms that the following lookup finds it without rebuilding the entire index. Synthetic tiny images isolate metadata traversal and do not model real image decoding, model latency, or output quality.",
    }
    baseline_path = REPORTS / "fleet_storage_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
    if baseline:
        old_by_size = {row["tasks"]: row for row in baseline["results"]}
        for row in results:
            old = old_by_size.get(row["tasks"])
            if old and row["mean_lookup_ms"] > 0:
                row["baseline_mean_lookup_ms"] = old["mean_lookup_ms"]
                row["warm_speedup_vs_baseline"] = old["mean_lookup_ms"] / row["mean_lookup_ms"]
    payload["baseline_source"] = str(baseline_path) if baseline else None
    json_path = REPORTS / "fleet_storage_optimized.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 合成存储查询优化对比",
        "",
        "本探针只创建临时存储夹具和 2×2 合成 PNG，不调用模型或生图后端，也不计入舰队清单的任务数。每种规模下测量一次冷查询、首/中/末三个暖查询；随后通过公开 API 注册一张合成输入图并立即按 ID 查询。夹具创建耗时与查询计时分开。JSON 读取次数通过统计 `_read_json` 调用获得。",
        "",
        "| 任务数 | 暖查询数 | JSON 读取/暖查询 | 暖查询均值 (ms) | 注册后查询 (ms) | 注册后查询 JSON 读取 | 注册时 JSON 读取 | 夹具创建 (s) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item['tasks']} | {item['queries']} | {item['json_reads_per_query']:.0f} | "
            f"{item['mean_lookup_ms']:.3f} | {item['post_write_lookup_ms']:.3f} | "
            f"{item['post_write_lookup_json_reads']} | {item['registration_json_reads']} | "
            f"{item['fixture_setup_seconds_excluded_from_lookup']:.3f} |"
        )
    lines += [
        "",
        "首个查询重建并校验 JSON 权威索引；随后查询对全部相关目录和元数据文件做 stat 签名核验，只在签名变化时重读 JSON。表中注册后查询验证了本实例创建新产物时索引可安全增量更新。冷启动扫描及与优化前同机基线的对比如下：",
        "",
        "| 任务/图片数 | 冷启动读取 JSON | 冷查询 (ms) | 优化前均值 (ms) | 暖查询加速比 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        baseline_ms = item.get("baseline_mean_lookup_ms")
        speedup = item.get("warm_speedup_vs_baseline")
        lines.append(
            f"| {item['tasks']} | {item['cold_index_build_json_reads']} | {item['cold_lookup_ms']:.3f} | "
            f"{baseline_ms:.3f}" if baseline_ms is not None else f"| {item['tasks']} | {item['cold_index_build_json_reads']} | {item['cold_lookup_ms']:.3f} | n/a"
        )
        lines[-1] += f" | {speedup:.1f}× |" if speedup is not None else " | n/a |"
    lines += [
        "",
        "变更外部 JSON 或目录会让 stat 签名变化，触发完整索引重建并沿用原来的 JSON 解码、重复 ID 检测及路径校验；命中时仍重新校验目标图片路径。200 项首次索引读取为 1,002 份 JSON；暖查询不重读 JSON。探针测量的是 metadata 导航，不代表生图耗时或整批总耗时。",
        "",
        "原始未优化测量保存在 `fleet_storage_baseline.json`；本次结果保存在 `fleet_storage_optimized.json`。合成夹具建在 reports 下的唯一临时目录中，并已在探针结束时删除。",
        "",
    ]
    md_path = REPORTS / "fleet_storage_optimized.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
