"""One authorized Luna image; stable IDs make re-entry read the same execution."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_workbench import CodexImageBackend, StorageConfig, StorageManager
from image_workbench.batch_runner import _write_json
from image_workbench.task_runner import TaskRunner

directory = ROOT / "reports" / "fleet-200"
directory.mkdir(exist_ok=True)
journal = directory / "calibration.json"
if journal.exists():
    state = json.loads(journal.read_text(encoding="utf-8"))
else:
    state = {"project_id": uuid4().hex, "task_id": uuid4().hex, "execution_id": uuid4().hex,
             "model": "gpt-6-luna", "status": "planned"}
    _write_json(journal, state)
storage = StorageManager(StorageConfig(data_root=ROOT / "data"))
try:
    storage.get_project(state["project_id"])
except FileNotFoundError:
    storage.create_project("Luna 舰队 · 校准", project_id=state["project_id"])
try:
    storage.get_task(state["project_id"], state["task_id"])
except FileNotFoundError:
    storage.create_task(state["project_id"], "generate",
        "生成一张方形1:1商业静物摄影：一只无文字白色陶瓷杯放在浅灰石台上，右侧一枝绿色橄榄枝，左上柔和自然光，背景淡米色，构图干净，物体真实清晰。仅一张图片。",
        task_id=state["task_id"])
record = TaskRunner(storage).run(state["project_id"], state["task_id"],
    CodexImageBackend(model=state["model"]), timeout_seconds=600,
    idempotency_key=state["execution_id"])
state.update(status=record.status, record=asdict(record))
_write_json(journal, state)
print(json.dumps(state, ensure_ascii=False, indent=2), flush=True)
