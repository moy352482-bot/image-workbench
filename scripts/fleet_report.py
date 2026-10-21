"""Audit fleet artifacts and produce a local gallery; no generation or mutation of images."""
from collections import Counter
from dataclasses import asdict
import hashlib
import html
import json
import os
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_workbench import StorageConfig, StorageManager
from image_workbench.batch_runner import _write_json, _atomic_text
from image_workbench.task_runner import TaskRunner
from image_workbench.timing import utc_now

DIRECTORY = ROOT / "reports" / "fleet-200"


def main():
    fleet = json.loads((DIRECTORY / "fleet_results.json").read_text(encoding="utf-8"))
    storage = StorageManager(StorageConfig(data_root=ROOT / "data"))
    runner = TaskRunner(storage)
    errors, images, rows, checked_ids = [], [], [], set()
    recoveries = 0
    stop = fleet.get("user_stop", {})
    for path in sorted((DIRECTORY / "batches").glob("*/*/batch_results.json")):
        batch = json.loads(path.read_text(encoding="utf-8"))
        rows.extend(batch["results"])
        for row in batch["results"]:
            try:
                if row["task_id"] is None:
                    continue
                current = {item.artifact_id: item for item in storage.list_task_artifacts(row["project_id"], row["task_id"])}
                for expected in [*row["input_summary"], *row["output_summary"]]:
                    item = current[expected["artifact_id"]]
                    image_path = storage._artifact_file(item)
                    raw = image_path.read_bytes()
                    if (item.sha256 != expected["sha256"] or hashlib.sha256(raw).hexdigest() != item.sha256
                            or len(raw) != item.size_bytes):
                        raise ValueError("归档内容或元数据哈希改变")
                    storage._inspect_image(image_path)
                    checked_ids.add(item.artifact_id)
                if not row["success"]:
                    continue
                record = runner.get_execution(row["project_id"], row["task_id"], row["execution_id"])
                if record.status != "succeeded" or record.backend != "codex":
                    raise ValueError("成功计数中存在未成功的真实 Codex 执行")
                run = runner.get_execution_directory(row["project_id"], row["task_id"], row["execution_id"])
                command = json.loads((run / "work" / "command.json").read_text(encoding="utf-8"))["argv"]
                if command[command.index("--model") + 1] != "gpt-6-luna":
                    raise ValueError("模型命令与舰队约定不符")
                response = json.loads((run / "work" / "response.json").read_text(encoding="utf-8"))
                recovery_path = run / "work" / "artifact_recovery.json"
                if recovery_path.exists():
                    proof = json.loads(recovery_path.read_text(encoding="utf-8"))
                    if proof["execution_id"] != record.execution_id or proof["generation_retried"] is not False:
                        raise ValueError("恢复证据不符合无重新生成约定")
                    sources = [Path(proof["image_path"])]
                    recoveries += 1
                else:
                    if response["execution_id"] != record.execution_id or response["status"] != "ok":
                        raise ValueError("缺少本次生成的明确产物证据")
                    sources = [Path(value) for value in response["image_paths"]]
                source_hashes = {hashlib.sha256(source.read_bytes()).hexdigest() for source in sources}
                if set(record.output_artifact_ids) != {item["artifact_id"] for item in row["output_summary"]}:
                    raise ValueError("输出登记与执行结果不一致")
                for expected in row["output_summary"]:
                    item = current[expected["artifact_id"]]
                    if item.sha256 not in source_hashes or item.source_artifact_id != row["base_artifact_id"]:
                        raise ValueError("输出与缓存源图或历史基底不一致")
                    images.append({**asdict(item), "case_id": row["case_id"], "group": row["group"],
                                   "path": str(storage._artifact_file(item))})
            except Exception as exc:
                errors.append({"case_id": row["case_id"], "error": f"{type(exc).__name__}: {exc}"})
    hashes = {item["sha256"] for item in images}
    successful = [row["total_seconds"] for row in rows if row["success"]]
    evidence = {"verified_at": utc_now(), "fleet_status": fleet["status"], "checked_artifacts": len(checked_ids),
                "user_interrupted_count": len(stop.get("interrupted_cases", [])),
                "verified_real_output_count": len(images), "distinct_real_image_count": len(hashes),
                "session_path_recovery_count": recoveries, "integrity_errors": errors,
                "median_success_seconds": statistics.median(successful) if successful else None,
                "p95_success_seconds": sorted(successful)[max(0, (95 * len(successful) + 99) // 100 - 1)] if successful else None,
                "groups": dict(Counter(item["group"] for item in images)), "images": images}
    _write_json(DIRECTORY / "verified_artifacts.json", evidence)
    issues = []
    for row in rows:
        for warning in row.get("warnings", []):
            issues.append({"case_id": row["case_id"], "category": "recovered_warning", "message": warning,
                           "execution_id": row["execution_id"], "log_directory": row["log_directory"]})
        if not row["success"]:
            issues.append({"case_id": row["case_id"], "category": "execution_failure", "message": row["error"],
                           "failure_type": row["failure_type"], "log_directory": row["log_directory"]})
        if row["total_seconds"] >= 180:
            issues.append({"case_id": row["case_id"], "category": "slow_request_observation", "threshold_seconds": 180,
                           "total_seconds": row["total_seconds"], "codex_seconds": row["codex_seconds"],
                           "message": "最终完成；耗时集中于 Codex 进程等待。180 秒仅为本报告观察阈值，不是失败标准。",
                           "log_directory": row["log_directory"]})
    issues.extend({**item, "category": "user_interrupted"} for item in stop.get("interrupted_cases", []))
    calibration_path = DIRECTORY / "calibration.json"
    if calibration_path.exists():
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        recovery_path = DIRECTORY / "calibration_recovery.json"
        issues.append({"case_id": "calibration_excluded_from_main_count", "category": "calibration_failure",
                       "execution_id": calibration["execution_id"], "message": calibration["record"]["error"],
                       "recovery_evidence": str(recovery_path) if recovery_path.exists() else None,
                       "original_failure_preserved": True})
    _write_json(DIRECTORY / "issues.json", {"generated_at": utc_now(), "counts": dict(Counter(item["category"] for item in issues)),
                                          "items": issues})
    cards = []
    for item in images:
        relative = os.path.relpath(item["path"], DIRECTORY).replace("\\", "/")
        url, label = html.escape(relative, quote=True), html.escape(item["case_id"])
        cards.append(f'<a class="card" href="{url}" target="_blank" rel="noopener"><img loading="lazy" src="{url}" alt="{label}"><strong>{label}</strong><span>{item["width"]} × {item["height"]} · {html.escape(item["group"])}</span></a>')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Luna 舰队图片实测</title><style>
    *{box-sizing:border-box}body{margin:0;background:#f4f7f6;color:#173d38;font:15px/1.6 system-ui,sans-serif}header{background:#173d38;color:white;padding:36px 5vw}h1{margin:0 0 10px;font-size:32px}main{padding:25px 5vw}.stats{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:26px}.stats b{font-size:27px;display:block}.stats div{padding:16px 24px;border-radius:12px;background:white}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:20px}.card{background:white;border-radius:12px;overflow:hidden;text-decoration:none;color:inherit;box-shadow:0 2px 12px #173d380b}.card img{width:100%;height:235px;object-fit:contain;background:#e8eeeb;display:block}.card strong,.card span{display:block;margin:9px 14px;font-size:12px;overflow-wrap:anywhere}.card span{color:#657e75}footer{margin:32px 0;color:#657e75}a{color:#09695f}</style>'''
    state_note = "已按用户要求停止，原 200 张计划不再继续。" if fleet["status"] == "stopped_by_user" else f'批测状态：{html.escape(fleet["status"])}。'
    document += f'<header><h1>Luna 舰队 · 真实图片实测</h1><div>{state_note}按执行 ID、模型命令、源图哈希和归档文件逐项核对。点击图片可打开原图。</div></header><main><div class="stats"><div><b>{len(hashes)}</b>不同真实图片</div><div><b>{len(rows)}</b>已完成任务</div><div><b>{evidence["user_interrupted_count"]}</b>用户停止时中断</div><div><b>{sum(not row["success"] for row in rows)}</b>已完成任务失败</div><div><b>{recoveries}</b>路径恢复，无重新生成</div><div><b>{len(errors)}</b>完整性问题</div></div><div class="grid">'
    document += "".join(cards) + f'</div><footer>核对时间：{html.escape(evidence["verified_at"])}。校准图和本地合成测试不计入以上数量；工程校验通过不等于每张画面均经人工审美验收。 <a href="fleet_results.csv">任务 CSV</a> · <a href="issues.json">异常与中断记录</a> · <a href="verified_artifacts.json">校验明细</a></footer></main></html>'
    _atomic_text(DIRECTORY / "gallery.html", document)
    print(json.dumps({key: value for key, value in evidence.items() if key != "images"}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
