"""小规模顺序批测。每次运行独立项目/报告目录，失败不自动重新生图。"""

from collections import Counter
from dataclasses import asdict
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
from statistics import mean, median
import traceback
from uuid import uuid4

from .locking import FileLock
from .storage import StorageManager
from .task_runner import StateUpdateError, TaskRunner
from .timing import measure, phase_seconds, utc_now


GROUPS = {"pure_generation", "reference_generation", "edit", "boundary"}
CSV_FIELDS = (
    "batch_id", "case_id", "attempt_id", "task_id", "project_id", "execution_id", "operation", "group",
    "backend", "success", "expected_mode", "expectation_met", "failure_type", "total_seconds",
    "codex_seconds", "prepare_seconds", "validate_seconds", "register_seconds", "input_count",
    "output_count", "error", "log_directory",
)


def load_manifest(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("批测清单必须是 schema_version=1 的 JSON 对象")
    if not isinstance(value.get("suite_name"), str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value["suite_name"]) is None:
        raise ValueError("suite_name 仅允许 1–80 位字母、数字、下划线、横线")
    cases = value.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ValueError("小范围批测每份清单需要 1–20 个任务")
    seen = {}
    for case in cases:
        if not isinstance(case, dict) or re.fullmatch(r"[a-z0-9_]{1,80}", str(case.get("case_id", ""))) is None:
            raise ValueError("case_id 必须是小写字母、数字或下划线")
        cid = case["case_id"]
        if cid in seen:
            raise ValueError(f"重复 case_id: {cid}")
        if case.get("group") not in GROUPS or case.get("operation") not in {"generate", "edit"}:
            raise ValueError(f"{cid}: 无效 group/operation")
        if not isinstance(case.get("project_name"), str) or not case["project_name"].strip():
            raise ValueError(f"{cid}: 缺少 project_name")
        # 空字符串是明确保留的负例；类型错误是清单错误，不启动任何任务。
        if not isinstance(case.get("prompt"), str):
            raise ValueError(f"{cid}: prompt 必须是字符串")
        if case.get("expected_mode") not in {"success", "expected_failure"}:
            raise ValueError(f"{cid}: 无效 expected_mode")
        if case["expected_mode"] == "expected_failure" and not isinstance(case.get("expected_failure_type"), str):
            raise ValueError(f"{cid}: 负例必须指定 expected_failure_type")
        if not isinstance(case.get("tags", []), list) or any(not isinstance(tag, str) for tag in case.get("tags", [])):
            raise ValueError(f"{cid}: tags 必须是字符串列表")
        if not isinstance(case.get("input_images", []), list):
            raise ValueError(f"{cid}: input_images 必须是列表")
        for item in case.get("input_images", []):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
                raise ValueError(f"{cid}: 参考图必须有 path")
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts or ":" in str(relative):
                raise ValueError(f"{cid}: 参考图路径必须位于清单目录内")
            if not (path.parent / relative).resolve().is_relative_to(path.parent):
                raise ValueError(f"{cid}: 参考图链接越出清单目录")
            if item.get("role", "reference") not in {"subject", "style", "reference"}:
                raise ValueError(f"{cid}: 无效图片角色")
        if case["operation"] == "edit":
            base = seen.get(case.get("base_case"))
            if base is None or base["project_name"] != case["project_name"]:
                raise ValueError(f"{cid}: base_case 必须引用同项目的前序任务")
        elif case.get("base_case") is not None:
            raise ValueError(f"{cid}: generate 不能指定 base_case")
        ratio = case.get("expected_aspect_ratio")
        if ratio is not None and (not isinstance(ratio, str) or re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", ratio) is None):
            raise ValueError(f"{cid}: expected_aspect_ratio 格式错误")
        seen[cid] = case
    return path, value


def _atomic_text(path, content):
    # 报告不进入 StorageManager 的索引；同目录临时文件 + replace 避免半截 JSON。
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    with temporary.open("x", encoding="utf-8", newline="") as stream:
        stream.write(content)
        stream.flush()
    temporary.replace(path)


def _write_json(path, value):
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _backend_model(backend):
    """Best available stable model label for resume compatibility checks."""
    value = getattr(backend, "model", None) or getattr(backend, "model_name", None)
    return str(value) if value is not None else None


def _manifest_fingerprint(manifest):
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(rows):
    groups = {}
    for name in sorted({row["group"] for row in rows}):
        subset = [row for row in rows if row["group"] == name]
        groups[name] = {"total": len(subset), "succeeded": sum(row["success"] for row in subset),
                        "expectation_met": sum(row["expectation_met"] for row in subset)}
    normal = [row for row in rows if row["expected_mode"] == "success"]
    codex = [row["codex_seconds"] for row in rows if row["codex_seconds"] is not None]
    successful_codex = [row["codex_seconds"] for row in rows if row["success"] and row["codex_seconds"] is not None]
    valid_seconds = [row["total_seconds"] for row in normal if row["success"]]
    return {
        "total": len(rows), "succeeded": sum(row["success"] for row in rows),
        "failed": sum(not row["success"] for row in rows),
        "success_rate": sum(row["success"] for row in rows) / len(rows) if rows else None,
        "normal_total": len(normal), "normal_succeeded": sum(row["success"] for row in normal),
        "successful_normal_mean_seconds": mean(valid_seconds) if valid_seconds else None,
        "successful_normal_median_seconds": median(valid_seconds) if valid_seconds else None,
        "successful_normal_max_seconds": max(valid_seconds) if valid_seconds else None,
        "expectation_met": sum(row["expectation_met"] for row in rows),
        "mean_total_seconds": mean(row["total_seconds"] for row in rows) if rows else None,
        "mean_codex_seconds": mean(codex) if codex else None,
        "mean_successful_codex_seconds": mean(successful_codex) if successful_codex else None,
        "codex_attempts": len(codex), "sum_task_seconds": sum(row["total_seconds"] for row in rows),
        "slowest": max(({"case_id": row["case_id"], "total_seconds": row["total_seconds"]} for row in rows), key=lambda v: v["total_seconds"], default=None),
        "failure_counts": dict(Counter(row["failure_type"] for row in rows if not row["success"])),
        "unexpected_failure_counts": dict(Counter(row["failure_type"] for row in rows if not row["expectation_met"])),
        "groups": groups,
    }


def write_reports(directory, metadata, rows):
    summary = summarize(rows)
    _write_json(directory / "batch_results.json", {**metadata, "summary": summary, "results": rows})
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_text(directory / "batch_results.csv", "\ufeff" + output.getvalue())
    seconds = lambda v: "未调用" if v is None else f"{v:.3f} 秒"
    rate = f"{summary['success_rate']:.1%}" if rows else "未开始"
    lines = [f"# 批量测试：{metadata['suite_name']}", "", f"批次 `{metadata['batch_id']}`；后端 `{metadata['backend']}`；状态 `{metadata['status']}`。", "",
             f"任务 {summary['total']}/{metadata['planned_cases']}；执行成功 {summary['succeeded']}；失败 {summary['failed']}；执行成功率 {rate}。",
             f"正常任务成功 {summary['normal_succeeded']}/{summary['normal_total']}；符合预期 {summary['expectation_met']}/{summary['total']}（包含正确拒绝的负例）。", "",
             f"平均任务耗时：{seconds(summary['mean_total_seconds'])}；任务耗时合计：{seconds(summary['sum_task_seconds'])}。",
             f"平均 Codex 进程耗时：{seconds(summary['mean_codex_seconds'])}；成功任务平均 Codex 耗时：{seconds(summary['mean_successful_codex_seconds'])}。",
             f"Codex 阶段记录 {summary['codex_attempts']} 次；批次墙钟耗时：{seconds(metadata.get('wall_seconds'))}。",
             f"最慢任务：{summary['slowest']}。", "", "| 分组 | 数量 | 执行成功 | 符合预期 |", "|---|---:|---:|---:|"]
    for name, group in summary["groups"].items():
        lines.append(f"| {name} | {group['total']} | {group['succeeded']} | {group['expectation_met']} |")
    lines += ["", f"失败类型及次数：`{json.dumps(summary['failure_counts'], ensure_ascii=False)}`。",
              f"非预期问题：`{json.dumps(summary['unexpected_failure_counts'], ensure_ascii=False)}`。", "",
              "## 问题与建议", ""]
    lines.insert(5, f"有效任务（正常且成功）的端到端耗时：平均 {seconds(summary['successful_normal_mean_seconds'])}；中位 {seconds(summary['successful_normal_median_seconds'])}；最大 {seconds(summary['successful_normal_max_seconds'])}。")
    unexpected = [row for row in rows if not row["expectation_met"]]
    for row in unexpected:
        lines.append(f"- `{row['case_id']}`：`{row['failure_type']}`；详见 `cases/{row['attempt_id']}/result.json` 和其日志路径。")
    if not unexpected:
        lines.append("- 当前已完成任务未发现非预期执行失败；这不代表长期可靠性已被证明。")
    lines.append(f"- 批次结束后复核 {metadata.get('integrity_checked', 0)} 个已登记文件；完整性错误 {len(metadata.get('integrity_errors', []))} 项。")
    for issue in metadata.get("integrity_errors", []):
        lines.append(f"- 文件完整性问题 `{issue['artifact_id']}`：{issue['error']}")
    warnings = [(row["case_id"], warning) for row in rows for warning in row.get("warnings", [])]
    lines.extend(f"- `{cid}`：{warning}" for cid, warning in warnings)
    lines += ["- 保持单进程顺序执行。前端接入前先提供明确的后台任务状态、超时说明及仅归档恢复入口。",
              "- 持续记录 CLI/模型变化带来的波动；超时不自动重试生图，避免重复消耗额度。", "",
              "## 统计口径与证据", "",
              "- UTC 起止时间用于对齐日志；秒数使用单调时钟。Codex 秒数包含进程启动、模型等待及进程结束，不是图片工具服务端净渲染时间。",
              "- 任务总耗时包括任务/输入登记、执行器和文件核查，不包括写入本条批测报告。prepare 包含批测准备和执行器快照准备。",
              "- 未进入的阶段为 0 秒；未调用 Codex 为 null，不参与 Codex 平均值。local 后端是文件导入测试，不证明生图质量或模型可用性。",
              "- 故意提交的负例记为执行失败，但可符合预期；清单在建任务前被拒绝时 task_id 为 null，以 attempt_id 追踪，未伪造任务 ID。",
              "- 每条结果保存输入/输出摘要、阶段时间、错误、执行日志路径及文件完整性检查。归档重试累计活动耗时，不计两次操作间的空闲时间。",
              "- 每次批测创建新项目和新报告目录；重跑的一致性指状态、引用及文件完整性一致，不要求随机生成图片的像素一致。", ""]
    _atomic_text(directory / "batch_summary.md", "\n".join(lines))
    return summary


class BatchRunner:
    def __init__(self, storage: StorageManager):
        self.storage = storage
        self.runner = TaskRunner(storage)

    def _artifact_summary(self, artifact):
        # The caller already has the verified Artifact record; avoid a global ID lookup.
        path = self.storage.artifact_file(artifact)
        data = path.read_bytes()
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ValueError(f"文件内容与登记记录不一致: {artifact.artifact_id}")
        self.storage.inspect_image(path)
        return {**asdict(artifact), "path": str(path)}

    def run(self, manifest_path, backend, *, reports_root="reports", timeout_seconds=600, progress=None,
            resume_directory=None, stop_when=None):
        if resume_directory is not None:
            directory = Path(resume_directory).resolve()
            if not directory.is_dir():
                raise FileNotFoundError(f"批次目录不存在: {directory}")
            with FileLock(directory / ".batch.lock"):
                return self._run_locked(manifest_path, backend, reports_root=reports_root,
                                        timeout_seconds=timeout_seconds, progress=progress,
                                        resume_directory=directory, stop_when=stop_when)

        _, manifest = load_manifest(manifest_path)
        batch_id = uuid4().hex
        directory = Path(reports_root).absolute() / f"{utc_now()[:19].replace(':', '').replace('-', '')}-{manifest['suite_name']}-{batch_id[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "cases").mkdir()
        with FileLock(directory / ".batch.lock"):
            return self._run_locked(manifest_path, backend, reports_root=reports_root,
                                    timeout_seconds=timeout_seconds, progress=progress,
                                    stop_when=stop_when, _prepared=(batch_id, directory))

    def _run_locked(self, manifest_path, backend, *, reports_root="reports", timeout_seconds=600, progress=None,
                    resume_directory=None, stop_when=None, _prepared=None):
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是有限正数")
        manifest_path, manifest = load_manifest(manifest_path)
        model = _backend_model(backend)
        fingerprint = _manifest_fingerprint(manifest)
        if resume_directory is None:
            batch_id, directory = _prepared
            _write_json(directory / "manifest.json", manifest)
            metadata = {"schema_version": 1, "batch_id": batch_id, "suite_name": manifest["suite_name"], "backend": backend.name,
                        "model": model, "manifest_fingerprint": fingerprint,
                        "manifest_source": str(manifest_path), "data_root": str(self.storage.data_root), "report_directory": str(directory),
                        "planned_cases": len(manifest["cases"]), "status": "running", "started_at": utc_now(), "ended_at": None, "wall_seconds": None}
            _write_json(directory / "metadata.json", metadata)
            journal = {"schema_version": 1, "cases": {}}
            _write_json(directory / "case_journal.json", journal)
            rows, projects, previous, tracked = [], {}, {}, {}
        else:
            directory = Path(resume_directory).resolve()
            metadata = _read_json(directory / "metadata.json")
            if (metadata.get("schema_version") != 1 or metadata.get("manifest_fingerprint") != fingerprint
                    or metadata.get("backend") != backend.name or metadata.get("model") != model
                    or Path(metadata.get("data_root", "")).resolve() != self.storage.data_root.resolve()
                    or metadata.get("suite_name") != manifest["suite_name"]
                    or metadata.get("planned_cases") != len(manifest["cases"])):
                raise ValueError("恢复批次与当前 manifest/backend/model/data_root 不匹配")
            if _read_json(directory / "manifest.json") != manifest:
                raise ValueError("恢复批次 manifest 内容不匹配")
            journal = _read_json(directory / "case_journal.json")
            if journal.get("schema_version") != 1 or not isinstance(journal.get("cases"), dict):
                raise ValueError("批次 case journal 无效")
            rows = []
            for case in manifest["cases"]:
                entry = journal["cases"].get(case["case_id"])
                if entry and entry.get("state") == "completed":
                    result_path = directory / "cases" / entry["attempt_id"] / "result.json"
                    if result_path.is_file():
                        rows.append(_read_json(result_path))
            projects = metadata.get("projects", {})
            previous = {row["case_id"]: row for row in rows}
            tracked = {item["artifact_id"]: (item["sha256"], item["project_id"], item["task_id"]) for row in rows
                       for item in [*row.get("input_summary", []), *row.get("output_summary", [])]}
            metadata.update(status="running", ended_at=None, wall_seconds=None)
            _write_json(directory / "metadata.json", metadata)
        batch_events = []
        stop_requested = False
        write_reports(directory, metadata, rows)
        with measure(batch_events, "batch"):
            for index, case in enumerate(manifest["cases"], 1):
                if progress:
                    progress(f"[{index}/{len(manifest['cases'])}] {case['case_id']} 开始")
                saved = journal["cases"].get(case["case_id"])
                if saved and saved.get("state") == "completed" and case["case_id"] in previous:
                    continue
                row, journal, projects = self._run_case(case, manifest_path.parent, backend, metadata["batch_id"], directory,
                                                        projects, previous, tracked, timeout_seconds, journal, saved)
                if row is not None:
                    rows.append(row)
                    previous[case["case_id"]] = row
                    metadata["projects"] = projects
                    _write_json(directory / "metadata.json", metadata)
                    _write_json(directory / "case_journal.json", journal)
                if row is None:
                    break
                _write_json(directory / "cases" / row["attempt_id"] / "result.json", row)
                write_reports(directory, metadata, rows)
                completed = journal["cases"].setdefault(case["case_id"], {})
                completed.update(state="uncertain" if row["failure_type"] == "resume_uncertain" else "completed",
                                 attempt_id=row["attempt_id"], project_id=row["project_id"],
                                 task_id=row["task_id"], execution_id=row["execution_id"])
                _write_json(directory / "case_journal.json", journal)
                if progress:
                    progress(f"[{index}/{len(manifest['cases'])}] {case['case_id']}: {row['status']} / {row['failure_type'] or 'ok'} / {row['total_seconds']:.2f}s")
                if row["failure_type"] in {"interrupted", "resume_uncertain"}:
                    break
                if stop_when is not None and stop_when(row):
                    stop_requested = True
                    break
            # 整批结束后重新核对所有已登记输入/输出：检测后续编辑误覆盖旧版本。
            integrity_errors = []
            groups = {}
            for artifact_id, (original_hash, project_id, task_id) in tracked.items():
                groups.setdefault((project_id, task_id), {})[artifact_id] = original_hash
            for (project_id, task_id), expected in groups.items():
                try:
                    artifacts = {item.artifact_id: item for item in self.storage.list_task_artifacts(project_id, task_id)}
                    for artifact_id, original_hash in expected.items():
                        artifact = artifacts.get(artifact_id)
                        if artifact is None:
                            integrity_errors.append({"artifact_id": artifact_id, "error": "任务元数据中已找不到图片"})
                            continue
                        self._artifact_summary(artifact)
                        if artifact.sha256 != original_hash:
                            integrity_errors.append({"artifact_id": artifact_id, "error": "批次中途元数据哈希变化"})
                except (OSError, ValueError) as exc:
                    integrity_errors.extend({"artifact_id": artifact_id, "error": str(exc)} for artifact_id in expected)
        uncertain = any(row.get("failure_type") == "resume_uncertain" for row in rows)
        metadata.update(status=("needs_attention" if uncertain else "stopped" if stop_requested else
                                "completed" if len(rows) == len(manifest["cases"]) else "interrupted"), ended_at=utc_now(),
                        wall_seconds=batch_events[0]["seconds"], integrity_checked=len(tracked), integrity_errors=integrity_errors)
        _write_json(directory / "metadata.json", metadata)
        summary = write_reports(directory, metadata, rows)
        return {**metadata, "summary": summary, "acceptance_passed": len(rows) == len(manifest["cases"]) and all(row["expectation_met"] for row in rows) and not integrity_errors}

    def _run_case(self, case, manifest_dir, backend, batch_id, report_dir, projects, previous, tracked, timeout, journal, saved):
        entry = dict(saved or {})
        attempt_id = entry.get("attempt_id") or uuid4().hex
        project_id = entry.get("project_id")
        task_id = entry.get("task_id")
        execution_id = entry.get("execution_id") or uuid4().hex
        case_dir = report_dir / "cases" / attempt_id
        case_dir.mkdir(exist_ok=True)
        _write_json(case_dir / "case.json", {**case, "resolved_inputs": [str(manifest_dir / item["path"]) for item in case.get("input_images", [])]})
        row = {"batch_id": batch_id, "case_id": case["case_id"], "attempt_id": attempt_id, "task_id": None,
               "project_id": None, "execution_id": None, "operation": case["operation"], "group": case["group"],
               "backend": backend.name, "expected_mode": case["expected_mode"], "success": False, "expectation_met": False,
               "status": "preparing", "failure_type": None, "error": None, "input_count": 0, "output_count": 0,
               "input_summary": [], "output_summary": [], "base_artifact_id": None, "tags": case.get("tags", []),
               "log_directory": str(case_dir), "log_paths": [str(case_dir / "case.json"), str(case_dir / "case.log")],
               "warnings": [], "checks": {}, "timing_events": []}
        record, phase = None, "prepare"
        events = row["timing_events"]
        if entry.get("state") in {"executing", "uncertain"}:
            try:
                record = self.runner.get_execution(project_id, task_id, execution_id)
            except Exception as exc:
                row.update(status="interrupted", failure_type="resume_uncertain",
                            error=f"执行记录无法安全核实；为避免重复调用，批次已暂停：{exc}")
                row.update(project_id=project_id, task_id=task_id, execution_id=execution_id)
                row["total_seconds"] = 0.0
                row["prepare_seconds"] = 0.0
                row["codex_seconds"] = None
                row["validate_seconds"] = row["register_seconds"] = 0.0
                row["execution_total_seconds"] = None
                row["expectation_met"] = False
                _write_json(case_dir / "result.json", row)
                (case_dir / "case.log").write_text(row["error"], encoding="utf-8")
                entry.update(state="uncertain", attempt_id=attempt_id, project_id=project_id, task_id=task_id, execution_id=execution_id)
                journal["cases"][case["case_id"]] = entry
                return row, journal, projects
            if record.status in {"running", "archiving"}:
                row.update(status=record.status, failure_type="resume_uncertain",
                            error="执行仍处于活动状态；为避免重复调用，批次已暂停。确认原进程停止并人工核对后再处理。")
                self._capture_record(row, record)
                row.update(project_id=project_id, task_id=task_id, execution_id=execution_id)
                row["total_seconds"] = row["prepare_seconds"] = row["validate_seconds"] = row["register_seconds"] = 0.0
                row["codex_seconds"] = record.codex_seconds
                row["execution_total_seconds"] = record.total_seconds
                row["expectation_met"] = False
                _write_json(case_dir / "result.json", row)
                (case_dir / "case.log").write_text(row["error"], encoding="utf-8")
                entry.update(state="uncertain", attempt_id=attempt_id, project_id=project_id, task_id=task_id, execution_id=execution_id)
                journal["cases"][case["case_id"]] = entry
                return row, journal, projects
        with measure(events, "case_total"):
            try:
                with measure(events, "case_prepare"):
                    name = case["project_name"]
                    if name not in projects:
                        projects[name] = project_id or uuid4().hex
                        metadata_path = report_dir / "metadata.json"
                        persisted = _read_json(metadata_path)
                        persisted["projects"] = projects
                        _write_json(metadata_path, persisted)
                    row["project_id"] = projects[name]
                    project_id = row["project_id"]
                    try:
                        project = self.storage.get_project(project_id)
                        if project.name != name:
                            raise ValueError("恢复项目 ID 指向了不同项目")
                    except FileNotFoundError:
                        self.storage.create_project(name, project_id=project_id)
                    base_id = None
                    if case["operation"] == "edit":
                        base_row = previous[case["base_case"]]
                        if not base_row["success"] or not base_row["output_summary"]:
                            phase = "dependency"
                            raise ValueError(f"基底任务 {case['base_case']} 未成功，不调用后端")
                        base_id = base_row["output_summary"][0]["artifact_id"]
                    row["base_artifact_id"] = base_id
                    reused_task = False
                    recovering_prepare = entry.get("state") == "preparing"
                    if task_id:
                        try:
                            task = self.storage.get_task(row["project_id"], task_id)
                            if (task.operation, task.prompt, task.base_artifact_id) != (case["operation"], case["prompt"], base_id):
                                raise ValueError("持久化 task 与 case 内容不一致")
                            reused_task = True
                        except FileNotFoundError:
                            task = self.storage.create_task(row["project_id"], case["operation"], case["prompt"], base_id, task_id=task_id)
                    else:
                        task_id = uuid4().hex
                        entry.update(state="preparing", attempt_id=attempt_id, project_id=row["project_id"],
                                     task_id=task_id, execution_id=execution_id)
                        journal["cases"][case["case_id"]] = entry
                        _write_json(report_dir / "case_journal.json", journal)
                        task = self.storage.create_task(row["project_id"], case["operation"], case["prompt"], base_id, task_id=task_id)
                    row["task_id"] = task.task_id
                    if reused_task:
                        saved_inputs = [item for item in self.storage.list_task_artifacts(task.project_id, task.task_id) if item.kind == "input"]
                        expected_inputs = case.get("input_images", [])
                        if len(saved_inputs) > len(expected_inputs):
                            raise ValueError("恢复任务的已登记参考图数量与 manifest 不一致")
                        remaining = list(saved_inputs)
                        for item in expected_inputs:
                            source = manifest_dir / item["path"]
                            digest = hashlib.sha256(source.read_bytes()).hexdigest()
                            match = next((artifact for artifact in remaining if artifact.role == item.get("role", "reference")
                                          and artifact.original_filename == source.name and artifact.sha256 == digest), None)
                            if match is None and not recovering_prepare:
                                raise ValueError("恢复任务的参考图快照与 manifest 不一致")
                            if match is None:
                                match = self.storage.add_input_image(task.project_id, task.task_id, source, role=item.get("role", "reference"))
                            else:
                                remaining.remove(match)
                            row["input_summary"].append(self._artifact_summary(match))
                            tracked[match.artifact_id] = (match.sha256, match.project_id, match.task_id)
                        if remaining:
                            raise ValueError("恢复任务存在 manifest 未声明的参考图")
                    else:
                        for item in case.get("input_images", []):
                            artifact = self.storage.add_input_image(task.project_id, task.task_id, manifest_dir / item["path"], role=item.get("role", "reference"))
                            row["input_summary"].append(self._artifact_summary(artifact))
                            tracked[artifact.artifact_id] = (artifact.sha256, artifact.project_id, artifact.task_id)
                phase = "execute"
                entry.update(state="executing", attempt_id=attempt_id, project_id=task.project_id,
                             task_id=task.task_id, execution_id=execution_id)
                journal["cases"][case["case_id"]] = entry
                _write_json(report_dir / "case_journal.json", journal)
                record = self.runner.run(task.project_id, task.task_id, backend, timeout_seconds=timeout,
                                         idempotency_key=execution_id)
                phase = "audit"
                row.update(status=record.status, failure_type=record.failure_type, error=record.error)
                self._capture_record(row, record)
                outputs = [item for item in self.storage.list_task_artifacts(task.project_id, task.task_id) if item.kind == "output"]
                for artifact in outputs:
                    row["output_summary"].append(self._artifact_summary(artifact))
                    tracked[artifact.artifact_id] = (artifact.sha256, artifact.project_id, artifact.task_id)
                    if artifact.source_artifact_id != base_id:
                        raise ValueError("编辑输出引用了错误历史图片")
                if record.status == "succeeded":
                    if not outputs or set(record.output_artifact_ids) != {item.artifact_id for item in outputs}:
                        raise ValueError("成功状态的输出清单不完整")
                    request_path = self.runner._record_directory(record) / "work" / "request.json"
                    request = json.loads(request_path.read_text(encoding="utf-8"))
                    if base_id:
                        snapshot = self.storage.data_root / request["edit_image"]
                        base_summary = next((item for item in base_row["output_summary"] if item["artifact_id"] == base_id), None)
                        if base_summary is None or hashlib.sha256(snapshot.read_bytes()).hexdigest() != base_summary["sha256"]:
                            raise ValueError("实际传入后端的编辑基底快照不一致")
                    if len(request["reference_images"]) != len(row["input_summary"]):
                        raise ValueError("实际传入后端的参考图数量不一致")
                    for path in request["reference_images"]:
                        # Storage 的顺序由创建时间决定；逐一匹配 artifact_id，不假设输入声明顺序。
                        actual = next((item for item in row["input_summary"] if Path(path).stem == item["artifact_id"]), None)
                        if actual is None or hashlib.sha256((self.storage.data_root / path).read_bytes()).hexdigest() != actual["sha256"]:
                            raise ValueError("实际参考图快照与登记记录不一致")
                    row["checks"] = {"directories": True, "input_registration": True, "output_files": True, "version_reference": True, "snapshots": True}
                    row["success"] = True
                    if case.get("expected_aspect_ratio"):
                        a, b = map(int, case["expected_aspect_ratio"].split(":"))
                        row["checks"]["aspect_ratio"] = all(abs(item.width / item.height / (a / b) - 1) <= 0.02 for item in outputs)
                        if not row["checks"]["aspect_ratio"]:
                            row["warnings"].append(f"实际图片比例与提示词中的 {case['expected_aspect_ratio']} 不一致；未改变工程执行成功状态。")
            except StateUpdateError as exc:
                record = exc.record
                self._capture_record(row, record)
                row.update(status=record.status, failure_type="state_update_failed", error=str(exc))
                # 即使 execution.json 不可写，保存内存记录和已登记输出证据。
                row["execution_record_fallback"] = asdict(record)
            except KeyboardInterrupt:
                row.update(status="interrupted", failure_type="interrupted", error="批测被用户中断，不再提交后续任务")
            except Exception:
                row.update(status="failed", failure_type={"prepare": "prepare_failed", "dependency": "dependency_failed", "audit": "integrity_failed"}.get(phase, "unexpected_exception"), error=traceback.format_exc())
            if not row["success"] and row["task_id"]:
                try:
                    # 登记部分成功但状态更新失败时，仍列出真实已存在的输出。
                    artifacts = self.storage.list_task_artifacts(row["project_id"], row["task_id"])
                    row["output_summary"] = [self._artifact_summary(item) for item in artifacts if item.kind == "output"]
                    for item in artifacts:
                        tracked[item.artifact_id] = (item.sha256, item.project_id, item.task_id)
                except (OSError, ValueError) as exc:
                    row["warnings"].append(f"失败后的产物核查未完成：{exc}")
        row["total_seconds"] = phase_seconds(events, "case_total")
        row["prepare_seconds"] = phase_seconds(events, "case_prepare") + (record.prepare_seconds if record else 0)
        for name in ("codex", "validate", "register", "backend"):
            row[name + "_seconds"] = getattr(record, name + "_seconds") if record else (None if name == "codex" else 0.0)
        row["execution_total_seconds"] = record.total_seconds if record else None
        row["input_count"], row["output_count"] = len(row["input_summary"]), len(row["output_summary"])
        row["expectation_met"] = row["success"] if case["expected_mode"] == "success" else (not row["success"] and row["failure_type"] == case["expected_failure_type"])
        (case_dir / "case.log").write_text(row["error"] or "执行成功；文件、登记、引用及快照检查通过。", encoding="utf-8")
        return row, journal, projects

    def _capture_record(self, row, record):
        row["execution_id"] = record.execution_id
        row["timing_events"].extend(record.timing_events)
        directory = self.runner._record_directory(record)
        row["log_directory"] = str(directory)
        row["log_paths"].extend(str(directory / name) for name in ("execution.json", "stdout.log", "stderr.log"))
        row["log_paths"].extend(str(directory / "work" / name) for name in
                                 ("request.json", "command.json", "response.json", "process.json", "response.schema.json", "artifact_recovery.json")
                                 if (directory / "work" / name).is_file())
        if (directory / "work" / "artifact_recovery.json").is_file():
            row["warnings"].append("CLI回复缺路径，已从本次唯一会话产物恢复；未重生成")
