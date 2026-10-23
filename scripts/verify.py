"""Offline verification entry point. Run all checks or a selected feature suite."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMMON = ["test_architecture", "test_quality_guard", "test_module_isolation", "test_http_contract", "test_frontend_modules", "test_request_boundaries"]
SUITES = {
    "images": ["test_web", "test_worker", "test_workbench_state", "test_remote", "test_runner", "test_task_timing", "test_image_timer_browser", "test_scheduling", "test_asset_inputs", "test_asset_browser", "test_asset_e2e_browser", "test_image_prompting", "test_image_styles_browser", "test_deploy_release", "test_task_recovery"],
    "projects": ["test_project_web", "test_project_manager", "test_project_cli", "test_request_metadata_cache"],
    "accounts": ["test_accounts", "test_registration", "test_capacity", "test_script_limits"],
    "scripts": ["test_script_web", "test_script_service", "test_script_storage", "test_script_models",
                "test_script_templates", "test_script_remote", "test_script_adapter", "test_script_recovery", "test_video_pipeline",
                "test_script_creative_core", "test_script_creative_http", "test_script_direct_submit", "test_script_generation_browser",
                "test_plugins", "test_script_workflow", "test_task_recovery"],
    "storyboards": ["test_storyboard_http", "test_storyboard_planner", "test_storyboard_recovery", "test_storyboard_followup",
                   "test_video_pipeline", "test_script_web", "test_asset_storyboard_browser", "test_script_workflow"],
    "video": ["test_video", "test_h3_worker", "test_h3_skill", "test_video_pipeline", "test_pipeline_service", "test_asset_tray", "test_asset_tray_browser", "test_task_recovery", "test_video_state_browser"],
    "qwen": ["test_qwen"],
    "admin": ["test_admin", "test_deploy_release", "test_backup_workbench", "test_verification"],
    "storage": ["test_storage", "test_script_storage", "test_project_manager", "test_request_metadata_cache", "test_atomic_replace", "test_managed_files"],
}


def source_fingerprint():
    files = set()
    for folder in ("image_workbench", "config", "skills"):
        files.update(p for p in (ROOT / folder).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"})
    files.update((ROOT / "tests").glob("*.py"))
    for suffix in ("*.py", "*.sh", "*.ps1"):
        files.update((ROOT / "scripts").glob(suffix))
    for folder in ("tests/contracts", "tests/frontend"):
        files.update(p for p in (ROOT / folder).rglob("*") if p.is_file())
    for name in ("architecture.json", "ruff.toml", "requirements.txt", "requirements-web.txt",
                 "requirements-dev.txt", ".github/workflows/verify.yml"):
        files.add(ROOT / name)
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", choices=sorted(SUITES), help="Omit for complete regression; feature checks always include common contracts")
    parser.add_argument("--list", action="store_true", help="Show selected test modules without running")
    args = parser.parse_args(argv)
    selected = sorted(set(COMMON + SUITES[args.module])) if args.module else None
    if args.list:
        print("\n".join(selected) if selected else "All tests (unittest discover -s tests)")
        return 0
    # Lint and dependency checks run before importing the application.
    before = source_fingerprint()
    for check in ("check_quality.py", "check_architecture.py"):
        result = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts" / check)], cwd=ROOT)
        if result.returncode:
            return result.returncode
    if not shutil.which("node"):
        print("Node.js is required for frontend verification; checks failed.", flush=True)
        return 1
    command = [sys.executable, "-X", "utf8", "-m", "unittest"]
    command += ["-v", *("tests." + name for name in selected)] if selected else ["discover", "-s", "tests", "-v"]
    result = subprocess.run(command, cwd=ROOT)
    after = source_fingerprint()
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    if changed:
        print("Source changed during verification; results do not certify the current source:", flush=True)
        for path in changed:
            print("  " + path, flush=True)
        return result.returncode or 2
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
