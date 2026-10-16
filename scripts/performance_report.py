"""Summarize only the performance records explicitly listed in a manifest."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys


PHASES = ("prepare", "codex_launch", "codex_execution", "codex_combined", "artifact_check",
          "register", "total", "queue_or_idle", "runner", "other")
GROUPS = ("A_plain", "B_reference")
STATUSES = ("success", "failed")
LABELS = {"prepare": "准备", "codex_launch": "Codex 启动", "codex_execution": "Codex 执行整体",
          "codex_combined": "Codex 启动与执行合计", "artifact_check": "产物检查", "register": "登记",
          "total": "任务总时长", "queue_or_idle": "执行前等待或空闲", "runner": "Runner 内耗时", "other": "其他"}
LIMITATIONS = [
    "这是 manifest 明确选定样本的描述统计；10 次或其他小批次不能代表总体分布或可靠性。",
    "成功与失败独立统计；未知阶段保持 null，并从该阶段的已知值统计中排除。",
    "weighted_share 使用同一批阶段值与正数 total 均已知的样本：阶段秒数之和 / 这些样本的 total 秒数之和。",
    "各阶段可用样本可能不同；codex_combined 与 launch/execution 重叠，runner 与阶段重叠，不能把所有行相加。",
    "阶段耗时使用 monotonic/perf_counter；UTC 时间用于对齐事件，total 和 queue_or_idle 受系统时钟调整影响。",
    "turn.started 只是本机观察到的 CLI 代理边界，不是模型开始推理的证据；stdout JSONL 当前没有源时间戳。",
    "Codex 执行整体包含理解、调度、ImageGen、返回与进程收尾；当前没有图片工具开始/结束事件，不能区分模型内部耗时。",
    "阶段可能包含多个片段，应读取 durations_seconds 的片段合计；首末 UTC 时间是包络，不一定是连续阶段时长。",
]


def _reject_constant(value):
    raise ValueError(f"JSON 不允许非有限数值：{value}")


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=_reject_constant)


def validate_record(record):
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise ValueError("性能记录必须使用 schema_version=1")
    for key in ("task_id", "execution_id", "project_id", "operation", "backend", "execution_status"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise ValueError(f"性能记录缺少有效 {key}")
    if record.get("prompt_type") not in GROUPS or record.get("status") not in STATUSES:
        raise ValueError("prompt_type 或 status 无效")
    for key in ("reference_count", "output_count"):
        if type(record.get(key)) is not int or record[key] < 0:
            raise ValueError(f"{key} 必须为非负整数")
    if "failure_type" not in record or record["failure_type"] is not None and not isinstance(record["failure_type"], str):
        raise ValueError("failure_type 必须为字符串或 null")
    for key in ("timestamps", "durations_seconds", "measurements"):
        if not isinstance(record.get(key), dict):
            raise ValueError(f"性能记录缺少 {key} 对象")
    for key in ("phase_segments", "codex_events"):
        if not isinstance(record.get(key), list):
            raise ValueError(f"性能记录缺少 {key} 数组")
    for phase in PHASES:
        if phase not in record["durations_seconds"]:
            raise ValueError(f"durations_seconds 缺少 {phase}；未知值应显式保存为 null")
        value = record["durations_seconds"][phase]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(f"{phase} 必须为有限数值或 null")
    tool_seconds = record["measurements"].get("imagegen_tool_seconds")
    if tool_seconds is not None and (type(tool_seconds) not in (int, float) or not math.isfinite(tool_seconds)):
        raise ValueError("imagegen_tool_seconds 必须为有限数值或 null")


def load_manifest(path):
    path = Path(path).resolve()
    manifest = _json(path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("records"), list):
        raise ValueError("manifest 必须包含 records 绝对路径数组")
    metadata = manifest.get("sample_metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("sample_metadata 必须为对象")
    records, sources, seen_paths, seen_executions = [], [], set(), set()
    for value in manifest["records"]:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("records 中的每个值必须是性能 JSON 文件的绝对路径")
        source = Path(value).resolve(strict=True)
        if source in seen_paths:
            raise ValueError("manifest 包含重复文件，拒绝重复计数")
        raw = source.read_bytes()
        record = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
        validate_record(record)
        execution = record["task_id"], record["execution_id"]
        if execution in seen_executions:
            raise ValueError("manifest 包含重复 task_id/execution_id，拒绝重复计数")
        seen_paths.add(source)
        seen_executions.add(execution)
        records.append(record)
        sources.append({"path": str(source), "sha256": hashlib.sha256(raw).hexdigest(),
                        "task_id": record["task_id"], "execution_id": record["execution_id"]})
    return records, metadata, sources


def _known(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def metric(records, name, *, section="durations_seconds"):
    values = [record[section].get(name) for record in records]
    known = [value for value in values if _known(value)]
    paired = [(record[section].get(name), record["durations_seconds"]["total"]) for record in records
              if _known(record[section].get(name)) and _known(record["durations_seconds"]["total"])
              and record["durations_seconds"]["total"] > 0]
    numerator = sum(value for value, total in paired)
    denominator = sum(total for value, total in paired)
    return {"known_count": len(known), "missing_count": sum(value is None for value in values),
            "invalid_count": sum(value is not None and not _known(value) for value in values),
            "mean": statistics.mean(known) if known else None,
            "median": statistics.median(known) if known else None,
            "min": min(known) if known else None, "max": max(known) if known else None,
            "weighted_share": numerator / denominator if denominator else None,
            "share_sample_count": len(paired), "share_numerator_seconds": numerator if paired else None,
            "share_denominator_seconds": denominator if paired else None, "share_denominator": "total"}


def aggregate(records):
    result = {}
    for status in STATUSES:
        selected = [record for record in records if record["status"] == status]
        result[status] = {"count": len(selected),
                          "durations_seconds": {phase: metric(selected, phase) for phase in PHASES},
                          "measurements": {"imagegen_tool_seconds": metric(selected, "imagegen_tool_seconds", section="measurements")}}
    return result


def recommendations(overall):
    successful = overall["success"]
    if not successful["count"]:
        return {"short_term": ["尚无成功样本，先补齐可比的成功执行记录；失败样本仅用于定位失败路径。"],
                "medium_term": [], "long_term": []}
    phases, short, medium = successful["durations_seconds"], [], []
    execution = phases["codex_execution"]["weighted_share"]
    launch = phases["codex_launch"]["weighted_share"]
    register = phases["register"]["weighted_share"]
    if execution is not None and execution > 0.70:
        short.append("本批已观测成功样本由 Codex 执行整体（理解、调度、ImageGen、返回）主导；不能据此断言图片模型计算是瓶颈。")
        medium.append("保持模型、输出数量、图片规格和并发一致，补测 A/B；如需再拆分执行内部耗时，先取得可信图片工具边界。")
    if launch is not None and launch > 0.30:
        short.append("本批 Codex 启动占比超过 30%，先区分进程创建、CLI 初始化与认证等待；不要仅凭 turn.started 代理边界归因。")
        medium.append("对同环境的冷启动与连续启动做受控比较，再决定是否优化初始化或连接复用；本工具不改变 CLI 调用。")
    if register is not None and register > 0.20:
        short.append("本批登记占比超过 20%，核对分片记录中的 staging_copy、register 与 final_state_save，再定位拷贝或元数据写入成本。")
        medium.append("仅针对反复出现的登记成本开展存储实验，并核验产物完整性；本报告不实施优化。")
    elif register is not None and register < 0.05:
        short.append("本批登记占比低于 5%，保留现有存储实现；存储优化优先级低，当前证据不支持为提速减少复制。")
    if not short:
        short.append("当前可观测阶段未触发执行 >70%、启动 >30% 或登记 >20% 的观察阈值；不据此认定没有瓶颈。")
    if phases["codex_execution"]["missing_count"]:
        short.append("部分 Codex 启动/执行边界未知，查看 codex_combined，并保留 null；不能把合计时长全归入执行或模型。")
    if not medium:
        medium.append("按同一模型、输出规格与并发继续采集 A/B 可比样本，比较中位数和范围，而非用少量均值作总体结论。")
    return {"short_term": short, "medium_term": medium,
            "long_term": ["在瓶颈多批次复现后，围绕现有 Worker/队列做受控吞吐与资源占用实验，再评估结构调整。"]}


def build_report(records, *, sample_metadata=None, source_files=None, manifest_path=None):
    for record in records:
        validate_record(record)
    overall = aggregate(records)
    warnings = []
    for record in records:
        negative = [phase for phase in PHASES
                    if record["durations_seconds"][phase] is not None and record["durations_seconds"][phase] < 0]
        if negative:
            warnings.append({"task_id": record["task_id"], "execution_id": record["execution_id"],
                             "message": "负耗时保留原始值，但不参与统计；请核查时钟调整。", "phases": negative})
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "manifest_path": manifest_path, "sample_metadata": sample_metadata or {}, "record_count": len(records),
            "source_files": source_files or [], "records": records, "overall": overall,
            "groups": {group: aggregate([record for record in records if record["prompt_type"] == group]) for group in GROUPS},
            "failures": [{key: record[key] for key in ("task_id", "execution_id", "project_id", "prompt_type", "backend", "execution_status", "failure_type")}
                         for record in records if record["status"] == "failed"],
            "warnings": warnings, "limitations": LIMITATIONS,
            "recommendations": recommendations(overall) if records else {"short_term": [], "medium_term": [], "long_term": []}}


def _cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _seconds(value):
    return "未知" if value is None else f"{value:.3f}"


def render_markdown(report):
    lines = ["# 图片任务性能摘要", "", f"生成时间（UTC）：{report['generated_at']}", ""]
    if report["sample_metadata"]:
        lines.extend(["批次说明：", "", *[f"- {_cell(key)}：{_cell(value)}" for key, value in report["sample_metadata"].items()], ""])
    if not report["record_count"]:
        lines.extend(["尚无数据。manifest 的 records 为空；未扫描历史记录，未生成任何图片，也未推断性能结论。", ""])
    else:
        lines.extend([f"本报告仅包含 manifest 指定的 {report['record_count']} 次执行。成功与失败分开；这是本批描述统计，不能泛化到总体。", "",
                      "## 每次任务明细（秒）", "", "| task_id | A/B | 结果 | total | prepare | launch | execution | check | register |",
                      "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for record in report["records"]:
            group = "A" if record["prompt_type"] == "A_plain" else "B"
            outcome = "成功" if record["status"] == "success" else "失败"
            lines.append(f"| {_cell(record['task_id'])} | {group} | {outcome} | "
                         + " | ".join(_seconds(record["durations_seconds"][phase]) for phase in
                                      ("total", "prepare", "codex_launch", "codex_execution", "artifact_check", "register")) + " |")
        lines.extend(["", "明细按 manifest 顺序列出；launch/execution 为 Codex 代理边界，check 为产物检查。完整 execution_id 见 JSON。", "",
                      "## 总时长（秒）", "", "| 分组 | 结果 | 执行数 | 已知数 | 均值 | 中位数 | 最小 | 最大 |",
                      "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        scopes = [("整体", report["overall"]), ("A 普通提示", report["groups"]["A_plain"]), ("B 参考图片", report["groups"]["B_reference"])]
        for label, groups in scopes:
            for status in STATUSES:
                group, outcome = groups[status], "成功" if status == "success" else "失败"
                total = group["durations_seconds"]["total"]
                lines.append(f"| {label} | {outcome} | {group['count']} | {total['known_count']} | " + " | ".join(_seconds(total[key]) for key in ("mean", "median", "min", "max")) + " |")
        lines.extend(["", "## 阶段统计（秒）", "", "占比为同一样本分母的加权占比。各行已知样本可不同；重叠阶段不可相加。", ""])
        for label, groups in scopes:
            for status in STATUSES:
                group = groups[status]
                if not group["count"]:
                    continue
                lines.extend([f"### {label} · {'成功' if status == 'success' else '失败'}", "",
                              "| 阶段 | 已知 / 缺失 / 无效 | 均值 | 中位数 | 最小 | 最大 | 加权占比 | 占比分母样本数 |",
                              "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
                for phase in PHASES:
                    stats = group["durations_seconds"][phase]
                    share = "未知" if stats["weighted_share"] is None else f"{stats['weighted_share']:.1%}"
                    lines.append(f"| {LABELS[phase]} | {stats['known_count']} / {stats['missing_count']} / {stats['invalid_count']} | "
                                 + " | ".join(_seconds(stats[key]) for key in ("mean", "median", "min", "max"))
                                 + f" | {share} | {stats['share_sample_count']} |")
                lines.append("")
        if report["failures"]:
            lines.extend(["## 失败执行", "", "| task_id | execution_id | 分组 | 执行状态 | 失败类型 |", "| --- | --- | --- | --- | --- |"])
            for failure in report["failures"]:
                lines.append("| " + " | ".join(_cell(failure[key] if failure[key] is not None else "未知") for key in ("task_id", "execution_id", "prompt_type", "execution_status", "failure_type")) + " |")
            lines.append("")
        lines.extend(["## 建议（未实施）", ""])
        for key, title in (("short_term", "短期"), ("medium_term", "中期"), ("long_term", "长期")):
            lines.extend(f"- {title}：{value}" for value in report["recommendations"][key])
        lines.append("")
    lines.extend(["## 测量边界", "", *[f"- {value}" for value in report["limitations"]], ""])
    if report["warnings"]:
        lines.extend(["## 数据提示", "", *[f"- {_cell(item['execution_id'])}：{item['message']}（{', '.join(item['phases'])}）" for item in report["warnings"]], ""])
    lines.append("完整原始记录、来源文件摘要、同样本分母秒数及全部分组统计见 `performance_report.json`。")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        records, metadata, sources = load_manifest(args.manifest)
        directory = args.output.resolve()
        destinations = [directory / "performance_report.json", directory / "performance_summary.md"]
        protected = {args.manifest.resolve(), *[Path(source["path"]) for source in sources]}
        if any(path.resolve() in protected for path in destinations):
            raise ValueError("报告输出不能覆盖 manifest 或原始性能记录")
        report = build_report(records, sample_metadata=metadata, source_files=sources, manifest_path=str(args.manifest.resolve()))
        directory.mkdir(parents=True, exist_ok=True)
        destinations[0].write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        destinations[1].write_text(render_markdown(report), encoding="utf-8")
        print(f"已汇总 {len(records)} 次执行：{destinations[0]}")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"性能报告未生成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
