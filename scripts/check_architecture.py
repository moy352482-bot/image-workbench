"""Check dependency contracts without importing application code or third-party tools."""
import argparse
import ast
from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def module_name(path, root, package):
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([package, *parts])


def imports(source, module, *, package_module=False):
    """Include nested/conditional imports; handle relative and absolute spellings."""
    package = module if package_module else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, None
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                if node.level > len(parts):
                    raise ValueError(f"{module}:{node.lineno}: relative import escapes package")
                base = ".".join(parts[:len(parts) - node.level + 1])
                if node.module:
                    base += "." + node.module
            else:
                base = node.module or ""
            for alias in node.names:
                yield node.lineno, base, alias.name


def cycles(graph):
    visited, active, stack, found = set(), set(), [], []

    def visit(node):
        if node in active:
            found.append(stack[stack.index(node):] + [node])
            return
        if node in visited:
            return
        visited.add(node)
        active.add(node)
        stack.append(node)
        for target in sorted(graph[node]):
            visit(target)
        stack.pop()
        active.remove(node)

    for node in sorted(graph):
        visit(node)
    return found


def analyze(root=ROOT):
    root = Path(root)
    config = json.loads((root / "architecture.json").read_text(encoding="utf-8"))
    package = config["root_package"]
    package_root = root / package
    paths = {module_name(p, package_root, package): p for p in package_root.rglob("*.py") if "__pycache__" not in p.parts}
    errors, groups, graph = [], {}, {name: set() for name in paths}
    for name, path in sorted(paths.items()):
        short = name[len(package):].lstrip(".")
        matches = [group for group, spec in config["groups"].items()
                   if any(fnmatchcase(short, pattern) for pattern in spec["modules"])]
        if len(matches) != 1:
            errors.append(f"{path.relative_to(root)}: expected one architecture group, got {matches}. Register the module in architecture.json.")
        else:
            groups[name] = matches[0]
    edges = []
    for name, path in sorted(paths.items()):
        for line, base, symbol in imports(path.read_text(encoding="utf-8-sig"), name, package_module=path.name == "__init__.py"):
            source_group = groups.get(name)
            if base.split(".")[0] in {"fastapi", "starlette"} and source_group not in config["http_framework_groups"]:
                errors.append(f"{path.relative_to(root)}:{line}: {source_group} imports HTTP framework {base}. Put the adapter in http_api.")
            if base != package and not base.startswith(package + "."):
                continue
            target = base + "." + symbol if symbol and base + "." + symbol in paths else base
            if target not in paths:
                errors.append(f"{path.relative_to(root)}:{line}: unresolved internal import {target}.")
                continue
            if target == name:
                continue
            graph[name].add(target)
            edges.append({"source": name, "target": target, "line": line})
            allowed = config["groups"].get(source_group, {}).get("may_import", [])
            if "*" not in allowed and groups.get(target) not in allowed:
                errors.append(f"{path.relative_to(root)}:{line}: {source_group} -> {groups.get(target)} is forbidden ({target}). Inject a service/port or move shared code to its owning module.")
    for cycle in cycles(graph):
        errors.append("Circular dependency: " + " -> ".join(cycle))

    frontend = config.get("frontend")
    if frontend:
        template = (package_root / frontend["template"]).read_text(encoding="utf-8")
        scripts = re.findall(r'<script\b[^>]*\bsrc="/static/([^"?]+)', template)
        entry = Path(frontend["entry"]).name
        for symbol, relative in frontend["components"].items():
            name = Path(relative).name
            path = package_root / relative
            if not path.is_file():
                errors.append(f"Missing frontend component: {relative}")
                continue
            if scripts.count(name) != 1 or entry not in scripts or scripts.index(name) >= scripts.index(entry):
                errors.append(f"{name} must be loaded exactly once before {entry}.")
            # Component factories accept dependencies; no component may reach
            # into a sibling component or the page's application state.
            text = re.sub(r"(?m)^\s*//.*$", "", path.read_text(encoding="utf-8"))
            for sibling in frontend["components"]:
                if sibling != symbol and re.search(r"\bwindow\." + sibling + r"\b", text):
                    errors.append(f"{relative}: imports sibling {sibling}; pass a callback instead.")
            if re.search(r"\bstate\.", text):
                errors.append(f"{relative}: reaches into page state; pass values/callbacks instead.")
    return {"modules": len(paths), "edges": sorted(edges, key=lambda e: (e["source"], e["target"], e["line"])),
            "groups": groups, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print dependency graph and violations")
    args = parser.parse_args(argv)
    result = analyze()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Architecture: {result['modules']} modules, {len(result['edges'])} imports, {len(result['errors'])} violations")
        for error in result["errors"]:
            print(error)
    return bool(result["errors"])


if __name__ == "__main__":
    sys.exit(main())
