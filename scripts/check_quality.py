"""Run pinned Ruff and reject diagnostics beyond the reviewed legacy baseline."""

from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/contracts/ruff-baseline.json"
TARGETS = ("image_workbench", "scripts", "tests", "example.py")


def diagnostic_key(item, root=ROOT):
    """Use content, not line numbers, so inserting clean lines cannot hide new debt."""
    path = Path(item["filename"])
    if not path.is_absolute():
        path = root / path
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    first, last = item["location"]["row"], item["end_location"]["row"]
    source = "\n".join(line.strip() for line in lines[first - 1:last])
    return relative, item["code"], item["message"], source


def baseline_counts(document):
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported Ruff baseline schema")
    return Counter({
        (item["path"], item["code"], item["message"], item["source"]): item["count"]
        for item in document["diagnostics"]
    })


def new_diagnostics(diagnostics, baseline, root=ROOT):
    remaining = baseline.copy()
    new = []
    for item in diagnostics:
        key = diagnostic_key(item, root)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            new.append(item)
    return new


def main():
    command = [
        sys.executable, "-m", "ruff", "check", *TARGETS,
        "--config", str(ROOT / "ruff.toml"), "--no-fix", "--output-format", "json",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode not in (0, 1):
        print(result.stderr or result.stdout)
        print("Ruff did not run successfully. Install requirements-dev.txt; this check was NOT passed.")
        return result.returncode
    try:
        diagnostics = json.loads(result.stdout)
        baseline = baseline_counts(json.loads(BASELINE.read_text(encoding="utf-8")))
        new = new_diagnostics(diagnostics, baseline)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Ruff baseline could not be verified: {exc}")
        return 2
    print(f"Ruff: {len(diagnostics)} diagnostics, {len(new)} new; historical baseline {baseline.total()}")
    for item in new:
        relative = Path(item["filename"]).relative_to(ROOT)
        print(f"{relative}:{item['location']['row']}:{item['location']['column']}: {item['code']} {item['message']}")
    return bool(new)


if __name__ == "__main__":
    sys.exit(main())
