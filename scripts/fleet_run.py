"""Run/resume the authorized Luna fleet; count only verified real outputs."""
import argparse
import csv
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_workbench import CodexImageBackend, StorageConfig, StorageManager
from image_workbench.batch_runner import BatchRunner, _write_json, _atomic_text, summarize, CSV_FIELDS
from image_workbench.locking import FileLock
from image_workbench.timing import utc_now

DIRECTORY = ROOT / "reports" / "fleet-200"
MODEL = "gpt-6-luna"
TARGET = 200


def all_results():
    rows, batches = [], []
    for path in sorted((DIRECTORY / "batches").glob("*/*/batch_results.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        rows.extend(report["results"])
        batches.append({key: report.get(key) for key in
                       ("batch_id", "suite_name", "report_directory", "status", "summary", "integrity_errors")})
    return rows, batches


def snapshot(status, reason=None):
    rows, batches = all_results()
    outputs = [dict(item, case_id=row["case_id"], execution_id=row["execution_id"])
               for row in rows if row["success"] for item in row["output_summary"]]
    unique = {item["sha256"]: item for item in outputs}
    result = {"schema_version": 1, "updated_at": utc_now(), "status": status,
              "reason": reason, "model": MODEL, "target_valid_images": TARGET,
              "valid_image_count": len(unique), "registered_output_count": len(outputs),
              "duplicate_hash_count": len(outputs) - len(unique),
              "summary": summarize(rows), "batches": batches, "images": list(unique.values()),
              "errors": [{key: row.get(key) for key in
                          ("case_id", "project_id", "task_id", "execution_id", "status", "failure_type", "error", "log_paths")}
                         for row in rows if not row["success"]],
              "warnings": [{"case_id": row["case_id"], "warnings": row["warnings"]}
                           for row in rows if row.get("warnings")]}
    stop_path = DIRECTORY / "stopped_by_user.json"
    if stop_path.exists():
        result["user_stop"] = json.loads(stop_path.read_text(encoding="utf-8"))
        result["status"] = "stopped_by_user"
        result["reason"] = result["user_stop"]["reason"]
    _write_json(DIRECTORY / "fleet_results.json", result)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_text(DIRECTORY / "fleet_results.csv", "\ufeff" + output.getvalue())
    return result


def stop_reason(row):
    if (DIRECTORY / "stopped_by_user.json").exists():
        return "用户已停止本轮批测，不再提交后续任务。"
    error = (row.get("error") or "").lower()
    if any(token in error for token in ("quota", "rate limit", "rate_limit", "usage limit", "insufficient",
             "unauthorized", "authentication", "not supported", "unavailable", "额度", "限额", "登录", "不可用")):
        return "该请求遇到额度、鉴权或能力限制；停止后续提交，保留已完成结果。"
    rows, _ = all_results()
    last = rows[-3:]
    if len(last) == 3 and all(not item["success"] for item in last) and len({item["failure_type"] for item in last}) == 1:
        return "连续三项同类失败；停止提交以便检查根因。"
    return None


class FleetStop(RuntimeError):
    pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-image-generation", action="store_true")
    args = parser.parse_args()
    if not args.allow_image_generation:
        parser.error("必须显式提供 --allow-image-generation；本脚本使用真实账号生图额度")
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    manifests = sorted((ROOT / "tests" / "fleet_cases").glob("*/suite.json"))
    if len(manifests) != 12:
        raise ValueError("预期 12 份经检查的测试清单")
    storage = StorageManager(StorageConfig(data_root=ROOT / "data"))
    backend = CodexImageBackend(model=MODEL)
    with FileLock(DIRECTORY / "fleet.lock"):
        if (DIRECTORY / "stopped_by_user.json").exists():
            parser.error("本轮已由用户停止，不能自动续跑；新的真实生图须重新获得用户授权并建立新计划。")
        plan = {"model": MODEL, "target_valid_images": TARGET, "manifests": [str(path) for path in manifests],
                "data_root": str(storage.data_root)}
        plan_path = DIRECTORY / "plan.json"
        if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ValueError("持久化计划与当前执行不一致")
        _write_json(plan_path, plan)
        try:
            for manifest in manifests:
                current = snapshot("running")
                if current["valid_image_count"] >= TARGET:
                    break
                suite_root = DIRECTORY / "batches" / manifest.parent.name
                suite_root.mkdir(parents=True, exist_ok=True)
                existing = sorted(suite_root.glob("*/batch_results.json"))
                if len(existing) > 1:
                    raise FleetStop("同一分片出现多个批次；需先对账，不能猜测重跑")
                resume = existing[0].parent if existing else None
                if existing:
                    previous = json.loads(existing[0].read_text(encoding="utf-8"))
                    if previous["status"] == "completed":
                        continue

                def progress(message):
                    print(message, flush=True)
                    report = snapshot("running")
                    print(f"已校验不同图片 {report['valid_image_count']}/{TARGET}；已完成任务 {report['summary']['total']}；失败 {report['summary']['failed']}", flush=True)

                kwargs = {"resume_directory": resume} if resume else {}
                result = BatchRunner(storage).run(manifest, backend, reports_root=suite_root,
                    timeout_seconds=600, progress=progress, stop_when=lambda row: bool(stop_reason(row)), **kwargs)
                if result["status"] != "completed":
                    rows, _ = all_results()
                    raise FleetStop((stop_reason(rows[-1]) if rows else None) or "分片未完成；检查持久化执行记录后再恢复")
                if result.get("integrity_errors"):
                    raise FleetStop("发现图片完整性问题，停止并保留现场")
            result = snapshot("completed")
            if result["valid_image_count"] < TARGET:
                result = snapshot("incomplete", "主测和备用清单已用完，有效图片仍未达到目标")
        except BaseException as exc:
            snapshot("stopped", f"{type(exc).__name__}: {exc}")
            raise
        print(json.dumps({key: result[key] for key in
              ("status", "valid_image_count", "registered_output_count", "summary")}, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
