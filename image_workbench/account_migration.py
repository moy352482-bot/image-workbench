"""只增加账号归属，保留原目录和稳定 ID；支持只读预览与外部备份。"""
from pathlib import Path
from contextlib import closing
import sqlite3

from .accounts import IdentityStore
from .locking import FileLock
from .project_manager import ProjectManager


def migration_plan(storage):
    projects = ProjectManager(storage)
    plan, catalog, inventory, original = projects._migration_materials()
    active = []
    jobs = storage.files.path(".workbench", "jobs")
    if jobs.exists():
        for path in jobs.glob("*/*.json"):
            job = storage.files.read_json(path)
            if job["status"] in {"queued", "running", "uncertain"}:
                active.append(job["job_id"])
    return {"status": "dry_run", "owner": "moy", "unlimited_credit": True,
            "initial_credit_for_new_accounts": 200, "fingerprint": plan["fingerprint"],
            "counts": inventory["counts"], "main_projects": len(catalog["main_projects"]),
            "active_jobs": active, "image_files_moved": 0, "image_files_changed": 0,
            "generation_calls": 0, "historical_credit_charges": 0}, catalog, inventory, original


def migrate_accounts(storage, *, apply=False, expected_fingerprint=None, backup_directory=None):
    if not apply:
        return migration_plan(storage)[0]
    if not expected_fingerprint or backup_directory is None:
        raise ValueError("应用迁移需要 dry-run 指纹和数据根目录外的备份路径")
    storage.files.path(".workbench").mkdir(exist_ok=True)
    # 迁移要求执行器已停止；与网页写入共用控制锁。
    with FileLock(storage.files.path(".workbench", "worker.lock")), FileLock(storage.files.path(".workbench", "control.lock"), timeout=30):
        plan, catalog, inventory, original = migration_plan(storage)
        if plan["fingerprint"] != expected_fingerprint:
            raise ValueError("数据自 dry-run 后已变化，请重新预览")
        if plan["active_jobs"]:
            raise ValueError("存在活动或待确认任务，请先处理后再迁移")
        destination = ProjectManager(storage)._backup(backup_directory, plan, inventory, original)
        dbpath = storage.files.path(".identity", "accounts.sqlite3")
        existed = dbpath.exists()
        if existed:
            with closing(sqlite3.connect(dbpath)) as source, closing(sqlite3.connect(destination / "accounts.before.sqlite3")) as target:
                source.backup(target)
        identity = IdentityStore(storage)
        result = identity.bootstrap(catalog["main_projects"])
        after = migration_plan(storage)[0]
        if after["fingerprint"] != plan["fingerprint"]:
            raise ValueError("迁移期间原数据发生变化，未标记成功；备份已保留，请检查并重做预览")
        report = {**plan, **result, "status": "applied", "backup_directory": str(destination),
                  "identity_existed": existed, "integrity_verified": True}
        ProjectManager._write_result(destination / "accounts-result.json", report)
        return report
