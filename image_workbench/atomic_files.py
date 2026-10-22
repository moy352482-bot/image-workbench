"""Durable file replacement shared by workers; callers validate path ownership."""
import json
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

def replace_file(source, destination):
    """Keep replacement atomic while a Windows reader briefly holds the target."""
    deadline = time.monotonic() + 1.0
    delay = 0.01
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            # Windows may deny replacement while JSON is being read. Permanent
            # permissions and errors on other platforms must still surface.
            if getattr(exc, "winerror", None) not in {5, 32, 33} or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.1)

# Retain the shared helper name used by the concurrently maintained modules.
replace_with_retry = replace_file


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_bytes(storage, path, data):
    storage.files.check(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        if temporary is not None:
            storage.files.cleanup(temporary)
