"""Bounded metadata/file access, independent of projects, artifacts and identity.

Scopes are trusted service capabilities, not account authorization. Callers must
authorize the Workspace before selecting its scope. Every operation rechecks the
original data root as well as the narrower scope, including parent reparse points.
"""
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

from .atomic_files import replace_file


class MetadataError(ValueError):
    """Invalid metadata must surface rather than being silently skipped."""


class ManagedFiles:
    def __init__(self, root, *, anchor=None):
        self.root = Path(root).absolute()
        self.anchor = Path(anchor).absolute() if anchor is not None else self.root
        # Windows TEMP may contain an 8.3 alias. Keep the original spelling for
        # reparse-point checks and pin its real boundary before delegating scopes.
        self._resolved_root = self.root.resolve()
        self.path()

    def scope(self, *parts):
        """Delegate access to a subtree without broadening the original boundary."""
        return ManagedFiles(self.path(*parts), anchor=self.anchor)

    def path(self, *parts):
        candidate = self.root.joinpath(*parts)
        if not candidate.is_relative_to(self.root):
            raise ValueError("受管理路径超出授权目录")
        relative = candidate.relative_to(self.anchor)
        if ".." in relative.parts:
            raise ValueError("受管理路径包含路径穿越")
        current = self.anchor
        for part in (None, *relative.parts):
            if part is not None:
                current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError(f"受管理路径不允许符号链接或重解析点: {current}")
        if not candidate.resolve().is_relative_to(self._resolved_root):
            raise ValueError("受管理路径解析后超出授权目录")
        return candidate

    def check(self, path):
        return self.path(*Path(path).relative_to(self.root).parts)

    def read_json(self, path):
        deadline, delay = time.monotonic() + 1.0, 0.01
        while True:
            path = self.check(path)
            try:
                with path.open("r", encoding="utf-8") as stream:
                    payload = json.load(stream)
                break
            except PermissionError as exc:
                # Windows can deny opening a JSON file while its atomic replace
                # completes. CRT file reads may report EACCES without winerror.
                sharing = (getattr(exc, "winerror", None) in {5, 32, 33}
                           or (os.name == "nt" and getattr(exc, "winerror", None) is None and exc.errno == errno.EACCES))
                if not sharing or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise MetadataError(f"元数据不是有效的 UTF-8 JSON: {path}") from exc
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise MetadataError(f"不支持的元数据结构或版本: {path}")
        return payload

    def write_json(self, path, payload):
        """Preserve the existing on-disk JSON and atomic replacement semantics."""
        path = self.check(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", prefix=".tmp-",
                                             suffix=".tmp", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.check(path)
            replace_file(temporary, path)
        finally:
            if temporary is not None:
                self.cleanup(temporary)

    def cleanup(self, path, *, directory=False):
        """Best effort removal of a caller-created temporary file within scope."""
        try:
            path = self.check(path)
            if directory:
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def child_ids(self, directory):
        ids = []
        for child in self.check(directory).iterdir():
            if child.name.startswith("."):
                continue
            self.check(child)
            if child.is_dir():
                if not re.fullmatch(r"[0-9a-f]{32}", child.name):
                    raise MetadataError(f"无效的资源目录: {child}")
                ids.append(child.name)
        return ids
