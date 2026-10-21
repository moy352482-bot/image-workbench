"""Recover the calibration's exact session output without generating another image."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_workbench import BackendRequest, CodexImageBackend, LocalFileBackend, StorageConfig, StorageManager
from image_workbench.batch_runner import _write_json
from image_workbench.task_runner import TaskRunner

directory = ROOT / "reports" / "fleet-200"
journal = directory / "calibration.json"
state = json.loads(journal.read_text(encoding="utf-8"))
storage = StorageManager(StorageConfig(data_root=ROOT / "data"))
runner = TaskRunner(storage)
original = runner.get_execution(state["project_id"], state["task_id"], state["execution_id"])
run_dir = runner.get_execution_directory(state["project_id"], state["task_id"], state["execution_id"])
task = storage.get_task(state["project_id"], state["task_id"])
request = BackendRequest(state["execution_id"], task, run_dir / "work", (), None,
    run_dir / "stdout.log", run_dir / "stderr.log", 600, storage.config.max_image_bytes)
process = json.loads((request.work_dir / "process.json").read_text(encoding="utf-8"))
recovery_path = directory / "calibration_recovery.json"
if recovery_path.exists():
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
else:
    result = CodexImageBackend(model="gpt-6-luna")._collect_response(request, process["started_at_unix"])
    recovery = {"original_execution_id": original.execution_id, "recovery_execution_id": uuid4().hex,
                "image_paths": [str(path) for path in result.image_paths], "generation_retried": False}
    _write_json(recovery_path, recovery)
record = runner.run(task.project_id, task.task_id, LocalFileBackend(recovery["image_paths"]),
                    idempotency_key=recovery["recovery_execution_id"])
recovery["record"] = asdict(record)
_write_json(recovery_path, recovery)
print(json.dumps(recovery, ensure_ascii=False, indent=2))
