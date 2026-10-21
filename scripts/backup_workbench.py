"""Offline business-data backup, verification and isolated restore (no deployment)."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import time
from urllib.parse import quote


FORMAT = "image-workbench-backup-v1"
CHUNK = 1024 * 1024
MANIFEST = "manifest.json"
INCOMPLETE = ".incomplete"
SQLITE_HEADER = b"SQLite format 3\0"


def _absolute_path(path):
    """Normalize before adding Win32's extended namespace; no global setting."""
    value = os.fspath(path)
    if os.name == "nt":
        value = value.replace("/", "\\")
        if value.lower().startswith("\\\\?\\unc\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
            if not re.match(r"^[a-zA-Z]:\\", value):
                raise ValueError("只支持普通盘符或 UNC 文件路径")
        elif value.startswith(("\\\\.\\", "\\??\\")):
            raise ValueError("不允许 Windows 设备路径")
        raw_path = Path(value)
        parts = raw_path.parts[1:] if raw_path.anchor else raw_path.parts
        if any(part not in {".", ".."} and (part.endswith((".", " ")) or any(char in part for char in ':*?"<>|')) for part in parts):
            raise ValueError("Windows 路径包含歧义文件名或数据流")
    path = Path(os.path.abspath(value))
    if os.name == "nt":
        # The extended namespace does not normalize '..', '/' or trailing dots.
        # Normalize first and reject ambiguous names before entering it.
        if not path.is_absolute():
            raise ValueError("需要规范的绝对文件路径")
        value = str(path)
        path = Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return path


def _safe_path(path):
    """Reject links/junctions in every existing component, including parents."""
    path = _absolute_path(path)
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("符号链接或重解析点不能用于备份/恢复")
    return path


def _relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\0" in name:
        raise ValueError("清单包含非法路径")
    parts = name.split("/")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts):
        raise ValueError("清单包含非规范路径")
    if PurePosixPath(name).is_absolute() or any(re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p) for p in parts):
        raise ValueError("清单包含不安全路径")
    return name


def _private_mkdir(path):
    """Create only a NEW protected container; never change existing permissions."""
    if os.name != "nt":
        path.mkdir(mode=0o700)
        return
    # CreateDirectory receives a protected ACL at creation, avoiding a window of
    # inherited access. Windows local administrators and SYSTEM remain trusted.
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    token, length = wintypes.HANDLE(), wintypes.DWORD()
    sid_text, descriptor = wintypes.LPWSTR(), wintypes.LPVOID()

    def checked(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", wintypes.LPVOID), ("inherit", wintypes.BOOL)]

    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    try:
        checked(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length.value)
        checked(advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)))
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        checked(advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)))
        sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;" + sid_text.value + ")"
        checked(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        checked(kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)))
    finally:
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, wintypes.HLOCAL))
        if descriptor:
            kernel.LocalFree(descriptor)
        if token:
            kernel.CloseHandle(token)


def _paths_overlap(source, destination):
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        return True

    def same_existing_directory(first, second):
        try:
            return os.path.samefile(first, second)
        except FileNotFoundError:
            return False

    # Windows 8.3 aliases can name the same directory without sharing lexical
    # ancestors. Check identity in BOTH directions, after no-link validation.
    # Missing future targets are expected; access errors must still fail closed.
    return (any(same_existing_directory(source, parent) for parent in (destination, *destination.parents))
            or any(same_existing_directory(destination, parent) for parent in (source, *source.parents)))


def _new_destination(destination, source):
    destination = _safe_path(destination)
    if _paths_overlap(source, destination):
        raise ValueError("源目录与目标目录不能重叠")
    if destination.exists():
        raise FileExistsError("目标已存在；只允许恢复/备份到全新目录")
    if not destination.parent.is_dir():
        raise ValueError("目标父目录必须已存在")
    _private_mkdir(destination)
    _write_bytes(destination / INCOMPLETE, b"Incomplete operation; do not use as a backup or data root.\n")
    return destination


def _write_bytes(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _tree(root):
    root = _safe_path(root)
    if not root.is_dir():
        raise ValueError("源目录不存在")
    files, directories, seen = {}, [], set()

    def fail_walk(error):
        raise error

    for directory, subdirs, names in os.walk(root, followlinks=False, onerror=fail_walk):
        for name in sorted(subdirs + names):
            path = _safe_path(Path(directory) / name)
            relative = _relative(path.relative_to(root).as_posix())
            key = relative.casefold()
            if key in seen:
                raise ValueError("路径在 Windows 上有大小写冲突")
            seen.add(key)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = path
            else:
                raise ValueError("备份中只允许普通文件与目录")
    return files, sorted(directories)


def _digest(path):
    value, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            size += len(chunk)
            value.update(chunk)
    return {"sha256": value.hexdigest(), "size": size}


def _is_sqlite(path):
    with path.open("rb") as stream:
        return stream.read(16) == SQLITE_HEADER


def _inventory(root):
    files, directories = _tree(root)
    databases = {name for name, path in files.items() if not name.endswith(("-wal", "-shm", "-journal")) and _is_sqlite(path)}
    # A damaged account DB must not silently become an opaque ordinary file.
    if ".identity/accounts.sqlite3" in files and ".identity/accounts.sqlite3" not in databases:
        raise ValueError("账号数据库不是有效 SQLite 文件")
    sidecars = {name + suffix for name in databases for suffix in ("-wal", "-shm", "-journal")}
    orphan_sidecars = {name for name in files if name.endswith(("-wal", "-shm", "-journal"))} - sidecars
    if orphan_sidecars:
        raise ValueError("存在无法关联数据库的 SQLite 日志文件")
    # Readers may update shared-memory lock bookkeeping. WAL/journal bytes and
    # the database itself ARE compared before/after; SHM is never copied.
    snapshot = {name: _digest(path) for name, path in files.items() if name not in {db + "-shm" for db in databases}}
    return files, directories, databases, sidecars, snapshot


def _sqlite_uri(path):
    path = _absolute_path(path)
    # Path.as_uri() renders \\?\ as authority "%3F", which SQLite rejects.
    # Percent-encode the complete native filename instead; decoding restores
    # the extended path for SQLite's Windows VFS, including literal # and %.
    filename = "file:" + quote(str(path), safe="") if os.name == "nt" else path.as_uri()
    return filename + "?mode=ro"


def _sqlite_check(path):
    with closing(sqlite3.connect(_sqlite_uri(path) + "&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite 完整性检查失败")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite 外键检查失败")


def _sqlite_backup(source, destination):
    deadline = time.monotonic() + 30

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise ValueError("SQLite 快照超时；确认所有写入已停止")

    with closing(sqlite3.connect(_sqlite_uri(source), uri=True, timeout=5)) as original:
        with closing(sqlite3.connect(destination)) as target:
            original.backup(target, pages=256, progress=progress, sleep=0.05)
            target.execute("PRAGMA journal_mode=DELETE")
    if os.name != "nt":
        os.chmod(destination, 0o600)  # Only the new file, never the source.
    with destination.open("r+b") as stream:
        os.fsync(stream.fileno())
    _sqlite_check(destination)


def _copy_checked(source, destination, expected):
    value, size = hashlib.sha256(), 0
    with source.open("rb") as stream, os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        while chunk := stream.read(CHUNK):
            output.write(chunk)
            size += len(chunk)
            value.update(chunk)
        output.flush()
        os.fsync(output.fileno())
    if {"sha256": value.hexdigest(), "size": size} != expected:
        raise ValueError("文件在复制期间变化或校验不匹配")


def _summary(manifest):
    return {"format": FORMAT, "files": len(manifest["files"]),
            "directories": len(manifest["directories"]),
            "sqlite_databases": sum(row["kind"] == "sqlite" for row in manifest["files"].values()),
            "bytes": sum(row["size"] for row in manifest["files"].values()),
            "contains_private_data": True}


def backup(data_root, destination, *, writers_stopped=False):
    if not writers_stopped:
        raise ValueError("必须先停止网页、执行器和其他写入进程，再显式传入 --writers-stopped")
    source = _safe_path(data_root)
    files, directories, databases, sidecars, before = _inventory(source)
    if INCOMPLETE in files:
        raise ValueError("源目录来自未完成的恢复，不能备份")
    destination = _new_destination(destination, source)
    payload = destination / "data"
    payload.mkdir(mode=0o700)
    for name in directories:
        (payload / name).mkdir(mode=0o700)
    manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                "writers_stopped": True, "contains_private_data": True,
                "directories": directories, "files": {}}
    for name, path in sorted(files.items()):
        if name in sidecars:
            continue
        target = payload / name
        if name in databases:
            _sqlite_backup(path, target)
        else:
            _copy_checked(path, target, before[name])
        manifest["files"][name] = {**_digest(target), "kind": "sqlite" if name in databases else "file"}
    _, after_dirs, after_dbs, _, after = _inventory(source)
    if before != after or directories != after_dirs or databases != after_dbs:
        raise ValueError("源数据在备份期间变化；保留未完成副本，不能用于恢复")
    _verify_payload(payload, manifest)
    _write_bytes(destination / MANIFEST, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (destination / INCOMPLETE).unlink()
    return _summary(manifest)


def _read_manifest(backup_root):
    files, dirs = _tree(backup_root)
    if INCOMPLETE in files:
        raise ValueError("备份未完成，拒绝使用")
    if set(name for name in files if not name.startswith("data/")) != {MANIFEST} or any(name != "data" and not name.startswith("data/") for name in dirs):
        raise ValueError("备份容器结构不正确")
    path = backup_root / MANIFEST
    if path.stat().st_size > 16 * CHUNK:
        raise ValueError("备份清单过大")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("writers_stopped") is not True or manifest.get("contains_private_data") is not True:
        raise ValueError("未知或不完整的备份清单")
    if not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("directories"), list):
        raise ValueError("备份清单结构错误")
    names = list(manifest["files"]) + manifest["directories"]
    if len({_relative(name).casefold() for name in names}) != len(names):
        raise ValueError("清单存在重复路径")
    for entry in manifest["files"].values():
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size", "kind"}
                or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or type(entry["size"]) is not int or entry["size"] < 0
                or not isinstance(entry["kind"], str) or entry["kind"] not in {"file", "sqlite"}):
            raise ValueError("文件校验记录错误")
    return manifest


def _verify_payload(root, manifest, *, incomplete=False):
    files, directories = _tree(root)
    if incomplete:
        files.pop(INCOMPLETE, None)
    if set(files) != set(manifest["files"]) or set(directories) != set(manifest["directories"]):
        raise ValueError("文件/目录清单不匹配（缺失或额外内容）")
    for name, entry in manifest["files"].items():
        path = files[name]
        if _digest(path) != {key: entry[key] for key in ("sha256", "size")}:
            raise ValueError("文件内容校验失败")
        is_database = _is_sqlite(path)
        if name == ".identity/accounts.sqlite3" and not is_database:
            raise ValueError("账号数据库不是有效 SQLite 文件")
        if is_database != (entry["kind"] == "sqlite"):
            raise ValueError("SQLite 文件分类不匹配")
        if is_database:
            _sqlite_check(path)


def verify(backup_root):
    root = _safe_path(backup_root)
    manifest = _read_manifest(root)
    _verify_payload(root / "data", manifest)
    return _summary(manifest)


def restore(backup_root, destination):
    source = _safe_path(backup_root)
    verify(source)
    manifest = _read_manifest(source)
    destination = _new_destination(destination, source)
    for name in sorted(manifest["directories"]):
        (destination / name).mkdir(mode=0o700)
    for name, entry in manifest["files"].items():
        _copy_checked(source / "data" / name, destination / name, {key: entry[key] for key in ("sha256", "size")})
    _verify_payload(destination, manifest, incomplete=True)
    (destination / INCOMPLETE).unlink()
    return _summary(manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup", help="停止写入后备份整个数据根目录")
    create.add_argument("--data-root", type=Path, required=True)
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument("--writers-stopped", action="store_true")
    check = commands.add_parser("verify", help="核对清单、内容哈希及 SQLite 完整性")
    check.add_argument("--backup", type=Path, required=True)
    copy = commands.add_parser("restore", help="恢复为全新的隔离数据副本；不启动服务")
    copy.add_argument("--backup", type=Path, required=True)
    copy.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup(args.data_root, args.destination, writers_stopped=args.writers_stopped)
        elif args.command == "verify":
            result = verify(args.backup)
        else:
            result = restore(args.backup, args.destination)
        print(json.dumps({"status": "verified", "operation": args.command, **result}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        # File paths, SQL, private filenames and stored values never enter logs.
        print(json.dumps({"status": "failed", "operation": args.command, "error_type": type(exc).__name__,
                          "message": "操作未完成；确认停写、全新私有目标、清单和数据完整性。未完成目录不可用于恢复。"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
